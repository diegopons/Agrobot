"""
clima_siga.py
-------------
Precipitación medida y ETo calculada desde una estación del SIGA (Sistema de
Información y Gestión Agrometeorológica del INTA, siga.inta.gob.ar).

Por qué importa: para el lote de Manfredi la estación "Manfredi - EEA Manfredi"
está a 930 m. Eso es un pluviómetro prácticamente en el lote, contra grillas de
9 km (Open-Meteo) o 25 km (AgERA5). Comprobado el 02/10/2026 sobre 21 días:

    estación (pluviómetro)   27,0 mm
    Open-Meteo              27,4 mm   (+1 % en total, pero EAM 0,96 mm/día)
    AgERA5                  19,1 mm   (−29 % en total, EAM 0,49 mm/día)

Open-Meteo acierta el total y erra los días; AgERA5 sigue la forma pero
subestima el volumen. Ninguna de las dos reemplaza al pluviómetro.

Acceso: no hay API pública documentada. El paquete {siga} de R
(github.com/AgRoMeteorologiaINTA/siga) usa dos endpoints que acá se replican:

  * lista de estaciones:
        CdnaUV0iiERRpFQE.php?param_type=estacion&param_value=
  * serie diaria completa de una estación (Excel, ~2 MB):
        document/series/<id_interno>.xls

El nombre del PHP está ofuscado y fue deducido por ingeniería inversa, así que
puede cambiar sin aviso. Por eso este módulo nunca debe ser la única fuente:
el combinador (src/clima.py) cae a Open-Meteo cuando esto falla.

ETo: la estación NO mide radiación (0 de 266 días en 2026 tienen heliofanía o
radiación global). Se usa Penman-Monteith FAO-56 con radiación solar estimada
a partir de la amplitud térmica (FAO-56 ec. 50, coeficiente krs). Las demás
variables sí son medidas: temperatura máxima y mínima, tensión de vapor y
viento. Validado contra la ETo de Open-Meteo sobre 21 días: 71,0 contra
74,8 mm (−5 %), EAM 0,43 mm/día, correlación 0,90.
"""
from __future__ import annotations

import io
import json
import logging
import math
import urllib.parse
import urllib.request
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BASE = "https://siga.inta.gob.ar/"
URL_ESTACIONES = BASE + "CdnaUV0iiERRpFQE.php?param_type=estacion&param_value="
URL_SERIE = BASE + "document/series/{id_interno}.xls"

# El viento de la planilla viene en km/h. Verificado empíricamente el
# 02/10/2026: calculando la ETo con km/h da 71,0 mm en 21 días contra 74,8 de
# Open-Meteo; interpretándolo como m/s daría 97,3 mm, un 30 % de más.
VIENTO_A_M_S = 1 / 3.6

COL = {
    "fecha": "Fecha",
    "t_max": "Temperatura_Abrigo_150cm_Maxima",
    "t_min": "Temperatura_Abrigo_150cm_Minima",
    "tension_vapor": "Tesion_Vapor_Media",   # sí, va con ese error de tipeo
    "humedad": "Humedad_Media",
    "viento": "Velocidad_Viento_200cm_Media",
    "lluvia": "Precipitacion_Pluviometrica",
}


class ErrorSIGA(RuntimeError):
    """Falla al consultar o interpretar datos del SIGA."""


def _pedir(url: str, timeout: int = 90) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception as exc:
        raise ErrorSIGA(f"No se pudo consultar el SIGA ({url}): {exc}") from exc


# ---------------------------------------------------------------------------
# Estaciones
# ---------------------------------------------------------------------------
def listar_estaciones() -> pd.DataFrame:
    """Estaciones del SIGA con su ubicación y rango de datos disponible."""
    try:
        datos = json.loads(_pedir(URL_ESTACIONES).decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ErrorSIGA(
            "El SIGA no devolvió JSON en el listado de estaciones. "
            "Puede haber cambiado el endpoint."
        ) from exc

    df = pd.DataFrame(datos)
    faltan = {"idInterno", "latitud", "longitud", "nombre"} - set(df.columns)
    if faltan:
        raise ErrorSIGA(f"El listado de estaciones no trae {sorted(faltan)}.")
    return df


def _distancia_km(lat1, lon1, lat2, lon2) -> float:
    """Distancia aproximada, suficiente para ordenar estaciones cercanas."""
    r = 6371.007181
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return r * math.hypot(x, y)


def _fecha_siga(valor) -> Optional[pd.Timestamp]:
    """'2026-10-23T00:00:00Z[UTC]' -> Timestamp (sin zona). None si no hay."""
    if valor is None or (isinstance(valor, float) and math.isnan(valor)):
        return None
    try:
        return pd.Timestamp(str(valor).split("[")[0]).tz_localize(None)
    except (ValueError, TypeError):
        return None


def estacion_mas_cercana(
    lat: float, lon: float, df_estaciones: Optional[pd.DataFrame] = None,
    max_km: float = 50.0, max_dias_sin_datos: int = 30,
) -> dict:
    """Estación con datos diarios recientes más cercana al punto, o error si no hay.

    El listado incluye estaciones cerradas hace años: la convencional de
    Manfredi (NH0438) dejó de registrar en 2014 y está más cerca de algunos
    lotes que la automática que sigue activa. Elegirla por distancia deja al
    lote sin dato medido, cayendo siempre a la fuente siguiente sin que se
    note. Se descartan las que no tienen un diario en los últimos días.
    """
    df = df_estaciones if df_estaciones is not None else listar_estaciones()
    df = df.dropna(subset=["latitud", "longitud", "idInterno"]).copy()
    df["km"] = [
        _distancia_km(lat, lon, la, lo)
        for la, lo in zip(df["latitud"], df["longitud"])
    ]
    df = df[df["km"] <= max_km].sort_values("km")
    if df.empty:
        raise ErrorSIGA(f"No hay estaciones del SIGA a menos de {max_km} km.")

    if "maximoDiario" in df.columns:
        limite = pd.Timestamp.today().normalize() - pd.Timedelta(days=max_dias_sin_datos)
        ultimo = df["maximoDiario"].map(_fecha_siga)
        activas = df[ultimo.notna() & (ultimo >= limite)]
        for _, inactiva in df[~df.index.isin(activas.index)].iterrows():
            if inactiva["km"] < (activas["km"].min() if not activas.empty else max_km):
                logger.info(
                    "SIGA: se descarta %s (%s) a %.1f km: último diario %s.",
                    inactiva.get("nombre"), inactiva["idInterno"], inactiva["km"],
                    _fecha_siga(inactiva["maximoDiario"]) or "sin dato",
                )
        if activas.empty:
            raise ErrorSIGA(
                f"No hay estaciones del SIGA con datos de los últimos "
                f"{max_dias_sin_datos} días a menos de {max_km} km."
            )
        df = activas
    e = df.iloc[0]
    return {
        "id_interno": str(e["idInterno"]),
        "nombre": str(e.get("nombre", "")),
        "lat": float(e["latitud"]),
        "lon": float(e["longitud"]),
        "altura_m": float(e.get("altura") or 0) or None,
        "km": float(e["km"]),
        "ultimo_diario": e.get("maximoDiario"),
    }


# ---------------------------------------------------------------------------
# Serie diaria
# ---------------------------------------------------------------------------
def _leer_excel(contenido: bytes) -> pd.DataFrame:
    """Lee la hoja 'Datos diarios' probando los motores disponibles.

    El archivo se sirve como .xls pero el formato real puede variar, así que se
    prueban los motores en orden en lugar de asumir uno.
    """
    errores = []
    for motor in (None, "openpyxl", "xlrd", "calamine"):
        try:
            kw = {"engine": motor} if motor else {}
            return pd.read_excel(io.BytesIO(contenido), sheet_name="Datos diarios", **kw)
        except Exception as exc:  # noqa: BLE001
            errores.append(f"{motor or 'auto'}: {exc}")
    raise ErrorSIGA(
        "No se pudo abrir la planilla de la estación. Probá instalar un motor "
        "de lectura (pip install xlrd openpyxl). Detalle:\n  " + "\n  ".join(errores)
    )


def descargar_serie(id_interno: str) -> pd.DataFrame:
    """Serie diaria completa de la estación, con las columnas normalizadas."""
    url = URL_SERIE.format(id_interno=urllib.parse.quote(id_interno))
    df = _leer_excel(_pedir(url))

    faltan = [c for c in COL.values() if c not in df.columns]
    if COL["fecha"] in faltan:
        raise ErrorSIGA(
            f"La planilla de {id_interno} no trae columna 'Fecha'. "
            f"Columnas recibidas: {list(df.columns)[:8]}…"
        )
    if faltan:
        logger.warning("La estación %s no trae %s.", id_interno, faltan)

    salida = pd.DataFrame({"fecha": pd.to_datetime(df[COL["fecha"]]).dt.normalize()})
    for clave, columna in COL.items():
        if clave == "fecha":
            continue
        salida[clave] = (
            pd.to_numeric(df[columna], errors="coerce") if columna in df.columns
            else np.nan
        )
    salida = salida.sort_values("fecha").drop_duplicates("fecha").reset_index(drop=True)
    logger.info(
        "SIGA %s: %d días (%s a %s).", id_interno, len(salida),
        salida["fecha"].min().date(), salida["fecha"].max().date(),
    )
    return salida


# ---------------------------------------------------------------------------
# ETo — Penman-Monteith FAO-56 con radiación estimada
# ---------------------------------------------------------------------------
def calcular_eto_fao56(
    df: pd.DataFrame, lat: float, altura_m: float = 292.0, krs: float = 0.16,
) -> pd.Series:
    """ETo diaria (mm) por FAO-56, con Rs estimada de la amplitud térmica.

    krs: 0,16 para zonas de interior y 0,19 para zonas costeras (FAO-56 ec. 50).
    Córdoba es interior.
    """
    t_max = df["t_max"].astype(float)
    t_min = df["t_min"].astype(float)
    t_med = (t_max + t_min) / 2
    u2 = df["viento"].astype(float) * VIENTO_A_M_S

    # Presión de vapor real: se prefiere la tensión de vapor medida (hPa -> kPa)
    ea = df["tension_vapor"].astype(float) / 10.0
    es_tmax = 0.6108 * np.exp(17.27 * t_max / (t_max + 237.3))
    es_tmin = 0.6108 * np.exp(17.27 * t_min / (t_min + 237.3))
    es = (es_tmax + es_tmin) / 2
    # Si falta la tensión de vapor, se reconstruye desde la humedad relativa
    ea = ea.where(ea.notna(), es * df["humedad"].astype(float) / 100.0)

    # Radiación extraterrestre
    phi = math.radians(lat)
    j = df["fecha"].dt.dayofyear.astype(float)
    dr = 1 + 0.033 * np.cos(2 * math.pi * j / 365)
    dec = 0.409 * np.sin(2 * math.pi * j / 365 - 1.39)
    ws = np.arccos(np.clip(-np.tan(phi) * np.tan(dec), -1, 1))
    ra = (24 * 60 / math.pi) * 0.0820 * dr * (
        ws * math.sin(phi) * np.sin(dec) + math.cos(phi) * np.cos(dec) * np.sin(ws)
    )

    # Radiación solar estimada y balance de radiación
    rs = np.minimum(krs * np.sqrt(np.clip(t_max - t_min, 0, None)) * ra, 0.75 * ra)
    rso = (0.75 + 2e-5 * altura_m) * ra
    rns = 0.77 * rs
    sigma = 4.903e-9
    rnl = (
        sigma
        * (((t_max + 273.16) ** 4 + (t_min + 273.16) ** 4) / 2)
        * (0.34 - 0.14 * np.sqrt(np.clip(ea, 0, None)))
        * (1.35 * np.minimum(rs / rso, 1) - 0.35)
    )
    rn = rns - rnl

    # Términos psicrométricos
    delta = 4098 * (0.6108 * np.exp(17.27 * t_med / (t_med + 237.3))) / (t_med + 237.3) ** 2
    presion = 101.3 * ((293 - 0.0065 * altura_m) / 293) ** 5.26
    gamma = 0.000665 * presion

    eto = (
        0.408 * delta * rn + gamma * (900 / (t_med + 273)) * u2 * np.clip(es - ea, 0, None)
    ) / (delta + gamma * (1 + 0.34 * u2))

    return pd.Series(np.clip(eto, 0, None), index=df.index).where(
        t_max.notna() & t_min.notna() & u2.notna() & ea.notna()
    )


# ---------------------------------------------------------------------------
# Interfaz para el combinador
# ---------------------------------------------------------------------------
def descargar_siga(
    fecha_inicio: str,
    fecha_fin: str,
    bbox_wsen: tuple[float, float, float, float],
    estacion_id: Optional[str] = None,
    max_km: float = 50.0,
    krs: float = 0.16,
) -> tuple[pd.DataFrame, dict]:
    """Serie (fecha, precipitacion_mm, eto_mm) medida en la estación más cercana.

    Devuelve además los datos de la estación usada, para dejarlos en el log.
    """
    west, south, east, north = bbox_wsen
    lat, lon = (south + north) / 2, (west + east) / 2

    estaciones = listar_estaciones()
    if estacion_id:
        fila = estaciones[estaciones["idInterno"].astype(str) == str(estacion_id)]
        if fila.empty:
            raise ErrorSIGA(f"No existe la estación '{estacion_id}' en el SIGA.")
        e = fila.iloc[0]
        est = {
            "id_interno": str(e["idInterno"]), "nombre": str(e.get("nombre", "")),
            "lat": float(e["latitud"]), "lon": float(e["longitud"]),
            "altura_m": float(e.get("altura") or 0) or None,
            "km": _distancia_km(lat, lon, float(e["latitud"]), float(e["longitud"])),
        }
    else:
        est = estacion_mas_cercana(lat, lon, estaciones, max_km=max_km)

    logger.info(
        "SIGA: estación '%s' (%s) a %.1f km del lote.",
        est["nombre"], est["id_interno"], est["km"],
    )

    serie = descargar_serie(est["id_interno"])
    serie["eto_calculada"] = calcular_eto_fao56(
        serie, lat=est["lat"], altura_m=est["altura_m"] or 292.0, krs=krs
    )

    rango = serie[
        (serie["fecha"] >= pd.to_datetime(fecha_inicio))
        & (serie["fecha"] <= pd.to_datetime(fecha_fin))
    ]
    salida = pd.DataFrame(
        {
            "fecha": rango["fecha"],
            "precipitacion_mm": rango["lluvia"],
            "eto_mm": rango["eto_calculada"],
        }
    ).dropna(subset=["precipitacion_mm", "eto_mm"], how="all").reset_index(drop=True)

    if salida.empty:
        raise ErrorSIGA(
            f"La estación {est['id_interno']} no tiene datos entre "
            f"{fecha_inicio} y {fecha_fin}."
        )
    logger.info(
        "SIGA: %d días con dato. Lluvia %.1f mm, ETo %.1f mm.",
        len(salida), salida["precipitacion_mm"].sum(), salida["eto_mm"].sum(),
    )
    return salida, est
