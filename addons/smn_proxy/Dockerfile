FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Chromium + driver for headless Cloudflare-challenge solving (Selenium).
# NOTE (Phase 0 spike): validate image size / RAM footprint on constrained
# hardware (e.g. Raspberry Pi aarch64) before relying on this in production.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY smn_proxy /app/smn_proxy
COPY rootfs/run.sh /run.sh
RUN chmod a+x /run.sh

EXPOSE 6942

CMD ["/run.sh"]
