"""
suelo_carta.py
--------------
Perfil de suelo de un lote a partir de las Cartas de Suelos de Córdoba,
consultadas en el WFS de IDECOR (Mapas Córdoba).

Las cartas 1:50.000 y 1:100.000 traen, por unidad cartográfica, el agua útil
acumulada a 1 m, 1,5 m y 2 m (au_1m, au_1_5m, au_2m). Esos valores salen del
método de Faule et al. (2024, INTA EEA Manfredi): CC estimada con la humedad
equivalente de cada serie, PMP con el factor PMP/CC de su clase textural, y
ponderación por los componentes de la unidad.

Con eso se arma el perfil de 10 capas de 20 cm que usa el balance. El balance
solo usa el agua útil de cada capa (CC − PMP en mm), así que el perfil
respeta exactamente los tres totales de la carta; dentro de cada tramo
(0–1 m, 1–1,5 m, 1,5–2 m) el agua se reparte en partes iguales. Los
porcentajes de CC y PMP de cada capa son nominales: se expresan con la
densidad y la relación PMP/CC de la textura franco limosa (la más frecuente
en la provincia, Tabla 1 del mismo trabajo) solo para que el perfil tenga el
formato de siempre.

Si el lote cruza varias unidades, se pondera por superficie. Las unidades sin
dato de agua útil (lagunas, áreas urbanas, sierras sin relevar) no entran en
el promedio; si cubren más de la mitad del lote, no se usa la carta.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from pyproj import Geod
from shapely.geometry import shape

logger = logging.getLogger(__name__)

URL_WFS = "https://idecor-ws.mapascordoba.gob.ar/geoserver/idecor/wfs"
# De más detalle a menos; las cartas no se superponen entre sí
CAPAS = (("carta_suelo_50mil_2025", "1:50.000"), ("carta_suelo_100mil_2025", "1:100.000"))

# Franco limoso, Tabla 1 de Faule et al. (2024)
RELACION_PMP_CC = 0.497
DENSIDAD = 1.22

_GEOD = Geod(ellps="WGS84")


class ErrorCarta(RuntimeError):
    """El servicio no respondió: hay que reintentar, no concluir que no hay carta."""


def huella_geometria(geojson: dict) -> str:
    return hashlib.sha256(json.dumps(geojson, sort_keys=True).encode()).hexdigest()[:16]


def _area(geom) -> float:
    return abs(_GEOD.geometry_area_perimeter(geom)[0])


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


CAMPOS_CARTA = ",".join(
    ["simbolo", "nombre", "cu", "ip", "tipo_unida", "au_1m", "au_1_5m", "au_2m", "geom"]
    + [f"{c}_{i}" for i in range(1, 5) for c in ("serie", "fase", "porc")]
)
CAPA_LIMITANTES = "limitantes_edaficas"
LIMITANTES = (("textura", "Textura superficial"), ("drenaje", "Drenaje"),
              ("prof_efect", "Profundidad efectiva"), ("salinidad", "Salinidad"),
              ("alcalinida", "Alcalinidad"))


def _pedir_unidades(capa: str, bbox, url: str, timeout: int, campos: str = CAMPOS_CARTA) -> list[dict]:
    # El contorno rectangular del lote como polígono: es una URL corta aunque el
    # lote tenga cientos de vértices. Con BBOX() el servidor devolvía unidades
    # que no tocan el rectángulo (orden de ejes); el recorte fino se hace acá.
    w, s, e, n = bbox
    rect = f"POLYGON(({w} {s},{e} {s},{e} {n},{w} {n},{w} {s}))"
    try:
        r = requests.get(url, timeout=timeout, params={
            "service": "WFS", "version": "2.0.0", "request": "GetFeature",
            "typeNames": f"idecor:{capa}", "outputFormat": "application/json",
            "srsName": "EPSG:4326", "propertyName": campos,
            "CQL_FILTER": f"INTERSECTS(geom, SRID=4326;{rect})",
        })
    except requests.RequestException as exc:
        raise ErrorCarta(f"IDECOR no respondió ({capa}): {exc}") from exc
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        raise ErrorCarta(f"IDECOR devolvió {r.status_code} para {capa}: {r.text[:200]}")
    return r.json().get("features", [])


def _limitantes(lote, area_lote: float, url: str, timeout: int) -> dict:
    """{atributo: [{"valor": ..., "pct": ...}]} con lo que cubre al menos el 5 % del lote.

    Es información para mostrar, no entra al balance: si la capa falla, se
    sigue sin ella en lugar de dejar al lote sin perfil.
    """
    try:
        feats = _pedir_unidades(CAPA_LIMITANTES, lote.bounds, url, timeout,
                                campos=",".join([c for c, _ in LIMITANTES] + ["geom"]))
    except ErrorCarta as e:
        logger.warning("Limitantes edáficas no disponibles: %s", e)
        return {}
    acum: dict = {c: {} for c, _ in LIMITANTES}
    for f in feats:
        if not f.get("geometry"):
            continue
        inter = shape(f["geometry"]).buffer(0).intersection(lote)
        if inter.is_empty:
            continue
        pct = 100 * _area(inter) / area_lote
        for c, _ in LIMITANTES:
            v = f["properties"].get(c)
            if v:
                acum[c][v] = acum[c].get(v, 0.0) + pct
    return {
        c: [{"valor": v, "pct": round(p, 1)} for v, p in sorted(d.items(), key=lambda x: -x[1]) if p >= 5]
        for c, d in acum.items() if d
    }


def perfil_desde_au(au_1m: float, au_1_5m: Optional[float], au_2m: Optional[float]) -> list[dict]:
    """10 capas de 20 cm cuyo agua útil acumulada da au_1m, au_1_5m y au_2m."""
    au_1_5m = au_1_5m if au_1_5m is not None else au_1m
    au_2m = au_2m if au_2m is not None else au_1_5m
    por_cm = [
        (0, 100, au_1m / 100),
        (100, 150, max(0.0, au_1_5m - au_1m) / 50),
        (150, 200, max(0.0, au_2m - au_1_5m) / 50),
    ]
    capas = []
    for desde in range(0, 200, 20):
        hasta = desde + 20
        au = sum(max(0, min(hasta, b) - max(desde, a)) * tasa for a, b, tasa in por_cm)
        espesor_mm = (hasta - desde) * 10
        cc = au / (DENSIDAD * espesor_mm * (1 - RELACION_PMP_CC)) * 100
        capas.append({
            "prof_hasta_cm": hasta,
            "cc_porcentaje": round(cc, 3),
            "pmp_porcentaje": round(cc * RELACION_PMP_CC, 3),
            "densidad_aparente": DENSIDAD,
        })
    return capas


def consultar(geojson_lote: dict, url: str = URL_WFS, min_cobertura_pct: float = 50.0,
              timeout: int = 120) -> dict:
    """Consulta la carta para el lote. Devuelve siempre un dict para guardar:
    con "perfil" si hubo datos, o con "motivo" si el lote no tiene cobertura.
    Levanta ErrorCarta si el servicio falla (en ese caso no hay que guardar nada).
    """
    lote = shape(geojson_lote).buffer(0)
    area_lote = _area(lote)
    unidades, sin_dato_pct = [], 0.0

    for capa, escala in CAPAS:
        for f in _pedir_unidades(capa, lote.bounds, url, timeout):
            if not f.get("geometry"):
                continue
            inter = shape(f["geometry"]).buffer(0).intersection(lote)
            if inter.is_empty:
                continue
            pct = 100 * _area(inter) / area_lote
            if pct < 0.1:
                continue
            p = f["properties"]
            au = {k: _num(p.get(k)) for k in ("au_1m", "au_1_5m", "au_2m")}
            if au["au_1m"] is None:
                sin_dato_pct += pct
                continue
            series = [
                {"serie": str(p[f"serie_{i}"]).title(), "fase": p.get(f"fase_{i}"),
                 "porc": _num(p.get(f"porc_{i}"))}
                for i in range(1, 5) if p.get(f"serie_{i}")
            ]
            unidades.append({"simbolo": p.get("simbolo"), "nombre": p.get("nombre"),
                             "tipo": p.get("tipo_unida"), "capacidad_uso": p.get("cu"),
                             "ip": _num(p.get("ip")), "series": series,
                             "escala": escala, "pct": round(pct, 1), **au})

    base = {
        "fuente": "Cartas de Suelos de Córdoba (IDECOR). Agua útil según Faule et al. (2024), INTA EEA Manfredi.",
        "consultado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "huella_geometria": huella_geometria(geojson_lote),
        "unidades": unidades,
        "sin_dato_pct": round(sin_dato_pct, 1),
    }
    cobertura = sum(u["pct"] for u in unidades)
    base["cobertura_pct"] = round(cobertura, 1)
    base["limitantes"] = _limitantes(lote, area_lote, url, timeout)
    if unidades:
        ips = [(u["ip"], u["pct"]) for u in unidades if u["ip"] is not None]
        if ips:
            base["ip"] = round(sum(v * w for v, w in ips) / sum(w for _, w in ips))
        base["capacidad_uso"] = max(unidades, key=lambda u: u["pct"])["capacidad_uso"]
    if cobertura < min_cobertura_pct:
        base["motivo"] = (
            "El lote está fuera de las cartas 1:50.000 y 1:100.000." if not unidades and not sin_dato_pct
            else f"Solo el {cobertura:.0f} % del lote tiene dato de agua útil en la carta."
        )
        return base

    def ponderado(campo):
        vals = [(u[campo] if u[campo] is not None else None, u["pct"]) for u in unidades]
        if any(v is None for v, _ in vals):
            return None
        return sum(v * w for v, w in vals) / cobertura

    au_1m, au_1_5m, au_2m = ponderado("au_1m"), ponderado("au_1_5m"), ponderado("au_2m")
    base.update({
        "au_1m": round(au_1m, 1),
        "au_1_5m": round(au_1_5m, 1) if au_1_5m is not None else None,
        "au_2m": round(au_2m, 1) if au_2m is not None else None,
        "perfil": perfil_desde_au(au_1m, au_1_5m, au_2m),
    })
    return base


def describir(carta: dict) -> str:
    """'Oncativo 72 %, Manfredi 28 % (1:50.000) · AU a 2 m: 311 mm'"""
    if not carta.get("perfil"):
        return carta.get("motivo", "sin datos de la carta")
    partes = ", ".join(
        f"{(u['nombre'] or u['simbolo'] or '?').replace('Consociación ', '').title()} {u['pct']:.0f} %"
        for u in sorted(carta["unidades"], key=lambda u: -u["pct"])
    )
    escalas = "/".join(sorted({u["escala"] for u in carta["unidades"]}))
    au = carta.get("au_2m") or carta.get("au_1_5m") or carta.get("au_1m")
    return f"{partes} ({escalas}) · AU a 2 m: {au:.0f} mm"
