# SMN Proxy (add-on de Home Assistant)

Resuelve el challenge de Cloudflare de `www.smn.gob.ar` con Chromium
headless + Selenium, mantiene un JWT vigente, y expone un proxy HTTP local
de la API JSON del SMN para que la integración `argentina_smn` no tenga
que lidiar con eso.

## Endpoints

- `GET /smn/v1/<path>` → `https://ws1.smn.gob.ar/v1/<path>` con
  `Authorization: JWT <token>`. **Validado y funcionando**: weather,
  forecast, sun, georef/location/{id}, georef/location/coord (antes
  siempre 401 sin sesión real), warning/alert, warning/shortterm,
  warning/heat|cold.
- `GET /smn/health` → estado de la sesión (`session_ready`, cantidad de
  cookies, timestamp del último refresh).

## Por qué no incluye mapas/radar/satélite

`mapa.smn.gob.ar` y `estaticos.smn.gob.ar` tienen su propio challenge de
Cloudflare, separado del de `ws1`. El `cf_clearance` que se obtiene al
resolver el challenge en `www.smn.gob.ar` no es válido para esos
subdominios, y no se logró resolverlo de forma confiable con un browser
automatizado (ni navegando directo, ni disparando el recurso como
subrecurso `fetch()` desde la página principal — el detalle técnico quedó
en el historial del desarrollo).

Más allá de la viabilidad técnica, `smn.gob.ar/robots.txt` tiene reglas
explícitas de `Disallow: /` para `ClaudeBot`, `anthropic-ai` y
`Claude-Web` — el sitio pide expresamente no ser accedido por agentes de
este tipo. Seguir invirtiendo en técnicas más agresivas para esquivar la
detección de bots de Cloudflare específicamente para esto no es algo que
valga la pena perseguir para una integración personal.

**Alternativa recomendada para radar animado**: instalar la
[Weather Radar Card](https://github.com/jpettitt/weather-radar-card) desde
HACS, apuntada a [RainViewer](https://www.rainviewer.com/) (cubre
Argentina). Es una card de Lovelace que dibuja el loop animado
directamente en el navegador — no necesita ningún proxy ni integración de
backend, y no depende de scrapear un sitio que no quiere ser scrapeado.

## Configuración (`config.yaml` options)

- `cache_ttl_minutes` (default 15): cuánto se cachean las respuestas antes
  de volver a pedirlas al origin.
- `token_refresh_minutes` (default 20): cada cuánto se relanza el browser
  para renovar la sesión.
- `log_level`: nivel de logging del proxy.

## Desarrollo / testing local

```bash
docker build -t smn-proxy .
docker run -d --name smn-proxy -p 6942:6942 smn-proxy
curl http://localhost:6942/smn/health
curl http://localhost:6942/smn/v1/weather/location/10824
```

Validado en un spike real (2026-09-28): con este mecanismo,
`weather/location/{id}`, `forecast/location/{id}`, `sun/location/{id}`,
`georef/location/{id}`, `georef/location/coord`,
`warning/alert/location/{id}` y `warning/shortterm/location/{id}` devuelven
200 con datos reales de forma sostenida.
