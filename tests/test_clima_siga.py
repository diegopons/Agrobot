"""La estación del SIGA se elige entre las que siguen publicando."""
import pandas as pd
import pytest

from src.clima_siga import ErrorSIGA, estacion_mas_cercana

HOY = pd.Timestamp.today().normalize()


def _sello(fecha):
    return fecha.strftime("%Y-%m-%dT00:00:00Z[UTC]")


ESTACIONES = pd.DataFrame([
    # Las dos de Manfredi tal como las devuelve el SIGA: la más cercana cerró en 2014
    {"idInterno": "NH0438", "nombre": "INTA - Manfredi (EMC)", "latitud": -31.85,
     "longitud": -63.75, "maximoDiario": "2014-05-08T00:00:00Z[UTC]"},
    {"idInterno": "A872907", "nombre": "Manfredi - EEA Manfredi", "latitud": -31.82,
     "longitud": -63.77, "maximoDiario": _sello(HOY - pd.Timedelta(days=2))},
])


def test_descarta_estaciones_cerradas():
    e = estacion_mas_cercana(-31.85, -63.75, ESTACIONES)
    assert e["id_interno"] == "A872907"


def test_sin_estaciones_activas_falla_para_pasar_a_la_fuente_siguiente():
    solo_cerrada = ESTACIONES.iloc[[0]]
    with pytest.raises(ErrorSIGA, match="últimos"):
        estacion_mas_cercana(-31.85, -63.75, solo_cerrada)


def test_sin_columna_de_fecha_se_queda_con_la_mas_cercana():
    e = estacion_mas_cercana(-31.85, -63.75, ESTACIONES.drop(columns="maximoDiario"))
    assert e["id_interno"] == "NH0438"
