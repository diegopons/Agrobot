# Alertas por Telegram

> **Con varios usuarios (Supabase)**, cada aviso va al chat que vinculó el
> dueño del lote desde el tablero (pestaña Cuenta), y el modo de aviso se
> elige por lote. Ya no hace falta `TELEGRAM_CHAT_ID`: solo el token del bot.
> Lo que sigue sobre el contenido del mensaje y los modos vale igual.

## Qué manda

Un mensaje como este:

```
🟠 Balance Hídrico — Lote 20
Datos al 2025-06-11

Condición: Déficit (42% de Agua Útil)
Agua útil actual: 44.0 mm de 104.8 mm
Déficit respecto a CC: 60.8 mm

NDVI: 0.48  |  ETc del día: 1.3 mm
Lluvia últimos 7 días: 0.0 mm

💧 Riego sugerido (por debajo del umbral de 50%)
   Lámina neta: 60.8 mm
   Lámina bruta: 71.5 mm (ef. 85%)

Verificar pronóstico antes de aplicar: el cálculo no contempla
lluvias previstas.
```

Escala de condición (por % de Agua Útil, editable en `notificaciones.py`):

| % AU | Condición | |
|---|---|---|
| ≥ 75 % | Óptimo | 🟢 |
| 50 – 75 % | Adecuado | 🟡 |
| 35 – 50 % | Déficit | 🟠 |
| < 35 % | Crítico | 🔴 |

## Configurar el bot

### 1. Crear el bot (si vas a usar uno nuevo)

1. En Telegram, buscá **@BotFather**.
2. Mandale `/newbot` y seguí los pasos (nombre y username del bot).
3. Te devuelve un **token**, algo tipo `123456789:AAG...`. Ese es el
   `TELEGRAM_BOT_TOKEN`.

> Si preferís reusar el bot que ya tenés andando para el agente de n8n,
> saltá este paso y usá el token de ese bot. Funciona igual — solo tené
> presente que quedan los dos sistemas atados al mismo bot.

### 2. Obtener el chat_id

1. Mandale cualquier mensaje a tu bot desde tu Telegram (o agregalo al
   grupo donde querés las alertas y escribí algo ahí).
2. Abrí en el navegador:
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`
3. Buscá en la respuesta `"chat":{"id": ...}`. Ese número es el
   `TELEGRAM_CHAT_ID` (en grupos es negativo, incluí el signo menos).

### 3. Cargarlos

**En GitHub Actions**: Settings → Secrets and variables → Actions →
New repository secret, con los nombres `TELEGRAM_BOT_TOKEN` y
`TELEGRAM_CHAT_ID`.

**En la PC local**: como variables de entorno de usuario, igual que las
credenciales de Copernicus.

### 4. Probar

Para verificar que llega el mensaje sin esperar a que haya déficit real,
poné temporalmente en `config.yaml`:

```yaml
telegram:
  modo: "siempre"
```

Corré el workflow a mano una vez, confirmá que llega, y volvé a
`modo: "cruce"`.

## Cuándo notifica

Configurable en `config.yaml` → `telegram.modo`:

- **`cruce`** (por defecto): solo cuando el %AU **cruza** el umbral hacia
  abajo. Es el recomendado: si el lote queda tres semanas en déficit, te
  avisa una vez, no veintiuna.
- **`deficit`**: todos los días que esté por debajo del umbral.
- **`siempre`**: cada corrida con días nuevos. Útil solo para probar.

## Sobre la lámina de riego — leer antes de usarla en serio

El número que sale es aritmética simple: cuántos mm faltan para llegar a
capacidad de campo, divididos por la eficiencia de aplicación. **No es una
recomendación de riego cerrada.** Lo que no contempla:

- **Pronóstico de lluvia.** Es la limitación más importante. Si hay un
  frente entrando, regar a reposición total es tirar agua y arriesgar
  anegamiento y lavado de nitratos.
- **Lámina máxima aplicable.** En déficit profundo el cálculo puede sugerir
  70-90 mm en una sola aplicación, que es más de lo que muchos equipos
  aplican por pasada y más de lo que el suelo infiltra sin escurrimiento.
  En la práctica eso se fracciona.
- **Capacidad y vuelta del equipo.**
- **Etapa fenológica.** El umbral fijo de 50% trata igual a un cultivo en
  implantación que en llenado de grano, cuando la sensibilidad al estrés
  es muy distinta.
- **La incertidumbre del propio balance**, que arrastra el error de AgERA5
  (celda de ~10 km) y del AU inicial estimado.

Los dos parámetros para ajustarla están en `config.yaml`:
`eficiencia_aplicacion` (el 0.85 por defecto es genérico de pivote en
buenas condiciones — bajalo si el sistema real es menos eficiente) y
`reposicion_objetivo_pct` (bajalo de 100 si querés riego deficitario
controlado).

Tratalo como un dato de apoyo para la decisión, no como la decisión.
