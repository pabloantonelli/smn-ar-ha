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
| `binary_sensor` | Alerta por tormenta / lluvia / viento / nevada / viento zonda | **Por cercanía**: hay un aviso a corto plazo de ese tipo a menos del radio configurado | 10 min |
| `binary_sensor` | Alerta por granizo | **Por cercanía**: hay un aviso a corto plazo que menciona granizo a menos del radio | 10 min |
| `binary_sensor` | Alerta a corto plazo | **Por cercanía**: hay cualquier aviso a corto plazo a menos del radio | 10 min |
| `binary_sensor` | Alerta por altas / bajas temperaturas / niebla / polvo / humo / ceniza volcánica | **Por zona**: el SMN tiene esa alerta para tu zona en la franja actual del día | 30 min |
| `binary_sensor` | Alerta meteorológica | **Anticipo por zona**: el SMN tiene alguna alerta para tu zona en la franja actual del día | 30 min |
| `sensor` | Pronóstico de corto plazo | Resumen en una frase de la situación de corto plazo | 10 min |
| `sensor` | Avisos por provincia (país) | Avisos activos en todo el país, agrupados por provincia | 10 min |
| `sensor` | Temperatura | Temperatura actual | 30 min |
| `sensor` | Sensación térmica | Temperatura percibida actual | 30 min |
| `sensor` | Humedad | Humedad relativa actual | 30 min |
| `sensor` | Velocidad del viento | Velocidad del viento actual | 30 min |
| `sensor` | Dirección del viento | Dirección del viento actual (punto cardinal) | 30 min |
| `sensor` | Pronóstico de hoy | Condición, máxima y mínima de hoy | 30 min |
| `sensor` | Pronóstico de mañana | Condición, máxima y mínima de mañana | 30 min |
| `sensor` | Próxima lluvia o tormenta | Cuándo es la próxima lluvia o tormenta, combinando avisos cercanos, alertas de tu zona y el pronóstico | 30 min |
| `camera` | Radar | Foto del radar de precipitación, con clima actual y zonas de alerta | 10 min |
| `camera` | Avisos Argentina | Mapa de todo el país con los polígonos de todos los avisos a corto plazo vigentes | 10 min |
| `camera` | Satélite Argentina | Animación satelital de todo el país (últimos ~100 min) | 20 min |
| `camera` | Satélite Argentina Infrarrojo | Igual, en infrarrojo | 20 min |
| `camera` | Satélite provincia | Animación satelital de tu provincia (según la resuelve SMN) | 20 min |
| `camera` | Satélite provincia Infrarrojo | Igual, en infrarrojo | 20 min |

### `weather`: clima actual y pronóstico

La entidad de clima estándar de HA: temperatura, sensación térmica,
humedad, presión, viento, visibilidad, y un pronóstico de 7 días con
detalle por franja horaria (madrugada/mañana/tarde/noche).

### Alertas (`binary_sensor`): por cercanía o por zona

El SMN publica dos tipos de información de alerta, y cada entidad usa la
más precisa que existe para su fenómeno. Todas tienen el atributo
`criterio`, que explica en una frase qué la enciende.

**Radio de alertas cercanas**: es uno solo para todas las alertas por
cercanía (30 km por defecto). Se cambia en Ajustes → Dispositivos y
Servicios → SMN → Configurar.

#### Por cercanía: tormenta, lluvia, viento, nevada, viento zonda, granizo y "Alerta a corto plazo"

Usan los **avisos a muy corto plazo** de todo el país (validez de 1 a 2 h).
Cada aviso trae el polígono del SMN, así que se puede medir la distancia
real desde tu ubicación (0 km = estás adentro). La entidad se enciende si
el aviso más cercano de ese tipo está dentro del radio.

- El tipo sale del título del aviso. Por ejemplo, "TORMENTAS FUERTES CON
  LLUVIAS INTENSAS, RÁFAGAS Y GRANIZO" enciende tormenta, lluvia, viento y
  granizo. "Viento" no incluye los avisos de viento zonda, que tienen su
  propia entidad.
- "Alerta a corto plazo" se enciende con cualquier aviso dentro del radio.
  Sus atributos traen `alert_count` y `alerts`, con el detalle de cada aviso
  dentro del radio (distancia, dirección, vigencia, zonas e `instructions`).
- Atributos: `distance_km` y `direction` (hacia dónde está el aviso),
  `title`, `end_date`, `zones` y `radius_km`. Se muestran aunque el aviso
  esté fuera del radio, para que veas qué tan lejos está.
- Tormenta, lluvia, viento, nevada y zonda también traen, como contexto, el
  pronóstico de tu zona para ese evento: `zone_level_now`,
  `zone_max_level_today` y `zone_levels` (por franja). Ese pronóstico **no**
  las enciende, porque una zona del SMN puede medir cientos de km.
- "Alerta por granizo" trae además `zone_alert_mentions`: si el texto de la
  alerta de tormenta de tu zona menciona granizo.

> "Granizo cercano" se fusionó en "Alerta por granizo", que ahora funciona
> por cercanía. La entidad vieja se borra sola al actualizar. Si tenías
> configurado su radio, se sigue usando hasta que guardes uno nuevo.

#### Por zona: temperaturas, niebla, polvo, humo, ceniza volcánica y "Alerta meteorológica"

El SMN no publica polígonos para estos fenómenos: sólo hay un pronóstico
de alertas para la **zona** del SMN a la que pertenece tu ubicación (un
departamento o un grupo de departamentos). Ese pronóstico trae un nivel por
**franja del día**: madrugada (0 a 6 h), mañana (6 a 12 h), tarde (12 a
18 h) y noche (18 a 24 h). La entidad se enciende sólo durante las franjas
con alerta (amarilla, naranja o roja) y cambia de estado al empezar cada
franja.

- Atributos: `level`, `level_name`, `color` y `severity` de la franja
  actual, `period`, `max_level_today`, `levels` (todas las franjas de hoy),
  `description` e `instruction`.
- **Alerta meteorológica** es el resumen y el **anticipo**: se enciende si
  tu zona tiene alguna alerta, de cualquier tipo (también tormenta o
  lluvia), en la franja actual. Se emite horas antes de que lleguen los
  avisos a corto plazo, pero no mide distancia. Atributos: `active_alerts`
  (las de la franja actual) y `today_alerts` (todo lo pronosticado para
  hoy, con sus franjas).
- El texto de `description` es genérico del SMN para ese nivel de alerta y
  puede mencionar otras regiones del país. Por ejemplo: "En cambio en el
  norte del Litoral…".

### `sensor` "Pronóstico de corto plazo"

Resume en una frase la situación de corto plazo para tu ubicación, con
esta prioridad: aviso de corto plazo vigente sobre tu ubicación (tormenta,
granizo, etc.) → alerta de tu zona en la franja actual → alerta de ola de
calor/frío → resumen del
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

### `sensor` "Próxima lluvia o tormenta"

Cuándo es la próxima lluvia o tormenta. El estado es un horario (Home
Assistant lo muestra como "en 3 horas") y queda vacío si no hay nada
pronosticado. Combina tres fuentes del SMN y se queda con la más próxima:

1. **Aviso cercano**: un aviso a corto plazo de lluvia o tormenta dentro
   del radio de alertas cercanas. Ya está ocurriendo cerca.
2. **Alerta de zona**: una alerta de lluvia o tormenta para tu zona, por
   franja del día (hoy y los próximos días).
3. **Pronóstico** de tu localidad: una franja con 30% o más de
   probabilidad de lluvia, o con condición de tormenta aunque la
   probabilidad sea baja.

Si dos fuentes coinciden en la misma franja, gana la más fuerte (aviso,
después alerta, después pronóstico). Por ejemplo, en vez de "lluvia 40%"
muestra "tormenta, alerta naranja".

Atributos:

- `tipo`: `lluvia` o `tormenta`. Sirve para que una automatización
  reaccione sólo a tormentas.
- `fuente`: `aviso cercano`, `alerta de zona` o `pronóstico`.
- `en_curso`: `true` si ya empezó (un aviso o una franja vigente). En ese
  caso el estado es el horario de inicio y se ve como "hace X".
- `proxima_tormenta`: el horario de la próxima tormenta, aunque antes
  venga lluvia.
- `probability` y `condition` del pronóstico para esa franja.
- `level` y `level_name`, si viene de una alerta.
- `distance_km`, `direction`, `title` y `end_date`, si viene de un aviso.
- `rain_expected` y `criterio`.

> Antes se llamaba "Próxima lluvia" y sólo usaba el pronóstico. Es la
> misma entidad, con el mismo `entity_id`.

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

### `camera` "Avisos Argentina"

Mapa de todo el país (OpenStreetMap) con las provincias y los polígonos de
**todos** los avisos a corto plazo vigentes, tal como los publica el SMN,
más un pin en tu ubicación. Abajo indica cuántos avisos hay vigentes.

### `camera` "Satélite Argentina" / "Satélite provincia" (y sus versiones infrarrojo)

Animaciones con imágenes reales del satélite geoestacionario GOES-East, de
los últimos ~100 minutos, como video MP4 corto en loop (con cuadros
intermedios para que el movimiento sea fluido):

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

La imagen de la cámara es un GIF animado, así que se ve animada en
cualquier tarjeta de cámara de Home Assistant y en cualquier navegador.
Además, el `entity_picture` de la entidad apunta a la misma animación como
video MP4 en loop: más fluido (con cuadros intermedios) y más liviano, ideal
para mandar por chat. Para ver el video en el dashboard, la integración trae
una tarjeta propia (no hace falta instalar nada aparte), que también sirve
para el radar y el mapa de avisos.

Se agrega desde el editor del dashboard: **Agregar tarjeta → SMN Mapa /
Satélite**, y se configura de forma visual (la cámara se elige de una lista
con solo las de SMN). En YAML, todas las opciones son opcionales salvo
`entity`:

```yaml
type: custom:smn-ar-video-card
entity: camera.cordoba_satelite_provincia_infrarrojo
name: Satélite            # por defecto, el nombre de la entidad
name_position: below      # below (debajo) | overlay (sobre la imagen) | hidden
aspect_ratio: auto        # auto | 16:9 | 4:3 | 1:1 | 3:4
fit: cover                # cover (llena, recorta bordes) | contain (sin recortar)
tap_action: play_pause    # play_pause | more_info | fullscreen | none
autoplay: true            # false: imagen fija hasta tocarla
playback_rate: 1          # 0.5 | 0.75 | 1 | 1.5 | 2
show_controls: false      # controles de video del navegador
show_updated: true        # hora de la última actualización de la animación
```

El video se reproduce en loop y sin sonido, y se actualiza solo cuando se
regenera la animación.

## Créditos

Basado en el trabajo de [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
