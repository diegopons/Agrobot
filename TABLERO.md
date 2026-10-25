# La aplicación web

Todo el sistema se maneja desde una sola página, publicada con GitHub Pages
desde la carpeta `docs/`. Para conectarla con Supabase, ver
[SUPABASE.md](SUPABASE.md).

## Ingreso

Cada persona entra con su correo: la página le manda un enlace y con eso
queda adentro, sin contraseña. Cada cuenta ve **solo sus lotes**; eso lo
garantiza la base de datos, no la página.

Arriba está el **selector de lotes**: cambia de lote en todas las pestañas a
la vez. **+ Nuevo lote** arranca uno en blanco. La página recuerda el último
lote elegido en ese navegador.

Tiene cuatro pestañas.

---

## Estado

El resultado del último día calculado del lote elegido.

- **Condición** (Óptimo / Adecuado / Déficit / Crítico) y el porcentaje de
  agua útil.
- **Perfil del suelo**: un corte hasta 2 metros con la línea de profundidad
  de raíz y una franja que representa qué proporción de la capacidad está
  ocupada por agua disponible. Se llena de izquierda a derecha a propósito:
  un llenado desde abajo sugeriría una napa, y el agua útil está distribuida
  en toda la zona de raíces.
- **La campaña hasta hoy**: el % de agua útil a lo largo del ciclo, con la
  banda que marca la zona por debajo del umbral y las lluvias colgando desde
  arriba.
- **Vigor del cultivo**: la curva de NDVI.
- **Últimos catorce días** en tabla.

Si corresponde regar, aparece la lámina sugerida, con el umbral y la
eficiencia de ese lote.

### Riegos aplicados

Al pie de la pestaña se carga cada riego: fecha, lámina en milímetros (con
coma o punto) y una nota opcional. Hay uno por día y por lote; cargar otro el
mismo día lo reemplaza.

- El riego **entra al balance como la lluvia, por separado**: hasta 15 mm
  entra completo, y por encima se descuenta el escurrimiento con la misma
  fórmula (2,43 · R^0,667). La serie guarda el aplicado (`riego_mm`) y el
  que entró al perfil (`riego_efectivo_mm`).
- Lo normal es cargar el riego de un día que **ya está calculado**. En ese
  caso, la corrida de la hora siguiente vuelve al día anterior al riego y
  recalcula desde ahí. Los días previos no cambian y no se abre una serie
  nueva. Lo mismo al corregir o borrar un riego.

## Lote

El polígono que define el área de cálculo.

- Mapa satelital donde podés **dibujar** el lote o **subir un archivo**
  (GeoJSON, KML o un ZIP con el shapefile completo).
- Medidas: superficie, perímetro, vértices y centro. Conviene verificar que
  la superficie sea la que esperás antes de guardar.
- **Evolución del NDVI**: un mosaico con una imagen por fecha con pasada
  satelital útil, de la más antigua a la más reciente. Sirve para ver si un
  valor raro en la curva fue una nube sobre parte del lote o algo real en
  todo el cultivo. Las zonas grises son nube o sombra detectada.

### Suelo del lote

Debajo del mapa, lo que dice la **Carta de Suelos de Córdoba** (IDECOR) sobre
ese polígono. El sistema la consulta solo, para todos los lotes de todos los
usuarios, en la primera corrida después de guardar o cambiar el polígono.

- Agua útil a 1 m, 1,5 m y 2 m, ponderada por la superficie del lote en cada
  unidad cartográfica (método de Faule et al., 2024, INTA EEA Manfredi).
- Capacidad de uso e índice de productividad.
- Las unidades que cruza el lote, con sus series y el porcentaje de cada una.
- Limitantes edáficas: textura superficial, drenaje, profundidad efectiva,
  salinidad y alcalinidad.

**Qué perfil usa el balance**, en este orden:

1. La calicata o el perfil cargado en Parámetros.
2. El de la carta, si las unidades con dato cubren al menos la mitad del lote.
3. El perfil por defecto (serie Oncativo).

Sin calicata, la tabla de la calicata muestra el perfil de la carta como punto
de partida; cargarle los valores medidos lo reemplaza en ese lote. La carta se
consulta una vez por polígono: si el servicio de IDECOR se cae, ese día se usa
el perfil por defecto y se reintenta en la corrida siguiente.

## Parámetros

Todo lo propio del lote, y el botón para guardarlo.

| Campo | Qué hace |
|---|---|
| Nombre del lote | Aparece en el aviso de Telegram y es como lo nombrás al bot. No se puede repetir dentro de la misma cuenta |
| Fecha de siembra | Desde cuándo calcular |
| Agua útil inicial | Lámina disponible estimada a la siembra |
| Profundidad de raíz inicial | En centímetros |
| Umbral de riego | % de agua útil que dispara la alerta |
| Eficiencia de aplicación | Para pasar de lámina neta a bruta |
| Cuándo avisar | Modo de notificación (ver TELEGRAM.md), o «Nunca» |
| Calcular este lote | Destildado, el lote queda pausado y conserva su serie |
| Recalcular desde la siembra | Fuerza el recálculo en la próxima corrida |

Debajo está la **calicata de siembra**. Se guarda junto con el lote, y solo
si la tocaste: guardar el lote sin abrir la calicata no le cambia el perfil.

**Guardar lote** escribe todo de una vez. Los resultados aparecen solos:

- un lote **nuevo o modificado** se calcula en la corrida de la hora
  siguiente;
- después, todos los lotes se actualizan **una vez por día**, a las 6 de la
  mañana.

**Borrar lote** lo elimina con toda su serie. Para dejar de calcularlo sin
perder nada, destildá «Calcular este lote».

## Cuenta

- **Vincular Telegram**: genera un código de un solo uso, válido 15 minutos.
  El enlace abre el bot y lo manda solo; si no, se le escribe
  `/vincular CODIGO`. Desde ahí, los avisos de todos tus lotes llegan a ese
  chat, y el bot responde ahí sobre ellos (ver N8N.md).
- **Nombre**: con el que te saluda el bot.

---

## Cuándo se recalcula solo el balance

El sistema guarda una huella del polígono, la fecha de siembra, las fuentes
de clima y el perfil de suelo. Si cualquiera cambia, el balance acumulado ya
no corresponde: se recalcula desde la siembra automáticamente.

Lo mismo pasa al cambiar el agua útil o la raíz inicial de un lote ya
calculado: solo se usan el día de la siembra, así que la página marca el
recálculo para que el cambio tenga efecto.

En todos los casos **la serie anterior no se pierde**: queda guardada como
una serie anterior del lote (`balance_diario.serie`), y la página muestra la
vigente. Mezclarlas daría una curva continua que nunca existió.

---

## Verlo sin publicarlo

Abrí una consola en `docs`, ejecutá `python -m http.server`, y entrá a
`http://localhost:8000`. Para poder ingresar, agregá esa dirección en
Supabase → Authentication → URL Configuration → Redirect URLs.

---

## Qué queda expuesto

El repositorio es público, que es lo que permite publicar la página sin
costo. Desde el modo multiusuario:

**No queda expuesto**: los lotes, las series, los resúmenes ni los mosaicos.
Viven en Supabase y cada cuenta accede solo a lo suyo. Tampoco las claves:
están en los Secrets de GitHub y en n8n.

**Sí queda visible**: el código y `docs/config.js`, que tiene la URL del
proyecto de Supabase y la clave *anon*. Están pensadas para ser públicas: sin
una sesión iniciada no dan acceso a ningún dato.

> Los datos del lote 20 anteriores a la migración siguen en el repositorio y
> en su historial. Ver SUPABASE.md, «Lo que queda público y lo que no».

---

## Un valor fijo en la página

`PROF_MAX = 200` (profundidad del perfil, en cm), al principio del bloque
`<script>` de `docs/index.html`, define la escala del dibujo del perfil. El
umbral y la eficiencia ya no están duplicados: la página los toma de cada
lote.
