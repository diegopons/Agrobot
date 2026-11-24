"""Open-Meteo: un corte de red suelto no debe dejar al lote sin clima."""
import io
import json
import urllib.error

import pytest

from src import clima_openmeteo as om


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen_que_falla(fallas, error):
    llamadas = []

    def urlopen(url, timeout=60):
        llamadas.append(url)
        if len(llamadas) <= fallas:
            raise error
        return _Resp(json.dumps({"daily": {}}).encode())
    return urlopen, llamadas


def test_reintenta_un_corte_de_red(monkeypatch):
    urlopen, llamadas = _urlopen_que_falla(2, OSError("handshake timed out"))
    monkeypatch.setattr(om.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    assert om._pedir("https://x", {}) == {"daily": {}}
    assert len(llamadas) == 3


def test_si_sigue_caido_falla_para_pasar_a_la_fuente_siguiente(monkeypatch):
    urlopen, llamadas = _urlopen_que_falla(9, OSError("handshake timed out"))
    monkeypatch.setattr(om.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    with pytest.raises(om.ErrorOpenMeteo, match="handshake"):
        om._pedir("https://x", {})
    assert len(llamadas) == 3


def test_un_error_400_no_se_reintenta(monkeypatch):
    error = urllib.error.HTTPError("https://x", 400, "Bad Request", None, None)
    urlopen, llamadas = _urlopen_que_falla(9, error)
    monkeypatch.setattr(om.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(om.time, "sleep", lambda s: None)
    with pytest.raises(om.ErrorOpenMeteo):
        om._pedir("https://x", {})
    assert len(llamadas) == 1
