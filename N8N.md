# El bot de n8n con varios usuarios

Antes el agente leía un único `estado_actual.json` público. Ahora cada
usuario tiene sus lotes, así que el bot tiene que saber **quién le escribe**
y responder solo sobre los lotes de esa persona.

La identidad sale del chat de Telegram: cada usuario vincula su chat desde
el tablero (**Cuenta → Vincular Telegram**) y Supabase guarda a qué cuenta
pertenece. El bot nunca le cree al texto del mensaje quién es el usuario:
usa siempre el `chat.id` que entrega Telegram.

---

## Puesta en marcha

El flujo listo está en `n8n/bot_multiusuario.json` (lo arma a partir de
`n8n/bot_original_v7.json`, el bot de un solo lote).

1. **Esquema**: volvé a correr `supabase/schema.sql` entero en el SQL Editor
   (trae `historico_por_chat`, nueva).
2. **Clave propia para n8n**: Supabase → Settings → API Keys → Secret keys →
   New secret key, con el nombre `n8n_bot`. Si alguna vez se filtra se borra sin
   cortar GitHub Actions, que usa `github_actions`.
3. **Credencial en n8n**: Credentials → New → **Custom Auth**, con nombre
   `Supabase (n8n)` y este JSON (con la clave del paso 2 en los dos lugares):

   ```json
   {
     "headers": {
       "apikey": "sb_secret_...",
       "Authorization": "Bearer sb_secret_..."
     }
   }
   ```

   Son los mismos dos encabezados que usa la corrida diaria (`src/almacen.py`).
   La clave queda en n8n, nunca en el flujo exportado.
4. **Importar**: en n8n, Workflows → Import from File →
   `n8n/bot_multiusuario.json`. En los nodos `vincular_telegram`,
   `estado_por_chat` e `historico_del_lote` elegí la credencial
   `Supabase (n8n)`; en los de Telegram y Gemini, las que ya tenías.
5. **Activar**: desactivá el flujo viejo (`balance_hidrico_telegram_y_local`)
   y activá el nuevo. Telegram entrega los mensajes a un solo webhook por bot:
   si quedan los dos activos, el último que se activó se queda con todo.

La clave secreta puede leer todo, por eso el flujo solo llama a tres
funciones de la base, que ya filtran por chat:

- `vincular_telegram(p_codigo, p_chat_id)`
- `estado_por_chat(p_chat_id)`
- `historico_por_chat(p_chat_id, p_lote_id, p_desde, p_hasta)`: la serie
  diaria de la campaña vigente, como mucho 400 días. Si el lote no es de
  ese chat devuelve una lista vacía.

El flujo nuevo no tiene el disparador de chat local del original: sin un chat
de Telegram no hay forma de saber de quién son los lotes. Se prueba desde
Telegram.

---

## El flujo

```
Telegram Trigger
   │
   ├─ ¿el texto es "/vincular CODIGO" o "/start CODIGO"?
   │      sí → HTTP: vincular_telegram → responder
   │
   └─ no → HTTP: estado_por_chat
              │
              ├─ null → responder "este chat no está vinculado"
              └─ datos → Agente IA (con los lotes del usuario) → responder
```

### 1. Detectar la vinculación

Nodo **IF** con la expresión:

```
{{ /^\/(vincular|start)\s+[A-Za-z0-9]{6}$/.test($json.message.text.trim()) }}
```

(El enlace del tablero abre el bot con `/start CODIGO`; quien copia el
mensaje manda `/vincular CODIGO`. Los dos sirven.)

### 2. Vincular

Nodo **HTTP Request**:

- Method: `POST`
- URL: `https://xxxx.supabase.co/rest/v1/rpc/vincular_telegram`
- Body (JSON):

  ```
  {
    "p_codigo": "{{ $json.message.text.trim().split(/\s+/)[1] }}",
    "p_chat_id": "{{ String($json.message.chat.id) }}"
  }
  ```

Devuelve el nombre del usuario si salió bien, o `null` si el código no
existe o venció (dura 15 minutos). Responder en consecuencia:

- nombre → "Listo, {{nombre}}: este chat quedó vinculado a tu cuenta.
  Vas a recibir acá los avisos de tus lotes."
- `null` → "El código no es válido o venció. Generá uno nuevo en el
  tablero, pestaña Cuenta."

### 3. Leer los lotes del usuario

Nodo **HTTP Request**:

- Method: `POST`
- URL: `https://xxxx.supabase.co/rest/v1/rpc/estado_por_chat`
- Body: `{ "p_chat_id": "{{ String($json.message.chat.id) }}" }`

Respuesta:

```json
{
  "usuario": "Diego",
  "lotes": [
    {
      "id": "…",
      "nombre": "Lote 20",
      "area_ha": 92.12,
      "fecha_siembra": "2026-05-28",
      "ultima_fecha_procesada": "2026-10-23",
      "ultimo_error": null,
      "resumen": { … el mismo contenido que tenía estado_actual.json … },
      "riegos_recientes": [ { "fecha": "2026-10-20", "mm": 25, "nota": "pivote 1" } ]
    }
  ]
}
```

Si la respuesta es `null`, el chat no está vinculado: responder "Este chat
no está vinculado a ninguna cuenta. Entrá al tablero → Cuenta → Vincular
Telegram." y **no** pasar nada al agente.

### 4. El agente

Reemplazá el nodo que antes descargaba `estado_actual.json` por la salida del
paso 3. En el prompt del sistema, agregá algo como:

> Estos son los lotes de {{usuario}}, con su último estado calculado:
> {{ JSON.stringify($json.lotes) }}
>
> Respondé solo sobre estos lotes. Si la pregunta no dice de qué lote y hay
> más de uno, respondé por todos brevemente o preguntá cuál. Si un lote tiene
> `resumen` en null todavía no se calculó; si tiene `ultimo_error`, decilo.
> Respetá las `advertencias` de cada resumen, igual que antes.

El contenido de cada `resumen` es el de `estado_actual.json`, así que las
instrucciones que ya tenía el agente siguen valiendo. La herramienta
`historico_del_lote` ya no baja el CSV del repo viejo: llama a
`historico_por_chat` con el `id` del lote y el período que pide la pregunta. Trae además
`perfil_de_suelo`: si el balance usa la calicata, la Carta de Suelos (con las
unidades y el agua útil a 2 m) o el perfil por defecto.

---

## Cuidado con los grupos

Si alguien vincula un **grupo** de Telegram, todos los miembros del grupo
ven lo que el bot responde ahí sobre los lotes de esa cuenta. Es útil para un
equipo de campo, pero conviene saberlo antes de hacerlo.
