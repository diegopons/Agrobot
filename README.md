# Balance Hídrico Periódico 

> **Varios usuarios y lotes.** Con Supabase configurado, el sistema corre el
> balance para todos los lotes de todos los usuarios, cada usuario entra al
> tablero con su correo y ve solo sus lotes, y el bot responde a cada uno
> sobre los suyos. Puesta en marcha: [SUPABASE.md](SUPABASE.md); bot:
> [N8N.md](N8N.md). Sin Supabase, todo lo que sigue funciona igual que
> antes, con un solo lote definido en `config.yaml`.

Versión "de producción" del notebook `balance_hidrico_gee`, pensada para
correr sola, de forma periódica, en una PC Windows (Task Scheduler). Misma
física agronómica que la planilla Excel y el notebook, con dos cambios de
fuente de datos:

| Variable                 | Antes (notebook)        | Ahora                                                        |
|---------------------------|--------------------------|--------------------------------------------------------------|
| NDVI                      | GEE (S2+L8+L9+MODIS)     | **Copernicus Data Space Ecosystem** (Sentinel-2 L2A, Statistical API) |
| Días sin imagen           | se repetía el último valor | se interpola entre observaciones reales                      |
| Manejo del lote           | archivo fijo             | mapa en la página web, con guardado y ejecución desde ahí     |
| Precipitación y ETo       | GEE (ERA5-Land)          | **Copernicus Climate Data Store** (AgERA5)                    |
| AOI                       | subida manual cada vez   | shapefile `aoi/lote_20.shp` fijo en el proyecto               |

El script es **incremental**: cada corrida solo pide y procesa los días
nuevos desde la última vez, arrancando desde el estado (AU real y
profundidad de raíz) que dejó la corrida anterior — no se reprocesa la
campaña completa cada vez.

## 1. Estructura

```
balance_hidrico_periodico/
├── aoi/lote_20.shp (+ .shx .dbf .prj .cpg)   # polígono del lote
├── config.yaml                                # toda la configuración
├── requirements.txt
├── run_balance_hidrico.bat                    # para Task Scheduler
├── src/
│   ├── aoi.py            # carga del lote (GeoJSON o shapefile)
│   ├── suelo.py          # parámetros de suelo (de BH p-Pons.xlsx)
│   ├── balance.py         # fórmulas del balance (Kc, Ks, modelo diario)
│   ├── ndvi_sentinelhub.py# NDVI vía Copernicus Data Space Ecosystem
│   ├── mapas_ndvi.py      # mosaico de imágenes NDVI del lote
│   ├── clima_agera5.py    # precipitación + ETo vía CDS / AgERA5
│   ├── estado.py          # estado.json + CSV histórico
│   ├── auth_cdse.py       # login OAuth2 en CDSE
│   ├── config.py
│   ├── almacen.py         # dónde viven los lotes: config.yaml (local) o Supabase
│   └── main.py            # orquestador: corre el balance para cada lote
├── supabase/schema.sql    # tablas y permisos para el modo multiusuario
├── scripts/migrar_a_supabase.py  # pasa el lote local a una cuenta de Supabase
├── tests/                 # pruebas sin red (python -m pytest tests -q)
├── data/                  # (se crea sola) estado.json + CSV histórico
└── logs/                  # (se crea sola) logs de cada corrida
```

## 2. Instalación (una sola vez)

1. Instalar Python 3.11+ en la PC.
2. Abrir `cmd` en la carpeta del proyecto y crear un entorno virtual:
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

## 3. Credenciales

### 3.1 Copernicus Data Space Ecosystem (NDVI)

1. Crear cuenta gratuita en <https://dataspace.copernicus.eu/>.
2. Ir a <https://shapps.dataspace.copernicus.eu/dashboard/> → **User
   Settings** → **OAuth clients** → **Create new OAuth client**.
3. Copiar el `client_id` y `client_secret` generados.
4. Definirlos como **variables de entorno de usuario** en Windows (así no
   quedan escritas en ningún archivo del proyecto):
   - Buscar "Editar las variables de entorno del sistema" → Variables de
     entorno → Nueva (en "Variables de usuario"):
     - `CDSE_CLIENT_ID` = tu client id
     - `CDSE_CLIENT_SECRET` = tu client secret

### 3.2 Copernicus Climate Data Store (precipitación + ETo)

1. Crear cuenta en <https://cds.climate.copernicus.eu/>.
2. Entrar una vez a la página del dataset
   <https://cds.climate.copernicus.eu/datasets/sis-agrometeorological-indicators>
   y aceptar la licencia ("Terms of use").
3. En tu perfil de CDS copiar tu API key personal.
4. Crear el archivo `C:\Users\<tu_usuario>\.cdsapirc` con:
   ```
   url: https://cds.climate.copernicus.eu/api
   key: TU_API_KEY_PERSONAL
   ```

> ⚠️ Las APIs de Copernicus cambian de tanto en tanto los nombres exactos de
> parámetros. Si `descargar_agera5()` empieza a fallar, entrar a la página
> del dataset, armar la consulta con el asistente ("Show API request") y
> pegar ese `request` dentro de `config.yaml` → `cds.request_template`, sin
> tocar el código.

## 4. Configurar el lote y la campaña

Editar `config.yaml`:
- `campana.fecha_siembra`, `au_real_inicial_mm`, `prof_raiz_inicial_cm`: solo
  se usan la **primera vez** (después el estado se guarda solo). Para
  arrancar una campaña nueva en el mismo lote, borrar `data/estado.json`.
- `balance.umbral_riego_pct`: umbral de alerta de riego.
- Si el suelo cambia, agregar una clave `suelo:` en `config.yaml` con la
  misma estructura que `SUELO_PROPIEDADES_DEFAULT` en `src/suelo.py`
  (por defecto usa la serie "Los llanos - Serie Oncativo" de `BH p-Pons.xlsx`).

## 5. Probar una corrida manual

```
venv\Scripts\activate
python -m src.main
```

Revisar `data/balance_hidrico_lote20.csv` y `logs/balance_hidrico.log`.

## 6. Programar la corrida periódica (Task Scheduler)

1. Abrir **Programador de Tareas** → **Crear tarea básica**.
2. Nombre: `Balance Hídrico Lote 20`.
3. Desencadenador: por ejemplo **Semanal**, cada 5 días (frecuencia
   razonable dado el revisit de Sentinel-2 y el rezago de AgERA5;
   correrlo todos los días no aporta datos nuevos).
4. Acción: **Iniciar un programa**.
   - Programa/script: ruta completa a `run_balance_hidrico.bat`
   - Iniciar en: la carpeta del proyecto (ej.
     `C:\Users\Diego\balance_hidrico_periodico`)
5. En las variables de entorno del usuario que ejecuta la tarea deben estar
   definidas `CDSE_CLIENT_ID` y `CDSE_CLIENT_SECRET` (punto 3.1).

Cada corrida:
- si no hay días nuevos disponibles (por el rezago de datos), no hace nada
  y lo deja en el log;
- si hay días nuevos, descarga NDVI + clima, corre el balance, actualiza
  `data/balance_hidrico_lote20.csv` y `data/estado.json`, y si el % de Agua
  Útil quedó por debajo del umbral, deja una alerta bien visible en el log.

## 6b. Cómo se trata el NDVI

Sentinel-2 pasa cada cinco días y las nubes agrandan los huecos, así que la
mayoría de los días no tiene imagen propia. El sistema hace tres cosas:

1. **Enmascara por píxel** con la banda SCL de Sentinel-2 L2A: descarta
   nubes, sombras de nube, cirros y nieve dentro del lote.
2. **Descarta la fecha entera** si menos del 70 % del lote quedó con píxeles
   válidos (`cdse.min_pixeles_validos_pct`). Con menos que eso el promedio
   describe la parte despejada, no el lote.
3. **Interpola linealmente** entre fechas con observación real. Antes se
   arrastraba el último valor, lo que convertía la curva en una escalera:
   durante el crecimiento subestimaba el NDVI y en senescencia lo
   sobreestimaba, y el Kc heredaba ese error.

El filtro de nubosidad de la escena completa está en 90 %
(`cdse.max_cloud_coverage`) a propósito: el enmascarado real lo hace el paso
1, píxel a píxel, así que un umbral bajo solo descartaría pasadas en las que
el lote podía estar despejado.

Los días posteriores a la última imagen no se pueden interpolar: se sostiene
el último valor y el log dice cuántos son.

## 6c. De dónde sale el clima

El balance combina varias fuentes. Cada día toma el dato de la primera que lo
tenga, y el CSV guarda en qué fuente salió cada valor (`fuente_pp`,
`fuente_eto`). El orden se define en `config.yaml` → `clima.fuentes`:

| | `siga` | `openmeteo` | `agera5` |
|---|---|---|---|
| Qué es | pluviómetro del INTA | grilla ~9 km | reanálisis, grilla ~25 km |
| Lluvia | **medida** | modelada | reanálisis |
| ETo | Penman-Monteith con datos de la estación | Penman-Monteith | reanálisis |
| Rezago | 1–2 días | hasta ayer | ~10 días |
| Credenciales | ninguna | ninguna | cuenta en Copernicus CDS |
| Pronóstico | no | sí, 16 días | no |

Por defecto: `siga` y, para lo que la estación no cubra, `openmeteo`. Si una
fuente falla, la corrida sigue con la siguiente; si no responde ninguna, se
aborta sin escribir nada, porque un día sin clima no es un día sin lluvia.

Para probar las fuentes desde tu máquina, sin tocar nada:

```
python probar_clima.py        # últimos 30 días
python probar_clima.py 90
```

Muestra el rezago real de cada una, las compara entre sí y arma la serie
combinada.

### Las fuentes no dan lo mismo

Medido sobre el lote de Manfredi en septiembre de 2026, con la estación del
INTA a 930 m como referencia:

| 21 días comunes | Total | vs estación | Error medio diario | Correlación |
|---|---|---|---|---|
| Estación (pluviómetro) | 27,0 mm | — | — | — |
| Open-Meteo | 27,4 mm | +1 % | 0,96 mm | 0,81 |
| AgERA5 | 19,1 mm | −29 % | 0,49 mm | 0,98 |

Open-Meteo acierta el total y reparte mal los días: el 09/09 puso 3,7 mm que no
cayeron y el 20/09 se perdió casi toda una lluvia de 5,75 mm. AgERA5 sigue
mejor la forma diaria pero subestima el volumen. Sobre 30 días la brecha de
Open-Meteo se abre a +22 % (61,7 contra 50,5 mm).

Por eso la estación va primero. Cambiar el orden a mitad de campaña mezcla
series incompatibles: al hacerlo, conviene correr con "reiniciar" marcado.

### La ETo de la estación

La estación de Manfredi **no mide radiación solar** (0 de 266 días en 2026
tienen heliofanía o radiación global), así que la ETo se calcula por
Penman-Monteith FAO-56 con la radiación estimada desde la amplitud térmica
(ec. 50, coeficiente `krs` = 0,16 para zonas de interior). Temperatura máxima y
mínima, tensión de vapor y viento sí son medidos.

Validación contra la ETo de Open-Meteo sobre 21 días: 71,0 contra 74,8 mm, es
decir −5 %, con 0,43 mm/día de error absoluto medio y correlación 0,90.

El viento de la planilla del SIGA viene en km/h. Se verificó empíricamente:
interpretándolo como m/s la ETo daría 97,3 mm en esos mismos 21 días, un 30 %
de más.

### Advertencia sobre el acceso al SIGA

No existe una API pública documentada del SIGA. Los endpoints que usa este
sistema están tomados del paquete {siga} de R
(github.com/AgRoMeteorologiaINTA/siga), que los dedujo por ingeniería inversa:
el nombre del PHP está ofuscado y puede cambiar sin aviso. Por eso `siga` nunca
debe ser la única fuente de la lista.

### El pronóstico

Con `pronostico.habilitado: true` y `openmeteo` entre las fuentes, después de
calcular el balance el sistema corre los días pronosticados sobre una copia del
estado y, si el lote va a cruzar el umbral, lo agrega al mensaje de Telegram:

> Proyección: cruzaría el umbral en 4 días (06/10), con 152 mm (48 %).
> Lluvia esperada en el período: 17 mm.

No modifica el estado ni el CSV, y no cambia la lámina sugerida: solo anticipa.
El NDVI no se pronostica, se sostiene el último observado, así que el horizonte
conviene corto (10 días por defecto, máximo 16).

## 7. Notas y limitaciones a tener en cuenta

- El AOI (`aoi/lote_20.shp`) descargado de Drive para armar este proyecto
  tenía el `.dbf` dañado (problema de esa copia puntual, no del formato).
  `src/aoi.py` ya contempla ese caso con un método de respaldo que lee la
  geometría igual (no necesitamos atributos, solo el polígono). Conviene
  igual reemplazar los 5 archivos por el shapefile original completo.
- AgERA5 tiene ~10 km de resolución: para un lote de ~40 ha el valor es
  representativo de la zona, no hiperlocal. Si más adelante se quiere mayor
  detalle espacial de lluvia, se puede sumar una fuente pluviométrica local
  (estación propia / IoT) como capa adicional en `df_clima`.
- Sentinel-2 no pasa todos los días (revisit ~5 días) y puede haber nubes.
  Ver la sección 6b sobre cómo se completan esos huecos. En un lote chico el
  problema se agrava: 2 ha son unos 200 píxeles, así que una nube pequeña
  tapa una fracción grande del lote.
- Los riegos reales (si se aplican) hoy quedan en 0 por defecto
  (`df_dias["riego_mm"] = 0.0` en `main.py`); si se registran en campo,
  ese es el lugar para cargarlos antes de correr el balance.
