import os
import json
import logging
import hmac
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
SHARED_SECRET = os.getenv("SHARED_SECRET", "").strip()
RATE_TYPE = os.getenv("RATE_TYPE", "offer").strip().lower()
TARGET_RATE = Decimal(os.getenv("TARGET_RATE", "0").replace(",", "."))
TARGET_DIRECTION = os.getenv("TARGET_DIRECTION", "below").strip().lower()
PORT = int(os.getenv("PORT", "8080"))
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook").strip()
TIMEZONE_LABEL = os.getenv("TIMEZONE_LABEL", "local time")

STATE_FILE = BASE_DIR / "state.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("vtb-telegram-bot")

state = {
    "bid": None, "offer": None, "tracked_rate": None,
    "last_hour": None, "target_triggered": False, "last_updated": None
}

def load_state():
    global state
    try:
        if STATE_FILE.exists():
            saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                state.update(saved)
    except Exception:
        log.exception("Не удалось загрузить state.json")

def save_state():
    try:
        temp = STATE_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(STATE_FILE)
    except Exception:
        log.exception("Не удалось сохранить state.json")

def tracked_rate():
    key = "bid" if RATE_TYPE == "bid" else "offer"
    value = state.get(key)
    return None if value is None else Decimal(str(value))

def fmt(value):
    if value is None:
        return "—"
    return f"{Decimal(str(value)):.4f}"

def rates_message(title="Курс юаня ВТБ"):
    rate = tracked_rate()
    return (
        f"{title}, сумма до 500 000 ₽\n\n"
        f"Покупка ВТБ (bid): {fmt(state.get('bid'))} ₽\n"
        f"Продажа ВТБ (offer): {fmt(state.get('offer'))} ₽\n"
        f"Отслеживаемый курс ({RATE_TYPE}): {fmt(rate)} ₽\n"
        f"Обновлено: {state.get('last_updated') or 'ещё не получено'}"
    )

async def send_to_chat(app, message):
    if CHAT_ID:
        await app.bot.send_message(chat_id=CHAT_ID, text=message)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Бот мониторинга курса юаня ВТБ.\n\n"
        f"ID этого чата: {update.effective_chat.id}\n"
        f"Отслеживаемый курс: {RATE_TYPE.upper()}\n"
        "Тариф: до 500 000 ₽\n\n"
        "/status — последний полученный курс\n"
        "/target 11.50 — задать целевой курс\n"
        "/target 0 — отключить порог\n"
        "/help — помощь"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Команды:\n/start — информация о боте\n/status — последний курс от российского сервера\n"
        "/target 11.50 — задать порог\n/target 0 — отключить порог\n\n"
        "Курс приходит от российского сервера, напрямую ВТБ этот бот не опрашивает."
    )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state.get("bid") is None or state.get("offer") is None:
        await update.effective_message.reply_text(
            "Курс ещё не получен от российского сервера. Проверьте, запущен ли poller.py."
        )
    else:
        await update.effective_message.reply_text(rates_message())

async def target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_RATE
    if not context.args:
        await update.effective_message.reply_text(
            f"Текущий целевой курс: {TARGET_RATE} ₽\n"
            f"Направление: {TARGET_DIRECTION}\nПример: /target 11.50"
        )
        return
    try:
        value = Decimal(context.args[0].replace(",", "."))
        if not value.is_finite() or value < 0:
            raise ValueError
        TARGET_RATE = value
        state["target_triggered"] = False
        save_state()
        await update.effective_message.reply_text(
            "Целевой курс отключён." if value == 0 else f"Целевой курс установлен: {value} ₽"
        )
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Укажите неотрицательное число, например /target 11.50")

async def receive_rates(request: web.Request):
    supplied = request.headers.get("X-Shared-Secret", "")
    if not SHARED_SECRET or not hmac.compare_digest(supplied, SHARED_SECRET):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    try:
        body = await request.json()
        bid = Decimal(str(body["bid"]))
        offer = Decimal(str(body["offer"]))
        updated_at = str(body.get("updated_at") or datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S"))
        if not bid.is_finite() or not offer.is_finite() or bid <= 0 or offer <= 0:
            raise ValueError("bid/offer must be positive")
    except Exception as exc:
        return web.json_response({"ok": False, "error": f"invalid payload: {exc}"}, status=400)

    previous_bid = None if state["bid"] is None else Decimal(str(state["bid"]))
    previous_offer = None if state["offer"] is None else Decimal(str(state["offer"]))
    previous_tracked = tracked_rate()
    first = previous_bid is None or previous_offer is None
    changed = first or bid != previous_bid or offer != previous_offer

    state["bid"] = str(bid)
    state["offer"] = str(offer)
    state["tracked_rate"] = str(bid if RATE_TYPE == "bid" else offer)
    state["last_updated"] = updated_at
    current_tracked = tracked_rate()

    app = request.app["telegram_app"]
    try:
        if first:
            await send_to_chat(app, "🟢 Мониторинг курса ВТБ запущен\n\n" + rates_message())
        elif changed:
            arrow = "🔄"
            if previous_tracked is not None and current_tracked > previous_tracked:
                arrow = "📈"
            elif previous_tracked is not None and current_tracked < previous_tracked:
                arrow = "📉"
            delta = (
                "—"
                if previous_tracked is None
                else f"{current_tracked - previous_tracked:+.4f}"
            )

            message = (
                f"{arrow} Изменение курса юаня ВТБ\n\n"
                f"Покупка: {fmt(bid)} ₽\n"
                f"Продажа: {fmt(offer)} ₽\n"
                f"Отслеживаемый курс ({RATE_TYPE}): {fmt(current_tracked)} ₽\n"
                f"Предыдущий: {fmt(previous_tracked)} ₽\n"
                f"Изменение: {delta} ₽\n"
                f"Время: {updated_at}"
            )
            await send_to_chat(app, message)

        if TARGET_RATE > 0:
            reached = current_tracked >= TARGET_RATE if TARGET_DIRECTION == "above" else current_tracked <= TARGET_RATE
            if reached and not state.get("target_triggered", False):
                await send_to_chat(
                    app,
                    "🚨 ДОСТИГНУТ ЦЕЛЕВОЙ КУРС!\n\n"
                    f"Текущий курс: {fmt(current_tracked)} ₽\n"
                    f"Целевой курс: {fmt(TARGET_RATE)} ₽\n"
                    f"Тип курса: {RATE_TYPE.upper()}\nВремя: {updated_at}"
                )
                state["target_triggered"] = True
            elif not reached and state.get("target_triggered", False):
                state["target_triggered"] = False

        now = datetime.now().astimezone()
        hour_key = now.strftime("%Y-%m-%d %H")
        if state.get("last_hour") != hour_key:
            await send_to_chat(app, "🕐 Часовая сводка — юань ВТБ\n\n" + rates_message())
            state["last_hour"] = hour_key
        save_state()
        return web.json_response({"ok": True, "changed": changed})
    except Exception as exc:
        log.exception("Ошибка обработки курса/отправки Telegram")
        save_state()
        return web.json_response({"ok": False, "error": "telegram_send_failed"}, status=502)

async def health(_request):
    return web.json_response({"ok": True, "has_rate": state.get("bid") is not None})

async def telegram_webhook(request: web.Request):
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret and not hmac.compare_digest(supplied, secret):
        return web.Response(status=403, text="Forbidden")
    update_data = await request.json()
    update = Update.de_json(update_data, request.app["telegram_app"].bot)
    await request.app["telegram_app"].process_update(update)
    return web.Response(text="OK")

async def start_web_server(app):
    web_app = web.Application(client_max_size=64 * 1024)
    web_app["telegram_app"] = app
    web_app.router.add_get("/health", health)
    web_app.router.add_post("/api/rates", receive_rates)
    web_app.router.add_post(WEBHOOK_PATH, telegram_webhook)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    app.bot_data["web_runner"] = runner
    log.info("HTTP endpoints started on port %s", PORT)

async def post_init(app):
    load_state()
    await start_web_server(app)
    public_url = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if public_url:
        if not webhook_secret:
            raise RuntimeError("Если задан PUBLIC_URL, обязательно задайте TELEGRAM_WEBHOOK_SECRET")
        webhook_url = f"{public_url}{WEBHOOK_PATH}"
        await app.bot.set_webhook(
            url=webhook_url,
            secret_token=webhook_secret,
            allowed_updates=["message"],
        )
        log.info("Telegram webhook установлен: %s", webhook_url)
    else:
        log.warning("PUBLIC_URL не задан: HTTP endpoint работает, но Telegram webhook автоматически не установлен.")

async def post_shutdown(app):
    runner = app.bot_data.get("web_runner")
    if runner:
        await runner.cleanup()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    if RATE_TYPE not in ("bid", "offer"):
        raise RuntimeError("RATE_TYPE должен быть bid или offer")
    if TARGET_DIRECTION not in ("below", "above"):
        raise RuntimeError("TARGET_DIRECTION должен быть below или above")

    req = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(req)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("target", target_command))
    log.info("Запуск Python-бота на BotHost")
    # run_webhook здесь не используется: единый aiohttp-сервер обрабатывает
    # и Telegram webhook, и POST с российского сервера.
    app.run_polling() if not os.getenv("PUBLIC_URL") else run_webhook_mode(app)

def run_webhook_mode(app):
    # Telegram updates принимаются нашим aiohttp endpoint; запускаем Application
    # без polling, сохраняя его event loop для aiohttp.
    import asyncio as _asyncio
    async def runner():
        await app.initialize()
        await app.start()
        await post_init(app)
        try:
            await _asyncio.Event().wait()
        finally:
            await post_shutdown(app)
            await app.stop()
            await app.shutdown()
    _asyncio.run(runner())

if __name__ == "__main__":
    main()
