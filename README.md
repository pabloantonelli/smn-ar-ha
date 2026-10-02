# SMN Argentina para Home Assistant

Lleva el clima oficial del [Servicio Meteorológico Nacional](https://www.smn.gob.ar)
a Home Assistant: clima actual y pronóstico de 7 días, alertas tempranas
por tipo de evento, avisos a muy corto plazo con precisión de punto exacto
(no por zona aproximada), y cámaras con radar de precipitación e imágenes
satelitales reales — en color, infrarrojo, fijas o animadas, de tu zona,
tu provincia o todo el país — todo con datos oficiales, no de un
agregador de terceros.

## Por qué dos componentes

`smn.gob.ar` no tiene una API pública. Su frontend consume una API JSON no
documentada (`ws1.smn.gob.ar/v1`) protegida por **Cloudflare**: hace falta
un JWT que solo se puede obtener resolviendo el challenge de Cloudflare con
un navegador real. Eso no es viable embebido en el proceso de Home
Assistant, así que se separa en dos partes:

- **`addons/smn_proxy`** — add-on de Home Assistant OS (Chromium headless +
  Selenium) que resuelve el challenge, mantiene el JWT vigente, y expone un
  proxy HTTP local con la API del SMN.
- **`custom_components/smn_ar`** — la integración de HA (weather,
  binary_sensors de alertas, sensor de resumen en texto, cámara de radar),
  que habla con ese proxy local en vez de pegarle directo a SMN.

## Instalación

1. Instalar el add-on **SMN Proxy** desde Supervisor → Add-on Store → (⋮) →
   Repositorios → agregar `https://github.com/pabloantonelli/smn-ar-ha`.
   Iniciarlo y esperar a que `http://<host-del-addon>:6942/smn/health`
   devuelva `"session_ready": true` (puede tardar ~30-40s la primera vez,
   mientras resuelve el challenge de Cloudflare).
2. Instalar la integración **SMN - Servicio Meteorológico Nacional** vía
   HACS: HACS → (⋮) → Repositorios personalizados → pegar la misma URL,
   categoría **Integration** → buscarla e instalarla.
3. Reiniciar Home Assistant.
4. Ajustes → Dispositivos y Servicios → Agregar integración → "SMN", con la
   latitud/longitud deseada y la URL del proxy (por defecto
   `http://localhost:6942`, ajustar al hostname real del add-on si hace
   falta — se ve en la pestaña "Info" del add-on).

## Entidades que expone

Se ve directo en HA vía Ajustes → Dispositivos y Servicios → SMN →
Documentación, o desde HACS al ver el repositorio.

**Sobre los nombres de entidad (`entity_id`)**: Home Assistant genera el
`entity_id` a partir del nombre traducido **en el momento en que la
entidad se crea por primera vez**. Para encontrar el `entity_id` real de
cualquiera de estas: Ajustes → Herramientas de desarrollo → Estados, y
filtrar por el nombre del dispositivo o una palabra del nombre visible.

| Plataforma | Nombre visible | Qué es | Actualiza cada |
|---|---|---|---|
| `weather` | (el nombre que le pusiste al configurar) | Clima actual + pronóstico de 7 días | 30 min |
| `binary_sensor` | Alerta meteorológica | ¿Hay alguna alerta activa hoy, de cualquier tipo? | 30 min |
| `binary_sensor` ×11 | Alerta por tormenta / lluvia / nieve / viento / viento zonda / altas y bajas temperaturas / niebla / polvo / humo / ceniza volcánica | Una por tipo de evento | 30 min |
| `binary_sensor` | Alerta por granizo | ¿Hay un aviso vigente que mencione caída de granizo? | 10-30 min |
| `binary_sensor` | Alerta a corto plazo | ¿Tu ubicación exacta está dentro de una zona de alerta activa ahora? | 10 min |
| `sensor` | Pronóstico de corto plazo | Resumen en una frase de la situación de corto plazo | 10 min |
| `sensor` | Avisos por provincia (país) | Avisos activos en todo el país, agrupados por provincia | 10 min |
| `sensor` | Temperatura | Temperatura actual | 30 min |
| `sensor` | Sensación térmica | Temperatura percibida actual | 30 min |
| `sensor` | Humedad | Humedad relativa actual | 30 min |
| `sensor` | Velocidad del viento | Velocidad del viento actual | 30 min |
| `sensor` | Dirección del viento | Dirección del viento actual (punto cardinal) | 30 min |
| `sensor` | Pronóstico de hoy | Condición, máxima y mínima de hoy | 30 min |
| `sensor` | Pronóstico de mañana | Condición, máxima y mínima de mañana | 30 min |
| `sensor` | Próxima lluvia | Cuándo es el próximo período con probabilidad de lluvia relevante | 30 min |
| `camera` | Radar | Foto del radar de precipitación, con clima actual y zonas de alerta | 10 min |
| `camera` | Satélite | Foto satelital alrededor de tu ubicación, con flecha de deriva de nubes | 10 min |
| `camera` | Satélite Infrarrojo | Igual, en infrarrojo (de noche y siempre útil para ver tormentas) | 10 min |
| `camera` | Satélite (animado) | GIF de la última hora y media de imágenes satelitales | 20 min |
| `camera` | Satélite Infrarrojo (animado) | Igual, en infrarrojo | 20 min |
| `camera` | Satélite Argentina | Foto satelital de todo el país | 15 min |
| `camera` | Satélite Argentina Infrarrojo | Igual, en infrarrojo | 15 min |
| `camera` | Satélite Argentina (animado) | GIF de todo el país | 30 min |
| `camera` | Satélite Argentina Infrarrojo (animado) | Igual, en infrarrojo | 30 min |
| `camera` | Satélite provincia | Foto satelital de tu provincia (según la resuelve SMN) | 15 min |
| `camera` | Satélite provincia Infrarrojo | Igual, en infrarrojo | 15 min |
| `camera` | Satélite provincia (animado) | GIF de tu provincia | 30 min |
| `camera` | Satélite provincia Infrarrojo (animado) | Igual, en infrarrojo | 30 min |

### `weather`: clima actual y pronóstico

La entidad de clima estándar de HA: temperatura, sensación térmica,
humedad, presión, viento, visibilidad, y un pronóstico de 7 días con
detalle por franja horaria (madrugada/mañana/tarde/noche).

### `binary_sensor` "Alerta por `<evento>`" (los 11 sensores por tipo)

Uno por cada tipo de alerta temprana del SMN. `on` = ese tipo de evento
tiene alerta activa hoy (amarilla/naranja/roja); `off` = sin alerta de ese
tipo. "Alerta meteorológica" es el resumen: `on` si cualquiera de los 11
está activo.

### `binary_sensor` "Alerta por granizo"

Indica si algún aviso vigente menciona caída de granizo. Atributos:
`match_count` y `matching_texts`, con el texto exacto que lo disparó.

### `binary_sensor` "Alerta a corto plazo": ¿estoy en zona de peligro?

La respuesta directa a "¿mi ubicación está dentro de algún polígono de
alerta ahora mismo?" (no una zona cercana, tu punto exacto).

- `on`: tu ubicación está dentro de al menos un aviso vigente de corto
  plazo (validez 1-2h).
- `off`: no lo está, aunque haya avisos activos en otras zonas (para eso
  está "Avisos por provincia (país)").

Atributos cuando está `on`: `alert_count`, y `alerts` con el detalle
completo (título, vigencia, zonas, severidad, e `instructions` con las
medidas de protección recomendadas).

### `sensor` "Pronóstico de corto plazo"

Resume en una frase la situación de corto plazo para tu ubicación, con
esta prioridad: aviso de corto plazo vigente (tormenta, granizo, etc.) →
alerta por evento activa hoy → alerta de ola de calor/frío → resumen del
pronóstico de hoy si no hay nada de lo anterior. El detalle completo de
cada caso queda en los atributos (`instrucciones`, `avisos_corto_plazo`,
`alertas_activas`, según corresponda).

### `sensor` "Avisos por provincia (país)"

Avisos a muy corto plazo vigentes en cualquier parte de Argentina ahora
mismo, agrupados por provincia — a diferencia del resto de los sensores
(específicos a tu ubicación), este es el panorama nacional.

### `sensor` temperatura / sensación térmica / humedad / viento / pronóstico de hoy y mañana

Los mismos datos que ya están en `weather`, pero como entidades
independientes — para graficarlos en Historial, usarlos en una condición
simple de automatización, o exponerlos a Alexa/Google como sensor suelto.
La dirección del viento también trae el valor exacto en grados en el
atributo `degrees`.

### `sensor` "Próxima lluvia"

Cuándo es el próximo período del pronóstico con probabilidad de lluvia
relevante (30% o más). El estado es un horario (Home Assistant lo muestra
como "en 3 horas"), y los atributos traen `rain_expected`, `probability`
y `condition`. Si no hay nada así de probable en el pronóstico disponible,
el estado queda vacío.

### Evento `smn_ar_shortterm_alert_changed`

Se dispara cada vez que cambia el conjunto de avisos a muy corto plazo
vigentes para tu ubicación — aparece uno nuevo, o se levanta uno
existente — para que una automatización reaccione al instante en vez de
tener que sondear el sensor "Pronóstico de corto plazo".

Datos del evento:

```yaml
entry_id: "<id de esta instancia de la integración>"
added:        # avisos nuevos desde la última actualización (puede estar vacío)
  - title: "Aviso por tormentas fuertes"
    date: "2026-10-02T18:00:00"
    end_date: "2026-10-02T20:00:00"
    zones: ["CORDOBA: Capital, ..."]
    instructions: "..."
removed_count: 0   # cantidad de avisos que estaban vigentes y dejaron de estarlo
current:       # lista completa de avisos vigentes después del cambio
  - ...
```

Ejemplo de automatización (notificar sólo cuando aparece un aviso nuevo):

```yaml
trigger:
  - trigger: event
    event_type: smn_ar_shortterm_alert_changed
condition:
  - condition: template
    value_template: "{{ trigger.event.data.added | length > 0 }}"
action:
  - action: notify.mobile_app_tu_telefono
    data:
      title: "Nuevo aviso SMN"
      message: "{{ trigger.event.data.added[0].title }}"
```

### `camera` "Radar"

Foto con el radar de precipitación de tu zona (~300km alrededor),
temperatura y pronóstico de las próximas horas, y cualquier zona de
alerta activa cercana dibujada sobre el mapa.

### `camera` "Satélite" / "Satélite Argentina" / "Satélite provincia" (y sus variantes infrarrojo/animado)

Imágenes reales del satélite geoestacionario GOES-East:

- **Satélite**: zoom fijo alrededor de tu ubicación (~700km), con una
  flecha que indica hacia dónde se están desplazando las nubes cuando hay
  una señal confiable (no es un dato de viento real, es aproximado).
- **Satélite Argentina** / **Satélite provincia**: zoom ajustado para que
  entre todo el país o toda tu provincia en el cuadro, con las provincias
  (en el mapa de Argentina) o los departamentos/partidos (en el de tu
  provincia) dibujados como referencia. "Satélite provincia" usa la
  provincia que el propio SMN resuelve para tu ubicación.
- **Infrarrojo**: la misma vista, pero con la capa de infrarrojo en vez
  de color real — útil de noche (el color real se ve negro) y en general
  para ver mejor la estructura de una tormenta. Trae una leyenda de
  colores en la esquina.
- **Animado**: un GIF con los últimos ~100 minutos, pensado para
  compartirse como **URL** (ver abajo), no como adjunto de una foto.

En todas, un pin marca tu ubicación exacta con la temperatura actual, y
las animaciones muestran una línea de tiempo abajo indicando qué tan
viejo es cada cuadro.

#### Cómo obtener la URL del GIF animado para compartirlo

La cámara "Satélite (animado)" (y, en general, cualquier `camera` de Home
Assistant) expone su imagen actual en el atributo `entity_picture`, que ya
viene como una URL firmada por HA (el mismo mecanismo que usan las
tarjetas de cámara del dashboard — no hace falta login para acceder a
ella).

1. **Encontrar el `entity_id` exacto**: Ajustes → Herramientas de
   desarrollo → Estados, buscar "Satélite (animado)" (ver la nota sobre
   `entity_id` más arriba en este README si no aparece con ese nombre
   exacto).
2. **Ver la URL**: en esa misma pantalla, mirar el atributo
   `entity_picture` del estado — algo como
   `/api/camera_proxy/camera.<tu_nombre>_satelite_animado?token=xxxxxxxx`.
   Ese token es temporal y HA lo renueva solo; no hace falta (ni conviene)
   guardarlo como fijo en ningún lado.
3. **Convertirla en una URL completa y accesible desde afuera de tu red**:
   anteponer tu URL externa de Home Assistant (Ajustes → Sistema →
   General → "URL externa de Home Assistant", o tu dominio de Nabu Casa si
   la usás):
   ```
   https://tu-dominio-externo.com/api/camera_proxy/camera.<tu_nombre>_satelite_animado?token=xxxxxxxx
   ```
   Si HA no tiene una URL externa configurada (sin Nabu Casa, reverse
   proxy o port-forward), esa URL solo funciona dentro de tu red local —
   eso depende de tu configuración de HA, no de esta integración.

**Para automatizar el envío** (ej. mandarla por Telegram/WhatsApp cada
tanto), en una plantilla Jinja de una automatización:

```yaml
{{ state_attr('camera.<tu_nombre>_satelite_animado', 'entity_picture') }}
```

Esto da el *path* relativo (`/api/camera_proxy/...?token=...`); si el
servicio al que se la mandás necesita la URL absoluta, hay que
concatenarle el dominio externo a mano en la plantilla, ej.:

```yaml
{{ 'https://tu-dominio-externo.com' ~ state_attr('camera.<tu_nombre>_satelite_animado', 'entity_picture') }}
```

## Créditos

Basado en el trabajo de [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
