# SMN Argentina para Home Assistant

El clima de Argentina en Home Assistant, con el **Servicio Meteorológico
Nacional** como base y otras fuentes para lo que el SMN no da: el pronóstico
hora por hora, el radar, el satélite y la chance de tormenta.

- Clima actual y pronóstico de 7 días del SMN.
- Avisos que se encienden sólo si la tormenta está **cerca de tu casa**, y
  las alertas de tu zona.
- Cuándo llega la próxima lluvia, la chance de tormenta y de lluvia mañana,
  y el pronóstico contado en palabras.
- Radar y satélite animados, con una leyenda de tormentas.

| Fuente | Qué aporta |
|---|---|
| [SMN](https://www.smn.gob.ar) | Clima actual, pronóstico diario y por franjas, alertas de zona y avisos a muy corto plazo |
| [Open-Meteo](https://open-meteo.com) | Pronóstico hora por hora, y los modelos ECMWF AIFS y UK Met Office para la chance de tormenta y de lluvia mañana |
| SINARAME y RainViewer | Radar, para ver la lluvia que se acerca |
| NASA GIBS (GOES-East) | Satélite visible e infrarrojo, para ver las tormentas donde no hay radar |
| OpenStreetMap | Los mapas de fondo |

Las chances no son las que dan las fuentes tal cual: se calibraron contra un
año de observaciones de aeropuertos argentinos, así que dicen cuántas veces
llovió o hubo tormenta de verdad en situaciones parecidas.

## Instalación

El SMN no tiene una API pública, así que hace falta un add-on que hace de
proxy ([por qué](#detalles-técnicos)).

1. **Add-on SMN Proxy**: Ajustes → Complementos → Tienda → (⋮) →
   Repositorios → agregar `https://github.com/pabloantonelli/smn-ar-ha`.
   Instalarlo e iniciarlo (la primera vez tarda unos 30–40 s).
2. **Integración**: HACS → (⋮) → Repositorios personalizados → la misma URL,
   categoría **Integration** → instalar **SMN - Servicio Meteorológico
   Nacional** y reiniciar Home Assistant.
3. Ajustes → Dispositivos y Servicios → Agregar integración → **SMN**.

Al agregarla se elige la ubicación y la URL del proxy (por defecto
`http://localhost:6942`; si no anda, usá el hostname de la pestaña "Info" del
add-on). En **Configurar** se cambia el radio de los avisos cercanos (30 km
por defecto).

## Entidades

Cada ubicación crea cinco dispositivos:

| Dispositivo | Entidades |
|---|---|
| **Clima actual** | El clima (`weather`, con pronóstico diario del SMN y por hora de Open-Meteo), Temperatura, Sensación térmica, Humedad, Viento, Dirección del viento |
| **Pronóstico** | **Resumen** (el pronóstico en palabras), **Próxima lluvia o tormenta**, **Chance de tormenta en 3 h**, **Chance de lluvia mañana**, Hoy, Mañana |
| **Avisos cercanos** | Tormenta, Lluvia, Granizo, Viento, Nevada, Zonda, Cualquier aviso, Avisos en el país |
| **Alertas de la zona** | Altas temperaturas, Bajas temperaturas, Niebla, Polvo, Humo, Ceniza volcánica, Cualquier alerta |
| **Mapas** | Radar, Avisos, Satélite y Satélite provincia (color e infrarrojo) |

**Avisos cercanos** son los avisos a muy corto plazo del SMN: valen para
ahora (1 a 2 horas) y se encienden si el aviso está dentro del radio.
**Alertas de la zona** son las que el SMN pronostica para tu zona y se
encienden durante la franja del día que corresponde (madrugada, mañana, tarde
o noche). "Cualquier alerta" también incluye tormenta y lluvia, y es el
anticipo: llega horas antes que los avisos.

Si instalaste antes de la 1.13.0, tus entidades conservan sus `entity_id`
(por ejemplo `binary_sensor.cordoba_alerta_por_tormenta`) aunque se vean con
el nombre nuevo.

### Resumen

Cuenta el pronóstico como lo diría una persona:

> **Hoy:** nublado, hasta 26 °C. No se descartan lluvias ni tormentas.
> **Esta noche:** parcialmente nublado, mínima de 18 °C.
> **Mañana jueves:** lluvia casi segura, sobre todo a la tarde, con posibles
> tormentas. Entre 18 y 26 °C.

El estado es un titular corto ("Hoy: alguna lluvia, no se descartan
tormentas · 19 a 26 °C"), o el aviso vigente si hay uno. `pronostico` tiene
el texto completo y `partes` cada parte por separado.

### Leyenda de los mapas

El Radar y los satélites infrarrojos muestran qué tan fuerte es cada nube
según lo fría que está su parte de arriba:

| Color | Qué significa |
|---|---|
| Celeste a azul | Nubes sin lluvia |
| Verde | Nubes espesas |
| Amarillo | Chubascos posibles |
| Naranja | Tormenta débil |
| Rojo | Tormenta |
| Violeta | Tormenta fuerte |

## Tarjetas

El pronóstico en palabras:

```yaml
type: markdown
content: >-
  {% for p in state_attr('sensor.casa_pronostico_de_corto_plazo', 'partes') or [] %}
  **{{ p.titulo }}:** {{ p.texto }}

  {% endfor %}
```

La próxima lluvia, con el mensaje ya armado:

```yaml
type: custom:mushroom-template-card
entity: sensor.casa_proxima_lluvia
multiline_secondary: true
icon: "{{ state_attr(entity, 'icono') }}"
color: "{{ state_attr(entity, 'color') }}"
primary: "{{ state_attr(entity, 'titulo') }}"
secondary: "{{ state_attr(entity, 'detalle') }}"
```

Los mapas como video en loop, con la tarjeta que trae la integración
(**Agregar tarjeta → SMN Mapa / Satélite**):

```yaml
type: custom:smn-ar-video-card
entity: camera.casa_satelite_provincia_infrarrojo
tap_action: fullscreen    # play_pause | more_info | fullscreen | none
aspect_ratio: auto        # auto | 16:9 | 4:3 | 1:1 | 3:4
playback_rate: 1          # 0.5 | 0.75 | 1 | 1.5 | 2
```

<details>
<summary>Todas las opciones de la tarjeta</summary>

```yaml
type: custom:smn-ar-video-card
entity: camera.casa_satelite_provincia_infrarrojo
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

</details>

## Automatizaciones

`smn_ar_shortterm_alert_changed` se dispara cuando aparece o se levanta un
aviso cercano, con `added` (los nuevos), `removed_count` y `current`:

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

Las alertas de la zona disparan `smn_ar_alert_created`, `smn_ar_alert_updated`
(cambio de nivel) y `smn_ar_alert_cleared`.

## Cómo se calcula

Todas las entidades tienen un atributo `criterio` que explica en una frase de
dónde sale lo que muestran.

- **Próxima lluvia o tormenta**: lo más próximo entre el radar o el satélite
  (hasta 2 horas, al minuto), un aviso cercano que se mueve hacia tu casa,
  una alerta de zona y los pronósticos. `probabilidad` es la chance de que
  llueva antes de `probabilidad_hasta`.
- **Chance de tormenta en 3 h**: combina la probabilidad de lluvia de
  Open-Meteo, la lluvia de otros dos modelos (ECMWF AIFS y UK Met Office), la
  energía para tormentas y la hora del día.
- **Chance de lluvia mañana**: según ECMWF AIFS, el modelo que mejor acertó a
  1, 2 y 3 días.
- **Resumen**: usa esas mismas chances, dichas en palabras ("puede llover"
  desde 35%, "probablemente llueva" desde 60%, "lluvia casi segura" desde
  85%), y la máxima y mínima del SMN. Si el SMN anuncia tormentas que no se
  ven probables, dice "no se descartan tormentas".

<details>
<summary>Las fuentes de "Próxima lluvia o tormenta"</summary>

1. **Radar o satélite (nowcast)**: proyecta las últimas imágenes hasta 2
   horas adelante, según hacia dónde y a qué velocidad se mueven las celdas.
   - **Radar del SINARAME** (red nacional: Ezeiza, Córdoba, Resistencia, Mar
     del Plata, Bahía Blanca y otros) si hay uno a menos de 180 km. Cuenta
     ecos desde 28 dBZ: los más débiles casi nunca eran lluvia en el suelo.
     Las imágenes salen del visor de Recursos Hídricos cada 10 minutos, con
     unos 25 de atraso. Se descartan imágenes rotas y cada radar aprende
     dónde tiene ecos fijos del terreno (las sierras, por ejemplo).
   - **RainViewer** donde no llega el SINARAME (casi sólo el litoral del río
     Uruguay).
   - **Satélite infrarrojo** (GOES-East) en el resto del país: ve los topes
     fríos de las tormentas (−58 °C o menos), no la lluvia débil.
   - Si no alcanza para medir el movimiento, usa el viento en 700 hPa.
2. **Aviso cercano**: su zona se mueve con las tormentas; cuenta cuando llega
   a tu ubicación.
3. **Alerta de zona** de lluvia o tormenta, por franja.
4. **Pronóstico horario** de Open-Meteo: una hora con 60% o más y al menos
   0,1 mm. Es tormenta si la chance de tormenta es de 25% o más.
5. **Pronóstico del SMN**: una franja con lluvia probable o con tormenta.

Las franjas del SMN y las alertas sólo cuentan si Open-Meteo también da
lluvia en esa franja: sin eso, en un año llovió el 2% de las veces.

</details>

<details>
<summary>Atributos de "Próxima lluvia o tormenta"</summary>

- `titulo`, `detalle`, `icono`, `color`: el mensaje ya armado.
- `probabilidad` y `probabilidad_hasta`: la chance de lluvia y hasta cuándo
  vale. Radar: 42–58% en 2 horas según el eco; satélite con topes fríos
  encima: ~80%; vacío donde no se pudo medir.
- `probabilidad_hora`: la chance sólo para la hora de inicio.
- `curva`: la chance acumulada hora por hora, hasta 6 horas.
- `probabilidad_tormenta`: la chance de tormenta en las 3 horas desde el
  inicio. Cuando la tormenta la anuncia el SMN, el `detalle` la muestra al
  lado.
- `probabilidad_smn`: el máximo del rango del SMN, sin calibrar.
- `tipo` (`lluvia` o `tormenta`), `fuente`, `minutos`, `en_curso`,
  `proxima_tormenta`.
- `distance_km`, `direction`, `hasta`, `velocidad_kmh`, `se_mueve_hacia`,
  `movimiento_segun`: dónde está, cuándo termina y cómo se mueve.

</details>

<details>
<summary>Qué tan bien funciona</summary>

- **Chance de tormenta**: probada sobre meses que no se usaron para
  ajustarla, tiene 35% de skill (0% es adivinar con el promedio, 100% es
  perfecta). El código de tormenta de los modelos, que se usaba antes,
  detectaba el 22% de las tormentas y la mitad de las veces no pasaba nada.
- **Chance de lluvia mañana**: AIFS fue el mejor modelo a 1, 2 y 3 días en
  Córdoba, Santa Fe, Tucumán y Ezeiza (en Mendoza le ganó el UK Met Office).
- **Leyenda de los mapas**: desde −58 °C (naranja) llovió el 77–87% de las
  veces; a −52 °C (amarillo), menos de la mitad. Que desde −71 °C (violeta)
  sea tormenta fuerte es la convención meteorológica. NASA dibuja en gris lo
  que está entre −71 y −80 °C; la integración lo pinta de violeta.

`scripts/verify_next_rain.py` compara el historial de "Próxima lluvia o
tormenta" de tu Home Assistant con el aeropuerto más cercano:

```sh
HA_TOKEN=<token de larga duración> python3 scripts/verify_next_rain.py \
    --url http://homeassistant.local:8123 \
    --entity sensor.casa_proxima_lluvia --station SACO
```

El recorder guarda 10 días por defecto; con más (`purge_keep_days: 60` en
`recorder:`) hay más lluvias para juzgar y queda registro de "Chance de lluvia
mañana" y del SMN para compararlos.

</details>

<details>
<summary>Cada cuánto se actualiza</summary>

- Avisos cercanos, próxima lluvia, radar y mapa de avisos: cada 10 min (el
  radar y el mapa, también al instante cuando cambia un aviso).
- Clima, pronóstico y alertas de zona: cada 30 min.
- Satélite: cada 20 min.

</details>

## Detalles técnicos

- **Por qué un proxy**: la web del SMN usa una API JSON no documentada
  (`ws1.smn.gob.ar/v1`) que exige un token, y ese token sólo se obtiene
  resolviendo el challenge de Cloudflare con un navegador real. El add-on
  (`addons/smn_proxy`) lo hace con Chromium headless y expone la API en un
  puerto local ([README](addons/smn_proxy/README.md)). La integración sólo
  habla con ese proxy.
- **Las demás fuentes** (Open-Meteo, SINARAME, RainViewer, NASA GIBS,
  OpenStreetMap) se consultan directo, sin pasar por el proxy. Todas son
  gratuitas y no necesitan clave.

## Créditos

Basado en [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
