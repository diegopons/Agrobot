"""
main.py
-------
Orquestador de una corrida del balance hídrico, para todos los lotes.

Lo dispara GitHub Actions (o el Programador de Tareas de Windows). Cada corrida
le pide los lotes al almacén (ver src/almacen.py) —el lote de config.yaml en
modo local, o todos los lotes activos de todos los usuarios en Supabase— y,
para cada uno:

  1. Lee el estado de la corrida anterior de ESE lote.
  2. Calcula qué rango de fechas es "nuevo" (desde el día siguiente al
     último procesado, hasta hoy - rezago).
  3. Si no hay días nuevos, pasa al lote siguiente.
  4. Descarga NDVI (Copernicus Data Space Ecosystem) y clima para ese rango.
  5. Corre el modelo de balance hídrico día a día, partiendo del estado
     anterior.
  6. Agrega los resultados nuevos a la serie del lote y guarda el nuevo estado.
  7. Publica el resumen para el bot y el tablero, y avisa por Telegram al
     dueño del lote si corresponde.

Un lote que falla no frena a los demás: el error queda registrado en ese lote
y la corrida termina con código 1 para que se vea en Actions.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

# Permite ejecutar tanto como módulo (`python -m src.main`) como script directo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.almacen import MAX_FECHAS_MOSAICO, Lote, crear_almacen
from src.balance import procesar_dias_nuevos
from src.clima_openmeteo import descargar_pronostico
from src import pronostico as mod_pronostico
from src import clima as mod_clima
from src import suelo_carta as mod_suelo_carta
from src import indices as mod_indices
from src import publicar_estado as mod_estado_json
from src.config import cargar_config
from src.estado import huella_corrida
from src.mapas_ndvi import generar_mosaico
from src.ndvi_sentinelhub import obtener_serie_ndvi, rellenar_ndvi
from src.notificaciones import armar_mensaje, debe_notificar, enviar_telegram
from src.suelo import SUELO_PROPIEDADES_DEFAULT, precalcular_parametros_suelo

log = logging.getLogger("main")


class CorridaAbortada(RuntimeError):
    """El lote no se puede procesar hoy; no se tocó su estado ni su serie."""


@dataclass
class Opciones:
    """Lo que llega del formulario de "Run workflow", común a todos los lotes."""
    fecha_fin: Optional[str] = None
    reiniciar: bool = False
    enviar_resumen: bool = False


def _env_bool(nombre: str) -> bool:
    return os.environ.get(nombre, "").strip().lower() in ("1", "true", "yes", "si", "sí")


# Parámetros de inicio que el formulario puede pisar en modo local
OVERRIDES_LOCALES = [
    ("BH_FECHA_SIEMBRA", ("campana", "fecha_siembra"), str),
    ("BH_AU_INICIAL_MM", ("campana", "au_real_inicial_mm"), float),
    ("BH_PROF_RAIZ_INICIAL_CM", ("campana", "prof_raiz_inicial_cm"), float),
    ("BH_UMBRAL_RIEGO_PCT", ("balance", "umbral_riego_pct"), float),
    ("BH_PROF_RAIZ_MAX_CM", ("balance", "prof_raiz_max_cm"), float),
    ("BH_MODO_AVISO", ("telegram", "modo"), str),
    ("BH_NOMBRE_LOTE", ("telegram", "nombre_lote"), str),
    ("BH_REZAGO_CLIMA", ("rezago_dias", "clima"), int),
    ("BH_EFICIENCIA_RIEGO", ("telegram", "eficiencia_aplicacion"), float),
]


def aplicar_parametros_de_entorno(cfg: dict) -> dict:
    """Permite sobrescribir parámetros de inicio sin editar config.yaml.

    El formulario de "Run workflow" en GitHub Actions pasa estos valores como
    variables de entorno. Si vienen vacíos (una corrida programada, por
    ejemplo), se usa lo que diga config.yaml. Solo vale en modo local: con
    Supabase, los parámetros de cada lote los define su dueño en el tablero.
    """
    aplicados = []
    for var, (seccion, clave), tipo in OVERRIDES_LOCALES:
        crudo = os.environ.get(var, "").strip()
        if not crudo:
            continue
        try:
            valor = tipo(crudo)
        except ValueError:
            log.warning("Valor inválido en %s ('%s'): se ignora.", var, crudo)
            continue
        cfg.setdefault(seccion, {})[clave] = valor
        aplicados.append(f"{seccion}.{clave}={valor}")

    if aplicados:
        log.info("Parámetros recibidos del formulario: %s", ", ".join(aplicados))

    return cfg


def enviar_resumen_actual(lote: Lote, almacen, guardado) -> None:
    """Reenvía por Telegram el estado del último día calculado.

    Se usa cuando se pide un resumen a demanda y no hay días nuevos que
    procesar: en vez de no mandar nada, se lee la última fila de la serie y se
    arma el mensaje con eso.
    """
    df = almacen.leer_serie(lote, guardado)
    if df.empty:
        log.warning("Se pidió un resumen pero todavía no hay resultados calculados.")
        return

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tg_token or not lote.telegram_chat_id:
        log.warning("Se pidió un resumen pero falta el token del bot o el chat del usuario.")
        return

    ultimo = df.iloc[-1]
    cfg = lote.cfg
    cfg_tg = cfg.get("telegram", {})
    mensaje = armar_mensaje(
        fecha=ultimo["fecha"].date(),
        au_real_mm=float(ultimo["au_real_mm"]),
        au_max_mm=float(ultimo["au_max_mm"]),
        pct_au=float(ultimo["pct_au"]),
        ndvi=float(ultimo["ndvi"]),
        etc_mm=float(ultimo["etc_ajustada_mm"]),
        precipitacion_7d_mm=float(df.tail(7)["precipitacion_mm"].sum()),
        umbral_riego_pct=cfg["balance"]["umbral_riego_pct"],
        nombre_lote=cfg_tg.get("nombre_lote", "Lote"),
        eficiencia_aplicacion=cfg_tg.get("eficiencia_aplicacion", 0.85),
        reposicion_objetivo_pct=cfg_tg.get("reposicion_objetivo_pct", 100.0),
    )
    if enviar_telegram(tg_token, lote.telegram_chat_id, mensaje):
        log.info("Resumen enviado por Telegram (datos al %s).", ultimo["fecha"].date())


def configurar_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
    )


def resolver_suelo(lote: Lote, almacen, aoi_info: dict) -> dict:
    """Decide de dónde sale el perfil de suelo del lote y lo deja en su cfg.

    1. La calicata o el perfil que cargó el usuario.
    2. La Carta de Suelos de Córdoba (IDECOR) para el polígono del lote. Se
       consulta una sola vez por polígono y queda guardada: si el servicio
       cambia o se cae, la huella del balance no se mueve y la campaña no se
       recalcula sin motivo.
    3. El perfil por defecto (serie Oncativo de la planilla BH p-Pons).

    La carta se consulta aunque el lote tenga calicata: el tablero la muestra
    igual (series, capacidad de uso, limitantes), solo que no entra al balance.

    Devuelve el origen, para el resumen y el tablero.
    """
    cfg = lote.cfg
    calicata = bool(cfg.get("suelo"))
    con_calicata = {"origen": "calicata o perfil cargado por el usuario"}

    cfg_carta = cfg.get("suelo_carta", {})
    if not cfg_carta.get("habilitado", True):
        return con_calicata if calicata else {"origen": "perfil por defecto"}

    carta = almacen.suelo_carta(lote)
    if not carta or carta.get("huella_geometria") != mod_suelo_carta.huella_geometria(aoi_info["geojson"]):
        try:
            carta = mod_suelo_carta.consultar(
                aoi_info["geojson"],
                url=cfg_carta.get("url", mod_suelo_carta.URL_WFS),
                min_cobertura_pct=float(cfg_carta.get("min_cobertura_pct", 50)),
            )
            almacen.guardar_suelo_carta(lote, carta)
            log.info("Carta de Suelos consultada: %s", mod_suelo_carta.describir(carta))
        except mod_suelo_carta.ErrorCarta as e:
            # No se guarda nada: la próxima corrida vuelve a intentar. Si
            # entonces responde, cambia el perfil y el balance se recalcula
            # solo desde la siembra con el suelo correcto.
            log.warning("No se pudo consultar la Carta de Suelos: %s. Se reintenta en la "
                        "próxima corrida.", e)
            if calicata:
                return con_calicata
            return {"origen": "perfil por defecto", "motivo": "IDECOR no respondió"}

    if calicata:
        return con_calicata
    if carta.get("perfil"):
        cfg["suelo"] = carta["perfil"]
        return {"origen": "Carta de Suelos de Córdoba (IDECOR)",
                "descripcion": mod_suelo_carta.describir(carta)}
    log.info("Sin perfil de la Carta de Suelos: %s", carta.get("motivo"))
    return {"origen": "perfil por defecto", "motivo": carta.get("motivo")}


def actualizar_indices(lote: Lote, almacen, cdse: tuple[str, str]) -> None:
    """Serie y mapas de índices espectrales del lote (ver src/indices.py).

    Va aparte del balance: avanza con su propio "hasta", y un fallo acá no
    toca el estado ni la serie del balance.
    """
    cfg = lote.cfg
    cfg_ix = cfg.get("indices", {})
    if not cfg_ix.get("habilitado", True):
        return
    aoi_info = lote.aoi()
    huella = mod_suelo_carta.huella_geometria(aoi_info["geojson"])
    siembra = str(cfg["campana"]["fecha_siembra"])

    meta = almacen.indices_meta(lote) or {}
    # Otro polígono u otra campaña: lo calculado describe otra cosa
    if meta and (meta.get("huella_geometria") != huella or meta.get("desde") != siembra):
        log.info("Índices: cambió el lote o la siembra; se recalculan desde la siembra.")
        almacen.borrar_indices(lote)
        meta = {}
    meta.update({"huella_geometria": huella, "desde": siembra})

    desde = pd.to_datetime(meta["hasta"]) + timedelta(days=1) if meta.get("hasta") \
        else pd.to_datetime(siembra)
    hasta = pd.to_datetime(date.today()) - timedelta(days=int(cfg["rezago_dias"].get("ndvi", 2)))
    if desde > hasta:
        return
    d, h = desde.strftime("%Y-%m-%d"), hasta.strftime("%Y-%m-%d")

    scl = tuple(cfg["cdse"].get("scl_validas", (4, 5)))
    filas = mod_indices.serie(
        *cdse, aoi_info["geojson"], d, h,
        resolucion_m=cfg["cdse"]["resolucion_m"],
        max_cloud_coverage=cfg["cdse"]["max_cloud_coverage"],
        min_cobertura_pct=cfg["cdse"].get("min_pixeles_validos_pct", 70.0),
        scl_validas=scl,
    )
    almacen.guardar_indices(lote, filas)
    meta["hasta"] = h
    log.info("Índices: %d fecha(s) despejadas entre %s y %s.", len(filas), d, h)

    # Mapas de la última fecha despejada, solo si es nueva
    if filas and filas[-1]["fecha"] > meta.get("fecha_mapa", ""):
        ultima = filas[-1]
        subidos = []
        for clave in [*mod_indices.INDICES, "rgb"]:
            png = mod_indices.mapa(*cdse, aoi_info["geojson"], aoi_info["bbox"], ultima["fecha"],
                                   clave, lado=int(cfg_ix.get("lado_px", 512)), scl_validas=scl)
            if png:
                almacen.publicar_mapa_indice(lote, clave, png)
                subidos.append(clave)
        if subidos:
            meta.update({"fecha_mapa": ultima["fecha"], "cobertura_mapa": ultima["cobertura_pct"],
                         "mapas": subidos})
            log.info("Índices: %d mapas del %s publicados.", len(subidos), ultima["fecha"])
    meta["leyendas"] = mod_indices.leyendas()
    almacen.guardar_indices_meta(lote, meta)


def correr_lote(lote: Lote, almacen, cdse: tuple[str, str], opciones: Opciones) -> str:
    """Procesa los días nuevos de un lote. Devuelve "ok" o "sin_novedad".

    Levanta CorridaAbortada (u otra excepción) si el lote no se pudo procesar.
    """
    cfg = lote.cfg
    cdse_client_id, cdse_client_secret = cdse

    # 1. AOI
    aoi_info = lote.aoi()
    log.info("Lote cargado: %.2f ha. BBox=%s", aoi_info["area_ha"], aoi_info["bbox"])
    origen_suelo = resolver_suelo(lote, almacen, aoi_info)
    log.info("Perfil de suelo: %s", origen_suelo.get("descripcion") or origen_suelo["origen"])

    # 2. Estado y suelo
    _orden_clima = [
        str(f).lower()
        for f in (cfg.get("clima", {}).get("fuentes")
                  or [cfg.get("clima", {}).get("fuente", "agera5")])
    ]
    # La huella ata el estado guardado al lote y a la fecha de siembra: si
    # cualquiera de los dos cambió, el balance acumulado no sirve y se reinicia.
    huella = huella_corrida(
        aoi_info["geojson"],
        cfg["campana"]["fecha_siembra"],
        fuentes_clima=_orden_clima,
        suelo=cfg.get("suelo"),
    )
    guardado = almacen.cargar_estado(lote, huella, reiniciar=opciones.reiniciar)

    # Un riego cargado, corregido o borrado en un día ya calculado: se vuelve
    # al día anterior y se recalcula desde ahí. Con una serie nueva no hace
    # falta, porque se recalcula todo desde la siembra.
    if lote.recalcular_desde:
        ultima = guardado.balance.ultima_fecha_procesada
        if not guardado.motivo_reinicio and ultima and str(lote.recalcular_desde) <= str(ultima):
            almacen.rebobinar(lote, guardado, str(lote.recalcular_desde))
        almacen.recalculo_atendido(lote)
    estado = guardado.balance

    df_suelo = precalcular_parametros_suelo(
        cfg.get("suelo", SUELO_PROPIEDADES_DEFAULT),
        llenado_inicial_pct=cfg["balance"].get("llenado_inicial_pct", 70.0),
    )

    # 3. Definir rango de fechas nuevo
    if estado.ultima_fecha_procesada:
        fecha_desde = pd.to_datetime(estado.ultima_fecha_procesada) + timedelta(days=1)
    else:
        fecha_desde = pd.to_datetime(cfg["campana"]["fecha_siembra"])

    _rezagos = {
        "siga": cfg["rezago_dias"].get("clima_siga", 2),
        "openmeteo": cfg["rezago_dias"].get("clima_openmeteo", 1),
        "agera5": cfg["rezago_dias"].get("clima", 10),
    }
    # Con varias fuentes alcanza con que una tenga el día: manda la más rápida.
    rezago_clima = min(
        (_rezagos[f] for f in _orden_clima if f in _rezagos),
        default=cfg["rezago_dias"]["clima"],
    )
    rezago = max(cfg["rezago_dias"]["ndvi"], rezago_clima)
    hoy = pd.to_datetime(date.today())
    fecha_hasta_disponible = hoy - timedelta(days=rezago)

    # El usuario puede pedir una fecha de fin. Vacío = hasta donde haya datos.
    fecha_pedida = None
    if opciones.fecha_fin:
        try:
            fecha_pedida = pd.to_datetime(opciones.fecha_fin)
        except (ValueError, TypeError):
            log.warning("Fecha de fin inválida ('%s'): se ignora.", opciones.fecha_fin)

    if fecha_pedida is not None:
        if fecha_pedida > hoy:
            log.warning(
                "La fecha de fin pedida (%s) es futura: se usa hoy como tope.",
                fecha_pedida.date(),
            )
            fecha_pedida = hoy
        if fecha_pedida > fecha_hasta_disponible:
            # Se intenta igual: los días sin clima se recortan más abajo, en
            # vez de entrar al balance como ceros y ensuciar la serie.
            log.warning(
                "Se pidió hasta el %s, pero el clima suele tener unos %d días de "
                "rezago. Se intentará igual y se recortará hasta donde haya datos.",
                fecha_pedida.date(), rezago,
            )
        fecha_hasta = fecha_pedida
    else:
        fecha_hasta = fecha_hasta_disponible

    if fecha_desde > fecha_hasta:
        log.info(
            "Nada nuevo para procesar todavía (último día procesado: %s; "
            "próximo disponible recién el %s por el rezago de datos).",
            estado.ultima_fecha_procesada,
            fecha_desde.date(),
        )
        if opciones.enviar_resumen:
            enviar_resumen_actual(lote, almacen, guardado)
        return "sin_novedad"

    fecha_desde_str = fecha_desde.strftime("%Y-%m-%d")
    fecha_hasta_str = fecha_hasta.strftime("%Y-%m-%d")
    log.info("Procesando rango nuevo: %s -> %s", fecha_desde_str, fecha_hasta_str)

    # 4. NDVI (Copernicus Data Space Ecosystem)
    log.info("Descargando NDVI (Sentinel-2 / CDSE Statistical API)...")
    df_ndvi = obtener_serie_ndvi(
        cdse_client_id,
        cdse_client_secret,
        aoi_info["geojson"],
        fecha_desde_str,
        fecha_hasta_str,
        max_cloud_coverage=cfg["cdse"]["max_cloud_coverage"],
        resolucion_m=cfg["cdse"]["resolucion_m"],
        min_pixeles_validos_pct=cfg["cdse"].get("min_pixeles_validos_pct", 70.0),
        scl_validas=tuple(cfg["cdse"].get("scl_validas", (4, 5))),
        ndvi_minimo_valido=float(cfg["cdse"].get("ndvi_minimo_valido", 0.0)),
    )
    log.info(
        "NDVI: %d fechas con imagen utilizable en el rango.",
        df_ndvi["ndvi"].notna().sum(),
    )
    diag_ndvi = {
        "fechas_con_dato": int(df_ndvi.attrs.get("fechas_con_dato", 0)),
        "descartes": dict(df_ndvi.attrs.get("descartes", {})),
    }
    # Fechas con observación real: son las que valen para el mosaico
    fechas_con_imagen = [
        d.strftime("%Y-%m-%d")
        for d in df_ndvi.loc[df_ndvi["ndvi"].notna(), "fecha"].sort_values()
    ]

    # 5. Clima: una o varias fuentes, por prioridad (ver src/clima.py)
    cfg_clima = cfg.get("clima", {})
    fuentes, estacion_siga = mod_clima.armar_fuentes(
        cfg, aoi_info["bbox"], fecha_desde_str, fecha_hasta_str
    )
    log.info("Clima: fuentes en orden %s", [n for n, _ in fuentes])
    df_clima, detalle_clima = mod_clima.combinar(
        fuentes, fecha_desde_str, fecha_hasta_str
    )
    log.info("Clima · origen de los datos — %s", mod_clima.resumen_origen(df_clima))
    if estacion_siga:
        log.info(
            "Clima · estación SIGA usada: %s (%s) a %.1f km.",
            estacion_siga.get("nombre"), estacion_siga.get("id_interno"),
            estacion_siga.get("km", float("nan")),
        )
    if detalle_clima["fallidas"]:
        for nombre, motivo in detalle_clima["fallidas"].items():
            log.warning("Clima · '%s' no se pudo usar: %s", nombre, motivo)

    # Sin clima no hay balance: con NaN el día se procesaría como ETc 0 y
    # lluvia 0, que no es "no pasó nada" sino "no sabemos qué pasó".
    if df_clima[["precipitacion_mm", "eto_mm"]].isna().all().all():
        raise CorridaAbortada(
            f"Ninguna fuente de clima devolvió datos para {fecha_desde_str} a "
            f"{fecha_hasta_str}. No se tocó el estado ni la serie."
        )

    # 6. Merge en calendario diario completo del rango
    rango_fechas = pd.date_range(fecha_desde_str, fecha_hasta_str, freq="D")
    df_dias = pd.DataFrame({"fecha": rango_fechas})
    df_dias = df_dias.merge(df_clima, on="fecha", how="left")
    df_dias = df_dias.merge(
        df_ndvi[["fecha", "ndvi"]] if "ndvi" in df_ndvi.columns else df_ndvi,
        on="fecha", how="left",
    )

    # El NDVI arrastrado de la corrida anterior pasa por el mismo control de
    # plausibilidad: si quedó guardado un valor contaminado (antes del
    # enmascarado por lista blanca, una fecha con agua o sombra podía pasar el
    # filtro), se lo descarta acá. Si no se descarta, el valor malo se sigue
    # arrastrando corrida tras corrida aunque el enmascarado ya esté corregido.
    ndvi_piso = float(cfg["cdse"].get("ndvi_minimo_valido", 0.0))
    ndvi_previo = estado.ultimo_ndvi_valido
    if ndvi_previo is not None and not (ndvi_piso <= ndvi_previo <= 1.0):
        log.warning(
            "El NDVI guardado de la corrida anterior (%.4f) está fuera del "
            "rango plausible [%.2f, 1.00]: se descarta y no se arrastra.",
            ndvi_previo, ndvi_piso,
        )
        diag_ndvi["ndvi_previo_descartado"] = round(float(ndvi_previo), 4)
        ndvi_previo = None
        # También se limpia en el estado: el módulo de balance lo usa como
        # respaldo si algún día quedara sin NDVI, y ahí volvería a entrar.
        estado.ultimo_ndvi_valido = None

    # Completar los días sin imagen antes de correr el balance
    df_dias, resumen_ndvi = rellenar_ndvi(
        df_dias,
        ndvi_previo=ndvi_previo,
        max_dias_interpolar=cfg["cdse"].get("max_dias_interpolar", 30),
    )
    log.info(
        "NDVI: %d observaciones reales, %d días interpolados, %d sostenidos al "
        "final; hueco mayor entre imágenes: %d días.",
        resumen_ndvi["observados"],
        resumen_ndvi["interpolados"],
        resumen_ndvi["arrastrados_al_final"],
        resumen_ndvi["hueco_mayor"],
    )

    # Los días del final sin dato de clima no se procesan: entrarían al balance
    # con ETo y lluvia en cero, lo que simula un día sin consumo ni aporte y
    # deja una serie que parece completa pero no lo es. Mejor cortar ahí y
    # retomarlos cuando la fuente los publique.
    sin_clima = df_dias["eto_mm"].isna()
    if sin_clima.any():
        primer_hueco = sin_clima.idxmax() if sin_clima.iloc[-1] else None
        if primer_hueco is not None and sin_clima.loc[primer_hueco:].all():
            recortados = int(sin_clima.loc[primer_hueco:].sum())
            ultimo_util = df_dias.loc[primer_hueco, "fecha"] - timedelta(days=1)
            log.warning(
                "Los últimos %d días (desde el %s) todavía no tienen datos de "
                "clima: se procesan hasta el %s y se retomarán más adelante.",
                recortados,
                df_dias.loc[primer_hueco, "fecha"].date(),
                ultimo_util.date(),
            )
            df_dias = df_dias.loc[:primer_hueco - 1] if primer_hueco > 0 else df_dias.iloc[0:0]
        else:
            log.warning(
                "Hay %d días sueltos sin dato de clima dentro del rango; "
                "se tratan como sin lluvia ni consumo.",
                int(sin_clima.sum()),
            )

    if df_dias.empty:
        log.info("No quedaron días con datos completos para procesar.")
        if opciones.enviar_resumen:
            enviar_resumen_actual(lote, almacen, guardado)
        return "sin_novedad"

    # Riegos que el usuario cargó en el tablero; los días sin riego van en 0
    riegos = almacen.riegos(lote, fecha_desde_str, fecha_hasta_str)
    df_dias["riego_mm"] = df_dias["fecha"].dt.strftime("%Y-%m-%d").map(riegos).fillna(0.0)
    if riegos:
        log.info("Riegos cargados en el rango: %d (%.1f mm).", len(riegos), sum(riegos.values()))

    # 7. Correr el balance de forma incremental
    df_resultados, estado_nuevo = procesar_dias_nuevos(
        df_dias,
        estado,
        df_suelo,
        umbral_riego_pct=cfg["balance"]["umbral_riego_pct"],
        incremento_raiz_diario_cm=cfg["balance"]["incremento_raiz_diario_cm"],
        prof_raiz_max_cm=cfg["balance"]["prof_raiz_max_cm"],
        kc_a=cfg["balance"]["kc_a"],
        kc_b=cfg["balance"]["kc_b"],
        recarga_por_capa=cfg["balance"].get("recarga_por_capa", True),
        umbral_lluvia_efectiva_mm=cfg["balance"].get("umbral_lluvia_efectiva_mm", 15.0),
        tabla_prof_raiz=cfg["balance"].get("prof_raiz_por_dds"),
        fecha_siembra=cfg["campana"]["fecha_siembra"],
    )
    # El balance no arrastra el origen de cada dato de clima; se lo vuelve a
    # pegar para que la serie diga de qué fuente salió cada día.
    columnas_origen = [c for c in ("fuente_pp", "fuente_eto") if c in df_dias.columns]
    if columnas_origen and not df_resultados.empty:
        df_resultados = df_resultados.merge(
            df_dias[["fecha", *columnas_origen]], on="fecha", how="left")

    # 8. Guardar serie y estado
    guardado.balance = estado_nuevo
    if fechas_con_imagen:
        guardado.fechas_con_imagen = sorted(
            set(guardado.fechas_con_imagen) | set(fechas_con_imagen)
        )[-MAX_FECHAS_MOSAICO:]
    almacen.guardar(lote, df_resultados, guardado, huella)

    # Para el resumen hace falta la campaña entera, no solo los días nuevos de
    # esta corrida: se relee la serie ya consolidada.
    try:
        df_completo = almacen.leer_serie(lote, guardado)
    except Exception:  # noqa: BLE001
        df_completo = df_resultados
    if df_completo.empty:
        df_completo = df_resultados

    # 9. Mosaico de imágenes NDVI (si está habilitado y hay imágenes nuevas)
    cfg_mapas = cfg.get("mapas", {})
    if cfg_mapas.get("habilitado", True) and fechas_con_imagen:
        try:
            destino = almacen.ruta_mosaico(lote)
            if generar_mosaico(
                cdse_client_id,
                cdse_client_secret,
                aoi_info["geojson"],
                guardado.fechas_con_imagen,
                destino=destino,
                columnas=cfg_mapas.get("columnas", 4),
                lado_px=cfg_mapas.get("lado_px", 220),
                titulo=f"NDVI — {cfg.get('telegram', {}).get('nombre_lote', 'Lote')}",
            ):
                almacen.publicar_mosaico(lote, destino)
        except Exception as e:  # noqa: BLE001
            # Un fallo acá no debe tumbar la corrida: el balance ya está calculado
            # y guardado, y el mosaico es información complementaria.
            log.warning("No se pudo generar el mosaico de NDVI: %s", e)

    # 10. Resumen, pronóstico y aviso de riego
    if df_resultados.empty:
        return "ok"

    ultimo = df_resultados.iloc[-1]
    fecha_ultimo = (
        ultimo["fecha"].date() if hasattr(ultimo["fecha"], "date") else ultimo["fecha"]
    )
    log.info(
        "Última fecha procesada: %s | %%AU=%.1f | AU real=%.1f mm | NDVI=%.3f",
        fecha_ultimo,
        ultimo["pct_au"],
        ultimo["au_real_mm"],
        ultimo["ndvi"],
    )

    umbral = cfg["balance"]["umbral_riego_pct"]

    # --- Proyección con pronóstico (no toca el estado ni la serie) ---
    linea_pronostico = None
    df_pronostico_pub = None
    ajuste_riego = None
    cfg_pron = cfg.get("pronostico", {})
    if cfg_pron.get("habilitado", False) and any(
            n == "openmeteo" for n, _ in fuentes):
        try:
            df_fc = descargar_pronostico(
                aoi_info["bbox"],
                dias=int(cfg_pron.get("dias", 10)),
                zona_horaria=cfg_clima.get(
                    "zona_horaria", "America/Argentina/Cordoba"),
            )
            df_proy = mod_pronostico.proyectar(
                estado_nuevo,
                df_fc,
                df_suelo,
                ndvi_actual=float(ultimo["ndvi"]),
                umbral_riego_pct=umbral,
                incremento_raiz_diario_cm=cfg["balance"]["incremento_raiz_diario_cm"],
                prof_raiz_max_cm=cfg["balance"]["prof_raiz_max_cm"],
                kc_a=cfg["balance"]["kc_a"],
                kc_b=cfg["balance"]["kc_b"],
                recarga_por_capa=cfg["balance"].get("recarga_por_capa", True),
                umbral_lluvia_efectiva_mm=cfg["balance"].get(
                    "umbral_lluvia_efectiva_mm", 15.0),
                tabla_prof_raiz=cfg["balance"].get("prof_raiz_por_dds"),
                fecha_siembra=cfg["campana"]["fecha_siembra"],
            )
            df_pronostico_pub = df_fc
            linea_pronostico = mod_pronostico.resumir(
                df_proy, umbral, ya_en_deficit=float(ultimo["pct_au"]) < umbral
            )
            if linea_pronostico:
                log.info("Pronóstico: %s", linea_pronostico)

            # Lámina descontando la lluvia de los próximos días. Se
            # calcula siempre; quien la publica decide si corresponde
            # mostrarla (solo si el lote está por debajo del umbral).
            cfg_tg_aj = cfg.get("telegram", {})
            falta_hoy = max(
                0.0,
                float(ultimo["au_max_mm"])
                * (float(cfg_tg_aj.get("reposicion_objetivo_pct", 100.0)) / 100.0)
                - float(ultimo["au_real_mm"]),
            )
            ajuste_riego = mod_pronostico.lamina_ajustada(
                falta_hoy,
                df_fc,
                eficiencia_aplicacion=float(
                    cfg_tg_aj.get("eficiencia_aplicacion", 0.85)),
                dias_descuento=int(cfg_pron.get("dias_descuento", 3)),
                umbral_lluvia_efectiva_mm=cfg["balance"].get(
                    "umbral_lluvia_efectiva_mm", 15.0),
            )
            if ajuste_riego:
                log.info(
                    "Riego: reposición total %.1f mm netos | ajustada "
                    "%.1f mm netos (descuenta %.1f mm hasta %s).",
                    falta_hoy,
                    ajuste_riego["lamina_neta_mm"],
                    ajuste_riego["descuento_mm"],
                    ajuste_riego["hasta"],
                )
        except Exception as e:  # noqa: BLE001
            # El balance ya está calculado y guardado: un fallo del
            # pronóstico no debe tumbar la corrida.
            log.warning("No se pudo calcular la proyección: %s", e)

    if ultimo["pct_au"] < umbral:
        log.warning(
            "⚠️  ALERTA DE RIEGO: %% Agua Útil (%.1f%%) por debajo del umbral (%s%%).",
            ultimo["pct_au"],
            umbral,
        )

    # --- Resumen para agentes conversacionales (n8n, bots) y el tablero ---
    try:
        resumen = mod_estado_json.construir_estado(
            df_completo,
            cfg,
            aoi_info,
            estacion_siga=estacion_siga,
            detalle_clima=detalle_clima,
            linea_pronostico=linea_pronostico,
            df_pronostico=df_pronostico_pub,
            ajuste_riego=ajuste_riego,
            diag_ndvi=diag_ndvi,
        )
        resumen["perfil_de_suelo"] = origen_suelo
        almacen.publicar_resumen(lote, resumen)
    except Exception as e:  # noqa: BLE001
        # El balance ya está guardado: que falle el resumen no debe tumbar nada.
        log.warning("No se pudo publicar el estado para agentes: %s", e)

    # --- Telegram, al dueño del lote ---
    cfg_tg = cfg.get("telegram", {})
    if cfg_tg.get("habilitado", False):
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not tg_token or not lote.telegram_chat_id:
            log.info(
                "Sin aviso por Telegram: %s.",
                "falta TELEGRAM_BOT_TOKEN" if not tg_token
                else "el usuario no vinculó su Telegram",
            )
        else:
            # %AU del día anterior al bloque nuevo, para detectar el cruce
            # de umbral (viene del estado previo, no del df de esta corrida).
            pct_au_previo = None
            if len(df_resultados) > 1:
                pct_au_previo = float(df_resultados.iloc[-2]["pct_au"])
            elif estado.ultima_fecha_procesada:
                # Corrida con un solo día nuevo: comparar contra el estado guardado
                au_max_ref = float(ultimo["au_max_mm"])
                if au_max_ref > 0:
                    pct_au_previo = (estado.au_real_mm / au_max_ref) * 100

            # Un resumen pedido a mano se manda siempre, sin importar el modo
            if opciones.enviar_resumen or debe_notificar(
                float(ultimo["pct_au"]),
                pct_au_previo,
                umbral,
                modo=cfg_tg.get("modo", "cruce"),
            ):
                lluvia_7d = float(df_completo.tail(7)["precipitacion_mm"].sum())
                mensaje = armar_mensaje(
                    fecha=fecha_ultimo,
                    au_real_mm=float(ultimo["au_real_mm"]),
                    au_max_mm=float(ultimo["au_max_mm"]),
                    pct_au=float(ultimo["pct_au"]),
                    ndvi=float(ultimo["ndvi"]),
                    etc_mm=float(ultimo["etc_ajustada_mm"]),
                    precipitacion_7d_mm=lluvia_7d,
                    umbral_riego_pct=umbral,
                    nombre_lote=cfg_tg.get("nombre_lote", "Lote"),
                    eficiencia_aplicacion=cfg_tg.get("eficiencia_aplicacion", 0.85),
                    reposicion_objetivo_pct=cfg_tg.get("reposicion_objetivo_pct", 100.0),
                    linea_pronostico=linea_pronostico,
                    ajuste_riego=ajuste_riego,
                )
                enviar_telegram(tg_token, lote.telegram_chat_id, mensaje)
            else:
                log.info("Sin condiciones para notificar (modo=%s).", cfg_tg.get("modo", "cruce"))

    return "ok"


def main(config_path: str = "config.yaml") -> int:
    cfg = cargar_config(config_path)
    configurar_logging(cfg["rutas"]["log_file"])

    log.info("========== Nueva corrida de Balance Hídrico ==========")

    # Credenciales (nunca en config.yaml)
    cdse = (os.environ.get("CDSE_CLIENT_ID"), os.environ.get("CDSE_CLIENT_SECRET"))
    if not all(cdse):
        log.error(
            "Faltan variables de entorno CDSE_CLIENT_ID / CDSE_CLIENT_SECRET. "
            "Ver README.md para cómo crearlas y configurarlas."
        )
        return 1

    try:
        almacen = crear_almacen(cfg)
    except RuntimeError as e:
        log.error("%s", e)
        return 1
    log.info("Almacén: %s", almacen.modo)

    opciones = Opciones(
        fecha_fin=os.environ.get("BH_FECHA_FIN", "").strip() or None,
        reiniciar=_env_bool("BH_REINICIAR_ESTADO"),
        enviar_resumen=_env_bool("BH_ENVIAR_RESUMEN"),
    )
    solo_id = os.environ.get("BH_LOTE_ID", "").strip().strip("'\"") or None
    if solo_id and almacen.modo == "supabase" and not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", solo_id):
        log.error("El id de lote '%s' no es válido: tiene que tener la forma "
                  "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 caracteres). Revisá que se haya "
                  "copiado completo.", solo_id)
        return 1

    if almacen.modo == "local":
        aplicar_parametros_de_entorno(cfg)
    else:
        ignorados = [v for v, _, _ in OVERRIDES_LOCALES if os.environ.get(v, "").strip()]
        if ignorados:
            log.warning(
                "Con Supabase los parámetros de cada lote se cargan en el tablero: "
                "se ignoran %s.", ", ".join(ignorados),
            )
        if opciones.reiniciar and not solo_id:
            log.warning(
                "Se pidió reiniciar sin indicar un lote: se ignora, para no "
                "recalcular los lotes de todos los usuarios. Indicar BH_LOTE_ID."
            )
            opciones.reiniciar = False

    # Las corridas de cada hora solo atienden lotes nuevos o modificados; los
    # días nuevos de todos los lotes los procesa la corrida diaria, a la hora
    # en que la estación del INTA ya consolidó el dato.
    solo_pendientes = _env_bool("BH_SOLO_PENDIENTES") and not solo_id
    try:
        lotes = almacen.lotes(solo_id=solo_id, solo_pendientes=solo_pendientes)
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo leer la lista de lotes: %s", e)
        return 1
    if not lotes:
        log.info("No hay lotes %spara procesar%s.",
                 "nuevos o modificados " if solo_pendientes else "activos ",
                 f" con id {solo_id}" if solo_id else "")
        return 1 if almacen.errores_de_carga else 0

    log.info("Lotes a procesar: %d", len(lotes))
    resultados: dict[str, str] = {}
    for lote in lotes:
        log.info("────────── Lote %s ──────────", lote.etiqueta)
        try:
            resultados[lote.etiqueta] = correr_lote(lote, almacen, cdse, opciones)
            almacen.registrar_corrida(lote)
        except Exception as e:  # noqa: BLE001
            if isinstance(e, CorridaAbortada):
                log.error("Lote %s: %s", lote.etiqueta, e)
            else:
                log.exception("Lote %s: la corrida falló.", lote.etiqueta)
            resultados[lote.etiqueta] = "error"
            try:
                almacen.registrar_corrida(lote, error=str(e) or type(e).__name__)
            except Exception as e2:  # noqa: BLE001
                log.warning("No se pudo registrar el error del lote: %s", e2)

        # Índices espectrales: aparte, para que un fallo no toque el balance
        try:
            actualizar_indices(lote, almacen, cdse)
        except Exception as e:  # noqa: BLE001
            log.warning("Lote %s: no se pudieron actualizar los índices espectrales: %s",
                        lote.etiqueta, e)

    errores = [n for n, r in resultados.items() if r == "error"]
    log.info(
        "Resumen: %d lotes · %d con días nuevos · %d sin novedad · %d con error%s",
        len(resultados),
        sum(r == "ok" for r in resultados.values()),
        sum(r == "sin_novedad" for r in resultados.values()),
        len(errores),
        f" ({', '.join(errores)})" if errores else "",
    )
    log.info("Corrida finalizada%s.", " con errores" if errores else " OK")
    return 1 if errores or almacen.errores_de_carga else 0


if __name__ == "__main__":
    sys.exit(main())
