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
| `camera` | Satélite Argentina | Animación satelital de todo el país (últimos ~100 min) | 20 min |
| `camera` | Satélite Argentina Infrarrojo | Igual, en infrarrojo | 20 min |
| `camera` | Satélite provincia | Animación satelital de tu provincia (según la resuelve SMN) | 20 min |
| `camera` | Satélite provincia Infrarrojo | Igual, en infrarrojo | 20 min |

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

### `camera` "Satélite Argentina" / "Satélite provincia" (y sus versiones infrarrojo)

Animaciones con imágenes reales del satélite geoestacionario GOES-East, de
los últimos ~100 minutos:

- **Satélite Argentina**: todo el país, con las provincias dibujadas.
- **Satélite provincia**: tu provincia (la que el propio SMN resuelve para
  tu ubicación), con sus departamentos/partidos dibujados.
- Las versiones normales son en **color real**; de noche pasan solas a una
  vista nocturna (nubes en gris sobre las luces de las ciudades).
- **Infrarrojo**: muestra la temperatura de los topes de nube — ideal para
  ver la estructura e intensidad de una tormenta. Trae una leyenda de
  colores.

En todas, un pin marca tu ubicación con la temperatura actual, y una línea
de tiempo abajo indica qué tan viejo es cada cuadro.

#### Mandarlas por WhatsApp (u otro chat)

La imagen de la cámara es un GIF animado, que se ve en el dashboard pero
WhatsApp no anima (sus "GIFs" en realidad son videos). Por eso cada cámara
expone también el atributo **`video_url`**: la misma animación como video
MP4 fluido (con cuadros intermedios), mucho más liviano. Es un endpoint de
la API de Home Assistant, así que pide autenticación (un token de acceso
de larga duración, o el token del Supervisor si lo pide un complemento).
[Hornero](https://github.com/pabloantonelli/hornero) lo usa solo: al
pasarle una de estas cámaras, manda el video.

## Créditos

Basado en el trabajo de [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
