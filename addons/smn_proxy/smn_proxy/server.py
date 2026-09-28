"""HTTP proxy exposing SMN's JSON API to the local Home Assistant network.

Solves the Cloudflare challenge in the background with a headless browser
(see cloudflare_session.py), then proxies:
  - GET /smn/v1/<path> -> https://ws1.smn.gob.ar/v1/<path> (Authorization: JWT)

Adapted from nixietab/OpenSMN's server.py (GPL-2.0): same caching/proxy
shape, refreshing the session on a timer instead of lazily on first 401.

NOTE: this deliberately does NOT proxy mapa.smn.gob.ar / estaticos.smn.gob.ar
(map tiles, satellite imagery). Those sit behind a separate per-hostname
Cloudflare challenge that couldn't be solved reliably from a headless
browser (see README.md), and smn.gob.ar's robots.txt explicitly disallows
Claude/Anthropic bots — not worth pushing further on evasion techniques
for a personal integration. For animated radar in Home Assistant, use the
"Weather Radar Card" (HACS) against RainViewer instead; it needs no
backend proxy at all.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from datetime import timedelta
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, request

from .cloudflare_session import SmnSession, fetch_session

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
logging.basicConfig(level=LOG_LEVEL, format="[%(asctime)s] %(levelname)s %(message)s")
log = logging.getLogger("smn_proxy")

PORT = int(os.getenv("PORT", "6942"))
CACHE_DIR = os.getenv("CACHE_DIR", "cache")
CACHE_TTL = timedelta(minutes=int(os.getenv("CACHE_TTL_MINUTES", "15")))
TOKEN_REFRESH_SECONDS = int(os.getenv("TOKEN_REFRESH_MINUTES", "20")) * 60

UPSTREAM_API = "https://ws1.smn.gob.ar/v1"

app = Flask(__name__)

_session_lock = threading.Lock()
_session = SmnSession()


def _refresh_session_loop() -> None:
    global _session
    while True:
        try:
            new_session = fetch_session()
            with _session_lock:
                _session = new_session
            log.info(
                "SMN session refreshed (token=%s, cookies=%d)",
                "yes" if new_session.token else "no",
                len(new_session.cookies),
            )
        except Exception as err:  # noqa: BLE001
            log.error("Failed to refresh SMN session: %s", err)
        time.sleep(TOKEN_REFRESH_SECONDS)


def _get_session() -> SmnSession:
    with _session_lock:
        return _session


def _cache_path(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()
    return os.path.join(CACHE_DIR, f"{digest}.bin")


def _load_cache(url: str) -> tuple[bytes, str] | None:
    path = _cache_path(url)
    meta_path = path + ".ctype"
    if not os.path.exists(path) or not os.path.exists(meta_path):
        return None
    age = time.time() - os.path.getmtime(path)
    if age > CACHE_TTL.total_seconds():
        return None
    with open(path, "rb") as f:
        data = f.read()
    with open(meta_path, "r", encoding="utf-8") as f:
        content_type = f.read().strip()
    return data, content_type


def _save_cache(url: str, data: bytes, content_type: str) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(url)
    with open(path, "wb") as f:
        f.write(data)
    with open(path + ".ctype", "w", encoding="utf-8") as f:
        f.write(content_type)


@app.route("/smn/v1/<path:subpath>")
def proxy_api(subpath: str):
    smn_session = _get_session()
    if not smn_session.is_valid:
        return jsonify({"error": "SMN session not ready yet, try again shortly"}), 503

    url = urljoin(UPSTREAM_API + "/", subpath)
    if request.query_string:
        url += "?" + request.query_string.decode("utf-8")

    cached = _load_cache(url)
    if cached:
        data, content_type = cached
        return Response(data, content_type=content_type)

    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Authorization": f"JWT {smn_session.token}",
    }

    try:
        resp = requests.get(
            url, headers=headers, cookies=smn_session.cookies, timeout=15
        )
    except requests.RequestException as err:
        log.error("Upstream request failed for %s: %s", url, err)
        return jsonify({"error": "Upstream request failed"}), 502

    if resp.status_code != 200:
        return Response(resp.content, status=resp.status_code,
                         content_type=resp.headers.get("Content-Type", "text/plain"))

    content_type = resp.headers.get("Content-Type", "application/json")
    _save_cache(url, resp.content, content_type)
    return Response(resp.content, content_type=content_type)


@app.route("/smn/health")
def health():
    smn_session = _get_session()
    return jsonify(
        {
            "session_ready": smn_session.is_valid,
            "has_token": bool(smn_session.token),
            "cookie_count": len(smn_session.cookies),
            "fetched_at": smn_session.fetched_at,
        }
    )


def main() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)

    # Prime the session synchronously so the first requests don't 503.
    global _session
    try:
        _session = fetch_session()
    except Exception as err:  # noqa: BLE001
        log.error("Initial SMN session fetch failed, will retry in background: %s", err)

    threading.Thread(target=_refresh_session_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
