#!/usr/bin/env bash
# vpnbot-helper.sh — единственная привилегированная точка входа для vpnbot.
# Устанавливается в /usr/local/bin, владелец root, запускается через
# sudo -n от пользователя vpnbot (см. /etc/sudoers.d/vpnbot).
#
# Каждая подкоманда сама проверяет своё имя устройства, так что даже если
# sudoers разрешает любые аргументы, внутрь vpnctl.sh попадают только
# проверенные значения.
set -euo pipefail

VPNCTL=/usr/local/bin/vpnctl.sh
COMPOSE_DIR=/opt/ru-proxy
BOT_OUT=/opt/vpn-bot/links
BOT_USER=vpnbot

die() { echo "Ошибка: $*" >&2; exit 1; }

name_ok() { [[ "${1:-}" =~ ^[a-z0-9_-]{2,32}$ ]]; }

# Xray на RU-сервере — это ещё и SOCKS-порт 127.0.0.1:1080, через который сам
# бот (TELEGRAM_PROXY) ходит в Telegram. Пока контейнер перезапускается,
# и ещё некоторое время после — пока не поднимется туннель до EU-сервера —
# исходящие запросы бота не проходят, и он не может ответить в чат. Просто
# проверки "порт слушает" мало: порт Xray открывает рано, а сам туннель
# VLESS+XHTTP+Reality до Европы поднимается на секунды дольше. Поэтому ждём
# (до 30 секунд) настоящего успешного запроса через прокси до api.telegram.org
# — того же пути, которым ходит сам бот, — прежде чем отдать управление назад.
# Не считается ошибкой: устройство уже добавлено/удалено к этому моменту,
# ждём только сеть, а бот сам умеет повторить отправку при неудаче.
wait_socks() {
  local i
  for i in $(seq 1 30); do
    # Без -f: любой полученный HTTP-ответ (даже 404 от голого api.telegram.org)
    # доказывает, что туннель работает. Нужен именно код возврата curl — 0
    # означает "соединение и TLS прошли", а не конкретный HTTP-статус.
    curl -sS --max-time 3 -x socks5h://127.0.0.1:1080 -o /dev/null \
      https://api.telegram.org 2>/dev/null && return 0
    sleep 1
  done
  echo "Предупреждение: за 30 секунд после перезапуска xray не удалось достучаться до api.telegram.org через SOCKS-туннель 127.0.0.1:1080" >&2
  return 0
}

mkdir -p "$BOT_OUT"

cmd="${1:-}"
[ $# -gt 0 ] && shift || true

case "$cmd" in
  add)
    name="${1:-}"; name_ok "$name" || die "плохое имя устройства"
    "$VPNCTL" add-device "$name"
    "$VPNCTL" sync-clients
    ( cd "$COMPOSE_DIR" && docker compose restart xray )
    wait_socks
    ;;

  remove)
    name="${1:-}"; name_ok "$name" || die "плохое имя устройства"
    "$VPNCTL" remove-device "$name"
    "$VPNCTL" sync-clients
    ( cd "$COMPOSE_DIR" && docker compose restart xray )
    wait_socks
    rm -f "$BOT_OUT/$name.txt" "$BOT_OUT/$name"-*.png
    ;;

  links)
    if [ -n "${1:-}" ]; then
      name_ok "$1" || die "плохое имя устройства"
      VPN_OUT="$BOT_OUT" "$VPNCTL" links "$1"
    else
      VPN_OUT="$BOT_OUT" "$VPNCTL" links
    fi
    chown -R "$BOT_USER":"$BOT_USER" "$BOT_OUT"
    ;;

  list)
    # имена устройств из vpn.env — сверить со state.json бота
    awk -F'"' '/^DEVICES=/{print $2}' /root/vpn.env
    ;;

  status)
    echo "== контейнеры =="
    docker ps --format '{{.Names}}: {{.Status}}' | grep -E '^(xray|mtg):' || echo "xray/mtg не запущены"
    echo "== проверка туннеля (последние 5 записей) =="
    journalctl -t tunnel-check --since "-30 min" --no-pager 2>/dev/null | tail -n 5 || echo "нет записей"
    ;;

  *)
    die "неизвестная команда helper: $cmd"
    ;;
esac
