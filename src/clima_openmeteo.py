"""
clima_openmeteo.py
------------------
Precipitación y evapotranspiración de referencia diarias desde Open-Meteo,
como alternativa a AgERA5 (src/clima_agera5.py).

Por qué: AgERA5 publica el dato definitivo con unos 10 días de atraso, así que
el balance siempre mira hacia atrás. Open-Meteo entrega el día de ayer y
además pronóstico, que es lo que permite anticipar el aviso de riego.

Dos endpoints:

  * Archive API (archive-api.open-meteo.com)
        Con el modelo por defecto ("best_match") devuelve dato hasta HOY: empalma
        el reanálisis ERA5 con el modelo de pronóstico para los días recientes.
        Con `models=era5` devuelve ERA5 puro, homogéneo pero con ~7 días de
        rezago y sobre una grilla más gruesa.
  * Forecast API (api.open-meteo.com), con `past_days`
        Los últimos ~92 días más el pronóstico. Sirve de respaldo para la cola
        reciente cuando el archivo viene con rezago.

Verificado el 02/10/2026 sobre el lote (-31,83 / -63,76):
  - por defecto, el archivo llegó hasta el mismo día, sin huecos;
  - con models=era5, el último dato era del 25/09 (7 días de rezago) y los
    valores diferían: 20/09 dio 3,7 mm de lluvia contra 0,8 mm del modelo por
    defecto, sobre una grilla de ~25 km en vez de ~9 km.

Consecuencia a tener en cuenta: con el modelo por defecto, los días recientes
salen de un modelo de pronóstico y pueden cambiar retroactivamente cuando el
modelo se actualiza. El balance es incremental, así que un día ya escrito en el
CSV queda congelado con el valor que tenía ese día. Si eso importa, conviene
reprocesar los últimos días de tanto en tanto (correr con "reiniciar").

Sin credenciales: la API es abierta para uso no comercial.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

URL_ARCHIVO = "https://archive-api.open-meteo.com/v1/archive"
URL_PRONOSTICO = "https://api.open-meteo.com/v1/forecast"

VAR_PP = "precipitation_sum"
VAR_ETO = "et0_fao_evapotranspiration"

MAX_PAST_DAYS = 92      # tope que acepta la Forecast API
MAX_FORECAST_DAYS = 16  # ídem


class ErrorOpenMeteo(RuntimeError):
    """Falla al consultar o interpretar la respuesta de Open-Meteo."""


ESPERAS_REINTENTO = (5, 15)   # segundos antes del 2.º y 3.er intento


def _pedir(url: str, params: dict, timeout: int = 60) -> dict:
    completa = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    logger.debug("Open-Meteo: %s", completa)
    # Un corte de red suelto (p. ej. "handshake timed out" el 23/11/2026) dejaba
    # al lote sin clima ese día: se reintenta. Un 4xx no, es un pedido mal armado.
    for intento, espera in enumerate((0, *ESPERAS_REINTENTO)):
        if espera:
            logger.warning("Open-Meteo: reintento %d en %d s.", intento, espera)
            time.sleep(espera)
        try:
            with urllib.request.urlopen(completa, timeout=timeout) as r:
                datos = json.load(r)
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or intento == len(ESPERAS_REINTENTO):
                raise ErrorOpenMeteo(f"No se pudo consultar Open-Meteo: {exc}") from exc
        except Exception as exc:  # red, timeout, JSON inválido
            if intento == len(ESPERAS_REINTENTO):
                raise ErrorOpenMeteo(f"No se pudo consultar Open-Meteo: {exc}") from exc

    # La API responde 200 con {"error": true, "reason": ...} en varios casos
    if isinstance(datos, dict) and datos.get("error"):
        raise ErrorOpenMeteo(f"Open-Meteo rechazó la consulta: {datos.get('reason')}")
    return datos


def _a_dataframe(datos: dict, etiqueta_fuente: str) -> pd.DataFrame:
    """Convierte la respuesta en un DataFrame, validando que estén las variables."""
    diario = datos.get("daily")
    if not diario or "time" not in diario:
        raise ErrorOpenMeteo(
            "La respuesta de Open-Meteo no trae bloque 'daily'. "
            f"Claves recibidas: {sorted(datos)}"
        )
    faltantes = [v for v in (VAR_PP, VAR_ETO) if v not in diario]
    if faltantes:
        raise ErrorOpenMeteo(
            f"Open-Meteo no devolvió {faltantes}. Variables recibidas: "
            f"{sorted(k for k in diario if k != 'time')}. "
            "Puede que hayan cambiado de nombre en la API."
        )

    unidades = datos.get("daily_units", {})
    for var in (VAR_PP, VAR_ETO):
        u = unidades.get(var)
        if u and u != "mm":
            raise ErrorOpenMeteo(
                f"Open-Meteo devolvió {var} en '{u}' y el balance trabaja en mm."
            )

    df = pd.DataFrame(
        {
            "fecha": pd.to_datetime(diario["time"]),
            "precipitacion_mm": pd.to_numeric(diario[VAR_PP], errors="coerce"),
            "eto_mm": pd.to_numeric(diario[VAR_ETO], errors="coerce"),
        }
    )
    df["fuente"] = etiqueta_fuente
    return df


def _centro(bbox_wsen: tuple[float, float, float, float]) -> tuple[float, float]:
    west, south, east, north = bbox_wsen
    return (south + north) / 2.0, (west + east) / 2.0


def descargar_openmeteo(
    fecha_inicio: str,
    fecha_fin: str,
    bbox_wsen: tuple[float, float, float, float],
    modelo_archivo: str | None = None,
    zona_horaria: str = "America/Argentina/Cordoba",
) -> pd.DataFrame:
    """Serie diaria (fecha, precipitacion_mm, eto_mm, fuente) para el lote.

    Open-Meteo trabaja por punto, no por área: se consulta el centro del
    polígono. Para un lote de pocas hectáreas da igual, porque la grilla del
    reanálisis es mucho más gruesa que el lote.
    """
    lat, lon = _centro(bbox_wsen)
    inicio = pd.to_datetime(fecha_inicio).normalize()
    fin = pd.to_datetime(fecha_fin).normalize()
    if fin < inicio:
        raise ValueError("fecha_fin anterior a fecha_inicio")

    hoy = pd.Timestamp.today().normalize()
    partes: list[pd.DataFrame] = []

    # --- 1. Histórico: Archive API -----------------------------------------
    # Se pide hasta el final del rango; la API devuelve lo que tenga y recorta
    # sola los días que todavía no publicó.
    base = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "daily": [VAR_PP, VAR_ETO],
        "timezone": zona_horaria,
    }
    params = dict(base)
    params["start_date"] = inicio.strftime("%Y-%m-%d")
    params["end_date"] = fin.strftime("%Y-%m-%d")
    if modelo_archivo:
        params["models"] = modelo_archivo

    df_arch = _a_dataframe(_pedir(URL_ARCHIVO, params), "archivo")
    df_arch = df_arch.dropna(subset=["precipitacion_mm", "eto_mm"])
    if not df_arch.empty:
        partes.append(df_arch)
        ultimo_archivo = df_arch["fecha"].max()
    else:
        ultimo_archivo = inicio - pd.Timedelta(days=1)

    # --- 2. Cola reciente: Forecast API con past_days ------------------------
    faltan_desde = ultimo_archivo + pd.Timedelta(days=1)
    if faltan_desde <= fin:
        dias_atras = (hoy - faltan_desde).days + 1
        if dias_atras > MAX_PAST_DAYS:
            logger.warning(
                "Faltan %d días que la Forecast API no alcanza a cubrir (tope %d). "
                "Quedarán sin dato climático.", dias_atras, MAX_PAST_DAYS
            )
            dias_atras = MAX_PAST_DAYS
        params = dict(base)
        params["past_days"] = max(0, min(dias_atras, MAX_PAST_DAYS))
        params["forecast_days"] = 1  # solo interesa la cola pasada
        df_fc = _a_dataframe(_pedir(URL_PRONOSTICO, params), "reciente")
        df_fc = df_fc[(df_fc["fecha"] >= faltan_desde) & (df_fc["fecha"] <= fin)]
        df_fc = df_fc.dropna(subset=["precipitacion_mm", "eto_mm"])
        if not df_fc.empty:
            partes.append(df_fc)

    if not partes:
        raise ErrorOpenMeteo(
            f"Open-Meteo no devolvió ningún día con dato entre {fecha_inicio} "
            f"y {fecha_fin}."
        )

    df = pd.concat(partes, ignore_index=True)
    df = df.drop_duplicates(subset="fecha", keep="first").sort_values("fecha")
    df = df.reset_index(drop=True)

    por_fuente = df["fuente"].value_counts().to_dict()
    logger.info(
        "Open-Meteo: %d días (%s). Último dato: %s.",
        len(df),
        ", ".join(f"{k}={v}" for k, v in sorted(por_fuente.items())),
        df["fecha"].max().date(),
    )
    return df


def descargar_pronostico(
    bbox_wsen: tuple[float, float, float, float],
    dias: int = 14,
    zona_horaria: str = "America/Argentina/Cordoba",
) -> pd.DataFrame:
    """Pronóstico diario de lluvia y ETo a partir de mañana.

    Devuelve (fecha, precipitacion_mm, eto_mm, fuente='pronostico').
    """
    lat, lon = _centro(bbox_wsen)
    dias = max(1, min(int(dias), MAX_FORECAST_DAYS))
    params = {
        "latitude": round(lat, 4),
        "longitude": round(lon, 4),
        "daily": [VAR_PP, VAR_ETO],
        "timezone": zona_horaria,
        "forecast_days": dias,
    }
    df = _a_dataframe(_pedir(URL_PRONOSTICO, params), "pronostico")
    manana = pd.Timestamp.today().normalize() + pd.Timedelta(days=1)
    df = df[df["fecha"] >= manana].dropna(subset=["precipitacion_mm", "eto_mm"])
    df = df.sort_values("fecha").reset_index(drop=True)
    logger.info(
        "Pronóstico: %d días, hasta %s. Lluvia esperada: %.1f mm.",
        len(df),
        df["fecha"].max().date() if not df.empty else "—",
        df["precipitacion_mm"].sum(),
    )
    return df
