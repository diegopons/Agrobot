-- =============================================================================
-- Agrobot — usuarios, lotes y balance hídrico por lote
-- =============================================================================
-- Se corre una sola vez, completo, en Supabase → SQL Editor → New query.
-- Es idempotente en lo posible: volver a correrlo no borra datos.
--
-- Quién puede hacer qué:
--   · Cada usuario ve y edita SOLO sus lotes (Row Level Security).
--   · Los resultados (estado, serie diaria, resumen) los escribe únicamente el
--     proceso diario, que usa la clave service_role y no pasa por RLS. Un
--     usuario puede leerlos, nunca modificarlos.
--   · El bot (n8n) también usa service_role, y solo a través de las funciones
--     estado_por_chat y vincular_telegram: identifica al usuario por su chat
--     de Telegram y responde sobre los lotes de ese usuario y de nadie más.
-- =============================================================================

create extension if not exists pgcrypto;

-- -----------------------------------------------------------------------------
-- Perfiles: uno por usuario de Supabase Auth, creado solo al registrarse
-- -----------------------------------------------------------------------------
create table if not exists public.perfiles (
  id                  uuid primary key references auth.users(id) on delete cascade,
  nombre              text,
  -- Se completa con /vincular desde Telegram, nunca a mano: así nadie puede
  -- apuntar los avisos de su cuenta al chat de otra persona, ni reclamar un
  -- chat ajeno para leer lo que el bot responde ahí.
  telegram_chat_id    text unique,
  codigo_vinculacion  text unique,
  codigo_vence        timestamptz,
  creado              timestamptz not null default now()
);

create or replace function public.crear_perfil()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.perfiles (id, nombre)
  values (new.id, coalesce(new.raw_user_meta_data->>'nombre', split_part(new.email, '@', 1)))
  on conflict (id) do nothing;
  return new;
end
$$;

drop trigger if exists al_crear_usuario on auth.users;
create trigger al_crear_usuario
  after insert on auth.users
  for each row execute function public.crear_perfil();

-- Usuarios que ya existían antes de correr este script
insert into public.perfiles (id, nombre)
select u.id, split_part(u.email, '@', 1) from auth.users u
on conflict (id) do nothing;

-- -----------------------------------------------------------------------------
-- Lotes: la definición que carga el usuario desde el tablero
-- -----------------------------------------------------------------------------
create table if not exists public.lotes (
  id                       uuid primary key default gen_random_uuid(),
  usuario_id               uuid not null default auth.uid()
                             references public.perfiles(id) on delete cascade,
  nombre                   text not null check (length(trim(nombre)) > 0),
  -- Geometría GeoJSON (Polygon o MultiPolygon) en EPSG:4326
  geometria                jsonb not null
                             check (geometria->>'type' in ('Polygon', 'MultiPolygon')),
  area_ha                  double precision,
  fecha_siembra            date not null,
  au_real_inicial_mm       double precision not null default 40  check (au_real_inicial_mm >= 0),
  prof_raiz_inicial_cm     double precision not null default 1   check (prof_raiz_inicial_cm >= 0),
  umbral_riego_pct         double precision not null default 50  check (umbral_riego_pct between 0 and 100),
  eficiencia_aplicacion    double precision not null default 0.85
                             check (eficiencia_aplicacion > 0 and eficiencia_aplicacion <= 1),
  reposicion_objetivo_pct  double precision not null default 100
                             check (reposicion_objetivo_pct > 0 and reposicion_objetivo_pct <= 100),
  modo_aviso               text not null default 'cruce'
                             check (modo_aviso in ('cruce', 'deficit', 'siempre', 'nunca')),
  -- Perfil de suelo y calicata. null = perfil por defecto (src/suelo.py)
  suelo                    jsonb,
  -- Ajustes finos que pisan config.yaml para este lote (secciones balance,
  -- cdse, clima, pronostico, mapas, rezago_dias). Vacío = los de config.yaml.
  parametros               jsonb not null default '{}'::jsonb,
  activo                   boolean not null default true,
  -- Lo pone el usuario para forzar un recálculo desde la siembra; lo baja el
  -- proceso diario al atenderlo.
  reiniciar                boolean not null default false,
  creado                   timestamptz not null default now(),
  actualizado              timestamptz not null default now(),
  unique (usuario_id, nombre)
);

create index if not exists lotes_usuario_idx on public.lotes (usuario_id);

create or replace function public.marcar_actualizado()
returns trigger
language plpgsql
as $$
begin
  new.actualizado := now();
  return new;
end
$$;

drop trigger if exists lotes_actualizado on public.lotes;
create trigger lotes_actualizado
  before update on public.lotes
  for each row execute function public.marcar_actualizado();

-- Primer día que hay que volver a calcular porque se cargó, corrigió o borró
-- un riego de un día ya procesado. Lo completa el trigger de `riegos` y lo
-- limpia la corrida al atenderlo.
alter table public.lotes add column if not exists recalcular_desde date;

-- -----------------------------------------------------------------------------
-- Riegos aplicados: los carga el usuario, uno por día y por lote
-- -----------------------------------------------------------------------------
create table if not exists public.riegos (
  lote_id      uuid not null references public.lotes(id) on delete cascade,
  fecha        date not null check (fecha <= current_date + 1),
  mm           double precision not null check (mm > 0 and mm <= 500),
  nota         text,
  actualizado  timestamptz not null default now(),
  primary key (lote_id, fecha)
);

-- Un riego en un día ya calculado cambia todo lo que vino después: se marca
-- el lote para que la próxima corrida retroceda hasta ese día. Marcar el lote
-- también actualiza `lotes.actualizado`, así lo atiende la corrida de la hora.
create or replace function public.riego_marca_recalculo()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_lote uuid := coalesce(new.lote_id, old.lote_id);
  v_fecha date := least(coalesce(new.fecha, old.fecha), coalesce(old.fecha, new.fecha));
begin
  if tg_op <> 'DELETE' then
    new.actualizado := now();
  end if;
  update public.lotes
     set recalcular_desde = least(coalesce(recalcular_desde, v_fecha), v_fecha)
   where id = v_lote;
  return coalesce(new, old);
end
$$;

drop trigger if exists riegos_recalculo on public.riegos;
create trigger riegos_recalculo
  before insert or update or delete on public.riegos
  for each row execute function public.riego_marca_recalculo();

-- -----------------------------------------------------------------------------
-- Estado entre corridas: lo que antes era data/estado.json, uno por lote
-- -----------------------------------------------------------------------------
-- `serie` numera las campañas calculadas para el lote. Cuando cambia el
-- polígono, la siembra, el clima o el suelo, el balance se reinicia y la serie
-- nueva arranca con el número siguiente. Las anteriores quedan guardadas (es
-- lo que antes eran los CSV *_hasta_AAAAMMDD) y el tablero muestra la vigente.
create table if not exists public.estado_lote (
  lote_id                 uuid primary key references public.lotes(id) on delete cascade,
  serie                   integer not null default 1,
  huella                  text,
  ultima_fecha_procesada  date,
  au_real_mm              double precision,
  prof_raiz_cm            double precision,
  ultimo_ndvi_valido      double precision,
  -- Fechas con imagen Sentinel-2 útil, para armar el mosaico con las últimas
  -- aunque la corrida del día no traiga ninguna nueva.
  fechas_con_imagen       jsonb not null default '[]'::jsonb,
  ultima_corrida          timestamptz,
  ultimo_error            text
);

-- -----------------------------------------------------------------------------
-- Serie diaria: lo que antes era data/balance_hidrico_lote20.csv
-- -----------------------------------------------------------------------------
create table if not exists public.balance_diario (
  lote_id                    uuid not null references public.lotes(id) on delete cascade,
  serie                      integer not null,
  fecha                      date not null,
  precipitacion_mm           double precision,
  precipitacion_efectiva_mm  double precision,
  recarga_capa_mm            double precision,
  eto_mm                     double precision,
  ndvi                       double precision,
  prof_raiz_cm               double precision,
  au_max_mm                  double precision,
  kc                         double precision,
  ks                         double precision,
  etc_ajustada_mm            double precision,
  percolacion_mm             double precision,
  riego_mm                   double precision,
  au_real_mm                 double precision,
  pct_au                     double precision,
  fuente_pp                  text,
  fuente_eto                 text,
  primary key (lote_id, serie, fecha)
);
-- El riego escurre como la lluvia: lo que entra al perfil es el efectivo
alter table public.balance_diario add column if not exists riego_efectivo_mm double precision;

-- -----------------------------------------------------------------------------
-- Resumen para el bot y el tablero: lo que antes era docs/estado_actual.json
-- -----------------------------------------------------------------------------
create table if not exists public.resumen_lote (
  lote_id         uuid primary key references public.lotes(id) on delete cascade,
  fecha_del_dato  date,
  resumen         jsonb not null,
  actualizado     timestamptz not null default now()
);

-- -----------------------------------------------------------------------------
-- Permisos
-- -----------------------------------------------------------------------------
alter table public.perfiles       enable row level security;
alter table public.lotes          enable row level security;
alter table public.estado_lote    enable row level security;
alter table public.balance_diario enable row level security;
alter table public.resumen_lote   enable row level security;

-- Sin sesión iniciada no se ve nada
revoke all on public.perfiles, public.lotes, public.estado_lote,
              public.balance_diario, public.resumen_lote from anon;

-- Perfil: cada uno lee el suyo y solo puede cambiarse el nombre. El chat de
-- Telegram se escribe exclusivamente desde vincular_telegram().
revoke insert, update, delete on public.perfiles from authenticated;
grant select on public.perfiles to authenticated;
grant update (nombre) on public.perfiles to authenticated;

drop policy if exists perfil_propio_leer on public.perfiles;
create policy perfil_propio_leer on public.perfiles
  for select to authenticated using (id = auth.uid());

drop policy if exists perfil_propio_editar on public.perfiles;
create policy perfil_propio_editar on public.perfiles
  for update to authenticated using (id = auth.uid()) with check (id = auth.uid());

-- Lotes: cada uno administra los suyos, y no puede pasarle un lote a otro
grant select, insert, update, delete on public.lotes to authenticated;

drop policy if exists lotes_propios on public.lotes;
create policy lotes_propios on public.lotes
  for all to authenticated
  using (usuario_id = auth.uid())
  with check (usuario_id = auth.uid());

-- Riegos: cada uno los carga, corrige y borra en sus propios lotes
alter table public.riegos enable row level security;
revoke all on public.riegos from anon;
grant select, insert, update, delete on public.riegos to authenticated;

drop policy if exists riegos_de_lotes_propios on public.riegos;
create policy riegos_de_lotes_propios on public.riegos
  for all to authenticated
  using (exists (select 1 from public.lotes l where l.id = lote_id and l.usuario_id = auth.uid()))
  with check (exists (select 1 from public.lotes l where l.id = lote_id and l.usuario_id = auth.uid()));

-- Resultados: solo lectura, y solo de lotes propios
revoke insert, update, delete on public.estado_lote, public.balance_diario,
                                  public.resumen_lote from authenticated;
grant select on public.estado_lote, public.balance_diario, public.resumen_lote to authenticated;

drop policy if exists estado_de_lotes_propios on public.estado_lote;
create policy estado_de_lotes_propios on public.estado_lote
  for select to authenticated
  using (exists (select 1 from public.lotes l where l.id = lote_id and l.usuario_id = auth.uid()));

drop policy if exists balance_de_lotes_propios on public.balance_diario;
create policy balance_de_lotes_propios on public.balance_diario
  for select to authenticated
  using (exists (select 1 from public.lotes l where l.id = lote_id and l.usuario_id = auth.uid()));

drop policy if exists resumen_de_lotes_propios on public.resumen_lote;
create policy resumen_de_lotes_propios on public.resumen_lote
  for select to authenticated
  using (exists (select 1 from public.lotes l where l.id = lote_id and l.usuario_id = auth.uid()));

-- -----------------------------------------------------------------------------
-- Vinculación con Telegram
-- -----------------------------------------------------------------------------
-- 1. Desde el tablero, el usuario pide un código (generar_codigo_vinculacion).
-- 2. Le escribe al bot "/vincular CODIGO".
-- 3. n8n llama a vincular_telegram(codigo, chat_id) con la clave service_role.

create or replace function public.generar_codigo_vinculacion()
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  v_codigo text;
begin
  if auth.uid() is null then
    raise exception 'Hace falta iniciar sesión';
  end if;
  -- Sin 0/O ni 1/I: se tipea a mano en el celular
  v_codigo := (
    select string_agg(substr('ABCDEFGHJKLMNPQRSTUVWXYZ23456789', 1 + floor(random() * 32)::int, 1), '')
    from generate_series(1, 6)
  );
  update public.perfiles
     set codigo_vinculacion = v_codigo,
         codigo_vence = now() + interval '15 minutes'
   where id = auth.uid();
  return v_codigo;
end
$$;

create or replace function public.desvincular_telegram()
returns void
language sql
security definer
set search_path = public
as $$
  update public.perfiles set telegram_chat_id = null where id = auth.uid();
$$;

create or replace function public.vincular_telegram(p_codigo text, p_chat_id text)
returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  v_id uuid;
  v_nombre text;
begin
  select id, nombre into v_id, v_nombre
    from public.perfiles
   where codigo_vinculacion = upper(trim(p_codigo))
     and codigo_vence > now();
  if v_id is null then
    return null;   -- código inexistente o vencido
  end if;
  -- Un chat pertenece a una sola cuenta: si estaba en otra, se mueve.
  update public.perfiles set telegram_chat_id = null
   where telegram_chat_id = p_chat_id and id <> v_id;
  update public.perfiles
     set telegram_chat_id = p_chat_id, codigo_vinculacion = null, codigo_vence = null
   where id = v_id;
  return coalesce(v_nombre, '');
end
$$;

-- -----------------------------------------------------------------------------
-- Consulta del bot: todo lo que necesita para responder, en una llamada
-- -----------------------------------------------------------------------------
-- Devuelve null si el chat no está vinculado a ninguna cuenta.
create or replace function public.estado_por_chat(p_chat_id text)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select jsonb_build_object(
    'usuario', p.nombre,
    'lotes', coalesce((
      select jsonb_agg(jsonb_build_object(
               'id', l.id,
               'nombre', l.nombre,
               'area_ha', round(l.area_ha::numeric, 2),
               'fecha_siembra', l.fecha_siembra,
               'ultima_fecha_procesada', e.ultima_fecha_procesada,
               'ultimo_error', e.ultimo_error,
               'resumen', r.resumen,
               'riegos_recientes', coalesce((
                 select jsonb_agg(jsonb_build_object('fecha', rg.fecha, 'mm', rg.mm, 'nota', rg.nota)
                                  order by rg.fecha desc)
                   from (select * from public.riegos x where x.lote_id = l.id
                          order by x.fecha desc limit 10) rg
               ), '[]'::jsonb)
             ) order by l.nombre)
        from public.lotes l
        left join public.estado_lote e on e.lote_id = l.id
        left join public.resumen_lote r on r.lote_id = l.id
       where l.usuario_id = p.id and l.activo
    ), '[]'::jsonb)
  )
  from public.perfiles p
  where p.telegram_chat_id = p_chat_id;
$$;

revoke all on function public.generar_codigo_vinculacion() from public, anon;
revoke all on function public.desvincular_telegram()       from public, anon;
revoke all on function public.vincular_telegram(text, text) from public, anon, authenticated;
revoke all on function public.estado_por_chat(text)         from public, anon, authenticated;
grant execute on function public.generar_codigo_vinculacion() to authenticated;
grant execute on function public.desvincular_telegram()       to authenticated;
grant execute on function public.vincular_telegram(text, text) to service_role;
grant execute on function public.estado_por_chat(text)         to service_role;

-- -----------------------------------------------------------------------------
-- Mosaicos de NDVI (Storage): mosaicos/<usuario_id>/<lote_id>.png
-- -----------------------------------------------------------------------------
insert into storage.buckets (id, name, public)
values ('mosaicos', 'mosaicos', false)
on conflict (id) do nothing;

drop policy if exists mosaicos_propios on storage.objects;
create policy mosaicos_propios on storage.objects
  for select to authenticated
  using (bucket_id = 'mosaicos' and (storage.foldername(name))[1] = auth.uid()::text);
