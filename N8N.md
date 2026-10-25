# El bot de n8n con varios usuarios

Antes el agente leía un único `estado_actual.json` público. Ahora cada
usuario tiene sus lotes, así que el bot tiene que saber **quién le escribe**
y responder solo sobre los lotes de esa persona.

La identidad sale del chat de Telegram: cada usuario vincula su chat desde
el tablero (**Cuenta → Vincular Telegram**) y Supabase guarda a qué cuenta
pertenece. El bot nunca le cree al texto del mensaje quién es el usuario:
usa siempre el `chat.id` que entrega Telegram.

---

## Credencial en n8n

**Credentials → New → Header Auth**, una sola vez:

| Name | Value |
|---|---|
| `apikey` | la clave **service_role** de Supabase |

Y en cada nodo HTTP, además, el header
`Authorization: Bearer <service_role>` (o una segunda credencial Header Auth
con ese header). La clave queda en n8n, nunca en el flujo exportado.

La clave service_role puede leer todo, por eso el flujo solo llama a dos
funciones de la base, que ya filtran por chat:

- `vincular_telegram(p_codigo, p_chat_id)`
- `estado_por_chat(p_chat_id)`

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
      "resumen": { … el mismo contenido que tenía estado_actual.json … }
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

El contenido de cada `resumen` es exactamente el de `estado_actual.json`,
así que las instrucciones que ya tenía el agente siguen valiendo.

---

## Cuidado con los grupos

Si alguien vincula un **grupo** de Telegram, todos los miembros del grupo
ven lo que el bot responde ahí sobre los lotes de esa cuenta. Es útil para un
equipo de campo, pero conviene saberlo antes de hacerlo.
