# Correr el balance hídrico en GitHub Actions (guía desde cero)

> **Con varios usuarios (Supabase) esto cambió.** El workflow ya no guarda
> resultados en el repositorio: los escribe en Supabase. Los Secrets
> necesarios son `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
> `CDSE_CLIENT_ID`, `CDSE_CLIENT_SECRET` y `TELEGRAM_BOT_TOKEN`
> (`TELEGRAM_CHAT_ID` ya no se usa). Corre a las 06:00 para todos los lotes
> y cada hora para los lotes nuevos o modificados, y el formulario de
> «Run workflow» pide el id de un lote en lugar de los parámetros de inicio,
> que ahora se cargan en el tablero. Ver [SUPABASE.md](SUPABASE.md). El resto
> de esta guía (cuentas, Secrets, ver una corrida, errores comunes) sigue
> valiendo.

Esta guía asume que nunca usaste GitHub. No hace falta saber Git para
que esto funcione: se puede hacer todo desde el navegador.

---

## Qué es esto, en dos párrafos

**GitHub** es un lugar donde se guardan carpetas de código ("repositorios").

**GitHub Actions** es un servicio de GitHub que te presta una computadora
Linux temporal para ejecutar algo cada tanto. Vos le dejás escrito, en un
archivo, qué querés que haga y con qué frecuencia; GitHub enciende esa
máquina a la hora indicada, corre el script, y la apaga.

Lo importante: **esa máquina se destruye al terminar**. Todo lo que el
script escribió se perdería. Por eso el workflow, como último paso, guarda
`data/estado.json` y el CSV de vuelta dentro del repositorio. Así, la
próxima corrida arranca leyendo el estado que dejó la anterior — que es
exactamente lo que necesita nuestro balance incremental.

Para este volumen de uso es **gratis** (los repos públicos tienen minutos
ilimitados; los privados, 2000 minutos por mes, y cada corrida nuestra usa
2-3 minutos).

---

## Paso 1 — Crear la cuenta y el repositorio

1. Crear cuenta en <https://github.com> (si no tenés).
2. Arriba a la derecha, botón **+** → **New repository**.
3. Completar:
   - **Repository name**: `balance-hidrico-lote20`
   - Elegir **Private** (recomendado: tus datos de lote no quedan públicos).
   - **No** tildes "Add a README file" (ya tenemos uno).
4. **Create repository**.

## Paso 2 — Subir los archivos

En la pantalla que aparece, hacé clic en **uploading an existing file**.

1. Descomprimí el ZIP del proyecto en tu PC.
2. Arrastrá **todo el contenido** de la carpeta `balance_hidrico_periodico`
   a la ventana del navegador.
3. Abajo, botón **Commit changes**.

> ⚠️ **Importante**: la carpeta `.github` empieza con un punto, y Windows
> a veces la oculta. Si al arrastrar no se subió, activá en el Explorador
> de Windows: pestaña **Vista** → tildar **Elementos ocultos**, y volvé a
> subirla. Sin esa carpeta, GitHub Actions no se entera de que tiene que
> hacer nada.

Verificá que en el repo se vea la carpeta `.github/workflows/` con el
archivo `balance_hidrico.yml` adentro.

## Paso 3 — Cargar las credenciales (Secrets)

Los "Secrets" son variables cifradas: el workflow las puede usar, pero
nadie puede leerlas mirando el repositorio.

En tu repo: **Settings** (arriba) → en el menú izquierdo,
**Secrets and variables** → **Actions** → botón **New repository secret**.

Cargá estos seis, uno por uno:

| Name | Secret (valor) |
|---|---|
| `CDSE_CLIENT_ID` | el client id de Copernicus Data Space |
| `CDSE_CLIENT_SECRET` | el client secret de Copernicus Data Space |
| `CDSAPI_URL` | `https://cds.climate.copernicus.eu/api` |
| `CDSAPI_KEY` | tu API key del Climate Data Store |
| `TELEGRAM_BOT_TOKEN` | el token de tu bot de Telegram |
| `TELEGRAM_CHAT_ID` | el chat_id donde querés recibir las alertas |

(Cómo obtener las cuatro primeras está en la sección 3 del `README.md`;
cómo obtener las dos de Telegram está en `TELEGRAM.md`.)

## Paso 4 — Probar la primera corrida a mano

No esperes al horario automático. Probalo ya:

1. Pestaña **Actions** del repo.
2. Si aparece un cartel pidiendo habilitar workflows, aceptalo.
3. En la lista de la izquierda, clic en **Balance Hídrico Lote 20**.
4. A la derecha: botón **Run workflow** → **Run workflow**.
5. Refrescá la página. Aparece una corrida en curso (círculo amarillo).
   Hacé clic para ver los pasos en vivo.

**Resultado esperado:**
- ✅ Círculo verde = funcionó. Andá a la carpeta `data/` del repo: debería
  estar el CSV con los resultados y `estado.json`.
- ❌ Cruz roja = falló. Hacé clic en el paso rojo para ver el error.
  Los más comunes están más abajo.

## Paso 5 — Ajustar el horario

Ya está programado para correr **todos los días a las 06:00 de Argentina**.
Si querés cambiarlo, editá en `.github/workflows/balance_hidrico.yml` la
línea del `cron` (recordá: **se escribe en horario UTC**, Argentina es UTC-3,
así que sumale 3 horas a la hora que querés).

```yaml
- cron: "0 9 * * *"     # todos los días 09:00 UTC = 06:00 Argentina
- cron: "0 9 */3 * *"   # cada 3 días
- cron: "0 9 * * 1"     # todos los lunes
```

Un par de cosas a saber del `schedule` de GitHub:
- No es puntual: puede arrancar entre 5 y 30 minutos tarde según la carga
  de GitHub. Para este uso da igual.
- En repos que no tienen actividad por ~60 días, GitHub **desactiva** los
  schedules automáticos y te manda un mail. Como el workflow commitea
  resultados en cada corrida, el repo se mantiene activo solo.

---

## El formulario de "Run workflow"

Al hacer clic en **Run workflow** aparece un formulario con estos campos.
Los que dejes vacíos usan lo que diga `config.yaml`.

| Campo | Para qué |
|---|---|
| `fecha_siembra` | Desde cuándo calcular el balance |
| `au_inicial_mm` | Agua útil estimada a la siembra |
| `prof_raiz_inicial_cm` | Profundidad de raíz inicial |
| `fecha_fin` | Hasta qué día calcular |
| `rezago_clima` | Días de atraso de AgERA5 (por defecto 10) |
| `umbral_riego_pct` | % que dispara la alerta |
| `nombre_lote` | Nombre en el aviso de Telegram |
| `modo_aviso` | Cuándo notificar |
| `reiniciar_estado` | Descarta lo acumulado y recalcula |
| `enviar_resumen` | Manda el resumen aunque no haya días nuevos |

Lo mismo se puede hacer, más cómodo, desde la pestaña «Parámetros y
ejecución» de la página web (ver TABLERO.md).

> **Importante**: no uses el botón **Re-run** de una corrida anterior. Un
> re-run repite la corrida original con el código y la configuración de
> aquel momento, no con los actuales. Para una corrida nueva, entrá al
> workflow y usá **Run workflow**.

## Errores comunes

**`Faltan variables de entorno CDSE_CLIENT_ID / CDSE_CLIENT_SECRET`**
Los secrets no están cargados o tienen el nombre mal escrito. Revisá el
Paso 3 — los nombres son sensibles a mayúsculas.

**El paso "Guardar resultados" falla con `permission denied` / `403`**
Settings → Actions → General → abajo, en "Workflow permissions", elegir
**Read and write permissions** → Save.

**Falla la descarga de AgERA5 con error de licencia**
Falta aceptar los términos del dataset. Entrá una vez con tu usuario a
<https://cds.climate.copernicus.eu/datasets/sis-agrometeorological-indicators>
y aceptá "Terms of use". Es por única vez.

**Falla la instalación de `geopandas`**
Nuestro `aoi.py` puede trabajar sin geopandas (usa `pyshp` como respaldo).
Si diera problemas en el runner, se puede comentar la línea `geopandas` de
`requirements.txt` y el script sigue funcionando igual.

**Cambié el código pero los resultados son los mismos**
El código nuevo no recalcula lo ya calculado. Hay que volver a ejecutar, y
si el estado dice que ya se procesó hasta cierta fecha, tildar
`reiniciar_estado` para que rehaga la campaña.

**La corrida termina en verde pero no aparece nada nuevo en el CSV**
Es lo normal y esperable la mayoría de los días: por el rezago de AgERA5
(~10 días) y el revisit de Sentinel-2 (~5 días), muchos días no hay
información nueva para procesar. El log lo dice explícitamente
("Nada nuevo para procesar todavía"). No es un error.

---

## Cómo ver los resultados

Tres formas:

1. **En el repo**: carpeta `data/` → el CSV. GitHub lo muestra como tabla.
2. **Historial**: pestaña Actions → cada corrida → sección "Artifacts" abajo,
   con el CSV y el log descargables (se guardan 30 días).
3. **En tu PC**: si instalás Git, `git clone` del repo y después `git pull`
   te trae los resultados actualizados. Pero no es necesario para que esto
   funcione.

## Si algo falla, ¿te enterás?

Por defecto GitHub te manda un mail cuando un workflow programado falla.
Se controla en tu perfil → Settings → Notifications → "Actions".

Lo que **no** hace hoy es avisarte de la alerta de riego: si el % de Agua
Útil baja del umbral, queda escrito en el log pero nadie te lo notifica.
Si querés que eso dispare un mail o un mensaje de Telegram, se puede
agregar al workflow — decime y lo armamos.
