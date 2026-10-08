"""Constants for the SMN integration."""
from typing import Final

from homeassistant.components.weather import (
    ATTR_CONDITION_CLEAR_NIGHT,
    ATTR_CONDITION_CLOUDY,
    ATTR_CONDITION_FOG,
    ATTR_CONDITION_LIGHTNING_RAINY,
    ATTR_CONDITION_PARTLYCLOUDY,
    ATTR_CONDITION_POURING,
    ATTR_CONDITION_RAINY,
    ATTR_CONDITION_SNOWY,
    ATTR_CONDITION_SNOWY_RAINY,
    ATTR_CONDITION_SUNNY,
    ATTR_CONDITION_WINDY,
    ATTR_FORECAST_CONDITION,
    ATTR_FORECAST_NATIVE_PRECIPITATION,
    ATTR_FORECAST_NATIVE_TEMP,
    ATTR_FORECAST_NATIVE_TEMP_LOW,
    ATTR_FORECAST_NATIVE_WIND_SPEED,
    ATTR_FORECAST_TIME,
)

DOMAIN: Final = "smn_ar"

# Fired whenever the set of avisos a muy corto plazo for the configured
# location changes (a new one appears, or an existing one is lifted) — lets
# automations react immediately instead of polling the summary sensor.
EVENT_SHORTTERM_ALERT_CHANGED: Final = f"{DOMAIN}_shortterm_alert_changed"

# Config keys
CONF_PROXY_URL: Final = "proxy_url"
CONF_LOCATION_ID: Final = "location_id"
# Options: radius around the configured location within which a short-term
# warning (aviso a muy corto plazo) turns on the proximity-based alerts.
CONF_ALERT_RADIUS_KM: Final = "alert_radius_km"
DEFAULT_ALERT_RADIUS_KM: Final = 30
# Older option (only "Granizo cercano" used it), still honored as a fallback.
CONF_HAIL_RADIUS_KM: Final = "hail_radius_km"

# Default local address of the "smn-proxy" HA add-on (see addons/smn_proxy).
# The add-on solves SMN's Cloudflare challenge and forwards requests to ws1
# with a valid JWT, since ws1.smn.gob.ar rejects unauthenticated requests
# (the old ws2.smn.gob.ar token endpoint used by earlier versions of this
# integration was decommissioned and no longer resolves).
DEFAULT_PROXY_URL: Final = "http://localhost:6942"

# API paths, relative to the configured proxy base URL (proxy exposes them
# under /smn/v1/... mirroring https://ws1.smn.gob.ar/v1/...)
API_PATH_PREFIX: Final = "/smn/v1"
API_COORD_PATH: Final = f"{API_PATH_PREFIX}/georef/location/coord"
API_FORECAST_PATH: Final = f"{API_PATH_PREFIX}/forecast/location"
API_WEATHER_PATH: Final = f"{API_PATH_PREFIX}/weather/location"
API_ALERT_PATH: Final = f"{API_PATH_PREFIX}/warning/alert/location"
API_SHORTTERM_ALERT_PATH: Final = f"{API_PATH_PREFIX}/warning/shortterm/location"
# Nationwide avisos a muy corto plazo (no location filter) — same data as
# smn.gob.ar's "Resumen por provincia", each aviso already includes a
# structured "provinces" field to group by.
API_SHORTTERM_NATIONWIDE_PATH: Final = f"{API_PATH_PREFIX}/warning/shortterm/"
API_HEAT_WARNING_PATH: Final = f"{API_PATH_PREFIX}/warning/heat/area"
API_COLD_WARNING_PATH: Final = f"{API_PATH_PREFIX}/warning/cold/area"
API_SUN_PATH: Final = f"{API_PATH_PREFIX}/sun/location"
API_GEOREF_PATH: Final = f"{API_PATH_PREFIX}/georef/location"

# Radar imagery: NOT sourced from SMN. mapa.smn.gob.ar sits behind a
# separate Cloudflare bot-management challenge that couldn't be solved
# reliably from a headless browser (see addons/smn_proxy/README.md), and
# smn.gob.ar's robots.txt explicitly disallows Claude/Anthropic bots.
# Instead this uses RainViewer's public Weather Maps API (no key needed,
# free for personal/community use per https://www.rainviewer.com/api.html,
# requires attribution "Weather data by RainViewer" — shown as the
# radar camera's attribution). It only covers precipitation radar, not
# satellite imagery.
RAINVIEWER_INDEX_URL: Final = "https://api.rainviewer.com/public/weather-maps.json"
# Basemap tiles so the radar mosaic shows a recognizable map underneath
# instead of a blank square when there's no precipitation. CARTO's free
# anonymous basemap CDN was retired (now requires an API key), so this uses
# OpenStreetMap's standard tile server directly instead — no key needed,
# but their usage policy requires a proper identifying User-Agent (set in
# radar.py) and discourages heavy automated use. Volume here is low (9
# tiles, only fetched lazily when the camera is actually viewed, cached
# for RADAR_UPDATE_INTERVAL), consistent with light personal-project use.
BASEMAP_TILE_URL_TEMPLATE: Final = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
BASEMAP_USER_AGENT: Final = (
    "smn-ar-ha-radar/1.0 (+https://github.com/pabloantonelli/smn-ar-ha)"
)
RADAR_CAMERA_ATTRIBUTION: Final = (
    "Imagery by NASA GIBS / NOAA GOES-East · Map © OpenStreetMap contributors"
)
# Spanish labels for HA condition strings, used to caption the radar
# snapshot image (drawn with Pillow, not HA's own translation system —
# HA core's own condition strings are Lokalise-managed and can't be
# localized by files in this repo, see the README's translations note).
CONDITION_LABELS_ES: Final = {
    ATTR_CONDITION_CLEAR_NIGHT: "Despejado",
    ATTR_CONDITION_CLOUDY: "Nublado",
    ATTR_CONDITION_FOG: "Niebla",
    ATTR_CONDITION_LIGHTNING_RAINY: "Tormenta",
    ATTR_CONDITION_PARTLYCLOUDY: "Parcialmente nublado",
    ATTR_CONDITION_POURING: "Lluvias fuertes",
    ATTR_CONDITION_RAINY: "Lluvia",
    ATTR_CONDITION_SNOWY: "Nieve",
    ATTR_CONDITION_SNOWY_RAINY: "Lluvia y nieve",
    ATTR_CONDITION_SUNNY: "Despejado",
    ATTR_CONDITION_WINDY: "Ventoso",
}

# Hourly forecast from Open-Meteo (open-meteo.com, free, no key, CC BY 4.0):
# SMN's own forecast only comes in 6-hour periods, too coarse for the
# weather entity's hourly forecast or to say when rain starts. Also gives
# the 700 hPa wind, the steering flow storms move with (the nowcast's
# fallback motion). Not through the proxy either.
OPEN_METEO_URL: Final = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_FORECAST_DAYS: Final = 3
OPEN_METEO_ATTRIBUTION: Final = "Hourly forecast by Open-Meteo.com"
# Our key in each hourly entry -> Open-Meteo's hourly variable.
OPEN_METEO_HOURLY_FIELDS: Final = {
    "probability": "precipitation_probability",
    "precipitation": "precipitation",
    "weather_code": "weather_code",
    "is_day": "is_day",
    "temperature": "temperature_2m",
    "apparent_temperature": "apparent_temperature",
    "humidity": "relative_humidity_2m",
    "wind_speed": "wind_speed_10m",
    "wind_gust_speed": "wind_gusts_10m",
    "wind_bearing": "wind_direction_10m",
    "cloud_coverage": "cloud_cover",
    "wind_speed_700hpa": "wind_speed_700hPa",
    "wind_direction_700hpa": "wind_direction_700hPa",
}
# WMO weather code (Open-Meteo's weather_code) -> HA condition. Clear sky
# at night becomes clear-night in weather.py (with Open-Meteo's is_day).
WMO_CONDITION_MAP: Final = {
    0: ATTR_CONDITION_SUNNY,
    1: ATTR_CONDITION_PARTLYCLOUDY,
    2: ATTR_CONDITION_PARTLYCLOUDY,
    3: ATTR_CONDITION_CLOUDY,
    45: ATTR_CONDITION_FOG,
    48: ATTR_CONDITION_FOG,
    51: ATTR_CONDITION_RAINY,
    53: ATTR_CONDITION_RAINY,
    55: ATTR_CONDITION_RAINY,
    56: ATTR_CONDITION_SNOWY_RAINY,
    57: ATTR_CONDITION_SNOWY_RAINY,
    61: ATTR_CONDITION_RAINY,
    63: ATTR_CONDITION_RAINY,
    65: ATTR_CONDITION_POURING,
    66: ATTR_CONDITION_SNOWY_RAINY,
    67: ATTR_CONDITION_SNOWY_RAINY,
    71: ATTR_CONDITION_SNOWY,
    73: ATTR_CONDITION_SNOWY,
    75: ATTR_CONDITION_SNOWY,
    77: ATTR_CONDITION_SNOWY,
    80: ATTR_CONDITION_RAINY,
    81: ATTR_CONDITION_RAINY,
    82: ATTR_CONDITION_POURING,
    85: ATTR_CONDITION_SNOWY,
    86: ATTR_CONDITION_SNOWY,
    95: ATTR_CONDITION_LIGHTNING_RAINY,
    96: ATTR_CONDITION_LIGHTNING_RAINY,
    99: ATTR_CONDITION_LIGHTNING_RAINY,
}

RADAR_ZOOM: Final = 9
RADAR_TILE_GRID: Final = 5  # 5x5 tiles around the configured location (~300km across)
RADAR_TILE_SIZE: Final = 256
RADAR_COLOR_SCHEME: Final = 2  # "Universal Blue"
# RainViewer's radar tiles top out here — zoom 8+ returns a "Zoom Level Not
# Supported" placeholder (verified directly against their tile server). When
# RADAR_ZOOM is higher (for a more detailed basemap), radar.py fetches at
# this zoom instead and scales the result up to fit.
RAINVIEWER_MAX_ZOOM: Final = 7
RADAR_UPDATE_INTERVAL: Final = 600  # 10 min, matches RainViewer's frame cadence
RADAR_ANIMATION_FRAMES: Final = 10  # last ~100 min (RainViewer keeps ~2h)

# Satellite imagery: NASA GIBS (Global Imagery Browse Services), a public
# WMTS/XYZ tile service (no API key) built on the same GOES-East ABI data
# NOAA STAR publishes, but exposed as a real zoomable tile pyramid in
# standard Web Mercator — unlike NOAA STAR's cdn.star.nesdis.noaa.gov,
# which only serves a handful of fixed-size full-sector JPEGs with no
# lat/lon georeference. GIBS updates every ~10 min. GeoColor includes its own
# night view; Band13 clean infrared (cloud-top temperature) is the separate
# "Infrarrojo" view.
GIBS_TILE_URL_TEMPLATE: Final = (
    "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
    "{layer}/default/{time}/{matrix_set}/{z}/{y}/{x}.png"
)
GIBS_LAYER_GEOCOLOR: Final = "GOES-East_ABI_GeoColor"
GIBS_LAYER_INFRARED: Final = "GOES-East_ABI_Band13_Clean_Infrared"
GIBS_MATRIX_SET_GEOCOLOR: Final = "GoogleMapsCompatible_Level7"
GIBS_MATRIX_SET_INFRARED: Final = "GoogleMapsCompatible_Level6"
GIBS_ATTRIBUTION: Final = "Imagery by NASA GIBS / NOAA GOES-East"
# GIBS' max zoom level per layer (same numbers as GIBS_MATRIX_SET_*).
GIBS_MAX_ZOOM_GEOCOLOR: Final = 7
GIBS_MAX_ZOOM_INFRARED: Final = 6
SATELLITE_TILE_SIZE: Final = 256
# How many past 10-min frames each satellite animation stitches together.
SATELLITE_ANIMATION_FRAMES: Final = 10  # last ~100 min
# Extra older candidate timestamps fetched beyond SATELLITE_ANIMATION_FRAMES,
# so a few incomplete frames (GIBS still publishing some of a timestamp's
# tiles) don't shrink the animation below its target frame count; the most
# recent SATELLITE_ANIMATION_FRAMES *complete* ones are kept.
SATELLITE_ANIMATION_LOOKBACK_BUFFER: Final = 6
SATELLITE_ANIMATION_UPDATE_INTERVAL: Final = 1200  # 20 min

# Default onboarding locations (Buenos Aires)
DEFAULT_HOME_LATITUDE: Final = -34.6037
DEFAULT_HOME_LONGITUDE: Final = -58.3816

# Update intervals, matched to SMN's real update cadence
DEFAULT_SCAN_INTERVAL: Final = 1800  # 30 min, weather/forecast update cadence
SHORTTERM_SCAN_INTERVAL: Final = 600  # 10 min, avisos a muy corto plazo

# Weather condition ID mappings - SMN ID to HA condition
# Based on SMN's official weather icon reference table
CONDITION_ID_MAP: Final = {
    3: ATTR_CONDITION_SUNNY,  # Despejado (día)
    5: ATTR_CONDITION_CLEAR_NIGHT,  # Despejado (noche)
    13: ATTR_CONDITION_SUNNY,  # Ligeramente nublado (día)
    14: ATTR_CONDITION_CLEAR_NIGHT,  # Ligeramente nublado (noche)
    19: ATTR_CONDITION_SUNNY,  # Algo nublado (día)
    20: ATTR_CONDITION_CLEAR_NIGHT,  # Algo nublado (noche)
    25: ATTR_CONDITION_PARTLYCLOUDY,  # Parcialmente nublado (día)
    26: ATTR_CONDITION_PARTLYCLOUDY,  # Parcialmente nublado (noche)
    37: ATTR_CONDITION_CLOUDY,  # Mayormente nublado (día)
    38: ATTR_CONDITION_CLOUDY,  # Mayormente nublado (noche)
    43: ATTR_CONDITION_CLOUDY,  # Nublado
    51: ATTR_CONDITION_WINDY,  # Ventoso
    61: ATTR_CONDITION_FOG,  # Neblina
    67: ATTR_CONDITION_FOG,  # Niebla
    69: ATTR_CONDITION_FOG,  # Niebla helada
    71: ATTR_CONDITION_RAINY,  # Llovizna
    72: ATTR_CONDITION_RAINY,  # Lluvias aisladas
    73: ATTR_CONDITION_RAINY,  # Lluvias
    74: ATTR_CONDITION_POURING,  # Chaparrones (día)
    75: ATTR_CONDITION_POURING,  # Chaparrones (noche)
    76: ATTR_CONDITION_LIGHTNING_RAINY,  # Tormentas aisladas
    77: ATTR_CONDITION_SNOWY_RAINY,  # Lluvias y Nevadas
    79: ATTR_CONDITION_SNOWY,  # Nevadas
    81: ATTR_CONDITION_LIGHTNING_RAINY,  # Tormentas
    83: ATTR_CONDITION_POURING,  # Lluvias fuertes
    85: ATTR_CONDITION_SNOWY,  # Nevadas fuertes
    89: ATTR_CONDITION_LIGHTNING_RAINY,  # Tormentas fuertes
    92: ATTR_CONDITION_SNOWY,  # Ventisca alta
    94: ATTR_CONDITION_SNOWY,  # Ventisca
    96: ATTR_CONDITION_SNOWY,  # Ventisca baja
}

# Weather condition text mappings - SMN text to HA condition (fallback)
CONDITIONS_MAP: Final = {
    ATTR_CONDITION_CLEAR_NIGHT: ["despejado noche", "clear night"],
    ATTR_CONDITION_CLOUDY: ["nublado", "cubierto", "cloudy", "overcast", "mayormente nublado"],
    ATTR_CONDITION_FOG: ["niebla", "fog", "neblina"],
    ATTR_CONDITION_PARTLYCLOUDY: [
        "parcialmente nublado",
        "partly cloudy",
        "algo nublado",
        "ligeramente nublado",
    ],
    ATTR_CONDITION_RAINY: [
        "lluvia",
        "llovizna",
        "rain",
        "drizzle",
        "lluvias aisladas",
    ],
    ATTR_CONDITION_POURING: ["chaparron", "lluvias fuertes"],
    ATTR_CONDITION_LIGHTNING_RAINY: ["tormenta"],
    ATTR_CONDITION_SNOWY: ["nieve", "snow", "nevada", "ventisca"],
    ATTR_CONDITION_SNOWY_RAINY: ["lluvias y nevadas"],
    ATTR_CONDITION_WINDY: ["ventoso"],
    ATTR_CONDITION_SUNNY: ["despejado", "soleado", "clear", "sunny"],
}

# Forecast attribute mappings
FORECAST_MAP: Final = {
    ATTR_FORECAST_CONDITION: "condition",
    ATTR_FORECAST_NATIVE_PRECIPITATION: "precipitation",
    ATTR_FORECAST_NATIVE_TEMP: "temperature",
    ATTR_FORECAST_NATIVE_TEMP_LOW: "templow",
    ATTR_FORECAST_NATIVE_WIND_SPEED: "wind_speed",
    ATTR_FORECAST_TIME: "datetime",
}

# Current weather attribute mappings (from weather endpoint)
ATTR_MAP: Final = {
    "temp": "temp",
    "st": "st",  # Sensación térmica (feels like)
    "humidity": "humidity",
    "pressure": "pressure",
    "wind_speed": "wind_speed",
    "wind_deg": "wind_deg",
    "visibility": "visibility",
    "weather": "weather",
    "description": "description",
}

# Alert event ID mappings - SMN event ID to event name
ALERT_EVENT_MAP: Final = {
    37: "lluvia",  # Rain
    39: "viento",  # Wind
    40: "niebla",  # Fog
    41: "tormenta",  # Thunderstorm
    42: "nevada",  # Snow
    43: "altas_temperaturas",  # High temperatures
    44: "bajas_temperaturas",  # Low temperatures
    45: "ceniza_volcanica",  # Volcanic ash
    46: "polvo",  # Dust
    47: "viento_zonda",  # Zonda wind
    54: "humo",  # Smoke
}

# Alert event icon mappings - event name to MDI icon
ALERT_EVENT_ICONS: Final = {
    "lluvia": "mdi:weather-rainy",
    "viento": "mdi:weather-windy",
    "niebla": "mdi:weather-fog",
    "tormenta": "mdi:weather-lightning",
    "nevada": "mdi:weather-snowy",
    "altas_temperaturas": "mdi:thermometer-high",
    "bajas_temperaturas": "mdi:thermometer-low",
    "ceniza_volcanica": "mdi:volcano",
    "polvo": "mdi:weather-dust",
    "viento_zonda": "mdi:weather-windy-variant",
    "humo": "mdi:smoke",
}

# Alert types SMN also issues as avisos a muy corto plazo, which come with a
# polygon: these alerts turn on by distance to the nearest matching aviso
# instead of by the zone-wide forecast. Each maps to (keywords any of which
# must appear in the aviso title, keywords that exclude it) — titles are
# matched upper-cased and without accents, e.g. "TORMENTAS FUERTES CON
# LLUVIAS INTENSAS, RAFAGAS Y OCASIONAL CAIDA DE GRANIZO." matches storm,
# rain, wind and hail. The remaining event types (temperatures, fog, dust,
# smoke, volcanic ash) only exist as zone forecasts.
ALERT_EVENT_NEARBY_KEYWORDS: Final = {
    "tormenta": (("TORMENTA",), ()),
    "lluvia": (("LLUVIA",), ()),
    "viento": (("RAFAGA", "VIENTO"), ("ZONDA",)),
    "nevada": (("NEVADA", "NIEVE"), ()),
    "viento_zonda": (("ZONDA",), ()),
}
HAIL_NEARBY_KEYWORDS: Final = (("GRANIZO",), ())

# Human-readable names (Spanish) used in the "criterio" attribute.
ALERT_EVENT_LABELS_ES: Final = {
    "lluvia": "lluvia",
    "viento": "viento",
    "niebla": "niebla",
    "tormenta": "tormenta",
    "nevada": "nevada",
    "altas_temperaturas": "altas temperaturas",
    "bajas_temperaturas": "bajas temperaturas",
    "ceniza_volcanica": "ceniza volcánica",
    "polvo": "polvo",
    "viento_zonda": "viento zonda",
    "humo": "humo",
}

WIND_CARDINAL_DIRECTIONS: Final = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


# % — minimum middle of SMN's rain_prob_range for a forecast period to count
# (the top of the range is too optimistic: "10-30%" rarely rains).
NEXT_RAIN_PROBABILITY_THRESHOLD: Final = 40
# Open-Meteo's hourly forecast (see OPEN_METEO_URL): an hour counts as rain
# with this probability and amount, or as a storm (weather code 95-99) with
# NEXT_RAIN_HOURLY_STORM_PROBABILITY.
NEXT_RAIN_HOURLY_PROBABILITY: Final = 40  # %
NEXT_RAIN_HOURLY_MIN_PRECIPITATION: Final = 0.2  # mm
NEXT_RAIN_HOURLY_STORM_PROBABILITY: Final = 30  # %
# Within an SMN period or zone alert that already says rain, the first hour
# Open-Meteo gives at least this much is taken as when it starts.
NEXT_RAIN_REFINE_PROBABILITY: Final = 20  # %

# Nowcast (nowcast.py): how far ahead it extrapolates the radar/infrared,
# how far around the location it looks, and how close counts as "here".
NOWCAST_HORIZON_MINUTES: Final = 120
NOWCAST_AREA_RADIUS_KM: Final = 220
NOWCAST_LOCATION_RADIUS_KM: Final = 5
NOWCAST_UPDATE_INTERVAL: Final = 600  # 10 min, the infrared/radar cadence
# Frames older than this don't count (SINARAME's arrive ~25-35 min late).
NOWCAST_MAX_AGE_MINUTES: Final = 75


def wind_cardinal(degrees: float | int | None) -> str | None:
    """Convert a wind bearing in degrees to a 16-point compass direction."""
    if degrees is None:
        return None
    index = round(degrees / 22.5) % 16
    return WIND_CARDINAL_DIRECTIONS[index]


# Alert level mappings - SMN alert level to severity
ALERT_LEVEL_MAP: Final = {
    1: {"name": "none", "color": "white", "severity": "info"},
    2: {"name": "advertencia", "color": "violeta", "severity": "warning"},  # Advisory
    3: {"name": "amarillo", "color": "amarillo", "severity": "warning"},  # Yellow alert
    4: {"name": "naranja", "color": "naranja", "severity": "error"},  # Orange alert
    5: {"name": "rojo", "color": "rojo", "severity": "error"},  # Red alert
}
