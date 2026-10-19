"""
main.py
-------
Orquestador de una corrida periódica del balance hídrico.

Pensado para ser disparado por el Programador de Tareas de Windows (o cron,
o lo que sea) cada N días. Cada corrida:

  1. Lee config.yaml y el estado de la corrida anterior (data/estado.json).
  2. Calcula qué rango de fechas es "nuevo" (desde el día siguiente al
     último procesado, hasta hoy - rezago).
  3. Si no hay días nuevos, no hace nada (log y sale).
  4. Descarga NDVI (Copernicus Data Space Ecosystem) y clima
     (Copernicus Climate Data Store / AgERA5) para ese rango.
  5. Corre el modelo de balance hídrico día a día, partiendo del estado
     anterior.
  6. Agrega los resultados nuevos al CSV histórico y guarda el nuevo estado.
  7. Si el % de Agua Útil final quedó por debajo del umbral de riego, lo
     deja bien visible en el log (alerta de riego).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# Permite ejecutar tanto como módulo (`python -m src.main`) como script directo.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.aoi import cargar_aoi
from src.balance import procesar_dias_nuevos
from src.clima_agera5 import descargar_agera5
from src.clima_openmeteo import descargar_openmeteo, descargar_pronostico
from src import pronostico as mod_pronostico
from src import clima as mod_clima
from src import publicar_estado as mod_estado_json
from src.config import cargar_config
from src.estado import (
    append_resultados_csv,
    archivar_csv,
    cargar_estado,
    guardar_estado,
    huella_corrida,
)
from src.mapas_ndvi import generar_mosaico
from src.ndvi_sentinelhub import obtener_serie_ndvi, rellenar_ndvi
from src.notificaciones import armar_mensaje, debe_notificar, enviar_telegram
from src.suelo import SUELO_PROPIEDADES_DEFAULT, precalcular_parametros_suelo


def _env_bool(nombre: str) -> bool:
    return os.environ.get(nombre, "").strip().lower() in ("1", "true", "yes", "si", "sí")


def aplicar_parametros_de_entorno(cfg: dict, log) -> dict:
    """Permite sobrescribir parámetros de inicio sin editar config.yaml.

    El formulario de "Run workflow" en GitHub Actions pasa estos valores como
    variables de entorno. Si vienen vacíos (una corrida programada, por
    ejemplo), se usa lo que diga config.yaml.
    """
    overrides = [
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

    aplicados = []
    for var, (seccion, clave), tipo in overrides:
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


def enviar_resumen_actual(cfg: dict, log) -> None:
    """Reenvía por Telegram el estado del último día calculado.

    Se usa cuando se pide un resumen a demanda y no hay días nuevos que
    procesar: en vez de no mandar nada, se lee la última fila del CSV
    histórico y se arma el mensaje con eso.
    """
    ruta_csv = Path(cfg["rutas"]["salida_csv"])
    if not ruta_csv.exists():
        log.warning("Se pidió un resumen pero todavía no hay resultados calculados.")
        return

    df = pd.read_csv(ruta_csv, parse_dates=["fecha"])
    if df.empty:
        log.warning("Se pidió un resumen pero el CSV está vacío.")
        return

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not tg_token or not tg_chat_id:
        log.warning("Se pidió un resumen pero faltan las credenciales de Telegram.")
        return

    ultimo = df.iloc[-1]
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
    if enviar_telegram(tg_token, tg_chat_id, mensaje):
        log.info("Resumen enviado por Telegram (datos al %s).", ultimo["fecha"].date())


def configurar_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8"), logging.StreamHandler()],
    )


def main(config_path: str = "config.yaml") -> int:
    cfg = cargar_config(config_path)
    configurar_logging(cfg["rutas"]["log_file"])
    log = logging.getLogger("main")

    log.info("========== Nueva corrida de Balance Hídrico ==========")

    # Parámetros que pueden llegar del formulario de "Run workflow"
    cfg = aplicar_parametros_de_entorno(cfg, log)
    reiniciar_estado = _env_bool("BH_REINICIAR_ESTADO")
    enviar_resumen = _env_bool("BH_ENVIAR_RESUMEN")

    # 1. Credenciales (nunca en config.yaml)
    cdse_client_id = os.environ.get("CDSE_CLIENT_ID")
    cdse_client_secret = os.environ.get("CDSE_CLIENT_SECRET")
    if not cdse_client_id or not cdse_client_secret:
        log.error(
            "Faltan variables de entorno CDSE_CLIENT_ID / CDSE_CLIENT_SECRET. "
            "Ver README.md para cómo crearlas y configurarlas."
        )
        return 1

    # 2. AOI
    # 'archivo' es la clave actual; 'shapefile' se mantiene por compatibilidad
    ruta_aoi = cfg["aoi"].get("archivo") or cfg["aoi"]["shapefile"]
    aoi_info = cargar_aoi(ruta_aoi)
    log.info("Lote cargado: %.2f ha. BBox=%s", aoi_info["area_ha"], aoi_info["bbox"])

    # 3. Estado y suelo
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
    estado, motivo_reinicio = cargar_estado(
        cfg["rutas"]["estado_json"],
        au_real_inicial=cfg["campana"]["au_real_inicial_mm"],
        prof_raiz_inicial=cfg["campana"]["prof_raiz_inicial_cm"],
        huella=huella,
        reiniciar=reiniciar_estado,
        devolver_motivo=True,
    )

    # Si el balance arranca de cero, la serie anterior corresponde a otro
    # cálculo: se aparta para que no se mezcle con la nueva.
    if motivo_reinicio:
        archivar_csv(cfg["rutas"]["salida_csv"], motivo_reinicio)
    df_suelo = precalcular_parametros_suelo(
        cfg.get("suelo", SUELO_PROPIEDADES_DEFAULT),
        llenado_inicial_pct=cfg["balance"].get("llenado_inicial_pct", 70.0),
    )

    # 4. Definir rango de fechas nuevo
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
    pedido = os.environ.get("BH_FECHA_FIN", "").strip()
    if pedido:
        try:
            fecha_pedida = pd.to_datetime(pedido)
        except (ValueError, TypeError):
            log.warning("Fecha de fin inválida ('%s'): se ignora.", pedido)
            fecha_pedida = None
    else:
        fecha_pedida = None

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
                "Se pidió hasta el %s, pero AgERA5 suele tener unos %d días de "
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
            (fecha_desde + timedelta(days=0)).date(),
        )
        if enviar_resumen:
            enviar_resumen_actual(cfg, log)
        return 0

    fecha_desde_str = fecha_desde.strftime("%Y-%m-%d")
    fecha_hasta_str = fecha_hasta.strftime("%Y-%m-%d")
    log.info("Procesando rango nuevo: %s -> %s", fecha_desde_str, fecha_hasta_str)

    # 5. NDVI (Copernicus Data Space Ecosystem)
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

    # 6. Clima: una o varias fuentes, por prioridad (ver src/clima.py)
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
        log.error(
            "Ninguna fuente de clima devolvió datos para %s a %s. Se aborta la "
            "corrida sin tocar el estado ni el CSV.", fecha_desde_str, fecha_hasta_str
        )
        return 1

    # 7. Merge en calendario diario completo del rango
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
    # retomarlos cuando AgERA5 los publique.
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
        if enviar_resumen:
            enviar_resumen_actual(cfg, log)
        return 0

    df_dias["riego_mm"] = 0.0  # cargar riegos reales acá si se registran en campo

    # 8. Correr el balance de forma incremental
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

    # 9. Guardar CSV histórico y estado
    append_resultados_csv(cfg["rutas"]["salida_csv"], df_resultados)

    # Para el JSON de los agentes hace falta la campaña entera, no solo los días
    # nuevos de esta corrida: se relee el CSV ya consolidado.
    try:
        df_completo_para_json = pd.read_csv(
            cfg["rutas"]["salida_csv"], parse_dates=["fecha"]
        )
    except Exception:  # noqa: BLE001
        df_completo_para_json = df_resultados
    guardar_estado(cfg["rutas"]["estado_json"], estado_nuevo, huella=huella)

    # 10. Mosaico de imágenes NDVI (si está habilitado)
    cfg_mapas = cfg.get("mapas", {})
    if cfg_mapas.get("habilitado", True) and fechas_con_imagen:
        try:
            generar_mosaico(
                cdse_client_id,
                cdse_client_secret,
                aoi_info["geojson"],
                fechas_con_imagen,
                destino=cfg_mapas.get("salida", "docs/ndvi_mosaico.png"),
                columnas=cfg_mapas.get("columnas", 4),
                lado_px=cfg_mapas.get("lado_px", 220),
                titulo=f"NDVI — {cfg.get('telegram', {}).get('nombre_lote', 'Lote')}",
            )
        except Exception as e:  # noqa: BLE001
            # Un fallo acá no debe tumbar la corrida: el balance ya está calculado
            # y guardado, y el mosaico es información complementaria.
            log.warning("No se pudo generar el mosaico de NDVI: %s", e)

    # 11. Notificación de estado / alerta de riego
    if not df_resultados.empty:
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

        # --- Proyección con pronóstico (no toca el estado ni el CSV) ---
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

        # --- JSON para agentes conversacionales (n8n, bots) ---
        try:
            mod_estado_json.publicar(
                mod_estado_json.construir_estado(
                    df_completo_para_json,
                    cfg,
                    aoi_info,
                    estacion_siga=estacion_siga,
                    detalle_clima=detalle_clima,
                    linea_pronostico=linea_pronostico,
                    df_pronostico=df_pronostico_pub,
                    ajuste_riego=ajuste_riego,
                    diag_ndvi=diag_ndvi,
                ),
                destino=cfg.get("rutas", {}).get(
                    "estado_json_publico", "docs/estado_actual.json"),
            )
        except Exception as e:  # noqa: BLE001
            # El balance ya está guardado: que falle el JSON no debe tumbar nada.
            log.warning("No se pudo publicar el estado para agentes: %s", e)

        # --- Telegram ---
        cfg_tg = cfg.get("telegram", {})
        if cfg_tg.get("habilitado", False):
            tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
            tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
            if not tg_token or not tg_chat_id:
                log.warning(
                    "Telegram habilitado en config pero faltan TELEGRAM_BOT_TOKEN / "
                    "TELEGRAM_CHAT_ID. Se omite la notificación."
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
                if enviar_resumen or debe_notificar(
                    float(ultimo["pct_au"]),
                    pct_au_previo,
                    umbral,
                    modo=cfg_tg.get("modo", "cruce"),
                ):
                    lluvia_7d = float(df_resultados.tail(7)["precipitacion_mm"].sum())
                    mensaje = armar_mensaje(
                        fecha=fecha_ultimo,
                        au_real_mm=float(ultimo["au_real_mm"]),
                        au_max_mm=float(ultimo["au_max_mm"]),
                        pct_au=float(ultimo["pct_au"]),
                        ndvi=float(ultimo["ndvi"]),
                        etc_mm=float(ultimo["etc_ajustada_mm"]),
                        precipitacion_7d_mm=lluvia_7d,
                        umbral_riego_pct=umbral,
                        nombre_lote=cfg_tg.get("nombre_lote", "Lote 20"),
                        eficiencia_aplicacion=cfg_tg.get("eficiencia_aplicacion", 0.85),
                        reposicion_objetivo_pct=cfg_tg.get("reposicion_objetivo_pct", 100.0),
                        linea_pronostico=linea_pronostico,
                        ajuste_riego=ajuste_riego,
                    )
                    enviar_telegram(tg_token, tg_chat_id, mensaje)
                else:
                    log.info("Sin condiciones para notificar (modo=%s).", cfg_tg.get("modo", "cruce"))

    log.info("Corrida finalizada OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
