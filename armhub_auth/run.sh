#!/usr/bin/with-contenv bashio

export TOKEN_CACHE_FILE="/data/.armadillo_tokens.json"
export DATA_DIR="/data"
export ADDON_MODE="true"
export PYTHONUNBUFFERED=1

bashio::log.info "Starting ARM Hub auth addon..."

exec python3 /app/webui.py
