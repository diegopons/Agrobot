"""
almacen.py
----------
Dónde viven los lotes y sus resultados. El orquestador (main.py) no sabe si
está corriendo un lote en una PC o todos los lotes de todos los usuarios: le
pide los lotes al almacén y le devuelve los resultados de cada uno.

  AlmacenLocal     un solo lote, el de config.yaml, con estado.json y el CSV
                   en data/. Es el modo original, para correr en una PC.
  AlmacenSupabase  todos los lotes activos de todos los usuarios, guardados en
                   Supabase (ver supabase/schema.sql). Lo usa la corrida diaria
                   de GitHub Actions.

Se elige con `crear_almacen()`: si están SUPABASE_URL y
SUPABASE_SERVICE_ROLE_KEY se usa Supabase; si no, el modo local. BH_ALMACEN
fuerza uno u otro (el workflow lo fija en "supabase" para que, si faltan los
secrets, falle en lugar de caer callado al modo local).
"""
from __future__ import annotations

import copy
import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

from . import estado as mod_estado
from . import publicar_estado as mod_estado_json
from .aoi import aoi_desde_geojson, cargar_aoi
from .balance import EstadoBalance

logger = logging.getLogger(__name__)

# Columnas de la serie diaria, en el orden del CSV histórico
COLUMNAS_BALANCE = (
    "fecha", "precipitacion_mm", "precipitacion_efectiva_mm", "recarga_capa_mm",
    "eto_mm", "ndvi", "prof_raiz_cm", "au_max_mm", "kc", "ks", "etc_ajustada_mm",
    "percolacion_mm", "riego_mm", "riego_efectivo_mm", "au_real_mm", "pct_au",
    "fuente_pp", "fuente_eto",
)

# Fechas con imagen que se recuerdan para el mosaico (que muestra las últimas 8)
MAX_FECHAS_MOSAICO = 16

# Secciones de config.yaml que un lote puede ajustar desde `lotes.parametros`.
# Las credenciales y las rutas no están: son del sistema, no del lote.
SECCIONES_AJUSTABLES = ("balance", "cdse", "clima", "pronostico", "mapas", "rezago_dias")


@dataclass
class Lote:
    """Un lote listo para correr: su configuración completa y su polígono."""
    id: str
    nombre: str
    cfg: dict
    usuario: str = ""
    usuario_id: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    reiniciar: bool = False
    # Primer día a recalcular porque cambió un riego de un día ya procesado
    recalcular_desde: Optional[str] = None
    geometria: Optional[dict] = None
    ruta_aoi: Optional[str] = None
    _aoi: Optional[dict] = field(default=None, repr=False)

    @property
    def etiqueta(self) -> str:
        return f"{self.nombre} ({self.usuario})" if self.usuario else self.nombre

    def aoi(self) -> dict:
        # Se calcula recién al procesar el lote: un polígono roto tiene que
        # fallar dentro de la corrida de ese lote, no al listar todos.
        if self._aoi is None:
            self._aoi = (
                cargar_aoi(self.ruta_aoi) if self.ruta_aoi
                else aoi_desde_geojson(self.geometria)
            )
        return self._aoi


@dataclass
class EstadoGuardado:
    """Lo que el almacén recuerda de la corrida anterior de un lote."""
    balance: EstadoBalance
    motivo_reinicio: Optional[str] = None
    fechas_con_imagen: list = field(default_factory=list)
    serie: int = 1


def _fusionar(base: dict, encima: dict) -> dict:
    salida = copy.deepcopy(base)
    for k, v in encima.items():
        if isinstance(v, dict) and isinstance(salida.get(k), dict):
            salida[k] = _fusionar(salida[k], v)
        else:
            salida[k] = copy.deepcopy(v)
    return salida


def config_de_lote(cfg_base: dict, fila: dict) -> dict:
    """Arma la configuración de un lote: config.yaml + lo propio del lote.

    config.yaml aporta lo que es del sistema (fuentes de clima, Copernicus,
    pronóstico); la fila de `lotes` aporta lo que es del lote (siembra, agua
    inicial, umbral, suelo, avisos).
    """
    cfg = copy.deepcopy(cfg_base)
    # El suelo de config.yaml es la calicata de un lote puntual: otro lote sin
    # calicata propia arranca del perfil por defecto, no del de ese lote.
    cfg.pop("suelo", None)

    for seccion, valores in (fila.get("parametros") or {}).items():
        if seccion in SECCIONES_AJUSTABLES and isinstance(valores, dict):
            cfg[seccion] = _fusionar(cfg.get(seccion) or {}, valores)

    cfg["campana"] = {
        "fecha_siembra": str(fila["fecha_siembra"]),
        "au_real_inicial_mm": float(fila["au_real_inicial_mm"]),
        "prof_raiz_inicial_cm": float(fila["prof_raiz_inicial_cm"]),
    }
    cfg.setdefault("balance", {})["umbral_riego_pct"] = float(fila["umbral_riego_pct"])

    tg = cfg.setdefault("telegram", {})
    tg["nombre_lote"] = fila["nombre"]
    modo = fila.get("modo_aviso") or "cruce"
    if modo == "nunca":
        tg["habilitado"] = False
    else:
        tg["modo"] = modo
    tg["eficiencia_aplicacion"] = float(fila["eficiencia_aplicacion"])
    tg["reposicion_objetivo_pct"] = float(fila["reposicion_objetivo_pct"])

    if fila.get("suelo"):
        cfg["suelo"] = fila["suelo"]
    return cfg


def _a_json(valor):
    """Valor de pandas/numpy a algo que JSON acepte (NaN -> null)."""
    if valor is None or valor is pd.NA or valor is pd.NaT:
        return None
    if isinstance(valor, (pd.Timestamp, datetime)):
        return str(valor.date())
    if hasattr(valor, "item"):          # escalares de numpy
        valor = valor.item()
    if isinstance(valor, float) and math.isnan(valor):
        return None
    return valor


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Modo local: un lote, archivos en disco
# =============================================================================
class AlmacenLocal:
    modo = "local"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.errores_de_carga: list[str] = []

    @property
    def _rutas(self) -> dict:
        return self.cfg["rutas"]

    def lotes(self, solo_id: Optional[str] = None, solo_pendientes: bool = False) -> list[Lote]:
        tg = self.cfg.get("telegram", {})
        return [Lote(
            id="local",
            nombre=tg.get("nombre_lote", "Lote"),
            cfg=self.cfg,
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            # 'archivo' es la clave actual; 'shapefile' se mantiene por compatibilidad
            ruta_aoi=self.cfg["aoi"].get("archivo") or self.cfg["aoi"]["shapefile"],
        )]

    def cargar_estado(self, lote: Lote, huella: str, reiniciar: bool = False) -> EstadoGuardado:
        cfg = lote.cfg
        est, motivo = mod_estado.cargar_estado(
            self._rutas["estado_json"],
            au_real_inicial=cfg["campana"]["au_real_inicial_mm"],
            prof_raiz_inicial=cfg["campana"]["prof_raiz_inicial_cm"],
            huella=huella,
            reiniciar=reiniciar,
            devolver_motivo=True,
        )
        # Si el balance arranca de cero, la serie anterior corresponde a otro
        # cálculo: se aparta para que no se mezcle con la nueva.
        if motivo:
            mod_estado.archivar_csv(self._rutas["salida_csv"], motivo)
            return EstadoGuardado(est, motivo)

        fechas = []
        ruta = Path(self._rutas["estado_json"])
        if ruta.exists():
            try:
                fechas = json.loads(ruta.read_text(encoding="utf-8")).get("fechas_con_imagen") or []
            except (ValueError, OSError):
                pass
        return EstadoGuardado(est, None, list(fechas))

    def guardar(self, lote: Lote, df_resultados: pd.DataFrame,
                guardado: EstadoGuardado, huella: str) -> None:
        mod_estado.append_resultados_csv(self._rutas["salida_csv"], df_resultados)
        mod_estado.guardar_estado(
            self._rutas["estado_json"], guardado.balance, huella=huella,
            extra={"fechas_con_imagen": guardado.fechas_con_imagen},
        )

    def leer_serie(self, lote: Lote, guardado: EstadoGuardado) -> pd.DataFrame:
        ruta = Path(self._rutas["salida_csv"])
        if not ruta.exists():
            return pd.DataFrame(columns=list(COLUMNAS_BALANCE))
        return pd.read_csv(ruta, parse_dates=["fecha"])

    def publicar_resumen(self, lote: Lote, resumen: dict) -> None:
        mod_estado_json.publicar(
            resumen,
            destino=self._rutas.get("estado_json_publico", "docs/estado_actual.json"),
        )

    def ruta_mosaico(self, lote: Lote) -> str:
        ruta = lote.cfg.get("mapas", {}).get("salida", "docs/ndvi_mosaico.png")
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        return ruta

    def publicar_mosaico(self, lote: Lote, ruta: str) -> None:
        pass  # ya quedó en docs/, que es de donde lo lee el tablero

    def registrar_corrida(self, lote: Lote, error: Optional[str] = None) -> None:
        pass  # el log de la corrida es el registro

    def riegos(self, lote: Lote, desde: str, hasta: str) -> dict:
        return {}   # en modo local no hay dónde cargarlos

    # en modo local no hay cuentas que aprobar
    def pedidos_sin_avisar(self) -> list[dict]:
        return []

    def administradores(self) -> list[dict]:
        return []

    def marcar_avisados(self, ids: list[str]) -> None:
        pass

    def aprobados_sin_avisar(self) -> list[dict]:
        return []

    def marcar_aprobacion_avisada(self, ids: list[str]) -> None:
        pass

    @property
    def _ruta_carta(self) -> Path:
        return Path(self._rutas["estado_json"]).with_name("suelo_carta.json")

    def suelo_carta(self, lote: Lote) -> Optional[dict]:
        try:
            return json.loads(self._ruta_carta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def guardar_suelo_carta(self, lote: Lote, carta: dict) -> None:
        self._ruta_carta.parent.mkdir(parents=True, exist_ok=True)
        self._ruta_carta.write_text(json.dumps(carta, ensure_ascii=False, indent=1), encoding="utf-8")

    # --- índices espectrales: data/indices.json y docs/indices/<indice>.png ---
    def _json(self, nombre: str, defecto):
        try:
            return json.loads(Path(self._rutas["estado_json"]).with_name(nombre).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defecto

    def _escribir_json(self, nombre: str, datos) -> None:
        ruta = Path(self._rutas["estado_json"]).with_name(nombre)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")

    def indices_meta(self, lote: Lote) -> Optional[dict]:
        return self._json("indices_meta.json", None)

    def guardar_indices_meta(self, lote: Lote, meta: dict) -> None:
        self._escribir_json("indices_meta.json", meta)

    def guardar_indices(self, lote: Lote, filas: list[dict]) -> None:
        todas = {f["fecha"]: f for f in self._json("indices.json", [])}
        todas.update({f["fecha"]: f for f in filas})
        self._escribir_json("indices.json", [todas[k] for k in sorted(todas)])

    def borrar_indices(self, lote: Lote) -> None:
        self._escribir_json("indices.json", [])

    def publicar_mapa_indice(self, lote: Lote, clave: str, png: bytes) -> None:
        ruta = Path("docs/indices") / f"{clave}.png"
        ruta.parent.mkdir(parents=True, exist_ok=True)
        ruta.write_bytes(png)

    def rebobinar(self, lote: Lote, guardado: EstadoGuardado, desde: str) -> None:
        pass

    def recalculo_atendido(self, lote: Lote) -> None:
        pass


# =============================================================================
# Modo Supabase: todos los lotes de todos los usuarios
# =============================================================================
class ErrorSupabase(RuntimeError):
    pass


class ClienteSupabase:
    """Lo mínimo de la API REST de Supabase (PostgREST + Storage).

    Se usa `requests` directo en lugar del paquete `supabase` para no sumar
    dependencias: son cinco operaciones.
    """

    TAMANO_LOTE = 500   # filas por pedido al escribir
    TAMANO_PAGINA = 1000  # Supabase devuelve como máximo 1000 filas por consulta

    def __init__(self, url: str, clave: str, timeout: int = 60):
        url = url.rstrip("/")
        # SUPABASE_REST_URL permite apuntar a un PostgREST suelto (pruebas)
        self.rest = os.environ.get("SUPABASE_REST_URL", "").rstrip("/") or f"{url}/rest/v1"
        self.storage = f"{url}/storage/v1"
        self.timeout = timeout
        self.sesion = requests.Session()
        self.sesion.headers.update({"apikey": clave, "Authorization": f"Bearer {clave}"})

    def _pedir(self, metodo: str, url: str, **kw) -> requests.Response:
        r = self.sesion.request(metodo, url, timeout=self.timeout, **kw)
        if r.status_code >= 400:
            raise ErrorSupabase(f"{metodo} {url} -> {r.status_code}: {r.text[:300]}")
        return r

    def select(self, tabla: str, params: dict) -> list[dict]:
        """Todas las filas que cumplen `params`, paginando de a 1000."""
        filas: list[dict] = []
        desde = 0
        while True:
            pagina = self._pedir(
                "GET", f"{self.rest}/{tabla}",
                params={**params, "limit": self.TAMANO_PAGINA, "offset": desde},
            ).json()
            filas.extend(pagina)
            if len(pagina) < self.TAMANO_PAGINA:
                return filas
            desde += self.TAMANO_PAGINA

    def upsert(self, tabla: str, filas: list[dict], on_conflict: str) -> None:
        for i in range(0, len(filas), self.TAMANO_LOTE):
            self._pedir(
                "POST", f"{self.rest}/{tabla}",
                params={"on_conflict": on_conflict},
                json=filas[i:i + self.TAMANO_LOTE],
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            )

    def update(self, tabla: str, filtros: dict, datos: dict) -> None:
        self._pedir("PATCH", f"{self.rest}/{tabla}", params=filtros, json=datos,
                    headers={"Prefer": "return=minimal"})

    def delete(self, tabla: str, filtros: dict) -> None:
        self._pedir("DELETE", f"{self.rest}/{tabla}", params=filtros,
                    headers={"Prefer": "return=minimal"})

    def subir(self, bucket: str, ruta: str, contenido: bytes, tipo: str) -> None:
        self._pedir("POST", f"{self.storage}/object/{bucket}/{ruta}", data=contenido,
                    headers={"Content-Type": tipo, "x-upsert": "true"})


class AlmacenSupabase:
    modo = "supabase"

    def __init__(self, cfg_base: dict, url: str, clave: str):
        self.cfg_base = cfg_base
        self.db = ClienteSupabase(url, clave)
        self.errores_de_carga: list[str] = []

    @staticmethod
    def _pendiente(fila: dict) -> bool:
        """Lote que no puede esperar a la corrida diaria: nuevo, sin ningún
        día calculado, con un recálculo pedido o modificado desde la última
        corrida."""
        est = fila.get("estado")
        if isinstance(est, list):
            est = est[0] if est else None
        if (not est or not est.get("ultima_fecha_procesada") or fila.get("reiniciar")
                or fila.get("recalcular_desde")):
            return True
        if not est.get("ultima_corrida"):
            return True
        return pd.Timestamp(fila["actualizado"]) > pd.Timestamp(est["ultima_corrida"])

    def lotes(self, solo_id: Optional[str] = None, solo_pendientes: bool = False) -> list[Lote]:
        params = {
            "select": "*,perfil:perfiles(nombre,telegram_chat_id,aprobado),"
                      "estado:estado_lote(ultima_fecha_procesada,ultima_corrida)",
            "activo": "eq.true",
            "order": "creado.asc",
        }
        if solo_id:
            params["id"] = f"eq.{solo_id}"

        try:
            filas = self.db.select("lotes", params)
        except ErrorSupabase as e:
            if solo_id and "22P02" in str(e):
                raise ErrorSupabase(
                    f"El id de lote '{solo_id}' no es válido: tiene que tener 36 caracteres, "
                    "de la forma xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx. Revisá que se haya "
                    "copiado completo.") from e
            raise

        lotes = []
        for fila in filas:
            if solo_pendientes and not self._pendiente(fila):
                continue
            perfil = fila.get("perfil") or {}
            # Cuenta a la que un administrador le quitó el acceso: sus lotes
            # quedan guardados pero no se calculan.
            if perfil.get("aprobado") is False:
                continue
            try:
                cfg = config_de_lote(self.cfg_base, fila)
            except (KeyError, TypeError, ValueError) as e:
                msg = f"Lote {fila.get('nombre')} ({fila.get('id')}): configuración inválida: {e}"
                logger.error(msg)
                self.errores_de_carga.append(msg)
                continue
            lotes.append(Lote(
                id=fila["id"],
                nombre=fila["nombre"],
                cfg=cfg,
                usuario=perfil.get("nombre") or str(fila["usuario_id"])[:8],
                usuario_id=fila["usuario_id"],
                telegram_chat_id=perfil.get("telegram_chat_id"),
                reiniciar=bool(fila.get("reiniciar")),
                recalcular_desde=fila.get("recalcular_desde"),
                geometria=fila["geometria"],
            ))
        return lotes

    def _filtro(self, lote: Lote) -> dict:
        return {"lote_id": f"eq.{lote.id}"}

    def cargar_estado(self, lote: Lote, huella: str, reiniciar: bool = False) -> EstadoGuardado:
        filas = self.db.select("estado_lote", {"select": "*", **self._filtro(lote)})
        previo = filas[0] if filas else None
        campana = lote.cfg["campana"]

        motivo = None
        if previo is not None:
            if reiniciar or lote.reiniciar:
                logger.warning("Se pidió reiniciar el estado: se descarta el balance acumulado.")
                motivo = "se pidió reiniciar"
            elif previo.get("huella") and previo["huella"] != huella:
                logger.warning(
                    "⚠️  Cambió alguno de los datos de base —lote, fecha de siembra, "
                    "fuente de clima o perfil de suelo— (huella %s -> %s). "
                    "El balance acumulado no corresponde: se reinicia desde cero.",
                    previo["huella"], huella,
                )
                motivo = "cambió el lote, la siembra, el clima o el perfil de suelo"

        if previo is not None and motivo is None:
            if previo.get("ultima_fecha_procesada") is None:
                # Todavía no procesó ningún día: se arranca de los valores
                # actuales del lote, por si los cambiaron desde el reinicio.
                est = EstadoBalance.inicial(
                    campana["au_real_inicial_mm"], campana["prof_raiz_inicial_cm"])
            else:
                est = EstadoBalance(
                    ultima_fecha_procesada=str(previo["ultima_fecha_procesada"]),
                    au_real_mm=float(previo["au_real_mm"]),
                    prof_raiz_cm=float(previo["prof_raiz_cm"]),
                    ultimo_ndvi_valido=previo.get("ultimo_ndvi_valido"),
                )
            logger.info("Estado previo cargado: %s (serie %s)", est.to_dict(), previo["serie"])
            return EstadoGuardado(est, None, list(previo.get("fechas_con_imagen") or []),
                                  int(previo["serie"]))

        # Lote nuevo o reinicio: serie nueva. Se guarda ya, para que el tablero
        # deje de mostrar la serie vieja aunque esta corrida no llegue a
        # procesar ningún día (siembra futura, clima caído).
        serie = int(previo["serie"]) + 1 if previo else 1
        est = EstadoBalance.inicial(campana["au_real_inicial_mm"], campana["prof_raiz_inicial_cm"])
        logger.info(
            "Se inicializa el balance (serie %d): AU=%.1f mm, profundidad de raíz=%.0f cm.",
            serie, est.au_real_mm, est.prof_raiz_cm,
        )
        # Restos de un intento anterior que escribió filas y no llegó a guardar el estado
        self.db.delete("balance_diario", {**self._filtro(lote), "serie": f"eq.{serie}"})
        if previo:
            self.db.delete("resumen_lote", self._filtro(lote))
        guardado = EstadoGuardado(est, motivo, [], serie)
        self._guardar_estado(lote, guardado, huella)
        if lote.reiniciar:
            self.db.update("lotes", {"id": f"eq.{lote.id}"}, {"reiniciar": False})
            lote.reiniciar = False
        return guardado

    def _guardar_estado(self, lote: Lote, guardado: EstadoGuardado, huella: str) -> None:
        b = guardado.balance
        self.db.upsert("estado_lote", [{
            "lote_id": lote.id,
            "serie": guardado.serie,
            "huella": huella,
            "ultima_fecha_procesada": b.ultima_fecha_procesada,
            "au_real_mm": _a_json(b.au_real_mm),
            "prof_raiz_cm": _a_json(b.prof_raiz_cm),
            "ultimo_ndvi_valido": _a_json(b.ultimo_ndvi_valido),
            "fechas_con_imagen": list(guardado.fechas_con_imagen),
        }], on_conflict="lote_id")

    def guardar(self, lote: Lote, df_resultados: pd.DataFrame,
                guardado: EstadoGuardado, huella: str) -> None:
        # Primero la serie y después el estado: si se corta en el medio, el
        # estado sigue apuntando al último día guardado completo y la próxima
        # corrida reescribe esas filas (el upsert no duplica).
        if not df_resultados.empty:
            cols = [c for c in COLUMNAS_BALANCE if c in df_resultados.columns]
            filas = []
            for rec in df_resultados[cols].to_dict("records"):
                fila = {"lote_id": lote.id, "serie": guardado.serie}
                fila.update({c: _a_json(rec[c]) for c in cols})
                fila["fecha"] = str(pd.to_datetime(rec["fecha"]).date())
                filas.append(fila)
            self.db.upsert("balance_diario", filas, on_conflict="lote_id,serie,fecha")
            logger.info("Serie del lote actualizada: %d días nuevos (serie %d).",
                        len(filas), guardado.serie)
        self._guardar_estado(lote, guardado, huella)

    def leer_serie(self, lote: Lote, guardado: EstadoGuardado) -> pd.DataFrame:
        filas = self.db.select("balance_diario", {
            "select": ",".join(COLUMNAS_BALANCE),
            **self._filtro(lote),
            "serie": f"eq.{guardado.serie}",
            "order": "fecha.asc",
        })
        df = pd.DataFrame(filas, columns=list(COLUMNAS_BALANCE))
        df["fecha"] = pd.to_datetime(df["fecha"])
        return df

    def publicar_resumen(self, lote: Lote, resumen: dict) -> None:
        self.db.upsert("resumen_lote", [{
            "lote_id": lote.id,
            "fecha_del_dato": resumen["estado_actual"]["fecha_del_dato"],
            "resumen": json.loads(json.dumps(resumen, default=str)),
            "actualizado": _ahora(),
        }], on_conflict="lote_id")
        logger.info(
            "Resumen publicado (%s, %.0f %% de agua útil).",
            resumen["estado_actual"]["fecha_del_dato"],
            resumen["estado_actual"]["porcentaje_agua_util"],
        )

    def ruta_mosaico(self, lote: Lote) -> str:
        return str(Path(tempfile.gettempdir()) / f"mosaico_{lote.id}.png")

    def publicar_mosaico(self, lote: Lote, ruta: str) -> None:
        # La carpeta es el usuario: la política de Storage solo deja leer la propia
        self.db.subir("mosaicos", f"{lote.usuario_id}/{lote.id}.png",
                      Path(ruta).read_bytes(), "image/png")

    # --- pedidos de acceso ---
    def pedidos_sin_avisar(self) -> list[dict]:
        return self.db.select("perfiles", {
            "select": "id,email,nombre,organizacion,motivo,creado",
            "aprobado": "eq.false", "aviso_admin": "eq.false", "order": "creado.asc"})

    def administradores(self) -> list[dict]:
        return self.db.select("perfiles", {"select": "email,telegram_chat_id", "es_admin": "eq.true"})

    def marcar_avisados(self, ids: list[str]) -> None:
        if ids:
            self.db.update("perfiles", {"id": f"in.({','.join(ids)})"}, {"aviso_admin": True})

    def aprobados_sin_avisar(self) -> list[dict]:
        return self.db.select("perfiles", {
            "select": "id,email,nombre,telegram_chat_id",
            "aprobado": "eq.true", "aviso_aprobacion": "eq.false"})

    def marcar_aprobacion_avisada(self, ids: list[str]) -> None:
        if ids:
            self.db.update("perfiles", {"id": f"in.({','.join(ids)})"}, {"aviso_aprobacion": True})

    def suelo_carta(self, lote: Lote) -> Optional[dict]:
        filas = self.db.select("estado_lote", {"select": "suelo_carta", **self._filtro(lote)})
        return filas[0].get("suelo_carta") if filas else None

    def guardar_suelo_carta(self, lote: Lote, carta: dict) -> None:
        self.db.upsert("estado_lote", [{"lote_id": lote.id, "suelo_carta": carta}],
                       on_conflict="lote_id")

    # --- índices espectrales ---
    def indices_meta(self, lote: Lote) -> Optional[dict]:
        filas = self.db.select("estado_lote", {"select": "indices_meta", **self._filtro(lote)})
        return filas[0].get("indices_meta") if filas else None

    def guardar_indices_meta(self, lote: Lote, meta: dict) -> None:
        self.db.upsert("estado_lote", [{"lote_id": lote.id, "indices_meta": meta}],
                       on_conflict="lote_id")

    def guardar_indices(self, lote: Lote, filas: list[dict]) -> None:
        if filas:
            self.db.upsert("indices_lote",
                           [{"lote_id": lote.id, **f} for f in filas], on_conflict="lote_id,fecha")

    def borrar_indices(self, lote: Lote) -> None:
        self.db.delete("indices_lote", self._filtro(lote))

    def publicar_mapa_indice(self, lote: Lote, clave: str, png: bytes) -> None:
        # Misma carpeta del usuario que el mosaico: la política de Storage es la misma
        self.db.subir("mosaicos", f"{lote.usuario_id}/{lote.id}/indices/{clave}.png", png, "image/png")

    def riegos(self, lote: Lote, desde: str, hasta: str) -> dict:
        """{fecha 'AAAA-MM-DD': mm} de los riegos cargados en el rango."""
        filas = self.db.select("riegos", {
            "select": "fecha,mm",
            **self._filtro(lote),
            "and": f"(fecha.gte.{desde},fecha.lte.{hasta})",
        })
        return {str(f["fecha"]): float(f["mm"]) for f in filas}

    def rebobinar(self, lote: Lote, guardado: EstadoGuardado, desde: str) -> None:
        """Vuelve el estado al cierre del día anterior a `desde` y borra la
        serie desde ahí, para recalcular esos días con los riegos corregidos.

        No abre una serie nueva: los días anteriores no cambian, así que
        siguen siendo la misma campaña.
        """
        previos = self.db.select("balance_diario", {
            "select": "fecha,au_real_mm,prof_raiz_cm,ndvi",
            **self._filtro(lote),
            "serie": f"eq.{guardado.serie}",
            "fecha": f"lt.{desde}",
            "order": "fecha.desc",
        })
        if previos:
            p = previos[0]
            guardado.balance = EstadoBalance(
                ultima_fecha_procesada=str(p["fecha"]),
                au_real_mm=float(p["au_real_mm"]),
                prof_raiz_cm=float(p["prof_raiz_cm"]),
                ultimo_ndvi_valido=p.get("ndvi"),
            )
        else:
            campana = lote.cfg["campana"]
            guardado.balance = EstadoBalance.inicial(
                campana["au_real_inicial_mm"], campana["prof_raiz_inicial_cm"])
        self.db.delete("balance_diario", {
            **self._filtro(lote), "serie": f"eq.{guardado.serie}", "fecha": f"gte.{desde}"})
        logger.info("Riegos corregidos: se recalcula desde el %s (estado al %s).",
                    desde, guardado.balance.ultima_fecha_procesada or "inicio")

    def recalculo_atendido(self, lote: Lote) -> None:
        if not lote.recalcular_desde:
            return
        # Solo si nadie cargó otro riego mientras corría: ese pedido es nuevo
        self.db.update("lotes", {"id": f"eq.{lote.id}",
                                 "recalcular_desde": f"eq.{lote.recalcular_desde}"},
                       {"recalcular_desde": None})
        lote.recalcular_desde = None

    def registrar_corrida(self, lote: Lote, error: Optional[str] = None) -> None:
        self.db.upsert("estado_lote", [{
            "lote_id": lote.id,
            "ultima_corrida": _ahora(),
            "ultimo_error": error[:500] if error else None,
        }], on_conflict="lote_id")


def crear_almacen(cfg: dict):
    url = os.environ.get("SUPABASE_URL", "").strip()
    clave = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    forzado = os.environ.get("BH_ALMACEN", "").strip().lower()

    if forzado == "local" or (not forzado and not (url and clave)):
        return AlmacenLocal(cfg)
    if forzado not in ("", "supabase"):
        raise RuntimeError(f"BH_ALMACEN='{forzado}' no es válido: usar 'local' o 'supabase'.")
    if not (url and clave):
        raise RuntimeError(
            "Faltan SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY para correr sobre Supabase. "
            "Ver SUPABASE.md."
        )
    return AlmacenSupabase(cfg, url, clave)
