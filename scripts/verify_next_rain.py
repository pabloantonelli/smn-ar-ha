"""How often "Próxima lluvia o tormenta" got it right, from Home Assistant's own history.

Every 10 minutes of the sensor's recorded history, did it say rain within
2 hours, and did the nearest airport's METAR report rain in those 2 hours
(present or recent weather: RA, DZ, TS, also as RERA, RETSRA...)? Split by
`fuente`, and the radar also by echo strength, to check the thresholds and
the `probabilidad` tables in const.py against a real install.

Standard library only:

    HA_TOKEN=... python3 scripts/verify_next_rain.py \\
        --url http://homeassistant.local:8123 \\
        --entity sensor.casa_proxima_lluvia --station SACO

The recorder keeps 10 days by default (recorder: purge_keep_days): the
longer, the more rain events to judge by. METARs come from the Iowa
Environmental Mesonet. An airport tens of km away doesn't get exactly the
same rain, so read the numbers as a rough check.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import os
import re
import urllib.parse
import urllib.request

WINDOW = timedelta(hours=2)
STEP = timedelta(minutes=10)
_RAIN = re.compile(r"^(RE)?[-+]?(TS|SH)?(RA|DZ)|^(RE)?[-+]?TS")


def _get(url: str, token: str | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_history(url: str, token: str, entity: str) -> list[dict]:
    start = "2000-01-01T00:00:00Z"
    query = urllib.parse.urlencode({"filter_entity_id": entity, "end_time": datetime.now(timezone.utc).isoformat()})
    data = json.loads(_get(f"{url}/api/history/period/{start}?{query}", token))
    return data[0] if data else []


def fetch_metars(station: str, start: datetime, end: datetime) -> list[tuple[datetime, bool]]:
    query = urllib.parse.urlencode(
        {
            "station": station,
            "data": "metar",
            "year1": start.year, "month1": start.month, "day1": start.day,
            "year2": end.year, "month2": end.month, "day2": end.day,
            "tz": "Etc/UTC", "format": "onlycomma", "latlon": "no", "missing": "M", "direct": "no",
            "report_type": ["3", "4"],
        },
        doseq=True,
    )
    text = _get(f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?{query}").decode()
    reports = []
    for row in csv.DictReader(io.StringIO(text)):
        when = datetime.strptime(row["valid"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        tokens = row["metar"].split(" RMK")[0].split()
        reports.append((when, any(_RAIN.match(t) for t in tokens if not t.startswith("VC"))))
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True, help="Home Assistant, e.g. http://homeassistant.local:8123")
    parser.add_argument("--entity", required=True, help="the Próxima lluvia o tormenta sensor")
    parser.add_argument("--station", required=True, help="nearest airport's ICAO code, e.g. SACO, SAEZ, SABE")
    args = parser.parse_args()
    token = os.environ["HA_TOKEN"]

    history = fetch_history(args.url.rstrip("/"), token, args.entity)
    if not history:
        raise SystemExit("No history for that entity")
    states = []
    for state in history:
        try:
            when = datetime.fromisoformat(state["state"])
        except ValueError:
            when = None
        states.append((datetime.fromisoformat(state["last_changed"]), when, state["attributes"]))
    reports = fetch_metars(args.station, states[0][0] - timedelta(days=1), datetime.now(timezone.utc) + timedelta(days=1))
    if not reports:
        raise SystemExit(f"No METARs for {args.station}")

    def rained(start: datetime) -> bool:
        return any(wet for when, wet in reports if start <= when <= start + WINDOW)

    by_source: dict[str, list[int]] = {}
    by_echo: dict[str, list[int]] = {}
    t = states[0][0].replace(second=0, microsecond=0) + STEP
    i = 0
    while t + WINDOW <= reports[-1][0]:
        while i + 1 < len(states) and states[i + 1][0] <= t:
            i += 1
        _, when, attributes = states[i]
        said_rain = when is not None and when <= t + WINDOW
        source = (attributes.get("fuente") or "?") if said_rain else "(nada a la vista)"
        outcome = rained(t)
        by_source.setdefault(source, [0, 0])
        by_source[source][0] += 1
        by_source[source][1] += outcome
        if said_rain and source.startswith("radar"):
            dbz = attributes.get("intensity") or 0
            band = "< 28 dBZ" if dbz < 28 else "28-33 dBZ" if dbz < 33 else ">= 33 dBZ"
            by_echo.setdefault(band, [0, 0])
            by_echo[band][0] += 1
            by_echo[band][1] += outcome
        t += STEP

    hours = (t - states[0][0]).total_seconds() / 3600
    print(f"{args.entity} vs {args.station}, {hours / 24:.1f} días")
    print('Decía "llueve en menos de 2 h" según fuente -> llovió en el aeropuerto en esas 2 h:')
    for source, (n, wet) in sorted(by_source.items(), key=lambda item: -item[1][0]):
        print(f"  {source:35s} {n / 6:6.1f} h   llovió {wet / n:4.0%}")
    if by_echo:
        print("Radar, por intensidad del eco:")
        for band, (n, wet) in sorted(by_echo.items()):
            print(f"  {band:35s} {n / 6:6.1f} h   llovió {wet / n:4.0%}")


if __name__ == "__main__":
    main()
