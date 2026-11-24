"""
notificaciones.py
-----------------
Envío de alertas por Telegram vía Bot API (una sola llamada HTTP, sin
librerías extra más allá de `requests`).

Incluye el cálculo de la lámina de riego sugerida. Ojo con ese número:
es una sugerencia aritmética, no una recomendación agronómica cerrada.
Ver las notas en `calcular_lamina_riego()`.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


# ---------------------------------------------------------------------------
# Clasificación de la condición hídrica
# ---------------------------------------------------------------------------
# Cortes por % de Agua Útil. Son los de uso corriente, pero conviene
# ajustarlos al cultivo y al estado fenológico: un 55% en llenado de grano
# no significa lo mismo que en implantación.
CATEGORIAS = [
    (75, "Óptimo", "🟢"),
    (50, "Adecuado", "🟡"),
    (35, "Déficit", "🟠"),
    (0, "Crítico", "🔴"),
]


def clasificar_condicion(pct_au: float) -> tuple[str, str]:
    """Devuelve (etiqueta, emoji) según el % de Agua Útil."""
    for umbral, etiqueta, emoji in CATEGORIAS:
        if pct_au >= umbral:
            return etiqueta, emoji
    return "Crítico", "🔴"


# ---------------------------------------------------------------------------
# Lámina de riego
# ---------------------------------------------------------------------------
def calcular_lamina_riego(
    au_real_mm: float,
    au_max_mm: float,
    eficiencia_aplicacion: float = 0.85,
    reposicion_objetivo_pct: float = 100.0,
) -> dict:
    """Calcula la lámina de riego para llevar el perfil al objetivo.

    lámina neta  = (AU objetivo) - (AU actual)
    lámina bruta = lámina neta / eficiencia de aplicación

    ADVERTENCIAS (deliberadamente explícitas):
      - No considera el pronóstico de lluvia. Si hay un frente entrando en
        48 h, regar a reposición total es tirar agua y arriesgar
        anegamiento. El número que sale de acá es "cuánto falta HOY",
        no "cuánto conviene aplicar HOY".
      - `reposicion_objetivo_pct` < 100 permite riego deficitario
        controlado (estrategia habitual cuando el agua es limitante o el
        cultivo está en una etapa poco sensible).
      - La eficiencia por defecto (0.85) es un valor genérico de pivote en
        buenas condiciones. Aspersión con viento, o surcos, pueden estar
        bastante por debajo. Ajustar en config.yaml.
      - No contempla la capacidad real del equipo ni la lámina máxima que
        el suelo puede infiltrar sin escurrimiento.
    """
    au_objetivo = au_max_mm * (reposicion_objetivo_pct / 100.0)
    lamina_neta = max(0.0, au_objetivo - au_real_mm)
    lamina_bruta = lamina_neta / eficiencia_aplicacion if eficiencia_aplicacion > 0 else 0.0
    return {
        "lamina_neta_mm": round(lamina_neta, 1),
        "lamina_bruta_mm": round(lamina_bruta, 1),
        "au_objetivo_mm": round(au_objetivo, 1),
    }


# ---------------------------------------------------------------------------
# Armado del mensaje
# ---------------------------------------------------------------------------
def armar_mensaje(
    fecha,
    au_real_mm: float,
    au_max_mm: float,
    pct_au: float,
    ndvi: float,
    etc_mm: float,
    precipitacion_7d_mm: float,
    umbral_riego_pct: float,
    nombre_lote: str = "Lote 20",
    eficiencia_aplicacion: float = 0.85,
    reposicion_objetivo_pct: float = 100.0,
    linea_pronostico: str | None = None,
    ajuste_riego: dict | None = None,
) -> str:
    etiqueta, emoji = clasificar_condicion(pct_au)
    deficit_mm = round(max(0.0, au_max_mm - au_real_mm), 1)

    lineas = [
        # El nombre lo escribe el usuario: con "&" o "<" sin escapar, Telegram
        # rechaza el mensaje entero (parse_mode HTML).
        f"{emoji} <b>Balance Hídrico — {escape(str(nombre_lote))}</b>",
        f"<i>Datos al {fecha}</i>",
        "",
        f"<b>Condición:</b> {etiqueta} ({pct_au:.0f}% de Agua Útil)",
        f"<b>Agua útil actual:</b> {au_real_mm:.1f} mm de {au_max_mm:.1f} mm",
        f"<b>Déficit respecto a CC:</b> {deficit_mm:.1f} mm",
        "",
        f"NDVI: {ndvi:.2f}  |  ETc del día: {etc_mm:.1f} mm",
        f"Lluvia últimos 7 días: {precipitacion_7d_mm:.1f} mm",
    ]

    if pct_au < umbral_riego_pct:
        riego = calcular_lamina_riego(
            au_real_mm, au_max_mm, eficiencia_aplicacion, reposicion_objetivo_pct
        )
        lineas += [
            "",
            f"💧 <b>Riego</b> (por debajo del umbral de {umbral_riego_pct:.0f}%)",
            "",
            "<b>1 · Reposición total</b> — lo que falta hoy, sin mirar el cielo",
            f"   Neta: <b>{riego['lamina_neta_mm']:.1f} mm</b>  |  "
            f"Bruta: <b>{riego['lamina_bruta_mm']:.1f} mm</b> "
            f"(ef. {eficiencia_aplicacion:.0%})",
        ]
        if ajuste_riego:
            # El JSON guarda la fecha en ISO; el productor la lee en dd/mm.
            try:
                a, m, d = str(ajuste_riego["hasta"]).split("-")
                hasta = f"{int(d)}/{int(m)}"
            except ValueError:
                hasta = str(ajuste_riego["hasta"])
            lineas += [
                "",
                f"<b>2 · Ajustada al pronóstico</b> — descontando la lluvia "
                f"esperada hasta el {hasta}",
                f"   Neta: <b>{ajuste_riego['lamina_neta_mm']:.1f} mm</b>  |  "
                f"Bruta: <b>{ajuste_riego['lamina_bruta_mm']:.1f} mm</b>",
                f"   <i>Se descontaron {ajuste_riego['descuento_mm']:.1f} mm "
                f"efectivos de los {ajuste_riego['lluvia_pronosticada_mm']:.1f} mm "
                f"pronosticados en {ajuste_riego['ventana_dias']} días.</i>",
                "",
                "<i>La ajustada apuesta a que esa lluvia llega. Si no llega, "
                "falta reponer la diferencia.</i>",
            ]
        else:
            lineas += [
                "",
                "<i>Verificar pronóstico antes de aplicar: la lámina no "
                "contempla lluvias previstas.</i>",
            ]
    else:
        lineas += ["", "Sin necesidad de riego por ahora."]

    if linea_pronostico:
        lineas += ["", f"🔭 <i>{linea_pronostico}</i>"]

    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Envío
# ---------------------------------------------------------------------------
def enviar_telegram(token: str, chat_id: str, mensaje: str) -> bool:
    """Envía el mensaje. Devuelve True si salió bien.

    No lanza excepción a propósito: que falle la notificación no debe
    tumbar la corrida del balance (el dato ya quedó guardado en el CSV).
    """
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": mensaje,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if resp.status_code == 200:
            logger.info("Notificación de Telegram enviada.")
            return True
        logger.error("Telegram respondió %s: %s", resp.status_code,
                     _sin_token(resp.text, token))
        return False
    except Exception as e:  # noqa: BLE001
        # El error de requests trae la URL, y la URL lleva el token del bot.
        # El log se sube como artifact de un repo público: nunca el token.
        logger.error("No se pudo enviar la notificación de Telegram: %s",
                     _sin_token(str(e), token))
        return False


def _sin_token(texto: str, token: str) -> str:
    return texto.replace(token, "<TOKEN>") if token else texto


def debe_notificar(
    pct_au_hoy: float,
    pct_au_previo: Optional[float],
    umbral_riego_pct: float,
    modo: str = "cruce",
) -> bool:
    """Decide si corresponde mandar mensaje.

    modo="cruce"   -> solo cuando cruza el umbral hacia abajo (evita el
                      mensaje diario repetido mientras sigue en déficit).
    modo="siempre" -> en cada corrida con días nuevos.
    modo="deficit" -> todos los días que esté por debajo del umbral.
    """
    if modo == "siempre":
        return True
    if modo == "deficit":
        return pct_au_hoy < umbral_riego_pct
    # modo "cruce" (por defecto)
    if pct_au_hoy >= umbral_riego_pct:
        return False
    if pct_au_previo is None:
        return True  # primera corrida y ya está en déficit
    return pct_au_previo >= umbral_riego_pct
