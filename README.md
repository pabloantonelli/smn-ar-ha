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

| Entidad | Qué es | Frecuencia de actualización |
|---|---|---|
| `weather.<ubicación>` | Clima actual + pronóstico de 7 días (diario y por franjas mañana/tarde/noche), con probabilidad de lluvia | 30 min |
| `binary_sensor.<ubicación>_weather_alert` | Alerta general (hay alguna alerta activa) | 30 min |
| `binary_sensor.<ubicación>_alert_<evento>` (11 sensores: tormenta, lluvia, nieve, viento, zonda, temperaturas altas/bajas, niebla, polvo, humo, ceniza volcánica) | Una por tipo de evento del sistema de alerta temprana del SMN | 30 min |
| `binary_sensor.<ubicación>_short_term_alert` | Avisos a muy corto plazo (los de `smn.gob.ar/avisos_a_muy_corto_plazo`, validez de 1-2 horas) | 10 min |
| `sensor.<ubicación>_short_term_summary` | Resumen en texto, en español, de la situación de corto plazo (ver abajo) | 10 min |
| `camera.<ubicación>_radar` | Radar de precipitación animado (GIF, últimos ~30-60 min de movimiento) centrado en la ubicación | 10 min |

### `sensor.<ubicación>_short_term_summary`: pronóstico de corto plazo en texto

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

### `camera.<ubicación>_radar`: radar animado

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
misma data ya validada que usa `sensor.<ubicación>_short_term_summary`. A
diferencia del radar de RainViewer, esto es 100% confiable para Argentina
porque sale directo de la API del SMN, no de un agregador de terceros con
cobertura pareja a nivel mundial pero floja en esta región.

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
