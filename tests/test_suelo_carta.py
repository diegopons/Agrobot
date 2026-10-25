"""Perfil de suelo desde la Carta de Suelos de Córdoba (WFS de IDECOR), sin red."""
import pytest
from shapely.geometry import box, mapping

from src import suelo_carta as sc
from src.suelo import precalcular_parametros_suelo

LOTE = mapping(box(-63.87, -31.73, -63.86, -31.72))
OESTE = mapping(box(-63.88, -31.74, -63.8675, -31.71))   # cubre ~30 % del lote
ESTE = mapping(box(-63.8675, -31.74, -63.85, -31.71))    # ~70 %


def _unidad(geom, simbolo, au, **extra):
    return {"type": "Feature", "geometry": geom, "properties": {
        "simbolo": simbolo, "nombre": f"Consociación {simbolo.upper()}", "cu": "IIIc", "ip": 70,
        "tipo_unida": "Consociación", "au_1m": au[0], "au_1_5m": au[1], "au_2m": au[2],
        "serie_1": simbolo.upper(), "porc_1": 100, **extra}}


@pytest.fixture
def wfs(monkeypatch):
    respuestas = {}

    def pedir(capa, bbox, url, timeout, campos=sc.CAMPOS_CARTA):
        if isinstance(respuestas.get(capa), Exception):
            raise respuestas[capa]
        return respuestas.get(capa, [])
    monkeypatch.setattr(sc, "_pedir_unidades", pedir)
    return respuestas


def test_el_perfil_respeta_los_totales_de_la_carta():
    df = precalcular_parametros_suelo(sc.perfil_desde_au(160, 231, 301))
    acum = dict(zip(df["prof_cm"], df["au_max_acum_mm"]))
    assert acum[100] == pytest.approx(160, abs=0.1)
    assert acum[200] == pytest.approx(301, abs=0.1)
    # a 1,5 m cae en el medio de la capa 140-160
    assert (acum[140] + acum[160]) / 2 == pytest.approx(231, abs=0.1)


def test_pondera_por_superficie(wfs):
    wfs["carta_suelo_50mil_2025"] = [_unidad(OESTE, "Mf", (181, 258, 335)),
                                     _unidad(ESTE, "Ot", (160, 231, 301))]
    c = sc.consultar(LOTE)
    pcts = {u["simbolo"]: u["pct"] for u in c["unidades"]}
    assert pcts["Mf"] == pytest.approx(25, abs=1) and pcts["Ot"] == pytest.approx(75, abs=1)
    esperado = (pcts["Mf"] * 335 + pcts["Ot"] * 301) / 100
    assert c["au_2m"] == pytest.approx(esperado, abs=0.2)
    assert c["perfil"] and c["capacidad_uso"] == "IIIc"
    assert sc.describir(c).startswith("Ot 75 %, Mf 25 % (1:50.000)")


def test_unidades_sin_dato_no_entran_al_promedio(wfs):
    wfs["carta_suelo_50mil_2025"] = [_unidad(OESTE, "L", (None, None, None)),
                                     _unidad(ESTE, "Ot", (160, 231, 301))]
    c = sc.consultar(LOTE)
    assert c["au_2m"] == 301                      # la laguna no baja el promedio
    assert c["sin_dato_pct"] == pytest.approx(25, abs=1)


def test_poca_cobertura_no_da_perfil(wfs):
    wfs["carta_suelo_50mil_2025"] = [_unidad(OESTE, "Mf", (181, 258, 335))]
    c = sc.consultar(LOTE)
    assert "perfil" not in c and "25 %" in c["motivo"]


def test_fuera_de_las_cartas(wfs):
    c = sc.consultar(LOTE)
    assert "perfil" not in c and "fuera" in c["motivo"]


def test_completa_con_la_carta_100mil(wfs):
    wfs["carta_suelo_50mil_2025"] = [_unidad(OESTE, "Mf", (181, 258, 335))]
    wfs["carta_suelo_100mil_2025"] = [_unidad(ESTE, "Ot", (160, 231, 301))]
    c = sc.consultar(LOTE)
    assert {u["escala"] for u in c["unidades"]} == {"1:50.000", "1:100.000"}
    assert c["cobertura_pct"] == pytest.approx(100, abs=0.5)


def test_servicio_caido_levanta_error(wfs):
    wfs["carta_suelo_50mil_2025"] = sc.ErrorCarta("timeout")
    with pytest.raises(sc.ErrorCarta):
        sc.consultar(LOTE)


def test_limitantes_caidas_no_impiden_el_perfil(wfs):
    wfs["carta_suelo_50mil_2025"] = [_unidad(LOTE, "Ot", (160, 231, 301))]
    wfs["limitantes_edaficas"] = sc.ErrorCarta("timeout")
    c = sc.consultar(LOTE)
    assert c["perfil"] and c["limitantes"] == {}
