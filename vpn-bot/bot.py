#!/usr/bin/env python3
"""
vpnbot — телеграм-бот для семейного самообслуживания VPN и Telegram-прокси.

Бот не трогает /root/vpn.env напрямую: все привилегированные операции идут
через sudo в /usr/local/bin/vpnbot-helper.sh, который вызывает vpnctl.sh.
Сам бот работает от имени непривилегированного пользователя vpnbot.

Подробности установки — в telegram-bot-setup.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------------------------------
# Конфигурация (из переменных окружения — см. .env.example)
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ["BOT_TOKEN"]
FAMILY_CHAT_ID = int(os.environ.get("FAMILY_CHAT_ID", "0"))
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
MAX_DEVICES_PER_USER = int(os.environ.get("MAX_DEVICES_PER_USER", "5"))
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY", "").strip() or None

STATE_FILE = Path(os.environ.get("STATE_FILE", "/opt/vpn-bot/state.json"))
LINKS_DIR = Path(os.environ.get("LINKS_DIR", "/opt/vpn-bot/links"))
HELPER = os.environ.get("HELPER_PATH", "/usr/local/bin/vpnbot-helper.sh")

NAME_RE = re.compile(r"^[a-z0-9_-]{2,32}$")
RESERVED_NAMES = {"telegram", "emergency", "main", "backup"}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("vpnbot")

_state_lock = asyncio.Lock()

HELP_TEXT = (
    "Я выдаю доступы к VPN и Telegram-прокси. Работаю только в семейном чате "
    "и в личных сообщениях с теми, кого уже видел в этом чате.\n\n"
    "/add имя — добавить новое устройство и получить ссылки и QR\n"
    "/claim имя — привязать к себе устройство, выпущенное ещё до бота\n"
    "/mydevices — мои устройства\n"
    "/links [имя] — прислать ссылки ещё раз\n"
    "/qr [имя] — прислать QR-коды ещё раз\n"
    "/remove [имя] — удалить устройство\n"
    "/telegram — ссылка на Telegram-прокси\n"
    "/emergency — аварийный профиль (если основной сервер недоступен)\n"
    "/whoami — мой Telegram ID и ID этого чата\n"
)
ADMIN_HELP_TEXT = (
    "\nКоманды администратора:\n"
    "/devices — список всех устройств\n"
    "/status — состояние серверов\n"
)


# --------------------------------------------------------------------------
# Состояние бота: кто владеет каким устройством, кто уже писал в семейный чат
# --------------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"devices": {}, "known_users": {}}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    try:
        os.chmod(STATE_FILE, 0o600)
    except OSError:
        pass


async def remember_user(user) -> None:
    async with _state_lock:
        state = _load_state()
        state["known_users"][str(user.id)] = {
            "name": user.full_name,
            "username": user.username or "",
        }
        _save_state(state)


# --------------------------------------------------------------------------
# Доступ: команды работают только в семейном чате и в личке с уже известными
# --------------------------------------------------------------------------

def restricted(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        user = update.effective_user
        if user is None or chat is None:
            return
        if chat.id == FAMILY_CHAT_ID:
            await remember_user(user)
        elif chat.type == "private":
            state = _load_state()
            known = str(user.id) in state["known_users"] or user.id in ADMIN_IDS
            if not known:
                await update.effective_message.reply_text(
                    "Мы ещё не знакомы. Напишите любую команду боту в семейном "
                    "чате, а потом возвращайтесь сюда — здесь я присылаю ссылки "
                    "и QR-коды в личку."
                )
                return
        else:
            return  # чужой чат — молча игнорируем
        return await handler(update, context)

    return wrapper


def admin_only(handler):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not user or user.id not in ADMIN_IDS:
            await update.effective_message.reply_text(
                "Эта команда только для администратора."
            )
            return
        return await handler(update, context)

    return wrapper


# --------------------------------------------------------------------------
# Вызов привилегированного helper-скрипта
# --------------------------------------------------------------------------

async def run_helper(*args: str, timeout: float = 90) -> str:
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", HELPER, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("сервер не ответил вовремя (таймаут)")
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip() or "неизвестная ошибка")
    return out.decode(errors="replace")


# --------------------------------------------------------------------------
# Отправка ссылок и QR-кодов
# --------------------------------------------------------------------------

async def fetch_and_send(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    name: str,
    include_text: bool,
    include_qr: bool,
) -> bool:
    try:
        await run_helper("links", name)
    except RuntimeError as e:
        log.exception("links helper failed for %s", name)
        await try_send(
            lambda: context.bot.send_message(user_id, f"Не получилось получить ссылки: {e}"),
            what=f"dm error(links helper) {name}",
        )
        return False

    txt = LINKS_DIR / f"{name}.txt"
    if not txt.exists():
        await try_send(
            lambda: context.bot.send_message(user_id, "Устройство не найдено на сервере."),
            what=f"dm error(missing) {name}",
        )
        return False

    lines = [l for l in txt.read_text(encoding="utf-8").splitlines() if l.strip()]
    labels = [
        "Основной профиль (используйте по умолчанию):",
        "Запасной профиль (если основной не работает):",
    ]

    # Каждое сообщение отправляем через try_send: /add только что перезапустил
    # xray на RU-сервере, а вместе с ним на несколько секунд пропадает и
    # SOCKS-туннель, через который сам бот ходит в Telegram — без ретрая
    # именно эта отправка чаще всего и терялась.
    if include_text:
        if not await try_send(
            lambda: context.bot.send_message(user_id, f"📱 Устройство «{name}»"),
            what=f"dm intro {name}",
        ):
            return False
        for label, link in zip(labels, lines):
            if not await try_send(
                lambda label=label, link=link: context.bot.send_message(user_id, f"{label}\n{link}"),
                what=f"dm link {name}",
            ):
                return False
    if include_qr:
        for suffix, caption in (
            ("main", "QR основного профиля"),
            ("backup", "QR запасного профиля"),
        ):
            png = LINKS_DIR / f"{name}-{suffix}.png"
            if png.exists() and not await try_send(
                lambda png=png, caption=caption: context.bot.send_photo(
                    user_id, photo=png.read_bytes(), caption=caption
                ),
                what=f"dm qr {name}",
            ):
                return False
    return True


async def fetch_and_send_special(
    context: ContextTypes.DEFAULT_TYPE, user_id: int, key: str, title: str, note: str = ""
) -> bool:
    try:
        await run_helper("links")  # без имени — обновляет всё, включая telegram/emergency
    except RuntimeError as e:
        log.exception("links helper (special) failed for %s", key)
        await try_send(
            lambda: context.bot.send_message(user_id, f"Ошибка: {e}"),
            what=f"dm error(special helper) {key}",
        )
        return False

    txt = LINKS_DIR / f"{key}.txt"
    png = LINKS_DIR / f"{key}.png"
    if not txt.exists():
        await try_send(
            lambda: context.bot.send_message(user_id, "Ссылка ещё не создана на сервере."),
            what=f"dm error(missing) {key}",
        )
        return False

    link = txt.read_text(encoding="utf-8").strip()
    msg = f"🔒 {title}\n{link}"
    if note:
        msg += f"\n\n{note}"
    if not await try_send(lambda: context.bot.send_message(user_id, msg), what=f"dm {key}"):
        return False
    if png.exists() and not await try_send(
        lambda: context.bot.send_photo(user_id, photo=png.read_bytes()), what=f"dm qr {key}"
    ):
        return False
    return True


async def resilient(
    coro_factory, *, what: str, attempts: int = 3, delay: float = 3.0, giveup: tuple = ()
):
    """Повторяет вызов Telegram API, который может упасть из-за того, что
    /add или /remove только что перезапустили xray на RU-сервере — вместе с
    ним на несколько секунд пропадает и SOCKS-туннель (TELEGRAM_PROXY), через
    который сам бот ходит в Telegram. coro_factory — функция без аргументов,
    возвращающая новую корутину на каждый вызов (нельзя await-нуть одну и ту
    же корутину дважды). giveup — исключения, которые повторять бессмысленно
    (например Forbidden: человек просто не нажал Start, сеть тут ни при чём)
    — при них отдаём None сразу, без ожидания."""
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except giveup as e:
            log.info("%s: не повторяю (%s): %s", what, type(e).__name__, e)
            return None
        except TelegramError as e:
            log.warning("%s: попытка %d/%d не удалась: %s", what, attempt, attempts, e)
            if attempt < attempts:
                await asyncio.sleep(delay)
    log.error("%s: не удалось после %d попыток", what, attempts)
    return None


async def try_send(coro_factory, *, what: str) -> bool:
    """resilient(), но для отправки конкретному человеку: Forbidden (не нажал
    Start) не повторяем, остальное — до 3 попыток. Возвращает True/False."""
    return await resilient(coro_factory, what=what, giveup=(Forbidden,)) is not None


async def deliver_notice(update: Update, ok: bool, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        return
    text = (
        "Отправил вам в личные сообщения ✅"
        if ok
        else (
            f"Не получилось написать вам в личные сообщения. Откройте чат со мной "
            f"(@{context.bot.username}), нажмите Start и повторите команду здесь."
        )
    )
    await resilient(
        lambda: update.effective_message.reply_text(text), what="deliver_notice"
    )


# --------------------------------------------------------------------------
# Разбор имени устройства в команде
# --------------------------------------------------------------------------

async def resolve_device_name(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> str | None:
    state = _load_state()
    if context.args:
        name = context.args[0].strip().lower()
        device = state["devices"].get(name)
        if not device:
            await update.effective_message.reply_text("Такого устройства нет. Список: /mydevices")
            return None
        if device["owner_id"] != user.id and user.id not in ADMIN_IDS:
            await update.effective_message.reply_text("Это не ваше устройство.")
            return None
        return name

    mine = [d for d, v in state["devices"].items() if v["owner_id"] == user.id]
    if len(mine) == 1:
        return mine[0]
    if not mine:
        await update.effective_message.reply_text("У вас пока нет устройств. Добавьте: /add имя")
    else:
        await update.effective_message.reply_text(
            "У вас несколько устройств, уточните имя: " + ", ".join(sorted(mine))
        )
    return None


# --------------------------------------------------------------------------
# Команды
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = HELP_TEXT
    if user and user.id in ADMIN_IDS:
        text += ADMIN_HELP_TEXT
    await update.effective_message.reply_text(text)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Ваш Telegram ID: {user.id}\nЭтот чат: {chat.id} ({chat.type})"
    )


@restricted
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.effective_message.reply_text(
            "Использование: /add имя_устройства\n"
            "Имя латиницей: буквы, цифры, «-» и «_», например mama-phone"
        )
        return

    name = context.args[0].strip().lower()
    if not NAME_RE.match(name) or name in RESERVED_NAMES:
        await update.effective_message.reply_text(
            "Имя должно быть от 2 до 32 символов: латинские буквы, цифры, «-» и «_».\n"
            "Слова telegram, emergency, main, backup зарезервированы."
        )
        return

    state = _load_state()
    if name in state["devices"]:
        await update.effective_message.reply_text(f"Устройство «{name}» уже существует.")
        return

    owned = [d for d, v in state["devices"].items() if v["owner_id"] == user.id]
    if user.id not in ADMIN_IDS and len(owned) >= MAX_DEVICES_PER_USER:
        await update.effective_message.reply_text(
            f"У вас уже {len(owned)} устройств(а) — это максимум для одного человека. "
            "Удалите старое (/remove) или попросите администратора."
        )
        return

    status_msg = await update.effective_message.reply_text(
        "Добавляю устройство и перезапускаю сервер, подождите..."
    )
    try:
        await run_helper("add", name)
    except RuntimeError as e:
        log.exception("add-device failed for %s", name)
        await resilient(
            lambda: status_msg.edit_text(f"Не получилось добавить устройство: {e}"),
            what="add-fail edit",
        )
        return

    async with _state_lock:
        state = _load_state()
        state["devices"][name] = {
            "owner_id": user.id,
            "owner_name": user.full_name,
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_state(state)

    ok = await fetch_and_send(context, user.id, name, include_text=True, include_qr=True)
    await resilient(
        lambda: status_msg.edit_text(f"✅ Устройство «{name}» добавлено."),
        what="add-success edit",
    )
    await deliver_notice(update, ok, context)


@restricted
async def cmd_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not context.args:
        await update.effective_message.reply_text(
            "Использование: /claim имя_устройства — для устройства, которое уже "
            "существует на сервере (выпущено до появления бота), но ни к кому "
            "в боте не привязано. Список таких устройств видит администратор "
            "командой /devices."
        )
        return

    name = context.args[0].strip().lower()
    state = _load_state()
    existing = state["devices"].get(name)
    if existing:
        if existing["owner_id"] == user.id:
            await update.effective_message.reply_text(
                f"«{name}» уже привязано к вам. Ссылки: /links {name}"
            )
        else:
            await update.effective_message.reply_text(
                "Это устройство уже привязано к другому человеку. Если это ошибка — "
                "обратитесь к администратору."
            )
        return

    try:
        raw = await run_helper("list")
    except RuntimeError as e:
        log.exception("list helper failed for claim %s", name)
        await update.effective_message.reply_text(f"Не удалось проверить сервер: {e}")
        return

    real_names = {tok.split("=", 1)[0] for tok in raw.split() if "=" in tok}
    if name not in real_names:
        await update.effective_message.reply_text(
            "На сервере нет устройства с таким именем. Проверьте имя или спросите "
            "администратора (/devices)."
        )
        return

    async with _state_lock:
        state = _load_state()
        state["devices"][name] = {
            "owner_id": user.id,
            "owner_name": user.full_name,
            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        _save_state(state)

    await update.effective_message.reply_text(
        f"Готово: «{name}» теперь привязано к вам, UUID и ссылки не менялись — "
        f"переподключать устройство не нужно. Получить их: /links {name} или /qr {name}"
    )


@restricted
async def cmd_mydevices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state = _load_state()
    mine = sorted(d for d, v in state["devices"].items() if v["owner_id"] == user.id)
    if not mine:
        await update.effective_message.reply_text("У вас пока нет устройств. Добавьте: /add имя")
        return
    buttons = [
        [
            InlineKeyboardButton(f"🔗 {d}", callback_data=f"links:{d}"),
            InlineKeyboardButton("🗑", callback_data=f"remove:{d}"),
        ]
        for d in mine
    ]
    await update.effective_message.reply_text(
        "Ваши устройства:", reply_markup=InlineKeyboardMarkup(buttons)
    )


@restricted
async def cmd_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = await resolve_device_name(update, context, user)
    if not name:
        return
    ok = await fetch_and_send(context, user.id, name, include_text=True, include_qr=False)
    await deliver_notice(update, ok, context)


@restricted
async def cmd_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = await resolve_device_name(update, context, user)
    if not name:
        return
    ok = await fetch_and_send(context, user.id, name, include_text=False, include_qr=True)
    await deliver_notice(update, ok, context)


@restricted
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    name = await resolve_device_name(update, context, user)
    if not name:
        return
    buttons = [
        [
            InlineKeyboardButton("Да, удалить", callback_data=f"remove_confirm:{name}"),
            InlineKeyboardButton("Отмена", callback_data="cancel"),
        ]
    ]
    await update.effective_message.reply_text(
        f"Удалить «{name}»? Ссылки и QR для него перестанут работать.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@restricted
async def cmd_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ok = await fetch_and_send_special(
        context,
        user.id,
        "telegram",
        "Telegram-прокси",
        "Откройте ссылку на устройстве — Telegram сам предложит подключить прокси.\n"
        "Не пересылайте её посторонним.",
    )
    await deliver_notice(update, ok, context)


@restricted
async def cmd_emergency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ok = await fetch_and_send_special(
        context,
        user.id,
        "emergency",
        "Аварийный профиль",
        "Только если основной сервер недоступен: этот профиль подключается напрямую "
        "к европейскому серверу в обход RU-сервера. Не используйте его постоянно.",
    )
    await deliver_notice(update, ok, context)


@restricted
@admin_only
async def cmd_devices(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = _load_state()
    try:
        raw = await run_helper("list")
    except RuntimeError as e:
        raw = ""
        await update.effective_message.reply_text(f"Не удалось прочитать vpn.env: {e}")

    real_names = {tok.split("=", 1)[0] for tok in raw.split() if "=" in tok}
    lines = []
    for name, meta in sorted(state["devices"].items()):
        mark = "" if name in real_names else " ⚠ нет в vpn.env"
        lines.append(f"• {name} — {meta['owner_name']} ({meta['added_at'][:10]}){mark}")
    for name in sorted(real_names - set(state["devices"])):
        lines.append(f"• {name} — добавлено не через бота, привязать: /claim {name}")

    await update.effective_message.reply_text(
        "Все устройства:\n" + ("\n".join(lines) if lines else "пусто")
    )


@restricted
@admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        raw = await run_helper("status")
    except RuntimeError as e:
        raw = f"Ошибка: {e}"
    await update.effective_message.reply_text(raw)


# --------------------------------------------------------------------------
# Кнопки
# --------------------------------------------------------------------------

@restricted
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    action, _, name = query.data.partition(":")

    if action == "cancel":
        await query.edit_message_text("Отменено.")
        return

    state = _load_state()
    device = state["devices"].get(name)
    if not device:
        await query.edit_message_text("Такого устройства уже нет.")
        return

    is_owner = device["owner_id"] == user.id
    if not (is_owner or user.id in ADMIN_IDS):
        await query.answer("Это не ваше устройство.", show_alert=True)
        return

    if action == "links":
        ok = await fetch_and_send(context, user.id, name, include_text=True, include_qr=True)
        await query.answer(
            "Отправил в личку" if ok else "Не получилось: напишите мне /start в личке и повторите",
            show_alert=not ok,
        )
    elif action == "remove":
        buttons = [
            [
                InlineKeyboardButton("Да, удалить", callback_data=f"remove_confirm:{name}"),
                InlineKeyboardButton("Отмена", callback_data="cancel"),
            ]
        ]
        await query.edit_message_text(
            f"Удалить «{name}»? Ссылки и QR для него перестанут работать.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    elif action == "remove_confirm":
        try:
            await run_helper("remove", name)
        except RuntimeError as e:
            log.exception("remove-device failed for %s", name)
            await resilient(
                lambda: query.edit_message_text(f"Не получилось удалить: {e}"),
                what="remove-fail edit",
            )
            return
        async with _state_lock:
            state = _load_state()
            state["devices"].pop(name, None)
            _save_state(state)
        # run_helper("remove", ...) только что перезапустил xray — SOCKS-туннель
        # бота мог на пару секунд пропасть, поэтому с ретраем.
        await resilient(
            lambda: query.edit_message_text(f"Устройство «{name}» удалено."),
            what="remove-success edit",
        )


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("add", "добавить устройство"),
            BotCommand("claim", "привязать устройство, выпущенное до бота"),
            BotCommand("mydevices", "мои устройства"),
            BotCommand("links", "прислать ссылки ещё раз"),
            BotCommand("qr", "прислать QR-коды"),
            BotCommand("remove", "удалить устройство"),
            BotCommand("telegram", "ссылка на Telegram-прокси"),
            BotCommand("emergency", "аварийный профиль"),
            BotCommand("whoami", "мой Telegram ID"),
            BotCommand("help", "список команд"),
        ]
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Необработанная ошибка", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Что-то пошло не так, я записал ошибку в лог.")
        except Exception:
            pass


def main() -> None:
    if not FAMILY_CHAT_ID:
        log.warning(
            "FAMILY_CHAT_ID не задан — команды будут работать только в личке "
            "с администраторами. Узнать ID чата: добавьте бота в чат и напишите /whoami"
        )

    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)
    if TELEGRAM_PROXY:
        builder = builder.proxy(TELEGRAM_PROXY).get_updates_proxy(TELEGRAM_PROXY)
    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("claim", cmd_claim))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("links", cmd_links))
    app.add_handler(CommandHandler("qr", cmd_qr))
    app.add_handler(CommandHandler("mydevices", cmd_mydevices))
    app.add_handler(CommandHandler("telegram", cmd_telegram))
    app.add_handler(CommandHandler("emergency", cmd_emergency))
    app.add_handler(CommandHandler("devices", cmd_devices))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)

    log.info("vpnbot запущен")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
