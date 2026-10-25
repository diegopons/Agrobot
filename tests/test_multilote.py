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
        "riegos": ("lote_id", "fecha"),
    }
    DEFAULTS = {"estado_lote": {"serie": 1, "fechas_con_imagen": []}}
    OPERADORES = {"eq": lambda a, b: a == b, "lt": lambda a, b: a < b,
                  "lte": lambda a, b: a <= b, "gte": lambda a, b: a >= b}

    def __init__(self):
        self.t = {k: [] for k in self.CLAVES}
        self.archivos = {}

    @classmethod
    def _cumple(cls, fila, filtros):
        condiciones = []
        for k, v in filtros.items():
            if k in ("select", "order", "limit", "offset", "on_conflict"):
                continue
            if k == "and":          # and=(fecha.gte.X,fecha.lte.Y)
                for parte in v.strip("()").split(","):
                    campo, op, valor = parte.split(".", 2)
                    condiciones.append((campo, op, valor))
            else:
                op, valor = v.split(".", 1)
                condiciones.append((k, op, valor))
        for campo, op, esperado in condiciones:
            actual = fila.get(campo)
            if isinstance(actual, bool):
                actual = str(actual).lower()
            if not cls.OPERADORES[op](str(actual), esperado):
                return False
        return True

    def select(self, tabla, params):
        filas = [copy.deepcopy(f) for f in self.t[tabla] if self._cumple(f, params)]
        if tabla == "lotes" and "perfil:perfiles" in params.get("select", ""):
            perfiles = {p["id"]: p for p in self.t["perfiles"]}
            estados = {e["lote_id"]: e for e in self.t["estado_lote"]}
            for f in filas:
                p = perfiles.get(f["usuario_id"], {})
                f["perfil"] = {"nombre": p.get("nombre"),
                               "telegram_chat_id": p.get("telegram_chat_id")}
                e = estados.get(f["id"])
                f["estado"] = e and {k: e.get(k) for k in
                                     ("ultima_fecha_procesada", "ultima_corrida")}
        if "order" in params:
            campo, sentido = (params["order"].split(".") + ["asc"])[:2]
            filas.sort(key=lambda f: str(f.get(campo)), reverse=sentido == "desc")
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
                if tabla == "lotes":          # el trigger lotes_actualizado
                    f["actualizado"] = mod_almacen._ahora()

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
        "actualizado": "2026-10-01T00:00:00+00:00",
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
                "BH_ALMACEN", "BH_SOLO_PENDIENTES", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CDSE_CLIENT_ID", "x")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "y")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")

    # La Carta de Suelos (IDECOR) simulada: anota cada consulta
    carta = {"consultas": [], "falla": False, "au_2m": 300.0}

    def consultar_falso(geojson, url=None, min_cobertura_pct=50):
        carta["consultas"].append(geojson)
        if carta["falla"]:
            raise main_mod.mod_suelo_carta.ErrorCarta("IDECOR caído")
        return {"huella_geometria": main_mod.mod_suelo_carta.huella_geometria(geojson),
                "unidades": [{"simbolo": "Ot", "nombre": "Consociación ONCATIVO", "escala": "1:50.000",
                              "pct": 100.0, "au_1m": 160, "au_1_5m": 231, "au_2m": carta["au_2m"]}],
                "cobertura_pct": 100.0, "au_2m": carta["au_2m"],
                "perfil": main_mod.mod_suelo_carta.perfil_desde_au(160, 231, carta["au_2m"])}
    monkeypatch.setattr(main_mod.mod_suelo_carta, "consultar", consultar_falso)

    return {"cfg": str(ruta_cfg), "enviados": enviados, "tmp": tmp_path, "carta": carta}


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


def test_la_corrida_horaria_solo_atiende_lotes_nuevos_o_modificados(entorno, supabase, monkeypatch):
    main_mod.main(entorno["cfg"])
    monkeypatch.setenv("BH_SOLO_PENDIENTES", "true")
    corridas = {e["lote_id"]: e["ultima_corrida"] for e in supabase.t["estado_lote"]}

    # Sin cambios: no toca ningún lote (el roto sí, porque nunca llegó a calcular)
    main_mod.main(entorno["cfg"])
    for lote_id in ("l1", "l2", "l3"):
        assert _estado(supabase, lote_id)["ultima_corrida"] == corridas[lote_id]

    # Un lote nuevo y uno modificado: solo esos dos
    supabase.t["lotes"].append(_lote("l5", "u-beto", "Nuevo", "2026-10-01"))
    supabase.update("lotes", {"id": "eq.l1"}, {"fecha_siembra": "2026-09-05"})
    main_mod.main(entorno["cfg"])

    assert _serie(supabase, "l5")
    assert _estado(supabase, "l1")["serie"] == 2
    for lote_id in ("l2", "l3"):
        assert _estado(supabase, lote_id)["ultima_corrida"] == corridas[lote_id]


def test_el_riego_entra_como_la_lluvia_efectiva():
    from src.balance import precipitacion_efectiva
    assert precipitacion_efectiva(10) == 10
    assert precipitacion_efectiva(30) == pytest.approx(2.43 * 30 ** 0.667)


def test_los_riegos_cargados_entran_al_balance(entorno, supabase):
    dia = str((HOY - timedelta(days=10)).date())
    supabase.t["riegos"] = [{"lote_id": "l3", "fecha": dia, "mm": 30.0}]

    main_mod.main(entorno["cfg"])

    fila = next(f for f in _serie(supabase, "l3") if f["fecha"] == dia)
    assert fila["riego_mm"] == 30.0
    assert fila["riego_efectivo_mm"] == pytest.approx(2.43 * 30 ** 0.667, abs=0.01)
    # el de otro lote no recibe ese riego
    assert all(f["riego_mm"] == 0 for f in _serie(supabase, "l1"))


def test_un_riego_cargado_tarde_recalcula_desde_ese_dia(entorno, supabase, monkeypatch):
    main_mod.main(entorno["cfg"])
    antes = {f["fecha"]: f for f in _serie(supabase, "l1")}
    dia = str((HOY - timedelta(days=8)).date())

    # Lo que hace el trigger de la tabla riegos al cargar uno desde el tablero
    supabase.t["riegos"] = [{"lote_id": "l1", "fecha": dia, "mm": 12.0}]
    supabase.update("lotes", {"id": "eq.l1"}, {"recalcular_desde": dia})
    monkeypatch.setenv("BH_SOLO_PENDIENTES", "true")
    main_mod.main(entorno["cfg"])

    despues = {f["fecha"]: f for f in _serie(supabase, "l1")}
    assert set(despues) == set(antes)                       # mismos días, sin duplicar
    assert _estado(supabase, "l1")["serie"] == 1            # misma campaña
    assert despues[dia]["riego_mm"] == 12.0
    assert despues[dia]["au_real_mm"] >= antes[dia]["au_real_mm"]
    previo = str((HOY - timedelta(days=9)).date())
    assert despues[previo] == antes[previo]                 # lo anterior no se toca
    assert next(f for f in supabase.t["lotes"] if f["id"] == "l1")["recalcular_desde"] is None
    # los otros lotes no se recalcularon
    assert _estado(supabase, "l3")["ultima_corrida"] is not None


def test_los_lotes_sin_calicata_toman_el_suelo_de_la_carta(entorno, supabase):
    perfil = [{"prof_hasta_cm": 200, "cc_porcentaje": 30, "pmp_porcentaje": 12,
               "densidad_aparente": 1.3}]
    supabase.update("lotes", {"id": "eq.l2"}, {"suelo": perfil})

    main_mod.main(entorno["cfg"])

    # una consulta por lote sin calicata (l1, l3 y el roto falla antes)
    assert len(entorno["carta"]["consultas"]) == 2
    assert _estado(supabase, "l1")["suelo_carta"]["au_2m"] == 300.0
    assert "suelo_carta" not in _estado(supabase, "l2") or not _estado(supabase, "l2").get("suelo_carta")
    res = {r["lote_id"]: r["resumen"] for r in supabase.t["resumen_lote"]}
    assert res["l1"]["perfil_de_suelo"]["origen"].startswith("Carta de Suelos")
    assert res["l2"]["perfil_de_suelo"]["origen"].startswith("calicata")
    # el AU máximo a la profundidad de raíz sale del perfil de la carta
    from src.suelo import obtener_au_max_para_profundidad, precalcular_parametros_suelo
    df = precalcular_parametros_suelo(main_mod.mod_suelo_carta.perfil_desde_au(160, 231, 300.0))
    ultima = _serie(supabase, "l1")[-1]
    assert ultima["au_max_mm"] == pytest.approx(
        obtener_au_max_para_profundidad(ultima["prof_raiz_cm"], df), abs=0.05)


def test_la_carta_se_consulta_una_vez_por_poligono(entorno, supabase):
    main_mod.main(entorno["cfg"])
    entorno["carta"]["consultas"].clear()
    supabase.update("lotes", {"id": "eq.l3"}, {"reiniciar": True})
    main_mod.main(entorno["cfg"])
    assert entorno["carta"]["consultas"] == []          # ya estaba guardada

    otro = {"type": "Polygon", "coordinates": [[[-63.76, -31.88], [-63.74, -31.88],
                                                [-63.74, -31.86], [-63.76, -31.88]]]}
    supabase.update("lotes", {"id": "eq.l3"}, {"geometria": otro})
    main_mod.main(entorno["cfg"])
    assert len(entorno["carta"]["consultas"]) == 1      # cambió el polígono


def test_si_idecor_se_cae_usa_el_perfil_por_defecto_y_reintenta(entorno, supabase):
    entorno["carta"]["falla"] = True
    main_mod.main(entorno["cfg"])
    assert not _estado(supabase, "l1").get("suelo_carta")
    assert _estado(supabase, "l1")["serie"] == 1
    res = {r["lote_id"]: r["resumen"] for r in supabase.t["resumen_lote"]}
    assert res["l1"]["perfil_de_suelo"]["motivo"] == "IDECOR no respondió"

    # Vuelve el servicio: se guarda la carta y, como cambió el perfil, se recalcula
    entorno["carta"]["falla"] = False
    main_mod.main(entorno["cfg"])
    assert _estado(supabase, "l1")["suelo_carta"]
    assert _estado(supabase, "l1")["serie"] == 2


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
