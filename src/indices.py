"""
indices.py
----------
Índices espectrales del lote calculados con bandas de Sentinel-2 L2A de
Copernicus Data Space: los mismos del script de Google Earth Engine que se
usaba en la EEA Manfredi (NDVI, EVI, GLI, SAVI, GCI, RGR, SIPI, ARVI, NBRI), más el NDRE de borde rojo.

Dos productos por lote:

  · Serie: por cada pasada con el lote despejado, la mediana y los
    percentiles 10 y 90 de cada índice sobre el polígono (Statistical API,
    una sola consulta para todos los índices).
  · Mapas: de la última fecha despejada, una imagen por índice y una en color
    natural, recortadas al lote (Process API).

Diferencias con el script de GEE, a propósito:

  · Copernicus entrega reflectancia (0–1). La colección COPERNICUS/S2_SR de GEE
    viene escalada ×10000, y con esos valores el EVI del script (que suma 1 en
    el denominador) no da el EVI: acá sí, porque las fórmulas están pensadas
    para reflectancia.
  · En lugar de filtrar escenas por nubosidad (< 20 %) y promediar 30 días, se
    enmascara píxel a píxel con la banda SCL —igual que el NDVI del balance— y
    se descarta la fecha si queda despejado menos del 70 % del lote.
  · El valor del lote es la mediana, no la media: SIPI, GCI y RGR son cocientes
    que se disparan sobre suelo desnudo y un puñado de píxeles arrastraría el
    promedio.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from .auth_cdse import obtener_token

logger = logging.getLogger(__name__)

STATISTICS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
PROCESS_URL = "https://sh.dataspace.copernicus.eu/api/v1/process"

# clave: (nombre, expresión en el evalscript, rango del mapa, invertir paleta)
# Las expresiones usan reflectancia; `d(a, b)` es a/b protegido contra b = 0.
INDICES = {
    "ndvi": ("NDVI", "d(B08 - B04, B08 + B04)", (0.0, 1.0), False),
    "evi":  ("EVI", "2.5 * d(B08 - B04, B08 + 6 * B04 - 7.5 * B02 + 1)", (0.0, 1.0), False),
    "savi": ("SAVI", "1.5 * d(B08 - B04, B08 + B04 + 0.5)", (0.0, 1.0), False),
    "arvi": ("ARVI", "d(B08 - 2 * B04 + B02, B08 + 2 * B04 + B02)", (0.0, 1.0), False),
    "gli":  ("GLI", "d((B03 - B04) + (B03 - B02), 2 * B03 + B04 + B02)", (0.0, 0.3), False),
    "gci":  ("GCI", "d(B08, B03) - 1", (0.0, 8.0), False),
    "rgr":  ("RGR", "d(B04, B03)", (0.4, 1.6), True),
    "sipi": ("SIPI", "d(B08 - B02, B08 - B04)", (0.8, 1.8), False),
    "nbri": ("NBRI", "d(B08 - B12, B08 + B12)", (-0.2, 0.8), False),
    # Borde rojo: B05 (705 nm) contra el NIR angosto B8A (865 nm), las dos de
    # 20 m. Sigue respondiendo a la clorofila cuando el NDVI ya se saturó.
    "ndre": ("NDRE", "d(B8A - B05, B8A + B05)", (0.0, 0.6), False),
}

# Paleta del script de GEE: azul para lo más bajo, del ocre al verde oscuro
PALETA = ["#0000ff", "#DF923D", "#F1B555", "#FCD163", "#99B718", "#74A901", "#66A000",
          "#529400", "#3E8601", "#207401", "#056201", "#004C00", "#023B01", "#012E01",
          "#011D01", "#011301"]

PERCENTILES = (10, 50, 90)

_FUNCION_D = "function d(a, b) { return Math.abs(b) < 1e-6 ? NaN : a / b; }"


def _condicion_scl(scl_validas) -> str:
    return " || ".join(f"s.SCL == {int(c)}" for c in scl_validas)


BANDAS = ["B02", "B03", "B04", "B05", "B08", "B8A", "B12", "SCL", "dataMask"]


def _variables() -> str:
    return ("var B02 = s.B02, B03 = s.B03, B04 = s.B04, B05 = s.B05, "
            "B08 = s.B08, B8A = s.B8A, B12 = s.B12;")


def evalscript_estadisticas(scl_validas=(4, 5)) -> str:
    salidas = ",\n      ".join(
        f'{{ id: "{k}", bands: 1, sampleType: "FLOAT32" }}' for k in INDICES)
    valores = "\n  ".join(
        f"var v_{k} = {expr};" for k, (_, expr, _r, _i) in INDICES.items())
    # Un píxel cuyo índice no es finito (cociente por cero) queda fuera del
    # cálculo de ESE índice: se lo reemplaza por NaN, que la Statistical API
    # no cuenta. El dataMask común es la máscara de nubes.
    retorno = ",\n    ".join(f"{k}: [isFinite(v_{k}) ? v_{k} : NaN]" for k in INDICES)
    return f"""//VERSION=3
function setup() {{
  return {{
    input: [{{ bands: {json.dumps(BANDAS)} }}],
    output: [
      {salidas},
      {{ id: "dataMask", bands: 1 }}
    ]
  }};
}}
{_FUNCION_D}
function evaluatePixel(s) {{
  {_variables()}
  var valida = s.dataMask == 1 && ({_condicion_scl(scl_validas)});
  {valores}
  return {{
    {retorno},
    dataMask: [valida ? 1 : 0]
  }};
}}
"""


def serie(client_id: str, client_secret: str, geojson: dict, desde: str, hasta: str,
          resolucion_m: int = 10, max_cloud_coverage: int = 90,
          min_cobertura_pct: float = 70.0, scl_validas=(4, 5)) -> list[dict]:
    """Una fila por pasada con el lote despejado:
    {"fecha", "cobertura_pct", "valores": {indice: {"mediana", "p10", "p90", "media"}}}
    """
    token = obtener_token(client_id, client_secret)
    payload = {
        "input": {
            "bounds": {"geometry": geojson,
                       "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
            "data": [{"type": "sentinel-2-l2a",
                      "dataFilter": {"maxCloudCoverage": max_cloud_coverage}}],
        },
        "aggregation": {
            "timeRange": {"from": f"{desde}T00:00:00Z", "to": f"{hasta}T23:59:59Z"},
            "aggregationInterval": {"of": "P1D"},
            "evalscript": evalscript_estadisticas(scl_validas),
            "resx": resolucion_m, "resy": resolucion_m,
        },
        "calculations": {"default": {"statistics": {"default": {
            "percentiles": {"k": list(PERCENTILES)}}}}},
    }
    r = requests.post(STATISTICS_URL, json=payload, timeout=180,
                      headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        raise RuntimeError(f"Statistical API ({r.status_code}): {r.text[:300]}")
    return interpretar_estadisticas(r.json(), min_cobertura_pct)


def interpretar_estadisticas(body: dict, min_cobertura_pct: float = 70.0) -> list[dict]:
    filas = []
    for intervalo in body.get("data", []):
        fecha = intervalo["interval"]["from"][:10]
        salidas = intervalo.get("outputs", {})
        stats_ndvi = (salidas.get("ndvi", {}).get("bands", {}).get("B0", {}).get("stats") or {})
        n = stats_ndvi.get("sampleCount", 0)
        validos = n - stats_ndvi.get("noDataCount", 0)
        cobertura = 100 * validos / n if n else 0.0
        if validos <= 0 or cobertura < min_cobertura_pct:
            continue
        valores = {}
        for k in INDICES:
            st = salidas.get(k, {}).get("bands", {}).get("B0", {}).get("stats") or {}
            pc = st.get("percentiles") or {}

            def p(q):
                v = pc.get(f"{float(q)}", pc.get(str(q)))
                return round(float(v), 4) if v is not None else None
            if p(50) is None:
                continue
            valores[k] = {"mediana": p(50), "p10": p(10), "p90": p(90),
                          "media": round(float(st["mean"]), 4) if st.get("mean") is not None else None}
        if valores:
            filas.append({"fecha": fecha, "cobertura_pct": round(cobertura, 1), "valores": valores})
    return filas


# ---------------------------------------------------------------------------
# Mapas
# ---------------------------------------------------------------------------
def _rgb(hexa: str) -> str:
    h = hexa.lstrip("#")
    return "[" + ", ".join(f"{int(h[i:i + 2], 16) / 255:.3f}" for i in (0, 2, 4)) + "]"


def evalscript_mapa(clave: str, scl_validas=(4, 5)) -> str:
    """PNG con el índice coloreado; nubes y sombras en gris, fuera del lote transparente."""
    validas = _condicion_scl(scl_validas)
    if clave == "rgb":
        cuerpo = "return [Math.min(1, 2.5 * s.B04), Math.min(1, 2.5 * s.B03), Math.min(1, 2.5 * s.B02), 1];"
    else:
        _, expr, (lo, hi), invertir = INDICES[clave]
        paleta = list(reversed(PALETA)) if invertir else PALETA
        colores = "[" + ", ".join(_rgb(c) for c in paleta) + "]"
        cuerpo = f"""if (!({validas})) return [0.78, 0.80, 0.84, 1];
  var v = {expr};
  if (!isFinite(v)) return [0.78, 0.80, 0.84, 1];
  var t = Math.max(0, Math.min(1, (v - ({lo})) / (({hi}) - ({lo}))));
  var P = {colores};
  var c = P[Math.min(P.length - 1, Math.floor(t * P.length))];
  return [c[0], c[1], c[2], 1];"""
    return f"""//VERSION=3
function setup() {{
  return {{ input: [{{ bands: {json.dumps(BANDAS)} }}],
           output: {{ bands: 4 }} }};
}}
{_FUNCION_D}
function evaluatePixel(s) {{
  if (s.dataMask === 0) return [0, 0, 0, 0];
  {_variables()}
  {cuerpo}
}}
"""


def _tamano(bbox, lado: int) -> tuple[int, int]:
    import math
    w, s, e, n = bbox
    ancho_m = (e - w) * 111320 * math.cos(math.radians((s + n) / 2))
    alto_m = (n - s) * 110540
    if ancho_m >= alto_m:
        return lado, max(32, round(lado * alto_m / ancho_m))
    return max(32, round(lado * ancho_m / alto_m)), lado


def mapa(client_id: str, client_secret: str, geojson: dict, bbox, fecha: str, clave: str,
         lado: int = 512, scl_validas=(4, 5)) -> Optional[bytes]:
    token = obtener_token(client_id, client_secret)
    ancho, alto = _tamano(bbox, lado)
    payload = {
        "input": {
            "bounds": {"geometry": geojson,
                       "properties": {"crs": "http://www.opengis.net/def/crs/OGC/1.3/CRS84"}},
            "data": [{"type": "sentinel-2-l2a", "dataFilter": {
                "timeRange": {"from": f"{fecha}T00:00:00Z", "to": f"{fecha}T23:59:59Z"}}}],
        },
        "output": {"width": ancho, "height": alto,
                   "responses": [{"identifier": "default", "format": {"type": "image/png"}}]},
        "evalscript": evalscript_mapa(clave, scl_validas),
    }
    r = requests.post(PROCESS_URL, json=payload, timeout=120,
                      headers={"Authorization": f"Bearer {token}", "Accept": "image/png"})
    if r.status_code != 200:
        logger.warning("Mapa de %s del %s: Process API respondió %s", clave, fecha, r.status_code)
        return None
    return r.content


def leyendas() -> dict:
    """Rangos y paleta de cada mapa, para dibujar la leyenda en el tablero."""
    return {k: {"nombre": n, "min": r[0], "max": r[1],
                "paleta": list(reversed(PALETA)) if inv else PALETA}
            for k, (n, _e, r, inv) in INDICES.items()}
