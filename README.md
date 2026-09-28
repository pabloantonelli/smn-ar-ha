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
  binary_sensors de alertas), que habla con ese proxy local en vez de
  pegarle directo a SMN.

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
| `weather.<ubicación>` | Clima actual + pronóstico de 7 días (diario y por franjas mañana/tarde/noche) | 30 min |
| `binary_sensor.<ubicación>_weather_alert` | Alerta general (hay alguna alerta activa) | 30 min |
| `binary_sensor.<ubicación>_alert_<evento>` (11 sensores: tormenta, lluvia, nieve, viento, zonda, temperaturas altas/bajas, niebla, polvo, humo, ceniza volcánica) | Una por tipo de evento del sistema de alerta temprana del SMN | 30 min |
| `binary_sensor.<ubicación>_short_term_alert` | Avisos a muy corto plazo (los de `smn.gob.ar/avisos_a_muy_corto_plazo`, validez de 1-2 horas) | 10 min |

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

## Mapas / radar animado

Esta integración deliberadamente **no** incluye mapas ni radar. Ver
`addons/smn_proxy/README.md` para el detalle técnico de por qué (challenge
de Cloudflare independiente en `mapa.smn.gob.ar`/`estaticos.smn.gob.ar`, no
resuelto de forma confiable, y el propio `robots.txt` del SMN pide
explícitamente que agentes tipo Claude no accedan al sitio).

Para tener radar animado (loop, no una imagen estática) en Home Assistant,
usar otra integración ya existente, sin depender de esta:

1. **HACS → Frontend → Explorar y descargar repositorios** → buscar
   "Weather Radar Card" ([jpettitt/weather-radar-card](https://github.com/jpettitt/weather-radar-card))
   → Descargar. (Si no aparece en el listado, agregarla como repositorio
   personalizado con esa URL, categoría *Dashboard*).
2. Recargar el navegador (Ctrl+Shift+R) para que cargue la card nueva.
3. En cualquier dashboard, "Editar" → "Agregar tarjeta" → buscar
   "Weather Radar Card", o pegar directamente en modo YAML:

   ```yaml
   type: custom:weather-radar-card
   source: rainviewer
   center:
     lat: -31.4201
     lon: -64.1888
   zoom: 7
   ```

   (ajustar `lat`/`lon` a tu ubicación — por defecto usa la de Home
   Assistant si no se especifica). La card trae los frames de
   [RainViewer](https://www.rainviewer.com/) directamente al navegador y
   arma el loop animado sola; no necesita el add-on `smn_proxy` ni ninguna
   integración de backend.

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
