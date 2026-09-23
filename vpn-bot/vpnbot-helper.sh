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
# бот (TELEGRAM_PROXY) ходит в Telegram. Пока контейнер перезапускается, порт
# на секунду-две пропадает, и если бот в этот момент попробует отправить
# сообщение — оно не уйдёт. Ждём (до 15 секунд), пока порт снова откликнется,
# прежде чем отдать управление обратно боту. Не считается ошибкой: устройство
# уже добавлено/удалено к этому моменту, ждём только сеть.
wait_socks() {
  local i
  for i in $(seq 1 30); do
    (exec 3<>/dev/tcp/127.0.0.1/1080) 2>/dev/null && { exec 3>&- 3<&- 2>/dev/null; return 0; }
    sleep 0.5
  done
  echo "Предупреждение: SOCKS-порт 127.0.0.1:1080 не ответил за 15 секунд после перезапуска xray" >&2
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
