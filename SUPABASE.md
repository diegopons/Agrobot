# Varios usuarios y lotes (Supabase)

Agrobot corre el balance hídrico para **todos los lotes de todos los usuarios**.
Cada usuario entra al tablero con su correo, carga sus lotes y ve solo los
suyos. El bot de Telegram responde a cada uno sobre sus propios lotes.

```
Usuario ──login──> Tablero (GitHub Pages)  ── ve y edita solo sus lotes
                        │
                        ▼
                  ┌─ Supabase ──────────────────────────┐
                  │ perfiles      una fila por usuario   │
                  │ lotes         polígono, siembra,     │
                  │               umbral, calicata…      │
                  │ estado_lote   lo que era estado.json │
                  │ balance_diario  lo que era el CSV    │
                  │ resumen_lote  lo que era             │
                  │               estado_actual.json     │
                  │ mosaicos/     PNG de NDVI (Storage)  │
                  └──────────────────────────────────────┘
                        ▲                    ▲
     GitHub Actions ────┘                    └──── n8n (bot)
     06:00 todos los lotes, días nuevos        chat de Telegram → usuario
     cada hora: lotes nuevos o modificados     → sus lotes
```

Quién puede qué lo deciden las políticas de `supabase/schema.sql`, no el
código del tablero:

- Un usuario ve, crea, edita y borra **solo sus lotes**.
- Los resultados (estado, serie, resumen) los escribe **solo** la corrida
  diaria. El usuario los lee; no los puede modificar.
- El chat de Telegram de una cuenta se vincula con un código de un solo uso,
  nunca escribiéndolo a mano: nadie puede apuntar sus avisos al chat de otro
  ni hacerse pasar por otro ante el bot.

Si no se configura Supabase, el sistema sigue funcionando como antes: un lote,
el de `config.yaml`, con `data/estado.json` y el CSV (ver README.md).

---

## Paso 1 — Crear el proyecto

1. Entrá a <https://supabase.com> y creá una cuenta (gratis).
2. **New project**. Región: *South America (São Paulo)*. Guardá la contraseña
   de la base en algún lado (no se usa en Agrobot, pero no se recupera).
3. Cuando termine de crearse: **Project Settings → API**. Ahí están los tres
   datos que vas a necesitar:
   - **Project URL** (`https://xxxx.supabase.co`)
   - **anon public** — va en el tablero, es pública por diseño.
   - **service_role** — saltea todas las políticas. Va **solo** en los
     Secrets de GitHub y en n8n. Nunca en el tablero ni en el repo.

## Paso 2 — Crear las tablas

**SQL Editor → New query**, pegá el contenido completo de
`supabase/schema.sql` y **Run**. Tiene que terminar con *Success. No rows
returned*. Se puede volver a correr sin perder datos, y **hay que volver a
correrlo cada vez que cambia el archivo** (por ejemplo, cuando se agregó la
carga de riegos): agrega las tablas y columnas nuevas sin tocar lo que ya hay.

## Paso 3 — Ingreso por correo

En **Authentication → URL Configuration**:

- **Site URL**: la dirección del tablero, por ejemplo
  `https://diegopons.github.io/Agrobot/`
- **Redirect URLs**: la misma dirección.

> ⚠️ **El correo de Supabase de fábrica solo manda enlaces a los miembros del
> equipo del proyecto**, y unos pocos por hora. Para que entren otros usuarios
> hay que configurar un servidor de correo propio en
> **Authentication → Emails → SMTP Settings**. Brevo y Resend tienen planes
> gratuitos que alcanzan de sobra. Mientras no lo configures, podés entrar vos
> (sos dueño del proyecto) e invitar usuarios desde **Authentication → Users →
> Invite user**.

## Paso 4 — Conectar el tablero

Editá `docs/config.js` con la URL, la clave **anon** y el usuario del bot:

```js
window.AGROBOT = {
  supabaseUrl: "https://xxxx.supabase.co",
  supabaseAnonKey: "eyJhbGciOi...",
  botTelegram: "mi_agrobot_bot",
};
```

Activá GitHub Pages si no estaba: en el repo, **Settings → Pages → Branch:
main, carpeta /docs**.

## Paso 5 — Secrets de GitHub Actions

**Settings → Secrets and variables → Actions → New repository secret**:

| Name | Valor |
|---|---|
| `SUPABASE_URL` | la Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | la clave **service_role** |
| `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET` | como antes (README §3.1) |
| `TELEGRAM_BOT_TOKEN` | como antes |

`TELEGRAM_CHAT_ID` ya no se usa: cada aviso va al chat que vinculó el dueño
del lote. `CDSAPI_URL` / `CDSAPI_KEY` solo hacen falta si `agera5` está entre
las fuentes de clima.

## Paso 6 — Pasar el lote actual a tu cuenta

Para no perder la campaña ya calculada:

1. Entrá una vez al tablero con tu correo (así se crea tu cuenta).
2. En tu PC, en la carpeta del repo:

   ```
   set SUPABASE_URL=https://xxxx.supabase.co
   set SUPABASE_SERVICE_ROLE_KEY=eyJ...
   python -m scripts.migrar_a_supabase --email tu@correo.com --nombre "Lote 20"
   ```

Crea el lote con el polígono, la siembra, el umbral y la calicata de
`config.yaml`, y copia la serie, el estado, el resumen y el mosaico. La
corrida siguiente continúa desde el último día, sin recalcular. Se puede
volver a correr: actualiza el mismo lote.

## Paso 7 — Activarlo

Mergeá la rama a `main`. Desde ahí:

- **06:00** (Argentina): todos los lotes activos, con los días nuevos.
- **Cada hora, a los :30**: solo los lotes nuevos, los modificados desde el
  tablero o los que pidieron recalcular. Un lote recién cargado aparece
  calculado en menos de una hora.

Para correr un lote puntual a mano: **Actions → Balance Hídrico → Run
workflow**, con el id del lote (está en la tabla `lotes` de Supabase).

---

## Lo que queda público y lo que no

El repositorio sigue siendo público (es lo que hace gratis a GitHub Pages y a
Actions), pero **ya no tiene datos de lotes**: la corrida no commitea nada.

> ⚠️ Los archivos del lote 20 de antes de la migración siguen en el repo
> (`data/`, `docs/balance_hidrico_lote20.csv`, `docs/estado_actual.json`,
> `docs/lote_actual.geojson`, `docs/ndvi_mosaico.png`, `aoi/`). Una vez
> migrado, borralos con un commit para que dejen de publicarse. Quedan en el
> historial de Git: si ese lote es sensible, la única forma de sacarlos del
> todo es reescribir el historial o pasar a un repo nuevo.

## Administración

Todo se ve desde Supabase → **Table Editor**:

- `lotes`: todos los lotes de todos los usuarios. `activo = false` pausa un
  lote sin borrar su serie.
- `estado_lote.ultimo_error`: por qué falló la última corrida de un lote.
- **Authentication → Users**: dar de baja un usuario borra sus lotes y sus
  resultados.

Un lote que falla no frena a los demás. La corrida de Actions termina en rojo
para que se vea, y el error queda en `estado_lote.ultimo_error` y en el
tablero del usuario.

## Probarlo sin red

```
pip install pytest
python -m pytest tests -q
```

Corre el flujo completo con varios usuarios y lotes, con NDVI, clima y
Supabase simulados.
