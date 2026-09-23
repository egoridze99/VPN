#!/bin/sh
# update-geo.sh — свежие списки geosite/geoip для Xray на RU-сервере.
# Скачивает списки, проверяет с ними конфиг и только потом подменяет старые.
set -eu
ENV_FILE="${VPN_ENV:-/root/vpn.env}"
. "$ENV_FILE"
DIR="${GEO_DIR:-/opt/ru-proxy/geo}"
URL="https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { logger -t update-geo "ОШИБКА: $*"; echo "Ошибка: $*" >&2; exit 1; }

for f in geosite.dat geoip.dat; do
  # сначала через туннель; если Xray ещё не запущен — напрямую
  curl -fsSL --max-time 300 -x socks5h://127.0.0.1:1080 -o "$TMP/$f" "$URL/$f" 2>/dev/null \
    || curl -fsSL --max-time 300 -o "$TMP/$f" "$URL/$f" \
    || fail "не удалось скачать $f"
done

docker run --rm -v "$TMP":/usr/share/xray:ro \
  -v /opt/ru-proxy/xray.json:/etc/xray/config.json:ro \
  "$XRAY_IMAGE" xray -test -config /etc/xray/config.json >/dev/null 2>&1 \
  || fail "конфиг не проходит проверку с новыми списками, старые оставлены"

mkdir -p "$DIR"
cp "$TMP/geosite.dat" "$TMP/geoip.dat" "$DIR/"
if [ -n "$(docker ps -q -f name='^xray$')" ]; then
  (cd /opt/ru-proxy && docker compose restart xray >/dev/null)
fi
logger -t update-geo "списки обновлены"
echo "Списки обновлены: $DIR"
