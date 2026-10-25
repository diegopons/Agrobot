"""
Pruebas de la corrida para varios usuarios y lotes, sin red.

NDVI, clima, pronóstico, mosaico y Telegram se reemplazan por versiones
falsas; Supabase, por una base en memoria que entiende los filtros que usa
src/almacen.py. Lo que se prueba es el orquestador y el almacén: que cada
lote tenga su propia serie y su propio estado, que un lote roto no frene a
los demás, que la corrida sea incremental y que cada aviso vaya al dueño.

    python -m pytest tests -q
"""
from __future__ import annotations

import copy
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
import yaml

import src.main as main_mod
from src import almacen as mod_almacen
from src.almacen import AlmacenSupabase

HOY = pd.Timestamp(date.today())

CUADRADO = {
    "type": "Polygon",
    "coordinates": [[[-63.75, -31.88], [-63.74, -31.88], [-63.74, -31.87],
                     [-63.75, -31.87], [-63.75, -31.88]]],
}


# ---------------------------------------------------------------------------
# Fuentes de datos falsas
# ---------------------------------------------------------------------------
def _ndvi_falso(cid, csec, geojson, desde, hasta, **kw):
    fechas = pd.date_range(desde, hasta, freq="D")
    ndvi = [0.2 + 0.5 * (i % 30) / 30 if f.day % 5 == 0 else np.nan
            for i, f in enumerate(fechas)]
    df = pd.DataFrame({"fecha": fechas, "ndvi": ndvi})
    df.attrs["fechas_con_dato"] = int(df["ndvi"].notna().sum())
    df.attrs["descartes"] = {}
    return df


def _fuentes_falsas(cfg, bbox, desde, hasta):
    def obtener():
        fechas = pd.date_range(desde, hasta, freq="D")
        return pd.DataFrame({
            "fecha": fechas,
            "precipitacion_mm": [12.0 if f.day % 9 == 0 else 0.0 for f in fechas],
            "eto_mm": [4.0] * len(fechas),
        })
    return [("openmeteo", obtener)], None


def _pronostico_falso(bbox, dias=10, zona_horaria=None):
    fechas = pd.date_range(HOY, periods=dias, freq="D")
    return pd.DataFrame({"fecha": fechas, "precipitacion_mm": 1.0, "eto_mm": 5.0})


# ---------------------------------------------------------------------------
# Supabase en memoria
# ---------------------------------------------------------------------------
class BaseFalsa:
    CLAVES = {
        "lotes": ("id",), "perfiles": ("id",), "estado_lote": ("lote_id",),
        "balance_diario": ("lote_id", "serie", "fecha"), "resumen_lote": ("lote_id",),
    }
    DEFAULTS = {"estado_lote": {"serie": 1, "fechas_con_imagen": []}}

    def __init__(self):
        self.t = {k: [] for k in self.CLAVES}
        self.archivos = {}

    @staticmethod
    def _cumple(fila, filtros):
        for k, v in filtros.items():
            if k in ("select", "order", "limit", "offset", "on_conflict"):
                continue
            assert v.startswith("eq."), v
            esperado = v[3:]
            actual = fila.get(k)
            if isinstance(actual, bool):
                actual = str(actual).lower()
            if str(actual) != esperado:
                return False
        return True

    def select(self, tabla, params):
        filas = [copy.deepcopy(f) for f in self.t[tabla] if self._cumple(f, params)]
        if tabla == "lotes" and "perfil:perfiles" in params.get("select", ""):
            perfiles = {p["id"]: p for p in self.t["perfiles"]}
            for f in filas:
                p = perfiles.get(f["usuario_id"], {})
                f["perfil"] = {"nombre": p.get("nombre"),
                               "telegram_chat_id": p.get("telegram_chat_id")}
        if "order" in params:
            campo = params["order"].split(".")[0]
            filas.sort(key=lambda f: str(f.get(campo)))
        return filas

    def upsert(self, tabla, filas, on_conflict):
        claves = on_conflict.split(",")
        for nueva in filas:
            for f in self.t[tabla]:
                if all(str(f.get(k)) == str(nueva.get(k)) for k in claves):
                    f.update(copy.deepcopy(nueva))
                    break
            else:
                fila = copy.deepcopy(self.DEFAULTS.get(tabla, {}))
                fila.update(copy.deepcopy(nueva))
                self.t[tabla].append(fila)

    def update(self, tabla, filtros, datos):
        for f in self.t[tabla]:
            if self._cumple(f, filtros):
                f.update(datos)

    def delete(self, tabla, filtros):
        self.t[tabla] = [f for f in self.t[tabla] if not self._cumple(f, filtros)]

    def subir(self, bucket, ruta, contenido, tipo):
        self.archivos[f"{bucket}/{ruta}"] = contenido


def _lote(id_, usuario, nombre, siembra, geometria=CUADRADO, **extra):
    fila = {
        "id": id_, "usuario_id": usuario, "nombre": nombre, "geometria": geometria,
        "area_ha": 100.0, "fecha_siembra": siembra, "au_real_inicial_mm": 40.0,
        "prof_raiz_inicial_cm": 1.0, "umbral_riego_pct": 50.0,
        "eficiencia_aplicacion": 0.85, "reposicion_objetivo_pct": 100.0,
        "modo_aviso": "siempre", "suelo": None, "parametros": {}, "activo": True,
        "reiniciar": False, "creado": f"2026-10-0{id_[-1]}",
    }
    fila.update(extra)
    return fila


# ---------------------------------------------------------------------------
# Preparación
# ---------------------------------------------------------------------------
@pytest.fixture
def entorno(tmp_path, monkeypatch):
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    cfg["rutas"] = {
        "estado_json": str(tmp_path / "data/estado.json"),
        "salida_csv": str(tmp_path / "data/serie.csv"),
        "estado_json_publico": str(tmp_path / "docs/estado_actual.json"),
        "log_file": str(tmp_path / "logs/bh.log"),
    }
    cfg["aoi"] = {"archivo": str(tmp_path / "lote.geojson")}
    cfg["mapas"]["salida"] = str(tmp_path / "docs/mosaico.png")
    (tmp_path / "lote.geojson").write_text(
        '{"type":"Feature","properties":{},"geometry":%s}' % __import__("json").dumps(CUADRADO))
    ruta_cfg = tmp_path / "config.yaml"
    ruta_cfg.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    enviados = []

    def mosaico_falso(cid, csec, geo, fechas, destino, **kw):
        open(destino, "wb").write(b"PNG" + ",".join(fechas).encode())
        return True

    monkeypatch.setattr(main_mod, "obtener_serie_ndvi", _ndvi_falso)
    monkeypatch.setattr(main_mod.mod_clima, "armar_fuentes", _fuentes_falsas)
    monkeypatch.setattr(main_mod, "descargar_pronostico", _pronostico_falso)
    monkeypatch.setattr(main_mod, "generar_mosaico", mosaico_falso)
    monkeypatch.setattr(main_mod, "enviar_telegram",
                        lambda tok, chat, msg: enviados.append((chat, msg)) or True)
    for var in ("BH_LOTE_ID", "BH_REINICIAR_ESTADO", "BH_ENVIAR_RESUMEN", "BH_FECHA_FIN",
                "BH_ALMACEN", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CDSE_CLIENT_ID", "x")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "y")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")

    return {"cfg": str(ruta_cfg), "enviados": enviados, "tmp": tmp_path}


@pytest.fixture
def supabase(entorno, monkeypatch):
    base = BaseFalsa()
    base.t["perfiles"] = [
        {"id": "u-ana", "nombre": "ana", "telegram_chat_id": "111"},
        {"id": "u-beto", "nombre": "beto", "telegram_chat_id": "222"},
    ]
    base.t["lotes"] = [
        _lote("l1", "u-ana", "Norte", "2026-09-01"),
        _lote("l2", "u-ana", "Sur", "2026-09-20", modo_aviso="nunca"),
        _lote("l3", "u-beto", "Bajo", "2026-09-10"),
        # Polígono inválido: tiene que fallar solo, sin frenar al resto
        _lote("l4", "u-beto", "Roto", "2026-09-10",
              geometria={"type": "Polygon", "coordinates": []}),
    ]

    def crear(cfg):
        a = AlmacenSupabase(cfg, "http://supabase.falso", "clave")
        a.db = base
        return a

    monkeypatch.setattr(main_mod, "crear_almacen", crear)
    return base


def _serie(base, lote_id, serie=None):
    filas = [f for f in base.t["balance_diario"] if f["lote_id"] == lote_id
             and (serie is None or f["serie"] == serie)]
    return sorted(filas, key=lambda f: f["fecha"])


def _estado(base, lote_id):
    return next(f for f in base.t["estado_lote"] if f["lote_id"] == lote_id)


# ---------------------------------------------------------------------------
# Pruebas
# ---------------------------------------------------------------------------
def test_corre_todos_los_lotes_de_todos_los_usuarios(entorno, supabase):
    codigo = main_mod.main(entorno["cfg"])

    # El lote roto hace que la corrida termine en 1, pero los demás corrieron
    assert codigo == 1
    hasta = str((HOY - timedelta(days=2)).date())   # rezago de NDVI
    for lote_id, siembra in (("l1", "2026-09-01"), ("l2", "2026-09-20"), ("l3", "2026-09-10")):
        filas = _serie(supabase, lote_id)
        assert filas[0]["fecha"] == siembra
        assert filas[-1]["fecha"] == hasta
        assert len(filas) == len(pd.date_range(siembra, hasta))
        assert all(f["serie"] == 1 for f in filas)
        assert filas[-1]["fuente_pp"] == "openmeteo"     # el origen ya no se pierde
        est = _estado(supabase, lote_id)
        assert est["ultima_fecha_procesada"] == hasta
        assert est["ultimo_error"] is None
        assert est["huella"]

    assert _estado(supabase, "l4")["ultimo_error"]
    assert not _serie(supabase, "l4")

    # Un resumen por lote, con el nombre del lote
    resumenes = {r["lote_id"]: r for r in supabase.t["resumen_lote"]}
    assert set(resumenes) == {"l1", "l2", "l3"}
    assert resumenes["l3"]["resumen"]["lote"]["nombre"] == "Bajo"
    assert resumenes["l1"]["fecha_del_dato"] == hasta

    # El mosaico va a la carpeta del dueño
    assert set(supabase.archivos) == {
        "mosaicos/u-ana/l1.png", "mosaicos/u-ana/l2.png", "mosaicos/u-beto/l3.png"}

    # Cada aviso al chat del dueño; "Sur" tiene los avisos apagados
    chats = [c for c, _ in entorno["enviados"]]
    assert sorted(chats) == ["111", "222"]
    assert any("Norte" in m for c, m in entorno["enviados"] if c == "111")
    assert any("Bajo" in m for c, m in entorno["enviados"] if c == "222")


def test_la_segunda_corrida_no_reprocesa(entorno, supabase):
    main_mod.main(entorno["cfg"])
    antes = len(supabase.t["balance_diario"])
    entorno["enviados"].clear()

    main_mod.main(entorno["cfg"])

    assert len(supabase.t["balance_diario"]) == antes
    assert entorno["enviados"] == []


def test_la_serie_sigue_desde_el_estado_guardado(entorno, supabase):
    main_mod.main(entorno["cfg"])
    # Se simula que la última corrida fue hace 5 días
    corte = str((HOY - timedelta(days=7)).date())
    supabase.t["balance_diario"] = [f for f in supabase.t["balance_diario"]
                                    if not (f["lote_id"] == "l1" and f["fecha"] > corte)]
    ultima = _serie(supabase, "l1")[-1]
    est = _estado(supabase, "l1")
    est.update(ultima_fecha_procesada=corte, au_real_mm=ultima["au_real_mm"],
               prof_raiz_cm=ultima["prof_raiz_cm"])

    main_mod.main(entorno["cfg"])

    filas = _serie(supabase, "l1")
    fechas = [f["fecha"] for f in filas]
    assert fechas == sorted(set(fechas))                       # sin duplicados
    assert fechas[-1] == str((HOY - timedelta(days=2)).date())  # completó los 5 días
    assert _estado(supabase, "l1")["serie"] == 1               # misma serie, no reinició


def test_cambiar_la_siembra_abre_una_serie_nueva(entorno, supabase):
    main_mod.main(entorno["cfg"])
    filas_serie1 = len(_serie(supabase, "l1", 1))
    supabase.update("lotes", {"id": "eq.l1"}, {"fecha_siembra": "2026-09-15"})

    main_mod.main(entorno["cfg"])

    assert _estado(supabase, "l1")["serie"] == 2
    nueva = _serie(supabase, "l1", 2)
    assert nueva[0]["fecha"] == "2026-09-15"
    # La serie anterior no se borra
    assert len(_serie(supabase, "l1", 1)) == filas_serie1
    # Los otros lotes no se tocan
    assert _estado(supabase, "l3")["serie"] == 1


def test_reiniciar_desde_el_tablero(entorno, supabase):
    main_mod.main(entorno["cfg"])
    supabase.update("lotes", {"id": "eq.l3"}, {"reiniciar": True})

    main_mod.main(entorno["cfg"])

    assert _estado(supabase, "l3")["serie"] == 2
    assert _serie(supabase, "l3", 2)
    lote = next(f for f in supabase.t["lotes"] if f["id"] == "l3")
    assert lote["reiniciar"] is False                 # el pedido queda atendido
    assert _estado(supabase, "l1")["serie"] == 1


def test_reiniciar_sin_lote_no_recalcula_a_todos(entorno, supabase, monkeypatch):
    main_mod.main(entorno["cfg"])
    monkeypatch.setenv("BH_REINICIAR_ESTADO", "true")

    main_mod.main(entorno["cfg"])
    assert all(_estado(supabase, l)["serie"] == 1 for l in ("l1", "l2", "l3"))

    monkeypatch.setenv("BH_LOTE_ID", "l2")
    main_mod.main(entorno["cfg"])
    assert _estado(supabase, "l2")["serie"] == 2
    assert _estado(supabase, "l1")["serie"] == 1


def test_el_suelo_del_config_no_se_hereda(entorno):
    cfg = yaml.safe_load(open(entorno["cfg"], encoding="utf-8"))
    assert cfg.get("suelo")
    sin = mod_almacen.config_de_lote(cfg, _lote("l9", "u", "X", "2026-09-01"))
    assert "suelo" not in sin
    perfil = [{"prof_hasta_cm": 200, "cc_porcentaje": 30, "pmp_porcentaje": 12,
               "densidad_aparente": 1.3}]
    con = mod_almacen.config_de_lote(cfg, _lote("l9", "u", "X", "2026-09-01", suelo=perfil,
                                                parametros={"balance": {"kc_a": 1.5},
                                                            "rutas": {"log_file": "x"}}))
    assert con["suelo"] == perfil
    assert con["balance"]["kc_a"] == 1.5
    assert con["balance"]["umbral_riego_pct"] == 50.0
    assert con["rutas"] == cfg["rutas"]        # las rutas no se ajustan por lote


def test_modo_local_sigue_funcionando(entorno, monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    codigo = main_mod.main(entorno["cfg"])

    assert codigo == 0
    tmp = entorno["tmp"]
    df = pd.read_csv(tmp / "data/serie.csv")
    assert df["fecha"].iloc[0] == "2026-05-28"          # siembra de config.yaml
    assert "fuente_pp" in df.columns
    assert (tmp / "docs/estado_actual.json").exists()
    assert (tmp / "docs/mosaico.png").exists()
    estado = __import__("json").loads((tmp / "data/estado.json").read_text(encoding="utf-8"))
    assert estado["fechas_con_imagen"]
    assert [c for c, _ in entorno["enviados"]] == ["999"]

    # Segunda corrida: nada nuevo, y el estado con fechas extra se lee bien
    assert main_mod.main(entorno["cfg"]) == 0
    assert len(pd.read_csv(tmp / "data/serie.csv")) == len(df)
