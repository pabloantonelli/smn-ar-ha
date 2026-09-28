"""Solve SMN's Cloudflare challenge with a headless browser and harvest the
JWT used to authenticate requests to ws1.smn.gob.ar.

Adapted from nixietab/OpenSMN's tokenext.py (GPL-2.0).

KNOWN LIMITATION (validated with a real spike, see plan Phase 0): this only
reliably unlocks ws1.smn.gob.ar (the JSON API). mapa.smn.gob.ar and
estaticos.smn.gob.ar (map tiles / satellite images) enforce a separate,
per-hostname Cloudflare challenge that:
  - does NOT get satisfied by the ws1 cf_clearance cookie,
  - does NOT resolve via a top-level Selenium navigation to the resource
    (Cloudflare flags the request before the JS challenge even has a
    chance to run, most likely due to Selenium's automation fingerprint),
  - does NOT resolve via a background fetch() from the smn.gob.ar page
    either (fetch() retrieves the challenge HTML as inert bytes; it never
    executes the embedded challenge script the way a real navigation or
    iframe render would).
A real, non-automated browser session (e.g. a manual Chrome profile) does
pass this challenge when browsing www.smn.gob.ar normally, so the map/image
endpoints are left wired up in server.py for when a working bypass is
found (e.g. a non-headless browser, undetected-chromedriver, or a
real user's browser profile cookie import), but should be treated as
experimental/likely-unavailable for now. See addons/smn_proxy/README.md.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

SMN_URL = "https://www.smn.gob.ar/"

_TOKEN_PATTERNS = [
    r"localStorage\.setItem\(\s*['\"]token['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
    r"localStorage\.token\s*=\s*['\"]([^'\"]+)['\"]",
    r'["\']token["\']\s*:\s*["\']([^"\']+)["\']',
]

log = logging.getLogger(__name__)


@dataclass
class SmnSession:
    """Everything needed to make authenticated requests to ws1.smn.gob.ar."""

    token: str | None = None
    cookies: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0

    @property
    def is_valid(self) -> bool:
        return bool(self.token) and bool(self.cookies)


def _make_chrome_options() -> Options:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    return opts


def _extract_token(driver) -> str | None:
    try:
        token = driver.execute_script("return window.localStorage.getItem('token');")
        if token:
            return token
    except Exception:  # noqa: BLE001
        pass

    for pattern in _TOKEN_PATTERNS:
        match = re.search(pattern, driver.page_source)
        if match:
            return match.group(1)
    return None


def fetch_session(wait_seconds: int = 12, driver_wait_timeout: int = 30) -> SmnSession:
    """Load the SMN homepage, solve the challenge, and harvest token+cookies.

    Verified with curl through the resulting proxy: weather, forecast, sun,
    georef (including the previously-401 coordinate lookup), and all
    warning/* endpoints return 200 with real data using just this session.
    """
    options = _make_chrome_options()
    driver = webdriver.Chrome(options=options)

    try:
        log.info("Loading %s to solve Cloudflare challenge...", SMN_URL)
        driver.get(SMN_URL)
        time.sleep(wait_seconds)

        page = driver.page_source.lower()
        if "just a moment" in page or "checking your browser" in page:
            log.info("Cloudflare challenge detected, waiting a bit longer...")
            time.sleep(10)

        try:
            WebDriverWait(driver, driver_wait_timeout).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
        except Exception as err:  # noqa: BLE001
            log.warning("Timeout waiting for page body: %s", err)

        token = _extract_token(driver)
        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}

        if not token:
            log.error("Could not find JWT in localStorage or page source")
        if "cf_clearance" not in cookies:
            log.warning("cf_clearance cookie not found; requests may still be challenged")

        return SmnSession(token=token, cookies=cookies, fetched_at=time.time())
    finally:
        driver.quit()
