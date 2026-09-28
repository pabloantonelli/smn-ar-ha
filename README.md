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

## Estado actual (validado con un spike real, no solo en teoría)

✅ **Funciona end-to-end**: clima actual, pronóstico diario/horario,
amanecer/atardecer, alertas por evento, avisos a muy corto plazo, y la
resolución de ubicación por lat/lon (`georef/location/coord`) — probado
con `curl` contra el proxy real corriendo en Docker, todos devuelven 200
con datos reales.

🗺️ **Sin mapas/radar/satélite, a propósito**: `mapa.smn.gob.ar` y
`estaticos.smn.gob.ar` tienen su propio challenge de Cloudflare, separado
del de `ws1`, que no se logró resolver de forma confiable desde un browser
automatizado. Además, `smn.gob.ar/robots.txt` pide explícitamente que
agentes tipo `ClaudeBot`/`anthropic-ai` no accedan al sitio — no tiene
sentido invertir en técnicas más agresivas de evasión de bots para esto.
Para radar animado en Home Assistant, usar la
[Weather Radar Card](https://github.com/jpettitt/weather-radar-card)
(HACS) contra [RainViewer](https://www.rainviewer.com/): no depende de
esta integración ni de ningún proxy. Detalle en `addons/smn_proxy/README.md`.

## Instalación

1. Instalar el add-on **SMN Proxy** (`addons/smn_proxy`) desde Supervisor →
   Add-on Store → repositorios → agregar este repo. Iniciarlo y esperar a
   que `http://<host-del-addon>:6942/smn/health` devuelva
   `"session_ready": true` (puede tardar ~30-40s la primera vez, mientras
   resuelve el challenge).
2. Instalar la integración **SMN - Servicio Meteorológico Nacional**
   (`custom_components/argentina_smn`) vía HACS o copiando la carpeta a
   `config/custom_components/`.
3. Agregar la integración desde Ajustes → Dispositivos y Servicios, con la
   latitud/longitud deseada y la URL del proxy (por defecto
   `http://localhost:6942`, ajustar al hostname real del add-on).

## Créditos

Basado en el trabajo de [`catastrophicode/ha-ar-smn`](https://github.com/catastrophicode/ha-ar-smn)
(MIT) para la integración, y [`nixietab/OpenSMN`](https://github.com/nixietab/OpenSMN)
(GPL-2.0) para el enfoque del proxy de Cloudflare.
