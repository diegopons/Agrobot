"""
estado.py
---------
Persistencia entre corridas del script:
  - estado.json  -> último día procesado, AU real y profundidad de raíz
                     al cierre de esa corrida (para continuar mañana/la
                     próxima semana sin recalcular toda la temporada).
  - <salida>.csv -> serie histórica completa de resultados diarios
                     (se va agregando fila por fila en cada corrida).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

from .balance import EstadoBalance

logger = logging.getLogger(__name__)


def huella_corrida(
    geojson_aoi: dict, fecha_siembra: str, fuentes_clima=None, suelo=None
) -> str:
    """Identifica la combinación lote + fecha de siembra + fuente de clima.

    Si cambia el polígono o la fecha de siembra, el balance acumulado de
    antes ya no corresponde: hay que empezar de cero. Guardamos esta huella
    junto al estado para poder detectarlo solos, en lugar de depender de
    que alguien se acuerde de borrar estado.json a mano.

    La fuente de clima entra por el mismo motivo. Medido sobre este lote,
    Open-Meteo entregó un 22 % más de lluvia que el pluviómetro del INTA en
    30 días, y AgERA5 un 29 % menos en 21 días: seguir una serie empezada con
    una fuente usando otra no da una curva continua, da un escalón disfrazado
    de evolución. Cambiar el orden de `clima.fuentes` archiva la serie anterior
    y arranca de nuevo, igual que cambiar el lote.

    El perfil de suelo entra por la misma razón, y es el caso menos evidente de
    los tres: el agua que cada capa tenía al inicio se incorpora al balance el
    día en que la raíz llega a esa capa. Si se carga la calicata a mitad de
    campaña, las capas ya atravesadas aportaron el valor viejo y las que faltan
    aportarían el nuevo, con lo que la serie mezcla dos perfiles distintos. Al
    incluirlo acá, cargar o corregir la calicata archiva la serie anterior y
    recalcula desde la siembra, que es lo único que da una curva coherente.
    """
    material = json.dumps(geojson_aoi, sort_keys=True) + "|" + str(fecha_siembra)
    if fuentes_clima:
        material += "|" + ",".join(str(f).strip().lower() for f in fuentes_clima)
    if suelo:
        material += "|" + json.dumps(suelo, sort_keys=True, default=str)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def cargar_estado(
    path_estado: str | Path,
    au_real_inicial: float,
    prof_raiz_inicial: float,
    huella: str | None = None,
    reiniciar: bool = False,
    devolver_motivo: bool = False,
):
    """Carga el estado previo, o arranca de cero.

    Arranca de cero cuando: no hay archivo, `reiniciar` es True, o la huella
    guardada no coincide con la actual (cambió el lote o la fecha de siembra).

    Con devolver_motivo=True devuelve (estado, motivo), donde motivo es None
    si se continuó, o un texto explicando por qué se reinició. Quien llame
    necesita saberlo: un reinicio obliga a archivar el CSV anterior, porque
    la serie vieja corresponde a otro cálculo y mezclarlas da una curva que
    no existió nunca.
    """
    motivo = None
    path_estado = Path(path_estado)

    if reiniciar and path_estado.exists():
        logger.warning("Se pidió reiniciar el estado: se descarta el balance acumulado.")
        path_estado.unlink()
        motivo = "se pidió reiniciar"

    if path_estado.exists():
        with open(path_estado, "r", encoding="utf-8") as f:
            data = json.load(f)
        huella_guardada = data.pop("huella", None)

        if huella is not None and huella_guardada is not None and huella_guardada != huella:
            logger.warning(
                "⚠️  Cambió alguno de los datos de base —lote, fecha de siembra, "
                "fuente de clima o perfil de suelo— (huella %s -> %s). "
                "El balance acumulado no corresponde: se reinicia desde cero.",
                huella_guardada,
                huella,
            )
            motivo = "cambió el lote, la siembra, el clima o el perfil de suelo"
        elif huella is not None and huella_guardada is None:
            logger.info("Estado previo sin huella; se adopta la actual y se continúa.")
            logger.info("Estado previo cargado: %s", data)
            est = EstadoBalance.from_dict(data)
            return (est, None) if devolver_motivo else est
        else:
            logger.info("Estado previo cargado: %s", data)
            est = EstadoBalance.from_dict(data)
            return (est, None) if devolver_motivo else est

    logger.info(
        "Se inicializa el balance: AU=%.1f mm, profundidad de raíz=%.0f cm.",
        au_real_inicial,
        prof_raiz_inicial,
    )
    est = EstadoBalance.inicial(au_real_inicial, prof_raiz_inicial)
    return (est, motivo) if devolver_motivo else est


def guardar_estado(
    path_estado: str | Path,
    estado: EstadoBalance,
    huella: str | None = None,
    extra: dict | None = None,
) -> None:
    path_estado = Path(path_estado)
    path_estado.parent.mkdir(parents=True, exist_ok=True)
    data = estado.to_dict()
    if huella is not None:
        data["huella"] = huella
    if extra:
        data.update(extra)
    with open(path_estado, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def archivar_csv(path_csv: str | Path, motivo: str = "") -> Path | None:
    """Aparta el CSV anterior en lugar de borrarlo.

    Se llama cuando el balance se reinicia. Si se dejara el CSV, las filas
    viejas (calculadas con otro lote u otra fecha de siembra) se mezclarían
    con las nuevas y el tablero mostraría una serie continua que nunca
    existió. El archivo se conserva con fecha por si hace falta consultarlo.
    """
    path_csv = Path(path_csv)
    if not path_csv.exists():
        return None

    sello = datetime.now().strftime("%Y%m%d-%H%M%S")
    destino = path_csv.with_name(f"{path_csv.stem}_hasta_{sello}{path_csv.suffix}")
    # Dos archivados dentro del mismo segundo darían el mismo nombre y el
    # segundo pisaría al primero sin avisar.
    n = 2
    while destino.exists():
        destino = path_csv.with_name(f"{path_csv.stem}_hasta_{sello}-{n}{path_csv.suffix}")
        n += 1
    path_csv.rename(destino)
    logger.warning(
        "Serie anterior archivada como %s (%s). Se empieza una serie nueva.",
        destino.name,
        motivo or "reinicio del balance",
    )
    return destino


def append_resultados_csv(path_csv: str | Path, df_nuevos: pd.DataFrame) -> None:
    """Agrega filas nuevas al CSV histórico, sin duplicar fechas ya guardadas."""
    path_csv = Path(path_csv)
    path_csv.parent.mkdir(parents=True, exist_ok=True)

    if df_nuevos.empty:
        return

    if path_csv.exists():
        df_previo = pd.read_csv(path_csv, parse_dates=["fecha"])
        df_final = pd.concat([df_previo, df_nuevos], ignore_index=True)
        df_final = df_final.drop_duplicates(subset="fecha", keep="last")
        df_final = df_final.sort_values("fecha")
    else:
        df_final = df_nuevos.sort_values("fecha")

    df_final.to_csv(path_csv, index=False)
    logger.info("CSV actualizado: %s (%d filas totales)", path_csv, len(df_final))
