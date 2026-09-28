# SMN Argentina para Home Assistant

Integración de Home Assistant para el clima, pronóstico y alertas del
[Servicio Meteorológico Nacional](https://www.smn.gob.ar) de Argentina, más
un add-on que resuelve el acceso a su API no oficial.

## Por qué dos componentes

`smn.gob.ar` no tiene una API pública. Su frontend consume una API JSON no
documentada (`ws1.smn.gob.ar/v1`) protegida por **Cloudflare**: hace falta
un JWT que solo se puede obtener resolviendo el challenge de Cloudflare con
un navegador real. Eso no es viable embebido en el proceso de Home
Assistant, así que se separa en dos partes:

- **`addons/smn_proxy`** — add-on de Home Assistant OS (Chromium headless +
  Selenium) que resuelve el challenge, mantiene el JWT vigente, y expone un
  proxy HTTP local con la API del SMN.
- **`custom_components/argentina_smn`** — la integración de HA (weather,
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

Esta sección es la referencia completa de cada entidad — qué representa,
qué la enciende/actualiza, y qué hay en sus atributos. Se ve directo en
HA vía Ajustes → Dispositivos y Servicios → SMN → Documentación, o desde
HACS al ver el repositorio (`render_readme: true`).

**Sobre los nombres de entidad (`entity_id`)**: más abajo se listan por
plataforma y nombre visible, no por `entity_id` exacto — Home Assistant
genera el `entity_id` a partir del nombre traducido **en el momento en
que la entidad se crea por primera vez**, y no lo vuelve a cambiar
después aunque cambie el idioma. Según cuándo/con qué idioma activo
instalaste la integración, puede terminar siendo
`sensor.<algo>_short_term_forecast`, `sensor.<algo>_pronostico_de_corto_plazo`,
etc. Para encontrar el `entity_id` real de cualquiera de estas: Ajustes →
Herramientas de desarrollo → Estados, y filtrar por el nombre del
dispositivo o una palabra del nombre visible (ej. "corto plazo").

| Plataforma | Nombre visible | Qué es | Actualiza cada |
|---|---|---|---|
| `weather` | (el nombre que le pusiste al configurar) | Clima actual + pronóstico de 7 días | 30 min |
| `binary_sensor` | Alerta meteorológica | ¿Hay alguna alerta por evento activa hoy? | 30 min |
| `binary_sensor` ×11 | Alerta por tormenta / lluvia / nieve / viento / viento zonda / altas y bajas temperaturas / niebla / polvo / humo / ceniza volcánica | Una por tipo de evento | 30 min |
| `binary_sensor` | Alerta a corto plazo | **¿Tu ubicación exacta está dentro de una zona de alerta activa ahora?** | 10 min |
| `sensor` | Pronóstico de corto plazo | Resumen en una frase de la situación de corto plazo | 10 min |
| `sensor` | Avisos por provincia (país) | Avisos activos en todo el país, agrupados por provincia | 10 min |
| `camera` | Radar | Mapa con radar de precipitación + zonas de alerta dibujadas | 10 min |

### `weather`: clima actual y pronóstico

La entidad de clima estándar de HA. `state`/atributos: temperatura,
sensación térmica, humedad, presión, viento, visibilidad. Pestaña
"Pronóstico" (`async_forecast_daily`/`async_forecast_hourly`): 7 días,
con máxima/mínima diaria y, por franja horaria (madrugada/mañana/
tarde/noche), condición, temperatura, humedad, viento y probabilidad de
lluvia. Es el mismo pronóstico que muestra `smn.gob.ar` — la API no
ofrece nada "extendido" más allá de eso (se probaron endpoints
candidatos como `forecast/week`/`tendency`, ninguno existe).

### `binary_sensor` "Alerta por `<evento>`" (los 11 sensores por tipo)

Uno por cada tipo de evento del sistema de alerta temprana del SMN
(`warning/alert/location/{id}`). `on` (`Unsafe`) = ese evento tiene nivel
> 1 (amarillo/naranja/rojo) para hoy; `off` (`Safe`) = sin alerta de ese
tipo. "Alerta meteorológica" es el "resumen": `on` si cualquiera de los
11 está activo.

### `binary_sensor` "Alerta a corto plazo": ¿estoy en zona de peligro?

**Esta es la respuesta directa a "¿mi ubicación está dentro de algún
polígono de alerta ahora mismo?"** Usa `warning/shortterm/location/{id}`,
que es el propio SMN haciendo ese cálculo (punto-dentro-del-polígono)
contra la lat/lon exacta configurada — no es una aproximación nuestra.

- `on` (`Unsafe`): tu ubicación está dentro de al menos un aviso a muy
  corto plazo vigente (tormenta/granizo/etc., validez 1-2h).
- `off` (`Safe`): no lo está — aunque haya avisos activos en otras zonas
  cercanas (para eso está "Avisos por provincia (país)", ver abajo).

Atributos cuando está `on`: `alert_count`, y `alerts` con el detalle
completo de cada aviso (título, vigencia, zonas, severidad, y
`instructions` — las medidas de protección, tal cual las publica SMN).

Usalo para automatizaciones tipo "avisame por notificación si la Alerta
a corto plazo pasa a `on`" — es la señal más precisa y específica a tu
ubicación que expone esta integración.

### `sensor` "Pronóstico de corto plazo": el aviso de SMN en texto

Es exactamente el mismo texto que muestra el propio mapa de SMN al hacer
clic en un aviso (ej. "TORMENTAS FUERTES CON LLUVIAS INTENSAS Y OCASIONAL
CAIDA DE GRANIZO", con zonas y hora de validez) — el `state` es ese
título, y el atributo `avisos_corto_plazo` trae el resto del detalle.
Sintetiza en una frase (para leer directo en un dashboard, una
notificación o un TTS) todo lo que ya exponen los binary_sensors de
alerta, con esta prioridad:

1. Si hay un **aviso a muy corto plazo** vigente (tormenta, granizo, etc. —
   validez 1-2h): el título del aviso, hasta qué hora es válido, y la
   primera zona afectada. El texto completo con **todas las medidas de
   protección** (el mismo que publica `smn.gob.ar`, campo `instructions`
   de la API — no hace falta scrapear la página, ya viene en el JSON) va
   en el atributo `instrucciones`, y el resto de los avisos activos en
   `avisos_corto_plazo`.
2. Si no, pero hay una **alerta por evento** activa hoy (tormenta, viento,
   etc., nivel > 1): qué evento y qué nivel (amarillo/naranja/rojo), con
   el detalle en el atributo `alertas_activas`.
3. Si no, pero hay **alerta de ola de calor/frío**: lo indica.
4. Si no hay nada de lo anterior: un resumen del pronóstico de hoy
   (condición, máxima/mínima).

### `sensor` "Avisos por provincia (país)": avisos en todo el país

A diferencia de todo lo anterior (específico a tu ubicación), este usa
`warning/shortterm/` **sin** filtro de ubicación — los avisos a muy corto
plazo vigentes en cualquier parte de Argentina en este momento. `state`:
cantidad total + provincias afectadas (ej. "3 avisos vigentes en
Córdoba, Santa Fe"). Atributo `por_provincia`: diccionario con el detalle
de cada aviso agrupado por provincia. Es la misma entidad que alimenta
los polígonos que dibuja la cámara de radar (ver abajo) — mismos datos,
dos formas de verlos (texto vs. mapa).

### `camera` "Radar": radar animado

**No viene de SMN** — `mapa.smn.gob.ar` tiene su propio challenge de
Cloudflare que no se pudo resolver de forma confiable (detalle en
`addons/smn_proxy/README.md`), y el `robots.txt` del SMN pide
explícitamente que agentes tipo Claude no accedan al sitio. En su lugar,
esta cámara arma el GIF animado con tiles de precipitación de la
[API pública de RainViewer](https://www.rainviewer.com/api.html) (gratis
para uso personal, sin API key, requiere solo atribución) compuestas sobre
un mapa base de [OpenStreetMap](https://www.openstreetmap.org/copyright)
(tiles estándar, sin key, con `User-Agent` identificando el proyecto) —
sin el mapa base, cuando no hay lluvia en la zona el radar es 100%
transparente y se ve como un cuadro en blanco, así que el mapa de fondo es
necesario para que se vea "un mapa" y no "nada". Atribución de ambas
fuentes incluida como `attribution` de la entidad. Solo cubre radar de
precipitación, no imagen satelital (RainViewer no la ofrece).

**Importante — cobertura de RainViewer en Argentina es limitada.** Se
verificó que su red de radares tiene huecos notorios en el país: hubo
tormentas reales sobre Córdoba (confirmadas por Windy, Meteored y el mapa
del propio SMN) que RainViewer no mostraba en absoluto. Por eso la cámara
también dibuja, **siempre**, la zona de alerta activa (si hay alguna) como
un polígono rojo con un ícono de tormenta y el texto del aviso (título +
hora de validez) — usando el campo `geometry` de `warning/shortterm`, la
misma data ya validada que usa "Pronóstico de corto plazo". A diferencia
del radar de RainViewer, esto es 100% confiable para Argentina porque
sale directo de la API del SMN, no de un agregador de terceros con
cobertura pareja a nivel mundial pero floja en esta región. Los avisos
dibujados son los que se superponen con el área visible del mapa (no
solo los que caen exactamente sobre tu punto — usa la misma data que
"Avisos por provincia (país)"), así que se ve cualquier zona de alerta
cercana aunque tu ubicación puntual no esté dentro del polígono.

**Zoom**: la grilla es de 5×5 tiles a zoom 9 (`RADAR_ZOOM`/`RADAR_TILE_GRID`
en `const.py`), cubriendo aproximadamente 300km alrededor de tu ubicación
— suficiente para ver tu ciudad y alrededores con detalle, más avisos en
zonas vecinas. Si querés más o menos zoom, se ajusta ahí.

El pronóstico de 7 días ya viene incluido en la entidad `weather` — se ve
en la pestaña "Pronóstico" de su diálogo de más información, o en
cualquier tarjeta de clima de Lovelace. No hace falta nada adicional para
tenerlo: es el mismo pronóstico diario que muestra `smn.gob.ar`, la API no
ofrece un pronóstico "extendido" separado (se probaron varios endpoints
candidatos — `forecast/week`, `tendency`, etc. — ninguno existe; 7 días es
el máximo que da el SMN).

## Idioma: por qué a veces aparece en inglés

Los archivos de traducción (`translations/es.json`) ya cubren toda la
integración (entidades, servicios). Si las ves en inglés a pesar de tener
Home Assistant en español, es casi siempre porque **el idioma se resuelve
por perfil de usuario, no por el idioma general del sistema**: Perfil
(ícono abajo a la izquierda) → Idioma → Español. Un cambio ahí basta, sin
tocar nada de la integración.

## Qué significa "Safe" / "Unsafe" en los sensores de alerta

No es texto nuestro: es la traducción nativa de Home Assistant para el
`device_class: safety` de los binary sensors (así se llama en cualquier
integración que use esa clase, no solo esta). El significado:

- **Safe / Seguro** (`off`): no hay alerta activa de ese tipo.
- **Unsafe / Inseguro** (`on`): hay una alerta activa de ese tipo ahora
  mismo (por eso en la captura "Short-term alert", "Thunderstorm alert" y
  "Weather alert" aparecían en `Unsafe` — había tormenta activa).

Una vez corregido el idioma del perfil (ver arriba), Home Assistant lo
traduce solo a "Seguro"/"Inseguro" — es parte del core, no de esta
integración.

## Ícono de la integración

El placeholder gris "icon not available" que se ve en la tarjeta de la
integración se soluciona con
`custom_components/argentina_smn/brand/{icon.png,logo.png}`, que ya están
en este repo. Desde Home Assistant 2026.3 las integraciones custom pueden
traer su propio ícono así, con prioridad automática sobre el CDN de
[`home-assistant/brands`](https://github.com/home-assistant/brands) — no
hace falta ningún PR externo ni configuración adicional en
`manifest.json`. Alcanza con reiniciar Home Assistant después de
actualizar la integración para que el ícono aparezca. En versiones de HA
anteriores a 2026.3 va a seguir mostrando el placeholder (no hay forma de
evitarlo salvo actualizar HA).

## Estado actual (validado con un spike real, no solo en teoría)

✅ **Funciona end-to-end**: clima actual, pronóstico de 7 días,
amanecer/atardecer, alertas por evento, avisos a muy corto plazo, y la
resolución de ubicación por lat/lon (`georef/location/coord`) — probado
con `curl` contra el proxy real corriendo en Docker, todos devuelven 200
con datos reales.

## Créditos

Basado en el trabajo de [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
