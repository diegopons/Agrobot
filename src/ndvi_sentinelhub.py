"""
ndvi_sentinelhub.py
--------------------
Reemplaza la extracción de NDVI que antes se hacía en Google Earth Engine
(Sentinel-2 + Landsat + MODIS fusionados) por la Statistical API de
Copernicus Data Space Ecosystem, usando únicamente Sentinel-2 L2A (10 m).

La Statistical API calcula estadísticas (media, etc.) directamente en el
servidor sobre el polígono del lote, sin descargar imágenes completas —
es la forma correcta y liviana de sacar una serie temporal de un índice
para un AOI chico.

El enmascarado de nubes/sombras/nieve se hace vía la banda SCL (Scene
Classification Layer) de Sentinel-2 L2A dentro del evalscript.
"""
from __future__ import annotations

import logging
import math
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import requests

from .auth_cdse import obtener_token

logger = logging.getLogger(__name__)

STATISTICS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"

# Clases de la Scene Classification Layer de Sentinel-2 L2A:
#
#   0 sin datos          4 vegetación         8 nube, prob. media
#   1 saturado/defectuoso 5 suelo desnudo     9 nube, prob. alta
#   2 píxel oscuro/sombra 6 AGUA             10 cirros delgados
#   3 sombra de nube      7 sin clasificar   11 nieve o hielo
#
# Se declaran VÁLIDAS las clases aceptadas, no las rechazadas. Antes era al
# revés —se excluían 3, 8, 9, 10 y 11— y eso dejaba entrar al promedio las
# clases 0, 1, 2, 6 y 7, que sobre un lote no son dato: el agua da NDVI de
# −0,3 a −0,6, los píxeles oscuros dan valores negativos y ruidosos, y los
# que no tienen dato aportan cero. Esos píxeles además contaban como válidos
# para el control de cobertura, así que una fecha contaminada pasaba el filtro
# y arrastraba el promedio del lote a valores negativos.
#
# Con lista blanca, una clase nueva o inesperada queda afuera por defecto, que
# es el lado correcto para equivocarse: un hueco se interpola, un valor
# contaminado se propaga.
SCL_VALIDAS_DEFECTO = (4, 5)


def resolucion_en_grados(metros: float, geojson: dict) -> tuple[float, float]:
    """resx, resy en grados para pedir píxeles de `metros` con bounds en CRS84.

    La Statistical API interpreta resx/resy en las unidades del CRS de los
    bounds, y con CRS84 son GRADOS. Pedir resx=10 pensando en metros daba
    píxeles de 10° (~1.100 km): el lote entero caía en un único píxel y el
    "promedio del lote" era el valor de ese píxel suelto. Por eso el NDVI
    saltaba entre pasadas y tantas fechas salían fuera de rango.
    """
    from shapely.geometry import shape
    lat = shape(geojson).centroid.y
    resx = metros / (111_320 * max(0.01, math.cos(math.radians(lat))))
    resy = metros / 110_540
    return resx, resy


def _evalscript(scl_validas) -> str:
    """Evalscript con la lista blanca de clases SCL embebida."""
    condicion = " || ".join(f"scl == {int(c)}" for c in scl_validas)
    return """
//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04", "B08", "SCL", "dataMask"] }],
    output: [
      { id: "data", bands: 1, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}

function evaluatePixel(sample) {
  var scl = sample.SCL;
  var valida = (__CONDICION__);
  var ndvi = (sample.B08 - sample.B04) / (sample.B08 + sample.B04 + 1e-9);
  return {
    data: [ndvi],
    dataMask: [valida ? sample.dataMask : 0]
  };
}
""".replace("__CONDICION__", condicion)


def obtener_serie_ndvi(
    client_id: str,
    client_secret: str,
    geojson_geometry: dict,
    fecha_inicio: str,
    fecha_fin: str,
    max_cloud_coverage: int = 60,
    resolucion_m: int = 10,
    min_pixeles_validos_pct: float = 70.0,
    scl_validas=SCL_VALIDAS_DEFECTO,
    ndvi_minimo_valido: float = 0.0,
) -> pd.DataFrame:
    """Devuelve un DataFrame diario (fecha, ndvi, cobertura_pct) del rango.

    Los días sin pasada de Sentinel-2 (o completamente nublados) quedan con
    ndvi = NaN; el arrastre (ffill) se resuelve más adelante, en el módulo de
    balance, para no perder información sobre qué días son realmente datos
    satelitales vs. arrastrados.

    `ndvi_minimo_valido` es un control de plausibilidad sobre el promedio del
    lote: por debajo de ese valor la fecha se descarta. Sobre una superficie
    agrícola un NDVI medio negativo no es una medición baja, es un síntoma de
    que al promedio entró algo que no es el lote. El piso por defecto es cero,
    que es el único umbral que no puede descartar un dato legítimo: ni el suelo
    desnudo más húmedo da negativo.
    """
    token = obtener_token(client_id, client_secret)
    resx, resy = resolucion_en_grados(resolucion_m, geojson_geometry)

    payload = {
        "input": {
            "bounds": {
                "geometry": geojson_geometry,
                "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"},
            },
            "data": [
                {
                    "type": "sentinel-2-l2a",
                    "dataFilter": {"maxCloudCoverage": max_cloud_coverage},
                }
            ],
        },
        "aggregation": {
            "timeRange": {
                "from": f"{fecha_inicio}T00:00:00Z",
                "to": f"{fecha_fin}T23:59:59Z",
            },
            "aggregationInterval": {"of": "P1D"},
            "evalscript": _evalscript(scl_validas),
            # en grados: los bounds van en CRS84 (ver resolucion_en_grados)
            "resx": resx,
            "resy": resy,
        },
        "calculations": {"default": {}},
    }

    resp = requests.post(
        STATISTICS_URL,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Error consultando Statistical API de CDSE ({resp.status_code}): {resp.text}"
        )

    body = resp.json()
    filas = []
    descartes = {"cobertura": 0, "sin_pixeles": 0, "fuera_de_rango": 0}
    for intervalo in body.get("data", []):
        fecha_str = intervalo["interval"]["from"][:10]
        try:
            stats = intervalo["outputs"]["data"]["bands"]["B0"]["stats"]
        except KeyError:
            continue
        sample_count = stats.get("sampleCount", 0)
        nodata_count = stats.get("noDataCount", 0)
        validos = sample_count - nodata_count
        cobertura = (validos / sample_count * 100) if sample_count else 0.0
        if 0 < sample_count < 10:
            logger.warning("  %s: el lote cae en solo %d píxel(es); revisar la resolución.",
                           fecha_str, sample_count)

        # Un día con la mitad del lote tapado por una nube da un promedio que
        # describe la otra mitad, no el lote. Mejor descartarlo y dejar que la
        # interpolación cubra el hueco que meter un valor sesgado en la serie.
        if validos <= 0 or cobertura < min_pixeles_validos_pct:
            if validos > 0:
                logger.info(
                    "  %s descartado: solo %.0f%% del lote con píxeles válidos.",
                    fecha_str, cobertura,
                )
                descartes["cobertura"] += 1
            else:
                descartes["sin_pixeles"] += 1
            ndvi_val = None
        else:
            ndvi_val = stats.get("mean")
            # Control de plausibilidad. Si llega hasta acá un promedio fuera
            # de rango, el enmascarado dejó pasar algo que no es el lote.
            if ndvi_val is None or not (ndvi_minimo_valido <= ndvi_val <= 1.0):
                logger.warning(
                    "  %s DESCARTADO: NDVI medio %.4f fuera del rango "
                    "plausible [%.2f, 1.00] con %.0f%% de cobertura. "
                    "Revisá que el polígono cubra solo el lote.",
                    fecha_str, ndvi_val if ndvi_val is not None else float("nan"),
                    ndvi_minimo_valido, cobertura,
                )
                descartes["fuera_de_rango"] += 1
                ndvi_val = None
        filas.append({"fecha": fecha_str, "ndvi": ndvi_val, "cobertura_pct": round(cobertura, 1)})

    df = pd.DataFrame(filas)
    if df.empty:
        df = pd.DataFrame(columns=["fecha", "ndvi", "cobertura_pct"])
    df["fecha"] = pd.to_datetime(df["fecha"])

    validas = int(df["ndvi"].notna().sum()) if len(df) else 0
    logger.info(
        "NDVI: %d fecha(s) con dato de %d pasada(s). Descartadas: "
        "%d por cobertura, %d sin píxeles, %d fuera de rango.",
        validas, len(df), descartes["cobertura"], descartes["sin_pixeles"],
        descartes["fuera_de_rango"],
    )
    if descartes["fuera_de_rango"]:
        logger.warning(
            "%d fecha(s) con NDVI fuera de rango. La causa más frecuente es un "
            "polígono que incluye superficie que no es el lote (agua, camino, "
            "monte) o que está desplazado respecto del cultivo.",
            descartes["fuera_de_rango"],
        )
    # Se adjunta el diagnóstico al DataFrame para que la corrida pueda
    # publicarlo sin cambiar la firma de la función.
    df.attrs["descartes"] = descartes
    df.attrs["fechas_con_dato"] = validas
    return df


def rellenar_ndvi(
    df_dias: pd.DataFrame,
    ndvi_previo: float | None = None,
    max_dias_interpolar: int = 30,
) -> tuple[pd.DataFrame, dict]:
    """Completa los días sin imagen interpolando entre observaciones.

    Sentinel-2 pasa cada cinco días y las nubes agrandan los huecos, así que
    la mayoría de los días no tiene dato propio. Arrastrar el último valor
    (lo que hacíamos antes) convierte la curva en una escalera: durante el
    crecimiento subestima el NDVI, y en senescencia lo sobreestima, y el Kc
    hereda ese error.

    Interpolar linealmente entre dos fechas con imagen es una aproximación
    mucho más razonable, porque el canopeo cambia de forma gradual.

    Qué hace con los extremos:
      - Días anteriores a la primera observación: usa `ndvi_previo` (el
        último valor conocido de la corrida anterior) si existe; si no,
        repite hacia atrás la primera observación.
      - Días posteriores a la última observación: repite el último valor.
        No hay forma de hacer otra cosa, pero se informa cuántos son.

    Devuelve el DataFrame con 'ndvi' completo y un resumen para el log.
    """
    df = df_dias.sort_values("fecha").reset_index(drop=True).copy()
    serie = df["ndvi"]
    observados = int(serie.notna().sum())

    resumen = {
        "observados": observados,
        "total": len(df),
        "interpolados": 0,
        "arrastrados_al_final": 0,
        "hueco_mayor": 0,
    }

    if observados == 0:
        # Sin ninguna imagen en el tramo: queda lo que haya del estado previo
        if ndvi_previo is not None:
            df["ndvi"] = ndvi_previo
            resumen["arrastrados_al_final"] = len(df)
        return df, resumen

    primero = serie.first_valid_index()
    ultimo = serie.last_valid_index()

    # hueco más largo entre observaciones, para saber cuánto estamos suponiendo
    fechas_obs = df.loc[serie.notna(), "fecha"]
    if len(fechas_obs) > 1:
        resumen["hueco_mayor"] = int(fechas_obs.diff().dt.days.max())

    # tramo central: interpolación lineal sobre el tiempo
    df["ndvi"] = serie.interpolate(method="linear", limit_area="inside")

    # antes de la primera imagen
    if primero > 0:
        if ndvi_previo is not None:
            # se une linealmente el último valor conocido con la primera imagen
            df.loc[:primero, "ndvi"] = pd.Series(
                [ndvi_previo] + [None] * (primero - 1) + [serie[primero]]
            ).interpolate().values
        else:
            df.loc[:primero, "ndvi"] = serie[primero]

    # después de la última imagen: no queda más que sostener el valor
    if ultimo < len(df) - 1:
        df.loc[ultimo:, "ndvi"] = serie[ultimo]
        resumen["arrastrados_al_final"] = len(df) - 1 - ultimo

    resumen["interpolados"] = int(df["ndvi"].notna().sum()) - observados
    if resumen["hueco_mayor"] > max_dias_interpolar:
        logger.warning(
            "El hueco más largo sin imagen es de %d días: la interpolación en "
            "ese tramo es una suposición amplia.",
            resumen["hueco_mayor"],
        )
    return df, resumen


if __name__ == "__main__":
    # Prueba manual rápida (requiere credenciales reales en variables de entorno)
    import os
    import sys
    import json as _json

    logging.basicConfig(level=logging.INFO)
    cid = os.environ["CDSE_CLIENT_ID"]
    csec = os.environ["CDSE_CLIENT_SECRET"]
    geom = _json.load(open(sys.argv[1]))
    hoy = date.today()
    df = obtener_serie_ndvi(cid, csec, geom, str(hoy - timedelta(days=20)), str(hoy - timedelta(days=5)))
    print(df)
