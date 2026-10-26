"""
avisos_admin.py
---------------
Avisa a los administradores cuando alguien pide acceso, para que lo aprueben
desde el tablero (pestaña Usuarios) sin tener que entrar a Supabase.

Canales, los que estén configurados:
  · correo, por SMTP (SMTP_USUARIO / SMTP_CLAVE; con Gmail, una contraseña de
    aplicación), a la dirección de cada administrador;
  · Telegram, al chat que el administrador vinculó desde el tablero.

Un pedido se marca como avisado solo si salió por al menos un canal: si no hay
ninguno configurado o fallan todos, se reintenta en la corrida siguiente.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from html import escape

from .notificaciones import enviar_telegram

logger = logging.getLogger(__name__)

URL_TABLERO = "https://diegopons.github.io/Agrobot/"


def _texto(pedidos: list[dict], url: str) -> str:
    lineas = [f"{'Hay un pedido' if len(pedidos) == 1 else f'Hay {len(pedidos)} pedidos'} "
              "de acceso a Agrobot:", ""]
    for p in pedidos:
        lineas.append(f"• {p.get('nombre') or '(sin nombre)'} <{p.get('email')}>")
        if p.get("organizacion"):
            lineas.append(f"  Organización o actividad: {p['organizacion']}")
        if p.get("motivo"):
            lineas.append(f"  Para qué lo usaría: {p['motivo']}")
        lineas.append("")
    lineas.append(f"Para aprobarlo, entrá al tablero → pestaña Usuarios:\n{url}#usuarios")
    return "\n".join(lineas)


def _enviar_correo(destinos: list[str], asunto: str, cuerpo: str) -> bool:
    usuario = os.environ.get("SMTP_USUARIO", "").strip()
    clave = os.environ.get("SMTP_CLAVE", "").replace(" ", "").strip()
    if not (usuario and clave and destinos):
        return False
    msg = EmailMessage()
    msg["Subject"] = asunto
    msg["From"] = f"Agrobot <{usuario}>"
    msg["To"] = ", ".join(destinos)
    msg.set_content(cuerpo)
    try:
        host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
        with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PUERTO", "465")), timeout=30) as s:
            s.login(usuario, clave)
            s.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.warning("No se pudo mandar el aviso por correo: %s", e)
        return False


def avisar_aprobaciones(almacen) -> int:
    """Correo con el enlace para crear la contraseña a quien un administrador
    acaba de habilitar (y un Telegram, si lo vinculó).

    El enlace va solo por correo: un chat de Telegram puede ser un grupo. Si el
    correo no sale, no se marca y se reintenta con un enlace nuevo.
    """
    from .almacen import ErrorSupabase
    import requests

    aprobados = almacen.aprobados_sin_avisar()
    if not aprobados:
        return 0
    url = os.environ.get("AGROBOT_URL_TABLERO", URL_TABLERO).rstrip("/") + "/"
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    avisados = []
    for p in aprobados:
        if not p.get("email"):
            continue
        try:
            enlace = almacen.enlace_para_crear_clave(p, url)
        except (ErrorSupabase, requests.RequestException) as e:
            logger.warning("No se pudo generar el enlace de contraseña para %s: %s", p["email"], e)
            continue
        saludo = f"Hola{' ' + p['nombre'] if p.get('nombre') else ''}:"
        texto = (f"{saludo}\n\n"
                 "Tu cuenta de Agrobot ya está habilitada. Para entrar, creá tu contraseña con "
                 "este enlace (sirve una sola vez):\n\n"
                 f"{enlace}\n\n"
                 f"Después entrás siempre desde {url}, pestaña Ingresar, con tu correo y esa "
                 "contraseña. Si el enlace venció, pedí uno nuevo ahí mismo con "
                 "\"¿Te olvidaste la contraseña?\".\n\n"
                 "Dibujá tu lote y cargá la fecha de siembra: en menos de una hora tenés el "
                 "balance calculado.\n")
        if not _enviar_correo([p["email"]], "Tu cuenta de Agrobot está habilitada", texto):
            continue
        if token and p.get("telegram_chat_id"):
            enviar_telegram(token, p["telegram_chat_id"], escape(
                f"{saludo} tu cuenta de Agrobot ya está habilitada. Te mandamos a {p['email']} "
                "el enlace para crear tu contraseña."))
        avisados.append(p["id"])
    if avisados:
        almacen.marcar_aprobacion_avisada(avisados)
        logger.info("Aviso de cuenta habilitada enviado a %d usuario(s).", len(avisados))
    return len(avisados)


def avisar_pedidos_de_acceso(almacen) -> int:
    """Avisa los pedidos nuevos. Devuelve cuántos quedaron avisados."""
    pedidos = almacen.pedidos_sin_avisar()
    if not pedidos:
        return 0
    admins = almacen.administradores()
    url = os.environ.get("AGROBOT_URL_TABLERO", URL_TABLERO).rstrip("/") + "/"
    texto = _texto(pedidos, url)

    salio = _enviar_correo([a["email"] for a in admins if a.get("email")],
                           f"Agrobot: {len(pedidos)} pedido(s) de acceso", texto)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        html = escape(texto).replace(f"{url}#usuarios", f'<a href="{url}#usuarios">abrir el tablero</a>')
        for a in admins:
            if a.get("telegram_chat_id") and enviar_telegram(token, a["telegram_chat_id"], html):
                salio = True

    if not salio:
        logger.info("Hay %d pedido(s) de acceso sin avisar: no hay correo ni Telegram de "
                    "administrador configurado. Se ven en la pestaña Usuarios.", len(pedidos))
        return 0
    almacen.marcar_avisados([p["id"] for p in pedidos])
    logger.info("Aviso de %d pedido(s) de acceso enviado a los administradores.", len(pedidos))
    return len(pedidos)
