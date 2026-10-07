# SMN Argentina para Home Assistant

El clima oficial del [Servicio Meteorológico Nacional](https://www.smn.gob.ar)
en Home Assistant, con datos del SMN y no de un agregador de terceros.

- Clima actual y pronóstico de 7 días.
- Alertas que se encienden sólo si el fenómeno está **cerca de tu
  ubicación** (radio configurable), y alertas de tu zona según la franja
  del día.
- Cuándo viene la próxima lluvia o tormenta.
- Cámaras con radar, mapa de avisos del país e imágenes satelitales
  animadas (color e infrarrojo).

## Instalación

El SMN no tiene una API pública, y la que usa su web está protegida por
Cloudflare. Por eso la integración necesita un add-on que hace de proxy (ver
[Detalles técnicos](#detalles-técnicos)).

1. **Add-on SMN Proxy**: Ajustes → Complementos → Tienda → (⋮) →
   Repositorios → agregar `https://github.com/pabloantonelli/smn-ar-ha`.
   Instalarlo e iniciarlo. La primera vez tarda unos 30–40 s en estar listo.
2. **Integración**: HACS → (⋮) → Repositorios personalizados → la misma URL,
   categoría **Integration** → instalar **SMN - Servicio Meteorológico
   Nacional** y reiniciar Home Assistant.
3. Ajustes → Dispositivos y Servicios → Agregar integración → **SMN**.

## Configuración

- **Al agregar la integración**: la ubicación (latitud y longitud) y la URL
  del proxy. El valor por defecto es `http://localhost:6942`. Si no
  funciona, usá el hostname que figura en la pestaña "Info" del add-on.
- **Opciones** (Dispositivos y Servicios → SMN → Configurar): el **radio de
  alertas cercanas**, 30 km por defecto. Lo usan todas las alertas por
  cercanía y "Próxima lluvia o tormenta".

## Entidades

### Alertas

| Nombre | Se enciende cuando… |
|---|---|
| Alerta por tormenta / lluvia / viento / nevada / viento zonda | hay un aviso a corto plazo de ese tipo **dentro del radio** |
| Alerta por granizo | hay un aviso a corto plazo que menciona granizo **dentro del radio** |
| Alerta a corto plazo | hay cualquier aviso a corto plazo **dentro del radio** |
| Alerta por altas / bajas temperaturas / niebla / polvo / humo / ceniza volcánica | el SMN tiene esa alerta para **tu zona** en la franja actual del día |
| Alerta meteorológica | el SMN tiene **cualquier** alerta para tu zona en la franja actual del día |

### Clima y pronóstico

| Nombre | Qué muestra |
|---|---|
| Clima (`weather`) | Clima actual y pronóstico de 7 días por franja (madrugada, mañana, tarde y noche) |
| Temperatura, Sensación térmica, Humedad, Velocidad y Dirección del viento | Los valores actuales, como sensores sueltos para gráficos y automatizaciones |
| Pronóstico de hoy / de mañana | Condición, máxima y mínima |
| Próxima lluvia o tormenta | Cuándo es la próxima lluvia o tormenta ([cómo se calcula](#próxima-lluvia-o-tormenta)) |
| Pronóstico de corto plazo | Una frase que resume la situación, lista para notificaciones o para leerla en voz alta |
| Avisos por provincia (país) | Los avisos vigentes en todo el país, agrupados por provincia |

### Cámaras

| Nombre | Qué muestra |
|---|---|
| Radar | Radar de precipitación de ~300 km alrededor, con el clima actual y los avisos cercanos |
| Avisos Argentina | Mapa del país con los polígonos de todos los avisos vigentes y tu ubicación |
| Satélite Argentina / Satélite provincia | Animación satelital de los últimos ~100 min en color real. De noche pasa sola a una vista nocturna |
| … Infrarrojo | Igual, mostrando la temperatura de los topes de nube. Sirve para ver la intensidad de una tormenta |

### Actualización

- **Avisos a corto plazo**: cada 10 min.
- **Clima, pronóstico y alertas de zona**: cada 30 min.
- **Radar y mapa de avisos**: cada 10 min.
- **Satélite**: cada 20 min.

Para encontrar el `entity_id` de una entidad, entrá a Herramientas de
desarrollo → Estados y filtrá por el nombre. Home Assistant lo arma con el
nombre que tenía la entidad cuando se creó.

## Cómo funcionan las alertas

El SMN publica dos tipos de información, y cada alerta usa la más precisa
que existe para su fenómeno. Todas tienen el atributo **`criterio`**, que
explica en una frase qué la enciende.

**Por cercanía** (tormenta, lluvia, viento, nevada, viento zonda, granizo y
"Alerta a corto plazo"):

- **Fuente**: los avisos a muy corto plazo del SMN. Duran 1 a 2 h y cada
  uno trae su polígono, así que se mide la distancia real desde tu
  ubicación. 0 km quiere decir que estás adentro.
- **Atributos**: `distance_km` y `direction` muestran dónde está el aviso
  más cercano, aunque esté fuera del radio.
- **Tipo de aviso**: sale del título. Por ejemplo, "TORMENTAS FUERTES CON
  LLUVIAS INTENSAS, RÁFAGAS Y GRANIZO" enciende tormenta, lluvia, viento y
  granizo. El viento zonda tiene su propia alerta.

**Por zona** (temperaturas, niebla, polvo, humo, ceniza volcánica y "Alerta
meteorológica"):

- **Fuente**: el SMN sólo da un pronóstico para la zona a la que pertenece
  tu ubicación (uno o varios departamentos).
- **Cuándo se encienden**: el pronóstico trae un nivel (amarillo, naranja o
  rojo) por franja del día: madrugada 0–6, mañana 6–12, tarde 12–18 y noche
  18–24. La alerta se enciende sólo durante las franjas con alerta.
- **Alerta meteorológica**: es el **anticipo**. Incluye también tormenta y
  lluvia, y se emite horas antes que los avisos, pero no mide distancia. El
  atributo `today_alerts` muestra todo lo pronosticado para hoy.
- **Ojo con `description`**: es el texto genérico del SMN para ese nivel y
  puede nombrar otras regiones del país.

## Próxima lluvia o tormenta

El estado es un horario: Home Assistant lo muestra como "en 3 horas" y queda
vacío si no hay nada pronosticado. Se queda con lo más próximo de tres
fuentes:

1. **Aviso cercano**: un aviso de lluvia o tormenta dentro del radio.
2. **Alerta de zona**: de lluvia o tormenta, por franja, para hoy y los
   próximos días.
3. **Pronóstico** de tu localidad: una franja con 30% o más de probabilidad
   de lluvia, o con tormenta aunque la probabilidad sea baja.

Si dos coinciden en la misma franja, gana la más fuerte: primero el aviso,
después la alerta y después el pronóstico.

Atributos principales:

- **`tipo`**: `lluvia` o `tormenta`.
- **`fuente`**: de cuál de las tres fuentes salió.
- **`en_curso`**: `true` si ya empezó. En ese caso el estado se ve como
  "hace X".
- **`proxima_tormenta`**: la próxima tormenta, aunque antes venga lluvia.

## Automatizaciones

El evento `smn_ar_shortterm_alert_changed` se dispara cuando aparece o se
levanta un aviso a corto plazo para tu ubicación. Trae estos datos:
`added` (los avisos nuevos), `removed_count` y `current` (los vigentes).

```yaml
# Notificar cuando aparece un aviso nuevo
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

"Alerta meteorológica" dispara además `smn_ar_alert_created`,
`smn_ar_alert_updated` (cambio de nivel) y `smn_ar_alert_cleared` cuando
cambian las alertas de tu zona.

## Tarjeta "SMN Mapa / Satélite"

La integración trae su propia tarjeta, sin instalar nada aparte. Muestra
las cámaras como video en loop, que se ve más fluido que la imagen (un GIF
animado). Se agrega desde el editor del dashboard en **Agregar tarjeta →
SMN Mapa / Satélite**, y se configura de forma visual. En YAML todas las
opciones son opcionales salvo `entity`:

```yaml
type: custom:smn-ar-video-card
entity: camera.cordoba_satelite_provincia_infrarrojo
name: Satélite            # por defecto, el nombre de la entidad
name_position: below      # below | overlay | hidden
aspect_ratio: auto        # auto | 16:9 | 4:3 | 1:1 | 3:4
fit: cover                # cover (llena, recorta bordes) | contain
tap_action: play_pause    # play_pause | more_info | fullscreen | none
autoplay: true            # false: imagen fija hasta tocarla
playback_rate: 1          # 0.5 | 0.75 | 1 | 1.5 | 2
show_controls: false      # controles de video del navegador
show_updated: true        # hora de la última actualización
```

## Detalles técnicos

- **Por qué hace falta un proxy**: la web del SMN usa una API JSON no
  documentada (`ws1.smn.gob.ar/v1`) que exige un token. Ese token sólo se
  obtiene resolviendo el challenge de Cloudflare con un navegador real.
- **Qué hace el add-on** (`addons/smn_proxy`): resuelve el challenge con
  Chromium headless, mantiene vigente el token y expone la API en un puerto
  local. Ver su [README](addons/smn_proxy/README.md).
- **Qué hace la integración** (`custom_components/smn_ar`): sólo habla con
  ese proxy.
- **Otras fuentes**: el radar viene de RainViewer, el satélite de NASA GIBS
  (GOES-East) y los mapas base de OpenStreetMap. No pasan por el proxy.

## Créditos

Basado en [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
