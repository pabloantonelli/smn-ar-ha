#!/usr/bin/env sh
set -e

OPTIONS_FILE="/data/options.json"
if [ -f "$OPTIONS_FILE" ]; then
    CACHE_TTL_MINUTES=$(python3 -c "import json;print(json.load(open('$OPTIONS_FILE')).get('cache_ttl_minutes',15))")
    TOKEN_REFRESH_MINUTES=$(python3 -c "import json;print(json.load(open('$OPTIONS_FILE')).get('token_refresh_minutes',20))")
    LOG_LEVEL=$(python3 -c "import json;print(json.load(open('$OPTIONS_FILE')).get('log_level','info'))")
else
    CACHE_TTL_MINUTES=15
    TOKEN_REFRESH_MINUTES=20
    LOG_LEVEL=info
fi

export CACHE_TTL_MINUTES TOKEN_REFRESH_MINUTES LOG_LEVEL
export CACHE_DIR="/data/cache"
mkdir -p "$CACHE_DIR"

cd /app
exec python3 -m smn_proxy.server
