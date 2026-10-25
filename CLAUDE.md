# Agrobot — contexto para Claude

Balance hídrico diario por lote, multiusuario. Tablero público con portada en
https://diegopons.github.io/Agrobot/ (GitHub Pages desde `docs/`). Todo el texto
de cara al usuario está en castellano rioplatense (voseo).

## Cómo está armado

- **Corrida** (`python -m src.main`): GitHub Actions, `.github/workflows/balance_hidrico.yml`.
  06:00 ART todos los lotes; cada hora a los :30 solo lotes nuevos/modificados
  (`BH_SOLO_PENDIENTES`). Corrida manual con `lote_id`, `fecha_fin`, `reiniciar_estado`.
- **Almacén** (`src/almacen.py`): `AlmacenSupabase` (producción) o `AlmacenLocal`
  (un lote de `config.yaml`, modo original). `BH_ALMACEN=supabase` en Actions.
- **Supabase** (proyecto `iulgwrolimepdnhfgwqe`): esquema en `supabase/schema.sql`,
  idempotente; se vuelve a correr entero en el SQL Editor cada vez que cambia.
  RLS: cada usuario ve solo sus lotes; resultados de solo lectura; cuentas nuevas
  pendientes hasta que un admin las aprueba (pestaña Usuarios del tablero).
- **Datos por lote**: NDVI y 10 índices de Sentinel-2 (Copernicus Statistical/Process API),
  clima del SIGA del INTA (estación activa más cercana) + Open-Meteo, suelo desde la
  Carta de Suelos de Córdoba (WFS de IDECOR, `src/suelo_carta.py`) salvo calicata,
  riegos cargados por el usuario (entran como lluvia efectiva, por separado).
- **Avisos**: Telegram al dueño del lote; correo por Gmail SMTP para pedidos de
  acceso y aprobaciones (`src/avisos_admin.py`).

## Decisiones que no hay que deshacer

- Statistical API con bounds en CRS84: `resx/resy` **en grados**
  (`resolucion_en_grados`). Con 10 "metros" el lote era un único píxel de 10°.
- Cobertura de nubes contra los **píxeles del lote** (`pixeles_del_lote`), no del
  rectángulo de la grilla.
- La huella del estado (polígono, siembra, fuentes de clima, suelo) decide si se
  recalcula desde la siembra; un cambio de cualquiera abre una serie nueva.
- La carta de suelos se consulta una vez por polígono y se guarda; si IDECOR falla,
  perfil por defecto y reintento, sin guardar.
- El valor de los índices es la mediana (los cocientes se disparan en suelo desnudo).

## Trabajo con el usuario

- Commit y push a una rama propia después de cada etapa verificada, no al final.
- Correr `python -m pytest tests -q` antes de cada commit (hoy 32 pruebas).
- Las claves no se piden en el chat: van en `.env` local (ignorado por Git) o en
  los Secrets de GitHub. En la PC del usuario, Python necesita `truststore`
  (`truststore.inject_into_ssl()`) por intercepción TLS.

## Estado al 2026-10-25

Lotes: "Lote 20" (pivote de 92 ha sin cultivo, con calicata; el usuario pidió
dejarlo como está) y "lote21" (3,3 ha de trigo, siembra 20/05, perfil de la carta).

Pendiente:
1. Adaptar el bot de n8n (ver `N8N.md`): faltan el workflow exportado, dónde corre
   n8n y una secret key de Supabase propia para n8n.
2. Verificar que arranquen las corridas "schedule" de Actions (al cerrar no había).
3. Apagar el repo viejo `balance-hidrico-lote20`.
4. Rotar claves (la `sb_secret` quedó expuesta en una conversación).
5. Borrar del repo los datos viejos del Lote 20 (`data/`, `docs/*.csv|json|geojson|png`, `aoi/`).

Guías: `SUPABASE.md` (puesta en marcha), `N8N.md` (bot), `TABLERO.md` (uso).
