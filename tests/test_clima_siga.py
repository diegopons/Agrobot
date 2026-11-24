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


def test_si_cambia_el_php_del_listado_lo_busca_en_el_js(monkeypatch):
    """INTA renombra el PHP ofuscado sin aviso: se descubre el nuevo en su JS."""
    import json
    from src import clima_siga

    pedidos = []

    def pedir_falso(url, timeout=90):
        pedidos.append(url)
        if url == clima_siga.URL_ESTACIONES:
            raise ErrorSIGA("HTTP Error 404: Not Found")
        if url.endswith("js/urlserver.js"):
            return b"var URL_MYSQL_ESTACION = 'Nuevo123.php?param_type=estacion&param_value=';"
        if "Nuevo123.php" in url:
            return json.dumps([{"idInterno": "A1", "latitud": -31.8, "longitud": -63.7,
                                "nombre": "X"}]).encode()
        raise ErrorSIGA("no existe")

    monkeypatch.setattr(clima_siga, "_pedir", pedir_falso)
    df = clima_siga.listar_estaciones()
    assert list(df["idInterno"]) == ["A1"]
    assert pedidos[-1] == clima_siga.BASE + "Nuevo123.php?param_type=estacion&param_value="


def test_si_no_encuentra_el_php_nuevo_falla_para_pasar_a_la_fuente_siguiente(monkeypatch):
    from src import clima_siga

    def pedir_falso(url, timeout=90):
        raise ErrorSIGA("HTTP Error 404: Not Found")

    monkeypatch.setattr(clima_siga, "_pedir", pedir_falso)
    with pytest.raises(ErrorSIGA, match="404"):
        clima_siga.listar_estaciones()


def test_la_planilla_se_reintenta_y_se_baja_una_vez_por_corrida(monkeypatch):
    from src import clima_siga

    import io

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    llamadas = []

    def urlopen(url, timeout=90):
        llamadas.append(url)
        if len(llamadas) == 1:
            raise TimeoutError("The read operation timed out")
        return Resp(b"xls")

    monkeypatch.setattr(clima_siga.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(clima_siga.time, "sleep", lambda s: None)
    monkeypatch.setattr(clima_siga, "_planillas", {})
    # Planilla vacía: alcanza para ver cuántas veces se descargó
    monkeypatch.setattr(clima_siga, "_leer_excel", lambda b: pd.DataFrame())
    for _ in range(3):     # tres lotes cerca de la misma estación
        with pytest.raises(ErrorSIGA, match="Fecha"):
            clima_siga.descargar_serie("A872907")
    assert len(llamadas) == 2      # un corte + una descarga, y nada más
