import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

# =========================================================
# CONFIGURATION
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# GET endpoint of the BotHost node that provides the VTB rates.
# Set this in .env; do not put the VTB website URL here.
RATES_API_URL = os.getenv("RATES_API_URL", "").strip()

# Must match the secret configured on the BotHost rates node.
SHARED_SECRET = os.getenv("SHARED_SECRET", "").strip()

# "offer" = the bank sells CNY; "bid" = the bank buys CNY.
RATE_TYPE = os.getenv("RATE_TYPE", "offer").strip().lower()

# TARGET_RATE=0 disables threshold alerts.
TARGET_RATE = Decimal(os.getenv("TARGET_RATE", "0").replace(",", "."))
TARGET_DIRECTION = os.getenv("TARGET_DIRECTION", "below").strip().lower()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))
MAX_AMOUNT = Decimal("500000.00")

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# =========================================================
# HTTP SESSION FOR BOTHOST RATES NODE
# =========================================================

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "SVN-Yuan-Rate-Monitor/1.0",
})
if SHARED_SECRET:
    session.headers.update({"X-Shared-Secret": SHARED_SECRET})

state = {
    "rate": None,
    "bid": None,
    "offer": None,
    "last_hour": None,
    "target_triggered": False,
}


# =========================================================
# STATE
# =========================================================

def load_state():
    if not STATE_FILE.exists():
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            saved = json.load(file)
        if isinstance(saved, dict):
            state.update(saved)
    except Exception:
        log.exception("Could not read state.json; starting with fresh state")


def save_state():
    temp_file = STATE_FILE.with_suffix(".tmp")
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    temp_file.replace(STATE_FILE)


# =========================================================
# GET RATES FROM THE BOTOHOST NODE
# =========================================================

def get_yuan_rates():
    if not RATES_API_URL:
        raise RuntimeError(
            "RATES_API_URL is not set. Add the BotHost rates-node URL to .env"
        )

    response = session.get(RATES_API_URL, timeout=(10, 20))
    response.raise_for_status()
    data = response.json()

    # Supports both {"bid": ..., "offer": ...}
    # and {"rates": {"bid": ..., "offer": ...}}.
    if not isinstance(data, dict):
        raise ValueError(f"BotHost node returned a non-object JSON response: {data!r}")

    rates = data.get("rates", data)
    if not isinstance(rates, dict):
        raise ValueError(f"Unexpected rates field from BotHost node: {data!r}")

    try:
        bid = Decimal(str(rates["bid"]))
        offer = Decimal(str(rates["offer"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError(
            "BotHost node response must contain numeric 'bid' and 'offer' fields. "
            f"Received: {data!r}"
        ) from exc

    if (
        not bid.is_finite()
        or not offer.is_finite()
        or bid <= 0
        or offer <= 0
    ):
        raise ValueError("BotHost node returned an invalid bid/offer rate")

    log.info("Received CNY/RUB rates from BotHost node: bid=%s, offer=%s", bid, offer)
    return {"bid": bid, "offer": offer}


def get_tracked_rate(rates):
    return rates["bid"] if RATE_TYPE == "bid" else rates["offer"]


# =========================================================
# TELEGRAM HELPERS / COMMANDS
# =========================================================

async def send_message(app, message):
    await app.bot.send_message(chat_id=CHAT_ID, text=message)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Бот мониторинга курса юаня ВТБ.\n\n"
        f"ID этого чата: {update.effective_chat.id}\n"
        f"Отслеживаемый курс: {RATE_TYPE.upper()}\n"
        "Тариф: до 500 000 ₽\n\n"
        "/status — текущий курс\n"
        "/target 11.50 — задать целевой курс\n"
        "/target 0 — отключить целевой курс\n"
        "/help — помощь"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Команды:\n"
        "/start — информация о боте\n"
        "/status — текущие курсы ВТБ\n"
        "/target 11.50 — уведомлять при достижении порога\n"
        "/target 0 — отключить порог\n\n"
        "Проверка курса выполняется каждые "
        f"{CHECK_INTERVAL} секунд."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # As in code 1, /status requests the latest rate rather than only showing
    # the cached value. The request is made to the BotHost node.
    try:
        rates = await asyncio.to_thread(get_yuan_rates)
        await update.effective_message.reply_text(
            "Курс юаня ВТБ, сумма до 500 000 ₽\n\n"
            f"Покупка ВТБ (bid): {rates['bid']:.4f} ₽\n"
            f"Продажа ВТБ (offer): {rates['offer']:.4f} ₽\n"
            f"Отслеживаемый курс ({RATE_TYPE}): "
            f"{get_tracked_rate(rates):.4f} ₽"
        )
    except Exception as exc:
        log.exception("Could not fetch rate from BotHost node for /status")
        await update.effective_message.reply_text(
            f"Не удалось получить курс: {exc}"
        )


async def target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_RATE

    if not context.args:
        await update.effective_message.reply_text(
            f"Текущий целевой курс: {TARGET_RATE} ₽\n"
            f"Направление: {TARGET_DIRECTION}\n"
            "Пример: /target 11.50"
        )
        return

    try:
        value = Decimal(context.args[0].replace(",", "."))
        if not value.is_finite() or value < 0:
            raise ValueError("Target must be a non-negative number")

        TARGET_RATE = value
        state["target_triggered"] = False
        save_state()

        await update.effective_message.reply_text(
            f"Целевой курс установлен: {TARGET_RATE} ₽"
        )
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(
            "Укажите число, например /target 11.50"
        )


# =========================================================
# MONITORING LOOP — MESSAGE LOGIC FROM CODE 1
# =========================================================

async def monitor(app):
    load_state()

    while True:
        try:
            rates = await asyncio.to_thread(get_yuan_rates)
            rate = get_tracked_rate(rates)
            now = datetime.now().astimezone()

            previous = state.get("rate")
            previous_bid = state.get("bid")
            previous_offer = state.get("offer")

            if previous is None:
                state["rate"] = str(rate)
                state["bid"] = str(rates["bid"])
                state["offer"] = str(rates["offer"])
                save_state()

                await send_message(
                    app,
                    "🟢 Мониторинг курса ВТБ запущен\n\n"
                    f"Покупка: {rates['bid']:.4f} ₽\n"
                    f"Продажа: {rates['offer']:.4f} ₽\n"
                    f"Отслеживаемый курс: {rate:.4f} ₽\n"
                    "Тариф: до 500 000 ₽"
                )
            else:
                previous_rate = Decimal(str(previous))
                bid_changed = (
                    previous_bid is not None
                    and rates["bid"] != Decimal(str(previous_bid))
                )
                offer_changed = (
                    previous_offer is not None
                    and rates["offer"] != Decimal(str(previous_offer))
                )

                if rate != previous_rate or bid_changed or offer_changed:
                    if rate > previous_rate:
                        arrow = "📈"
                    elif rate < previous_rate:
                        arrow = "📉"
                    else:
                        arrow = "🔄"

                    await send_message(
                        app,
                        f"{arrow} Изменение курса юаня ВТБ\n\n"
                        f"Покупка: {rates['bid']:.4f} ₽\n"
                        f"Продажа: {rates['offer']:.4f} ₽\n"
                        f"Отслеживаемый курс: {rate:.4f} ₽\n"
                        f"Предыдущий отслеживаемый курс: {previous_rate:.4f} ₽\n"
                        f"Изменение: {rate - previous_rate:+.4f} ₽\n"
                        f"Время: {now:%d.%m.%Y %H:%M:%S}"
                    )

                state["rate"] = str(rate)
                state["bid"] = str(rates["bid"])
                state["offer"] = str(rates["offer"])
                save_state()

            # Target threshold alert
            if TARGET_RATE > 0:
                if TARGET_DIRECTION == "above":
                    reached = rate >= TARGET_RATE
                else:
                    reached = rate <= TARGET_RATE

                if reached and not state.get("target_triggered", False):
                    await send_message(
                        app,
                        "🚨 ДОСТИГНУТ ЦЕЛЕВОЙ КУРС!\n\n"
                        f"Текущий курс: {rate:.4f} ₽\n"
                        f"Целевой курс: {TARGET_RATE:.4f} ₽\n"
                        f"Тип курса: {RATE_TYPE.upper()}\n"
                        f"Время: {now:%d.%m.%Y %H:%M:%S}"
                    )
                    state["target_triggered"] = True
                    save_state()
                elif not reached and state.get("target_triggered", False):
                    state["target_triggered"] = False
                    save_state()

            # One hourly message per local hour
            hour_key = now.strftime("%Y-%m-%d %H")
            if state.get("last_hour") != hour_key:
                await send_message(
                    app,
                    "🕐 Часовая сводка — юань ВТБ\n\n"
                    f"Покупка: {rates['bid']:.4f} ₽\n"
                    f"Продажа: {rates['offer']:.4f} ₽\n"
                    f"Отслеживаемый курс: {rate:.4f} ₽\n"
                    f"Тариф: до {MAX_AMOUNT:.0f} ₽\n"
                    f"Время: {now:%d.%m.%Y %H:%M:%S}"
                )
                state["last_hour"] = hour_key
                save_state()

        except Exception:
            log.exception("Monitoring error: could not get rate or send Telegram message")

        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app):
    app.create_task(monitor(app))


# =========================================================
# STARTUP
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            f"BOT_TOKEN is missing. Check environment variables or {BASE_DIR / '.env'}"
        )

    if not CHAT_ID:
        raise RuntimeError(
            f"CHAT_ID is missing. Check environment variables or {BASE_DIR / '.env'}"
        )

    if not RATES_API_URL:
        raise RuntimeError(
            f"RATES_API_URL is missing. Check environment variables or {BASE_DIR / '.env'}"
        )

    if RATE_TYPE not in ("bid", "offer"):
        raise RuntimeError("RATE_TYPE must be 'bid' or 'offer'")

    if TARGET_DIRECTION not in ("below", "above"):
        raise RuntimeError("TARGET_DIRECTION must be 'below' or 'above'")

    if CHECK_INTERVAL < 1:
        raise RuntimeError("CHECK_INTERVAL must be at least 1 second")

    telegram_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )
    telegram_updates_request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=40,
        write_timeout=30,
        pool_timeout=30,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(telegram_request)
        .get_updates_request(telegram_updates_request)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("target", target_command))

    log.info("Starting Telegram bot")
    application.run_polling()


if __name__ == "__main__":
    main()
