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
  avisos cercanos**, 30 km por defecto. Lo usan los "Avisos cercanos" y
  "Próxima lluvia o tormenta".

## Entidades

Cada ubicación crea cinco dispositivos, así cada entidad tiene un nombre corto
que dice qué es: en "Córdoba · Avisos cercanos", "Tormenta" se enciende con un
aviso de tormenta cerca.

### Clima actual

| Nombre | Qué muestra |
|---|---|
| (el clima, `weather`) | Clima actual y pronóstico de 7 días del SMN. El pronóstico **por hora** es de [Open-Meteo](https://open-meteo.com) (3 días), porque el del SMN viene en franjas de 6 horas; si Open-Meteo no responde, vuelven las franjas del SMN. Su probabilidad de lluvia es la medida, no la de Open-Meteo, que exagera: cuando dice 70% para una hora, llueve en esa hora el 34% de las veces |
| Temperatura, Sensación térmica, Humedad, Viento, Dirección del viento | Los valores actuales, como sensores sueltos para gráficos y automatizaciones |

### Pronóstico

| Nombre | Qué muestra |
|---|---|
| Hoy / Mañana | Condición, máxima y mínima del SMN |
| Próxima lluvia o tormenta | Cuándo es la próxima lluvia o tormenta ([cómo se calcula](#próxima-lluvia-o-tormenta)) |
| Chance de tormenta en 3 h | La chance de tormenta en las próximas 3 horas, y en `proximas_horas` la de cada 3 horas del día siguiente ([cómo se calcula](#chance-de-tormenta-en-3-h)) |
| Chance de lluvia mañana | La chance de que llueva mañana, y en `dias` la de mañana, pasado y el día siguiente, cada una con la del SMN al lado ([cómo se calcula](#chance-de-lluvia-mañana)) |
| Resumen | Una frase que resume alertas, avisos y el pronóstico de hoy, lista para notificaciones o para leerla en voz alta |

### Avisos cercanos

Los avisos a muy corto plazo del SMN: valen para **ahora** (duran 1 a 2 horas)
y se encienden si el aviso está **dentro del radio** de tu ubicación.

| Nombre | Se enciende cuando… |
|---|---|
| Tormenta / Lluvia / Viento / Nevada / Zonda | hay un aviso de ese tipo dentro del radio |
| Granizo | hay un aviso que menciona granizo dentro del radio |
| Cualquier aviso | hay cualquier aviso dentro del radio |
| Avisos en el país | (sensor) los avisos vigentes en todo el país, agrupados por provincia |

### Alertas de la zona

Las alertas que el SMN pronostica para **tu zona** (uno o varios
departamentos): se encienden durante la **franja del día** con alerta
(madrugada, mañana, tarde o noche, de 6 horas).

| Nombre | Se enciende cuando… |
|---|---|
| Altas temperaturas / Bajas temperaturas / Niebla / Polvo / Humo / Ceniza volcánica | el SMN tiene esa alerta para tu zona en la franja actual |
| Cualquier alerta | el SMN tiene cualquier alerta para tu zona en la franja actual, también de tormenta o lluvia. `today_alerts` trae todo lo pronosticado para hoy |

### Mapas

| Nombre | Qué muestra |
|---|---|
| Radar | Animación de los últimos ~100 min de las nubes de tormenta vistas por satélite (infrarrojo), ~300 km alrededor, con el clima actual y los avisos cercanos. Los radares (SINARAME y RainViewer) no se dibujan: se usan para calcular la [próxima lluvia](#próxima-lluvia-o-tormenta) |
| Avisos | Mapa del país con los polígonos de todos los avisos vigentes y tu ubicación |
| Satélite / Satélite provincia | Animación satelital de los últimos ~100 min en color real. De noche pasa sola a una vista nocturna |
| … infrarrojo | Igual, mostrando la temperatura de los topes de nube. Sirve para ver la intensidad de una tormenta |

El Radar y los satélites infrarrojos llevan siempre la misma leyenda. Cuanto
más fría está la parte de arriba de una nube, más alta y fuerte es la tormenta:

| Color | Tope de la nube | Qué significa |
|---|---|---|
| Celeste a azul | −20 a −30 °C | Nubes sin lluvia |
| Verde | −31 a −45 °C | Nubes espesas |
| Amarillo | −46 a −57 °C | Chubascos posibles: a −52 °C llovió menos de la mitad de las veces |
| Naranja | −58 a −61 °C | Tormenta débil: desde −58 °C llovió el 77–87% de las veces |
| Rojo | −62 a −70 °C | Tormenta |
| Violeta | −71 °C o menos | Tormenta fuerte |

Los porcentajes salen de comparar con los aeropuertos. Que desde −71 °C sea
una tormenta fuerte es la convención meteorológica, no algo medido acá. En los
satélites infrarrojos, el gris son nubes bajas o el suelo, pero un centro
gris rodeado de rojo oscuro también es una tormenta fuerte: la paleta usa
grises entre −71 y −80 °C.

### Actualización

- **Avisos a corto plazo**: cada 10 min.
- **Clima, pronóstico (SMN y Open-Meteo) y alertas de zona**: cada 30 min.
- **Próxima lluvia (radar o satélite)**: cada 10 min.
- **Radar y mapa de avisos**: cada 10 min, y al instante cuando aparece o termina un aviso (así la imagen que mandás en una notificación ya trae el polígono).
- **Satélite**: cada 20 min.

Para encontrar el `entity_id` de una entidad, entrá a Herramientas de
desarrollo → Estados y filtrá por el nombre. Home Assistant lo arma con el
nombre que tenía la entidad cuando se creó: si instalaste antes de la 1.13.0,
tus entidades conservan sus `entity_id` (por ejemplo
`binary_sensor.cordoba_alerta_por_tormenta`) aunque ahora se vean con el
nombre nuevo.

## Cómo funcionan las alertas

El SMN publica dos tipos de información, y cada alerta usa la más precisa
que existe para su fenómeno. Todas tienen el atributo **`criterio`**, que
explica en una frase qué la enciende.

**Avisos cercanos** (tormenta, lluvia, viento, nevada, zonda, granizo y
"Cualquier aviso"):

- **Fuente**: los avisos a muy corto plazo del SMN. Duran 1 a 2 h y cada
  uno trae su polígono, así que se mide la distancia real desde tu
  ubicación. 0 km quiere decir que estás adentro.
- **Atributos**: `distance_km` y `direction` muestran dónde está el aviso
  más cercano, aunque esté fuera del radio.
- **Tipo de aviso**: sale del título. Por ejemplo, "TORMENTAS FUERTES CON
  LLUVIAS INTENSAS, RÁFAGAS Y GRANIZO" enciende tormenta, lluvia, viento y
  granizo. El viento zonda tiene su propia alerta.

**Alertas de la zona** (temperaturas, niebla, polvo, humo, ceniza volcánica
y "Cualquier alerta"):

- **Fuente**: el SMN sólo da un pronóstico para la zona a la que pertenece
  tu ubicación (uno o varios departamentos).
- **Cuándo se encienden**: el pronóstico trae un nivel (amarillo, naranja o
  rojo) por franja del día: madrugada 0–6, mañana 6–12, tarde 12–18 y noche
  18–24. La alerta se enciende sólo durante las franjas con alerta.
- **Cualquier alerta**: es el **anticipo**. Incluye también tormenta y
  lluvia, y se emite horas antes que los avisos, pero no mide distancia. El
  atributo `today_alerts` muestra todo lo pronosticado para hoy.
- **Ojo con `description`**: es el texto genérico del SMN para ese nivel y
  puede nombrar otras regiones del país.

## Próxima lluvia o tormenta

El estado es un horario: Home Assistant lo muestra como "en 40 minutos" y
queda vacío si no hay nada a la vista. Es la hora más probable, no una
certeza: mirá el atributo `probabilidad`. Se queda con lo más próximo de cinco
fuentes:

1. **Radar o satélite (nowcast)**: toma las últimas imágenes alrededor de tu
   ubicación, calcula hacia dónde y a qué velocidad se mueven las celdas, y
   las proyecta hasta 2 horas adelante. Da la hora con precisión de minutos.
   - **Radar del SINARAME**, la red nacional de radares (Ezeiza, Córdoba,
     Resistencia, Mar del Plata, Bahía Blanca y otros), si hay uno a menos de
     180 km. Ve la lluvia en sí, desde unos 28 dBZ (lluvia de débil a
     moderada): los ecos más débiles casi nunca terminaban en lluvia en el
     suelo (ecos de las sierras, lluvia que se evapora). Sale del visor público de Recursos Hídricos
     (radares.hidricosargentina.gob.ar), que publica cada 10 minutos con unos
     25 de atraso. Esas imágenes traen artefactos (barridos con anillos,
     interferencias, ecos del terreno), así que se descartan los frames rotos,
     no se cuenta lluvia donde el satélite no ve nubes, y cada radar aprende
     dónde está su clutter de terreno (las sierras, por ejemplo): los píxeles
     con eco en la mayoría de las imágenes de las últimas ~17 horas. Ese
     aprendizaje arranca a aplicarse a las ~6 horas de instalada la
     integración y se guarda entre reinicios. Si el radar más cercano no
     sirve, prueba con el siguiente.
   - **Radar de RainViewer** donde no llega el SINARAME y RainViewer tiene
     cobertura (casi sólo el litoral del río Uruguay).
   - **Satélite infrarrojo** (GOES-East) en el resto del país. Ve los topes
     fríos de las tormentas (-58 °C o menos), pero no la lluvia débil de
     nubes más bajas ni una tormenta que todavía no se formó: para eso
     quedan los pronósticos.
   - Si las imágenes no alcanzan para medir el movimiento, o el satélite
     sigue al yunque de la tormenta en vez de a la lluvia, usa el viento en
     700 hPa, que es el que arrastra las tormentas.
2. **Aviso cercano**: un aviso de lluvia o tormenta dentro del radio. Su
   zona se mueve con las tormentas: cuenta cuando llega a tu ubicación, y
   uno que pasa de largo no cuenta.
3. **Alerta de zona**: de lluvia o tormenta, por franja, para hoy y los
   próximos días.
4. **Pronóstico horario** de [Open-Meteo](https://open-meteo.com): una hora
   con 60% o más de probabilidad y al menos 0,1 mm. Es tormenta si además
   la [chance de tormenta](#chance-de-tormenta-en-3-h) de las 3 horas
   desde ella es de 25% o más (y la hora trae algo de lluvia: 30% y 0,1 mm).
5. **Pronóstico del SMN** de tu localidad: una franja con lluvia probable
   (el medio del rango de probabilidad en 40% o más; "10–30%" no cuenta) o
   con tormenta aunque la probabilidad sea baja.

Las franjas del SMN y las alertas duran 6 horas y abarcan toda una zona: sólo
cuentan si Open-Meteo también da lluvia en alguna hora de esa franja (30% o
más y al menos 0,1 mm), y esa hora es la que se toma como comienzo. Sin esa
confirmación casi nunca llueve en tu ubicación: en un año de observaciones
de 15 aeropuertos, una franja así tuvo lluvia el 2% de las veces. Donde hay
radar, los pronósticos siguen contando: la llovizna y la lluvia débil
quedan por debajo de lo que el radar toma como lluvia.

Atributos principales:

- **`titulo`** / **`detalle`** / **`icono`** / **`color`**: el mensaje ya
  armado para mostrar, por ejemplo "Tormenta posible desde las 14:00" y
  "25% antes de las 15:00 · 57% antes de las 18:00 · alerta amarilla".
- **`probabilidad`**: es una estimación, no una certeza: la chance de que
  llueva antes de `probabilidad_hasta`. Para los pronósticos sale de `curva`.
  Radar: 42–58% en 2 horas, según la intensidad del eco (medido sólo con el
  radar de Córdoba, así que es aproximado). Satélite con topes fríos encima:
  ~80%. Queda vacío donde no se pudo medir (avisos, una llegada proyectada por
  el satélite, o sin datos de Open-Meteo).
- **`probabilidad_hasta`**: hasta cuándo vale `probabilidad`. No se refiere a
  la hora de inicio.
- **`probabilidad_hora`**: la chance sólo para la hora de inicio. Vacío si la
  fuente no es un pronóstico.
- **`curva`**: la probabilidad acumulada hora por hora, hasta 6 horas, desde
  como mucho 2 horas antes del inicio. Combina las horas de Open-Meteo y corrige
  que las horas de lluvia vienen juntas, y suma la lluvia que dan otros dos
  modelos, el AIFS de ECMWF y el del UK Met Office (sin ellos, se calcula
  sólo con Open-Meteo). Calibrada contra un año de observaciones de 12
  aeropuertos argentinos. Es la misma con o sin alerta: las alertas y las
  franjas del SMN deciden si hay aviso, no cuánto da el número.
- **`probabilidad_smn`**: el máximo del rango que da el SMN para la franja,
  tal cual, sin calibrar: no es la chance de que llueva.
- **`tipo`**: `lluvia` o `tormenta`.
- **`probabilidad_tormenta`**: la [chance de tormenta](#chance-de-tormenta-en-3-h)
  que dan los modelos en las 3 horas desde el inicio. Cuando la "tormenta"
  viene del pronóstico o de una alerta del SMN, el `detalle` la muestra al lado
  ("según el SMN · chance de tormenta 4% en 3 h"): el SMN anuncia tormenta
  para toda una franja de 6 horas, y este número dice qué tan probable la ven
  los modelos en esas horas.
- **`fuente`**: `radar SINARAME <radar>`, `radar`, `satélite infrarrojo`,
  `aviso cercano`, `alerta de zona`, `pronóstico horario` o `pronóstico`.
- **`minutos`**: cuánto falta.
- **`en_curso`**: `true` si ya empezó. En ese caso el estado se ve como
  "hace X".
- **`distance_km`** / **`direction`**: a qué distancia está y de dónde viene.
- **`hasta`**: cuándo termina de pasar, si es dentro de las 2 horas.
- **`velocidad_kmh`** / **`se_mueve_hacia`** / **`movimiento_segun`**: cómo se
  mueven las tormentas y de dónde salió ese dato.
- **`proxima_tormenta`**: la próxima tormenta, aunque antes venga lluvia.

Una tarjeta sólo tiene que mostrar el mensaje:

```yaml
type: custom:mushroom-template-card
entity: sensor.casa_proxima_lluvia
multiline_secondary: true
icon: "{{ state_attr(entity, 'icono') }}"
color: "{{ state_attr(entity, 'color') }}"
primary: "{{ state_attr(entity, 'titulo') }}"
secondary: "{{ state_attr(entity, 'detalle') }}"
```

## Chance de tormenta en 3 h

La chance de que haya tormenta (truenos en el aeropuerto o cerca) en las
próximas 3 horas. Pondera, con pesos ajustados contra un año de observaciones
de 12 aeropuertos argentinos:

- la probabilidad de lluvia de Open-Meteo,
- la lluvia que dan el AIFS de ECMWF y el modelo del UK Met Office, los dos
  que más suman en Argentina,
- la energía disponible para tormentas (CAPE),
- la hora del día.

Probada sobre meses que no se usaron para ajustarla, tiene 35% de skill (0%
es adivinar con el promedio, 100% es perfecta); sin los dos modelos extra,
25%. El código de tormenta que da el modelo, que se usaba antes, sólo
detectaba el 22% de las tormentas y la mitad de las veces no pasaba nada. Si
los dos modelos extra no responden, `fuente` lo dice y se calcula sin ellos.

Atributos: `proximas_horas` (cada 3 horas del día siguiente), `maxima` y
`maxima_desde`.

## Chance de lluvia mañana

La chance de que llueva en algún momento de mañana, según cuánta lluvia da el
AIFS de ECMWF para el día entero. Es el modelo que mejor acertó si llueve o no
a 1, 2 y 3 días en Córdoba, Santa Fe, Tucumán y Ezeiza (en Mendoza le ganó
el del UK Met Office). `dias` trae mañana, pasado y el día siguiente, cada uno
con `probabilidad`, `mm` y `probabilidad_smn`, la más alta que da el SMN ese
día. El SMN no publica pronósticos pasados: guardar los dos en el historial
de Home Assistant permite compararlos más adelante.

### Qué tan bien le pega en tu casa

`scripts/verify_next_rain.py` cruza el historial del sensor en tu Home
Assistant con los reportes METAR del aeropuerto más cercano, y te dice por
fuente cuántas veces llovió cuando avisó:

```sh
HA_TOKEN=<token de larga duración> python3 scripts/verify_next_rain.py \
    --url http://homeassistant.local:8123 \
    --entity sensor.casa_proxima_lluvia --station SACO
```

El recorder guarda 10 días por defecto. Con más días hay más lluvias para
juzgar, y queda registro de "Chance de lluvia mañana" y del SMN para compararlos:

```yaml
recorder:
  purge_keep_days: 60
```

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

"Cualquier alerta" (en Alertas de la zona) dispara además `smn_ar_alert_created`,
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
- **Otras fuentes**: el radar viene de RainViewer y del SINARAME (visor de
  radares de Recursos Hídricos), el satélite de NASA GIBS (GOES-East), el
  pronóstico horario de Open-Meteo y los mapas base de OpenStreetMap. No
  pasan por el proxy.

## Créditos

Basado en [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
