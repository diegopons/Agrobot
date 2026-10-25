"""
publicar_estado.py
------------------
Publica `docs/estado_actual.json`: un resumen compacto del estado del lote,
pensado para que lo consuma un agente conversacional (n8n, un bot de Telegram,
lo que sea) sin tener que leer y interpretar el CSV completo.

Por qué un JSON aparte y no el CSV: el CSV tiene casi 200 filas y crece cada
día. Pasárselo entero a un modelo en cada pregunta es caro, lento y lo obliga a
recalcular cosas que el sistema ya sabe (la condición, la lámina sugerida, los
acumulados). Acá eso viene resuelto, con nombres explícitos y unidades en las
claves, de modo que el agente solo tenga que redactar.

El archivo se publica en GitHub Pages junto al tablero, así que queda accesible
sin credenciales en:
    https://<usuario>.github.io/<repo>/estado_actual.json
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


def _condicion(pct: float, umbral: float) -> str:
    if pct >= umbral:
        return "cómodo"
    if pct >= umbral * 0.6:
        return "déficit"
    return "crítico"


def construir_estado(
    df: pd.DataFrame,
    cfg: dict,
    aoi_info: dict,
    estacion_siga: Optional[dict] = None,
    detalle_clima: Optional[dict] = None,
    linea_pronostico: Optional[str] = None,
    df_pronostico: Optional[pd.DataFrame] = None,
    ajuste_riego: Optional[dict] = None,
    diag_ndvi: Optional[dict] = None,
    dias_detalle: int = 14,
) -> dict:
    """Arma el diccionario del estado a partir del DataFrame de resultados."""
    if df.empty:
        raise ValueError("No hay resultados para publicar.")

    df = df.sort_values("fecha")
    u = df.iloc[-1]
    fecha_u = pd.to_datetime(u["fecha"])
    hoy = pd.Timestamp.today().normalize()
    umbral = float(cfg["balance"]["umbral_riego_pct"])
    tg = cfg.get("telegram", {})
    eficiencia = float(tg.get("eficiencia_aplicacion", 0.85))

    au, au_max = float(u["au_real_mm"]), float(u["au_max_mm"])
    pct = float(u["pct_au"])
    falta = max(0.0, au_max - au)

    siembra = pd.to_datetime(cfg["campana"]["fecha_siembra"])

    estado = {
        "generado_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lote": {
            "nombre": tg.get("nombre_lote", "Lote"),
            "area_ha": round(float(aoi_info.get("area_ha", 0)), 2),
        },
        "campana": {
            "fecha_siembra": str(siembra.date()),
            "dias_desde_siembra": int((fecha_u - siembra).days),
        },
        "estado_actual": {
            "fecha_del_dato": str(fecha_u.date()),
            "dias_de_atraso": int((hoy - fecha_u).days),
            "agua_util_mm": round(au, 1),
            "agua_util_maxima_mm": round(au_max, 1),
            "porcentaje_agua_util": round(pct, 1),
            "condicion": _condicion(pct, umbral),
            "umbral_de_riego_pct": umbral,
            "necesita_riego": bool(pct < umbral),
            "falta_para_capacidad_de_campo_mm": round(falta, 1),
            "riego_sugerido_neto_mm": round(falta, 1) if pct < umbral else 0.0,
            "riego_sugerido_bruto_mm": (
                round(falta / eficiencia, 1) if pct < umbral else 0.0
            ),
            "eficiencia_de_aplicacion": eficiencia,
            # Dos láminas, a propósito. La "sugerida" es la necesidad del
            # cultivo hoy; la "ajustada" es la decisión de manejo con el
            # pronóstico a la vista. El agente tiene que dar las dos.
            "riego_ajustado_por_pronostico": (
                {
                    "neto_mm": ajuste_riego["lamina_neta_mm"],
                    "bruto_mm": ajuste_riego["lamina_bruta_mm"],
                    "lluvia_descontada_mm": ajuste_riego["descuento_mm"],
                    "lluvia_pronosticada_mm": ajuste_riego["lluvia_pronosticada_mm"],
                    "ventana_dias": ajuste_riego["ventana_dias"],
                    "hasta": ajuste_riego["hasta"],
                }
                if ajuste_riego and pct < umbral else None
            ),
            "ndvi": round(float(u["ndvi"]), 3),
            "profundidad_raiz_cm": round(float(u["prof_raiz_cm"]), 1),
            "consumo_del_dia_mm": round(float(u["etc_ajustada_mm"]), 2),
        },
        "acumulados_de_campana": {
            "lluvia_mm": round(float(df["precipitacion_mm"].sum()), 1),
            "lluvia_efectiva_mm": (
                round(float(df["precipitacion_efectiva_mm"].sum()), 1)
                if "precipitacion_efectiva_mm" in df else None
            ),
            "riego_aplicado_mm": round(float(df["riego_mm"].sum()), 1),
            # Días anteriores a esta columna no la tienen: ahí el riego era 0
            "riego_efectivo_mm": (
                round(float(pd.to_numeric(df["riego_efectivo_mm"]).fillna(df["riego_mm"]).sum()), 1)
                if "riego_efectivo_mm" in df else None
            ),
            "eto_mm": round(float(df["eto_mm"].sum()), 1),
            "consumo_del_cultivo_mm": round(float(df["etc_ajustada_mm"].sum()), 1),
            "percolacion_mm": round(float(df["percolacion_mm"].sum()), 1),
            "dias_calculados": int(len(df)),
        },
        "ultimos_dias": [
            {
                "fecha": str(pd.to_datetime(f["fecha"]).date()),
                "lluvia_mm": round(float(f["precipitacion_mm"]), 1),
                "eto_mm": round(float(f["eto_mm"]), 2),
                "consumo_mm": round(float(f["etc_ajustada_mm"]), 2),
                "agua_util_mm": round(float(f["au_real_mm"]), 1),
                "porcentaje_agua_util": round(float(f["pct_au"]), 1),
                "ndvi": round(float(f["ndvi"]), 3),
            }
            for _, f in df.tail(dias_detalle).iterrows()
        ],
    }

    # --- de dónde salieron los datos: el agente debe poder decirlo ---
    origen = {}
    for col, etiqueta in (("fuente_pp", "lluvia"), ("fuente_eto", "eto")):
        if col in df.columns:
            origen[etiqueta] = {
                str(k): int(v) for k, v in df[col].value_counts().items()
            }
    if estacion_siga:
        origen["estacion"] = {
            "nombre": estacion_siga.get("nombre"),
            "id": estacion_siga.get("id_interno"),
            "distancia_km": round(float(estacion_siga.get("km", 0)), 2),
        }
    if detalle_clima and detalle_clima.get("fallidas"):
        origen["fuentes_que_fallaron"] = detalle_clima["fallidas"]
    if origen:
        estado["origen_de_los_datos"] = origen

    # --- pronóstico ---
    if linea_pronostico or (df_pronostico is not None and not df_pronostico.empty):
        pron = {"resumen": linea_pronostico}
        if df_pronostico is not None and not df_pronostico.empty:
            pron["dias"] = int(len(df_pronostico))
            pron["lluvia_esperada_mm"] = round(
                float(df_pronostico["precipitacion_mm"].sum()), 1
            )
            # El total no alcanza para decidir: 50 mm mañana y 50 mm dentro de
            # nueve días llevan a riegos distintos. Va el detalle por día.
            pron["por_dia"] = [
                {
                    "fecha": str(pd.to_datetime(f["fecha"]).date()),
                    "lluvia_mm": round(float(f["precipitacion_mm"]), 1),
                    "eto_mm": round(float(f["eto_mm"]), 2),
                }
                for _, f in df_pronostico.iterrows()
            ]
            lluvias = df_pronostico[df_pronostico["precipitacion_mm"] >= 5]
            if not lluvias.empty:
                primera = lluvias.iloc[0]
                dias = (pd.to_datetime(primera["fecha"])
                        - pd.Timestamp.today().normalize()).days
                pron["proxima_lluvia"] = {
                    "fecha": str(pd.to_datetime(primera["fecha"]).date()),
                    "en_dias": int(dias),
                    "mm": round(float(primera["precipitacion_mm"]), 1),
                }
        estado["pronostico"] = pron

    # --- advertencias: lo que el agente NO debe afirmar de más ---
    advertencias = []
    atraso = estado["estado_actual"]["dias_de_atraso"]
    if atraso > 3:
        advertencias.append(
            f"El último dato es de hace {atraso} días: el estado puede haber "
            "cambiado desde entonces."
        )
    if estado["estado_actual"]["necesita_riego"]:
        aj = estado["estado_actual"]["riego_ajustado_por_pronostico"]
        prox = (estado.get("pronostico") or {}).get("proxima_lluvia")
        if aj:
            advertencias.append(
                f"Hay dos láminas y hay que dar las DOS: la de reposición "
                f"total ({estado['estado_actual']['riego_sugerido_bruto_mm']:.1f} mm "
                f"brutos), que es lo que falta hoy sin mirar el pronóstico, y la "
                f"ajustada ({aj['bruto_mm']:.1f} mm brutos), que descuenta "
                f"{aj['lluvia_descontada_mm']:.1f} mm de lluvia efectiva esperada "
                f"hasta el {aj['hasta']}. La ajustada apuesta a que esa lluvia "
                f"llega; si no llega, falta reponer la diferencia."
            )
        elif prox:
            advertencias.append(
                f"Se esperan {prox['mm']:.0f} mm el {prox['fecha']}, en "
                f"{prox['en_dias']} día(s): conviene descontarlos de la lámina o "
                "esperar. La lámina sugerida NO los descuenta."
            )
        else:
            advertencias.append(
                "La lámina sugerida repone hasta capacidad de campo y no "
                "descuenta la lluvia pronosticada."
            )
    # --- calidad del NDVI de la corrida -----------------------------------
    # Sin esto el enmascarado es invisible: una corrida que descarta todas las
    # fechas por contaminación publica exactamente lo mismo que una corrida
    # normal, con la curva sostenida sobre el último valor. Hay que poder verlo.
    if diag_ndvi:
        d = diag_ndvi.get("descartes") or {}
        fuera = int(d.get("fuera_de_rango", 0))
        con_dato = int(diag_ndvi.get("fechas_con_dato", 0))
        estado["calidad_ndvi"] = {
            "fechas_con_dato": con_dato,
            "descartadas_por_cobertura": int(d.get("cobertura", 0)),
            "descartadas_fuera_de_rango": fuera,
            "ndvi_previo_descartado": diag_ndvi.get("ndvi_previo_descartado"),
        }
        if fuera:
            advertencias.insert(0,
                f"{fuera} fecha(s) de Sentinel-2 se descartaron por NDVI medio "
                "fuera del rango plausible: el polígono puede incluir superficie "
                "que no es el lote (agua, camino, monte) o estar desplazado. "
                "Conviene revisarlo antes de usar la estimación."
            )
        if diag_ndvi.get("ndvi_previo_descartado") is not None:
            advertencias.insert(0,
                "Se descartó el NDVI que venía guardado de la corrida anterior "
                f"({diag_ndvi['ndvi_previo_descartado']}), porque estaba fuera "
                "del rango plausible."
            )
        if con_dato == 0:
            advertencias.insert(0,
                "INCONSISTENTE: en este tramo no quedó ninguna fecha con NDVI "
                "válido. La curva se sostiene sobre supuestos, no sobre "
                "observaciones: no responder sobre el estado del cultivo."
            )

    if estado["campana"]["dias_desde_siembra"] < 0:
        advertencias.insert(0,
            "INCONSISTENTE: el último dato es anterior a la fecha de siembra del "
            "config. El estado guardado no corresponde a esta campaña; no "
            "responder sobre la situación actual del lote hasta recalcular."
        )
    if atraso > 30:
        advertencias.insert(0,
            f"INCONSISTENTE: el último dato tiene {atraso} días. No describe la "
            "situación actual del lote."
        )
    advertencias.append(
        "Es una estimación de apoyo: no reemplaza la recorrida del lote ni una "
        "calicata."
    )
    estado["advertencias"] = advertencias

    return estado


def publicar(estado: dict, destino: str = "docs/estado_actual.json") -> None:
    ruta = Path(destino)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(
        json.dumps(estado, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "Estado publicado en %s (%s, %.0f %% de agua útil).",
        ruta,
        estado["estado_actual"]["fecha_del_dato"],
        estado["estado_actual"]["porcentaje_agua_util"],
    )
