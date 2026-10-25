"""
balance.py
----------
Mismas fórmulas agronómicas del notebook balance_hidrico_gee (v6) / planilla
Excel, pero reorganizadas para correr en modo INCREMENTAL: en cada corrida
periódica solo se procesan los días nuevos, partiendo del estado (AU real y
profundidad de raíz) dejado por la corrida anterior. Esto evita reprocesar
toda la temporada cada vez que se ejecuta el script.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

from .suelo import obtener_au_max_para_profundidad, agua_de_capas_alcanzadas


def precipitacion_efectiva(pp_mm: float, umbral_mm: float = 15.0) -> float:
    """Fracción de la lluvia que queda en el perfil.

    Misma fórmula que la planilla BH p-Pons: por debajo del umbral entra todo,
    y por encima se descuenta escurrimiento con 2,43 · Pp^0,667 (USDA-SCS).
    Con `umbral_mm` muy alto equivale a usar la lluvia caída completa.
    """
    if pp_mm <= 0:
        return 0.0
    if pp_mm < umbral_mm:
        return float(pp_mm)
    return float(2.43 * (pp_mm ** 0.667))


def calcular_kc_desde_ndvi(ndvi: float, a: float = 1.77, b: float = -0.198) -> float:
    kc = a * ndvi + b
    return max(0.1, min(kc, 1.2))


def calcular_ks(pct_au: float, pct_ur: float) -> float:
    if pct_au >= pct_ur:
        return 1.0
    elif pct_au <= 0:
        return 0.0
    else:
        return pct_au / pct_ur


@dataclass
class EstadoBalance:
    """Estado persistente entre corridas."""
    ultima_fecha_procesada: Optional[str]  # 'YYYY-MM-DD' o None si aún no corrió
    au_real_mm: float
    prof_raiz_cm: float
    ultimo_ndvi_valido: Optional[float] = None  # para rellenar huecos al inicio de una corrida

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EstadoBalance":
        # El estado guardado puede traer datos que no son del balance (huella,
        # fechas con imagen, serie): se toman solo los campos propios.
        campos = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in campos})

    @classmethod
    def inicial(cls, au_real_inicial: float, prof_raiz_inicial: float) -> "EstadoBalance":
        return cls(
            ultima_fecha_procesada=None,
            au_real_mm=au_real_inicial,
            prof_raiz_cm=prof_raiz_inicial,
        )


def procesar_dias_nuevos(
    datos_nuevos: pd.DataFrame,
    estado: EstadoBalance,
    df_suelo: pd.DataFrame,
    umbral_riego_pct: float = 50,
    incremento_raiz_diario_cm: float = 2.0,
    prof_raiz_max_cm: float = 200.0,
    kc_a: float = 1.77,
    kc_b: float = -0.198,
    recarga_por_capa: bool = True,
    umbral_lluvia_efectiva_mm: float = 15.0,
    tabla_prof_raiz: Optional[list] = None,
    fecha_siembra: Optional[str] = None,
) -> tuple[pd.DataFrame, EstadoBalance]:
    """Corre el balance hídrico día a día sobre `datos_nuevos` (columnas:
    fecha, precipitacion_mm, eto_mm, ndvi, riego_mm) partiendo del `estado`
    recibido, y devuelve (df_resultados_nuevos, estado_actualizado).

    Es exactamente la misma física que `modelo_balance_hidrico()` del
    notebook original, solo que arranca desde un estado guardado en vez de
    desde el día de siembra cada vez.
    """
    resultados = []
    au_real_ayer = estado.au_real_mm
    prof_raiz_actual = estado.prof_raiz_cm
    ultimo_ndvi_valido = estado.ultimo_ndvi_valido

    datos_nuevos = datos_nuevos.sort_values("fecha").reset_index(drop=True)

    usar_tabla = bool(tabla_prof_raiz) and fecha_siembra is not None
    if usar_tabla:
        siembra = pd.to_datetime(fecha_siembra)
        dds_tabla = [float(p[0]) for p in tabla_prof_raiz]
        cm_tabla = [float(p[1]) for p in tabla_prof_raiz]

    for _, dia in datos_nuevos.iterrows():
        prof_raiz_previa = prof_raiz_actual
        if usar_tabla:
            dds = (pd.to_datetime(dia["fecha"]) - siembra).days
            prof_raiz_actual = float(np.interp(dds, dds_tabla, cm_tabla))
        else:
            prof_raiz_actual = prof_raiz_actual + incremento_raiz_diario_cm
        prof_raiz_actual = max(prof_raiz_previa, min(prof_raiz_actual, prof_raiz_max_cm))
        au_max_dia = obtener_au_max_para_profundidad(prof_raiz_actual, df_suelo)

        # Al alcanzar una capa nueva, el cultivo accede al agua que ya estaba
        # almacenada ahí. Sin esto el lote aparece cada vez más seco a medida
        # que la raíz profundiza, que era el desvío principal contra la planilla.
        recarga_mm = (
            agua_de_capas_alcanzadas(prof_raiz_previa, prof_raiz_actual, df_suelo)
            if recarga_por_capa
            else 0.0
        )
        au_real_ayer = min(au_real_ayer + recarga_mm, au_max_dia)
        pct_au_inicial = (au_real_ayer / au_max_dia) * 100 if au_max_dia > 0 else 0

        ndvi_dia = dia["ndvi"]
        if pd.isna(ndvi_dia):
            # Sin dato satelital ese día (nubes, falta de pasada): se arrastra
            # el último NDVI válido conocido, igual que el ffill del notebook.
            ndvi_dia = ultimo_ndvi_valido if ultimo_ndvi_valido is not None else 0.3
        else:
            ultimo_ndvi_valido = ndvi_dia

        kc = calcular_kc_desde_ndvi(ndvi_dia, kc_a, kc_b)
        eto = dia["eto_mm"] if not pd.isna(dia["eto_mm"]) else 0.0
        etc = eto * kc
        ks = calcular_ks(pct_au_inicial, umbral_riego_pct)
        etc_ajustada = etc * ks

        precipitacion = dia["precipitacion_mm"] if not pd.isna(dia["precipitacion_mm"]) else 0.0
        precipitacion_ef = precipitacion_efectiva(precipitacion, umbral_lluvia_efectiva_mm)
        riego = dia.get("riego_mm", 0.0) or 0.0
        ingresos_agua = precipitacion_ef + riego
        au_real_hoy = au_real_ayer - etc_ajustada + ingresos_agua

        percolacion_profunda = max(0.0, au_real_hoy - au_max_dia)
        au_real_hoy = min(au_real_hoy, au_max_dia)
        au_real_hoy = max(au_real_hoy, 0.0)
        pct_au_final = (au_real_hoy / au_max_dia) * 100 if au_max_dia > 0 else 0

        resultados.append(
            {
                "fecha": dia["fecha"],
                "precipitacion_mm": round(precipitacion, 2),
                "precipitacion_efectiva_mm": round(precipitacion_ef, 2),
                "recarga_capa_mm": round(recarga_mm, 2),
                "eto_mm": round(eto, 2),
                "ndvi": round(ndvi_dia, 3),
                "prof_raiz_cm": round(prof_raiz_actual, 1),
                "au_max_mm": round(au_max_dia, 2),
                "kc": round(kc, 3),
                "ks": round(ks, 3),
                "etc_ajustada_mm": round(etc_ajustada, 2),
                "percolacion_mm": round(percolacion_profunda, 2),
                "riego_mm": round(riego, 2),
                "au_real_mm": round(au_real_hoy, 2),
                "pct_au": round(pct_au_final, 1),
            }
        )

        au_real_ayer = au_real_hoy

    df_resultados = pd.DataFrame(resultados)

    nuevo_estado = EstadoBalance(
        ultima_fecha_procesada=str(datos_nuevos["fecha"].max().date())
        if len(datos_nuevos) > 0
        else estado.ultima_fecha_procesada,
        au_real_mm=au_real_ayer,
        prof_raiz_cm=prof_raiz_actual,
        ultimo_ndvi_valido=ultimo_ndvi_valido,
    )

    return df_resultados, nuevo_estado
