"""
migrar_a_supabase.py
--------------------
Pasa el lote de este repositorio (config.yaml + aoi/ + data/) a Supabase como
un lote de la cuenta indicada, con su serie diaria, su estado, su resumen y su
mosaico. Así la corrida multiusuario sigue desde el último día calculado en
lugar de recalcular la campaña.

    set SUPABASE_URL=https://xxxx.supabase.co
    set SUPABASE_SERVICE_ROLE_KEY=eyJ...
    python -m scripts.migrar_a_supabase --email tu@correo.com

La cuenta tiene que existir: entrar una vez al tablero con ese correo antes de
correr esto. Se puede volver a correr: actualiza el lote del mismo nombre.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.almacen import AlmacenSupabase, EstadoGuardado, config_de_lote  # noqa: E402
from src.aoi import aoi_desde_geojson, cargar_aoi  # noqa: E402
from src.balance import EstadoBalance  # noqa: E402
from src.config import cargar_config  # noqa: E402
from src.estado import huella_corrida  # noqa: E402


def buscar_usuario(almacen: AlmacenSupabase, url: str, email: str) -> dict:
    db = almacen.db
    pagina = 1
    while True:
        r = db._pedir("GET", f"{url.rstrip('/')}/auth/v1/admin/users",
                      params={"page": pagina, "per_page": 200}).json()
        usuarios = r.get("users", r if isinstance(r, list) else [])
        for u in usuarios:
            if (u.get("email") or "").lower() == email.lower():
                return u
        if len(usuarios) < 200:
            sys.exit(f"No hay ninguna cuenta con el correo {email}. "
                     "Entrá una vez al tablero con ese correo y volvé a correr esto.")
        pagina += 1


def huella_en_supabase(cfg: dict, fila: dict) -> str:
    """La huella tal como la va a calcular la corrida a partir de lo guardado."""
    cfg_lote = config_de_lote(cfg, fila)
    aoi = aoi_desde_geojson(fila["geometria"])
    fuentes = [str(f).lower() for f in (cfg_lote.get("clima", {}).get("fuentes") or ["agera5"])]
    return huella_corrida(aoi["geojson"], cfg_lote["campana"]["fecha_siembra"],
                          fuentes_clima=fuentes, suelo=cfg_lote.get("suelo"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--email", required=True, help="correo de la cuenta dueña del lote")
    p.add_argument("--nombre", help="nombre del lote (por defecto, telegram.nombre_lote)")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--sin-serie", action="store_true",
                   help="crear solo el lote y dejar que la corrida calcule la campaña")
    args = p.parse_args()

    url = os.environ.get("SUPABASE_URL", "").strip()
    clave = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not (url and clave):
        sys.exit("Faltan SUPABASE_URL y SUPABASE_SERVICE_ROLE_KEY.")

    cfg = cargar_config(args.config)
    almacen = AlmacenSupabase(cfg, url, clave)
    db = almacen.db

    usuario = buscar_usuario(almacen, url, args.email)
    print(f"Cuenta: {args.email} ({usuario['id']})")

    aoi = cargar_aoi(cfg["aoi"].get("archivo") or cfg["aoi"]["shapefile"])
    tg = cfg.get("telegram", {})
    nombre = args.nombre or tg.get("nombre_lote") or "Lote"
    modo = tg.get("modo", "cruce") if tg.get("habilitado", True) else "nunca"
    datos = {
        "usuario_id": usuario["id"],
        "nombre": nombre,
        "geometria": json.loads(json.dumps(aoi["geojson"])),
        "area_ha": round(aoi["area_ha"], 2),
        "fecha_siembra": str(cfg["campana"]["fecha_siembra"]),
        "au_real_inicial_mm": float(cfg["campana"]["au_real_inicial_mm"]),
        "prof_raiz_inicial_cm": float(cfg["campana"]["prof_raiz_inicial_cm"]),
        "umbral_riego_pct": float(cfg["balance"]["umbral_riego_pct"]),
        "eficiencia_aplicacion": float(tg.get("eficiencia_aplicacion", 0.85)),
        "reposicion_objetivo_pct": float(tg.get("reposicion_objetivo_pct", 100)),
        "modo_aviso": modo,
        "suelo": cfg.get("suelo"),
        "reiniciar": False,
        "activo": True,
    }

    existentes = db.select("lotes", {"select": "id", "usuario_id": f"eq.{usuario['id']}",
                                     "nombre": f"eq.{nombre}"})
    if existentes:
        lote_id = existentes[0]["id"]
        db.update("lotes", {"id": f"eq.{lote_id}"}, datos)
        print(f"Lote «{nombre}» actualizado ({lote_id}).")
    else:
        r = db._pedir("POST", f"{db.rest}/lotes", json=datos,
                      headers={"Prefer": "return=representation"})
        lote_id = r.json()[0]["id"]
        print(f"Lote «{nombre}» creado ({lote_id}), {datos['area_ha']} ha.")

    if args.sin_serie:
        print("Listo. La próxima corrida calcula la campaña desde la siembra.")
        return 0

    fila = db.select("lotes", {"select": "*", "id": f"eq.{lote_id}"})[0]
    lote = almacen.lotes(solo_id=lote_id)[0]
    huella = huella_en_supabase(cfg, fila)

    ruta_estado = Path(cfg["rutas"]["estado_json"])
    ruta_csv = Path(cfg["rutas"]["salida_csv"])
    if not (ruta_estado.exists() and ruta_csv.exists()):
        print("No hay estado.json o CSV que migrar: la corrida calculará desde la siembra.")
        return 0

    estado_local = json.loads(ruta_estado.read_text(encoding="utf-8"))
    if estado_local.get("huella") != huella:
        print("⚠️  La huella del estado local no coincide con la del lote en Supabase "
              f"({estado_local.get('huella')} vs {huella}). Se migra la serie igual, pero "
              "la próxima corrida la va a archivar y recalcular desde la siembra.")

    df = pd.read_csv(ruta_csv, parse_dates=["fecha"])
    guardado = EstadoGuardado(
        balance=EstadoBalance.from_dict(estado_local),
        fechas_con_imagen=list(estado_local.get("fechas_con_imagen") or []),
        serie=1,
    )
    db.delete("balance_diario", {"lote_id": f"eq.{lote_id}"})
    almacen.guardar(lote, df, guardado, estado_local.get("huella") or huella)
    almacen.registrar_corrida(lote)
    print(f"Serie migrada: {len(df)} días, del {df['fecha'].min().date()} al "
          f"{df['fecha'].max().date()}.")

    ruta_resumen = Path(cfg["rutas"].get("estado_json_publico", "docs/estado_actual.json"))
    if ruta_resumen.exists():
        resumen = json.loads(ruta_resumen.read_text(encoding="utf-8"))
        resumen.setdefault("lote", {})["nombre"] = nombre
        almacen.publicar_resumen(lote, resumen)
        print("Resumen para el bot migrado.")

    ruta_mosaico = Path(cfg.get("mapas", {}).get("salida", "docs/ndvi_mosaico.png"))
    if ruta_mosaico.exists():
        almacen.publicar_mosaico(lote, str(ruta_mosaico))
        print("Mosaico de NDVI migrado.")

    print("Listo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
