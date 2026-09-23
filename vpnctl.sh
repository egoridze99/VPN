#!/usr/bin/env bash
# vpnctl.sh — все значения схемы в одном файле, подстановка в конфиги,
# список устройств и готовые ссылки/QR-коды для подключения.
set -euo pipefail

ENV_FILE="${VPN_ENV:-/root/vpn.env}"
OUT_DIR="${VPN_OUT:-/root/vpn-links}"
MTG_IMAGE="ghcr.io/9seconds/mtg:2"

die() { echo "Ошибка: $*" >&2; exit 1; }

load() {
  [ -f "$ENV_FILE" ] || die "нет файла $ENV_FILE (создайте его по шаблону)"
  set -a
  # shellcheck source=/dev/null
  . "$ENV_FILE"
  set +a
  XRAY_IMAGE="${XRAY_IMAGE:-teddysun/xray:26.3.27}"
}

# Записать KEY="value" в файл значений: заменить строку или добавить в конец
set_var() {
  local key="$1" val="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*|${key}=\"${val}\"|" "$ENV_FILE"
  else
    printf '%s="%s"\n' "$key" "$val" >> "$ENV_FILE"
  fi
}

gen_uuid() { docker run --rm "$XRAY_IMAGE" xray uuid; }

# Печатает "приватный публичный". Понимает старый (Private/Public key)
# и новый (PrivateKey/Password) формат вывода xray x25519.
gen_pair() {
  docker run --rm "$XRAY_IMAGE" xray x25519 | awk -F': *' '
    /[Pp]rivate/            { priv = $2 }
    /Password|[Pp]ublic/    { pub  = $2 }
    END { print priv, pub }'
}

qr_png() { if command -v qrencode >/dev/null; then qrencode -o "$2" "$1"; fi; }

cmd_init() {
  load
  local v a b
  for v in RU_IP EU_IP CLEANING_DOMAIN TG_HOST VPN_HOST VPN2_HOST EU_HOST; do
    [ -n "${!v:-}" ] || die "сначала заполните $v в $ENV_FILE"
  done
  command -v docker >/dev/null || die "нужен Docker (шаг 1.1)"
  [ -n "${EU_UUID:-}" ]        || set_var EU_UUID "$(gen_uuid)"
  [ -n "${EMERGENCY_UUID:-}" ] || set_var EMERGENCY_UUID "$(gen_uuid)"
  if [ -z "${RU_PRIVATE_KEY:-}" ]; then
    read -r a b <<<"$(gen_pair)"; set_var RU_PRIVATE_KEY "$a"; set_var RU_PUBLIC_KEY "$b"
  fi
  if [ -z "${EU_PRIVATE_KEY:-}" ]; then
    read -r a b <<<"$(gen_pair)"; set_var EU_PRIVATE_KEY "$a"; set_var EU_PUBLIC_KEY "$b"
  fi
  [ -n "${RU_SHORT_ID:-}" ]   || set_var RU_SHORT_ID "$(openssl rand -hex 8)"
  [ -n "${EU_SHORT_ID:-}" ]   || set_var EU_SHORT_ID "$(openssl rand -hex 8)"
  [ -n "${RU_XHTTP_PATH:-}" ] || set_var RU_XHTTP_PATH "/api/v2/$(openssl rand -hex 4)"
  [ -n "${EU_PATH:-}" ]       || set_var EU_PATH "/assets/$(openssl rand -hex 4)"
  [ -n "${MTG_SECRET:-}" ]    || set_var MTG_SECRET "$(docker run --rm "$MTG_IMAGE" generate-secret --hex "$TG_HOST" | tail -n1)"
  chmod 600 "$ENV_FILE"
  echo "Готово: пустые значения сгенерированы и записаны в $ENV_FILE"
  echo "Уже заполненные значения не менялись."
}

cmd_add_device() {
  local name="${1:-}" id
  [ -n "$name" ] || die "укажите имя: vpnctl.sh add-device mama-phone"
  [[ "$name" =~ ^[A-Za-z0-9_-]+$ ]] || die "имя только латиницей, цифрами, «-» и «_»"
  load
  case " ${DEVICES:-} " in *" $name="*) die "устройство $name уже есть" ;; esac
  id="$(gen_uuid)"
  set_var DEVICES "$(echo "${DEVICES:-} $name=$id" | xargs)"
  echo "Добавлено: $name ($id)"
  echo "Дальше: vpnctl.sh sync-clients, затем перезапуск xray"
}

cmd_remove_device() {
  local name="${1:-}" out="" pair
  [ -n "$name" ] || die "укажите имя: vpnctl.sh remove-device mama-phone"
  load
  for pair in ${DEVICES:-}; do [ "${pair%%=*}" = "$name" ] || out="$out $pair"; done
  set_var DEVICES "$(echo "$out" | xargs)"
  rm -f "$OUT_DIR/$name".txt "$OUT_DIR/$name"-*.png
  echo "Удалено: $name. Дальше: vpnctl.sh sync-clients, затем перезапуск xray"
}

# Заменяет плейсхолдеры (RU_IP, EU_HOST, …) в файлах на значения из vpn.env
cmd_render() {
  [ $# -gt 0 ] || die "укажите файлы: vpnctl.sh render /opt/xray/config.json"
  load
  local names f n v
  # длинные имена первыми, чтобы EU_PATH не задел EU_PATH_ENCODED и т.п.
  names="$(grep -oE '^[A-Z0-9_]+=' "$ENV_FILE" | tr -d '=' \
           | awk '{ print length, $0 }' | sort -rn | cut -d' ' -f2)"
  for f in "$@"; do
    [ -f "$f" ] || die "нет файла $f"
    cp "$f" "$f.bak"
    for n in $names; do
      [ "$n" = DEVICES ] && continue
      v="${!n:-}"; [ -n "$v" ] || continue
      v="${v//\\/\\\\}"; v="${v//|/\\|}"; v="${v//&/\\&}"
      sed -i "s|\b${n}\b|${v}|g" "$f"
    done
    echo "Подставлено: $f (прежняя версия: $f.bak)"
    if grep -nE '\b(UUID_[A-Z]+|[A-Z][A-Z0-9]*_(IP|HOST|KEY|ID|PATH|UUID|SECRET|DOMAIN|PORT))\b' "$f"; then
      echo "  ↑ эти плейсхолдеры остались: в vpn.env нет значения (UUID устройств заполняет sync-clients)"
    fi
  done
}

# Переписывает списки клиентов в обоих VPN-входах RU-сервера по списку DEVICES
cmd_sync_clients() {
  local f="${1:-/opt/ru-proxy/xray.json}" vision="[]" xhttp="[]" pair name id
  command -v jq >/dev/null || die "установите jq: apt install -y jq"
  load
  [ -n "${DEVICES:-}" ] || die "нет устройств: vpnctl.sh add-device имя"
  [ -f "$f" ] || die "нет файла $f"
  for pair in $DEVICES; do
    name="${pair%%=*}"; id="${pair#*=}"
    vision=$(jq -c --arg id "$id" --arg e "$name" '. + [{id: $id, flow: "xtls-rprx-vision", email: $e}]' <<<"$vision")
    xhttp=$(jq -c --arg id "$id" --arg e "$name-xhttp" '. + [{id: $id, email: $e}]' <<<"$xhttp")
  done
  cp "$f" "$f.bak"
  jq --argjson v "$vision" --argjson x "$xhttp" '
      (.inbounds[] | select(.tag == "vpn-vision") | .settings.clients) = $v
    | (.inbounds[] | select(.tag == "vpn-xhttp")  | .settings.clients) = $x' "$f.bak" > "$f"
  echo "Клиенты обновлены в $f (прежняя версия: $f.bak)"
  echo "Перезапуск: cd /opt/ru-proxy && docker compose restart xray"
}

# Собирает ссылки для всех устройств (или одного) и сохраняет их в $OUT_DIR
cmd_links() {
  local only="${1:-}" pair name id main backup tg emerg ru_path eu_path
  load
  mkdir -p "$OUT_DIR"; chmod 700 "$OUT_DIR"
  ru_path="${RU_XHTTP_PATH//\//%2F}"; eu_path="${EU_PATH//\//%2F}"

  tg="tg://proxy?server=${RU_IP}&port=443&secret=${MTG_SECRET}"
  echo "== Telegram-прокси (одна ссылка для всех) =="
  echo "$tg"
  echo "$tg" > "$OUT_DIR/telegram.txt"; qr_png "$tg" "$OUT_DIR/telegram.png"

  for pair in ${DEVICES:-}; do
    name="${pair%%=*}"; id="${pair#*=}"
    [ -z "$only" ] || [ "$only" = "$name" ] || continue
    main="vless://${id}@${RU_IP}:443?encryption=none&type=tcp&security=reality&sni=${VPN_HOST}&fp=chrome&pbk=${RU_PUBLIC_KEY}&sid=${RU_SHORT_ID}&flow=xtls-rprx-vision#${name}-main"
    backup="vless://${id}@${RU_IP}:443?encryption=none&type=xhttp&security=reality&sni=${VPN2_HOST}&fp=firefox&pbk=${RU_PUBLIC_KEY}&sid=${RU_SHORT_ID}&path=${ru_path}&mode=auto#${name}-backup"
    echo; echo "== $name =="
    echo "Основной:"; echo "$main"
    echo "Запасной:"; echo "$backup"
    printf '%s\n%s\n' "$main" "$backup" > "$OUT_DIR/$name.txt"
    qr_png "$main" "$OUT_DIR/$name-main.png"; qr_png "$backup" "$OUT_DIR/$name-backup.png"
  done

  emerg="vless://${EMERGENCY_UUID}@${EU_IP}:443?encryption=none&type=xhttp&security=reality&sni=${EU_HOST}&fp=firefox&pbk=${EU_PUBLIC_KEY}&sid=${EU_SHORT_ID}&path=${eu_path}&mode=auto#emergency"
  echo; echo "== Аварийный (только для своих устройств) =="
  echo "$emerg"
  echo "$emerg" > "$OUT_DIR/emergency.txt"; qr_png "$emerg" "$OUT_DIR/emergency.png"

  echo; echo "Ссылки и QR-картинки сохранены в $OUT_DIR"
}

# Показывает QR-коды прямо в терминале: vpnctl.sh qr mama-phone | telegram | emergency
cmd_qr() {
  local name="${1:-}" f l
  [ -n "$name" ] || die "укажите устройство, telegram или emergency"
  command -v qrencode >/dev/null || die "установите qrencode: apt install -y qrencode"
  f="$OUT_DIR/$name.txt"
  [ -f "$f" ] || die "нет $f — сначала выполните vpnctl.sh links"
  while IFS= read -r l; do
    echo; echo "${l##*#}"
    qrencode -t ansiutf8 "$l"
  done < "$f"
}

cmd_show() {
  load
  grep -vE '_PRIVATE_KEY=' "$ENV_FILE"
  echo "(приватные ключи скрыты)"
}

usage() {
  cat <<'EOF'
Использование: vpnctl.sh КОМАНДА [аргументы]

  init                     сгенерировать все пустые ключи, UUID, пути и секрет mtg
  add-device ИМЯ           добавить устройство (новый UUID)
  remove-device ИМЯ        удалить устройство
  sync-clients [ФАЙЛ]      записать устройства в xray.json RU-сервера
  render ФАЙЛ...           подставить значения вместо плейсхолдеров в конфиги
  links [ИМЯ]              собрать ссылки для всех устройств (или одного)
  qr ИМЯ                   показать QR-коды в терминале (ИМЯ, telegram, emergency)
  show                     показать значения (без приватных ключей)

Файл значений: /root/vpn.env (другой путь — переменная VPN_ENV)
EOF
}

case "${1:-}" in
  init)          cmd_init ;;
  add-device)    shift; cmd_add_device "$@" ;;
  remove-device) shift; cmd_remove_device "$@" ;;
  sync-clients)  shift; cmd_sync_clients "$@" ;;
  render)        shift; cmd_render "$@" ;;
  links)         shift; cmd_links "$@" ;;
  qr)            shift; cmd_qr "$@" ;;
  show)          cmd_show ;;
  *)             usage ;;
esac
