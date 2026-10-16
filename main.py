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
# НАСТРОЙКИ
# =========================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

# URL API ноды BotHost, которая получает курс
RATES_API_URL = os.getenv("RATES_API_URL", "").strip()

# Один и тот же секрет должен быть настроен на обеих сторонах
SHARED_SECRET = os.getenv("SHARED_SECRET", "").strip()

RATE_TYPE = os.getenv("RATE_TYPE", "offer").strip().lower()
TARGET_RATE = Decimal(
    os.getenv("TARGET_RATE", "0").replace(",", ".")
)
TARGET_DIRECTION = os.getenv("TARGET_DIRECTION", "below").strip().lower()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))
MAX_AMOUNT = Decimal("500000.00")

STATE_FILE = Path(
    os.getenv("DATA_DIR", str(BASE_DIR))
) / "state.json"

STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({"Accept": "application/json"})

if SHARED_SECRET:
    session.headers.update({"X-Shared-Secret": SHARED_SECRET})

state = {
    "rate": None,
    "bid": None,
    "offer": None,
    "last_hour": None,
    "last_updated": None,
    "previous_rate": None,
    "last_delta": None,
    "last_arrow": None,
    "target_triggered": False,
}


# =========================================================
# СОХРАНЕНИЕ СОСТОЯНИЯ
# =========================================================

def load_state():
    if not STATE_FILE.exists():
        return

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)

        if isinstance(saved, dict):
            state.update(saved)

    except Exception:
        log.exception("Не удалось загрузить state.json")


def save_state():
    temp_file = STATE_FILE.with_suffix(".tmp")

    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    temp_file.replace(STATE_FILE)


# =========================================================
# ПОЛУЧЕНИЕ КУРСА ИЗ НОДЫ BOTHOST
# =========================================================

def get_yuan_rates():
    if not RATES_API_URL:
        raise RuntimeError(
            "Не задан RATES_API_URL — укажи адрес API ноды BotHost"
        )

    response = session.get(RATES_API_URL, timeout=(10, 20))
    response.raise_for_status()
    data = response.json()

    # Поддерживаем как простой JSON, так и объект {"rates": {...}}
    rates = data.get("rates", data)

    try:
        bid = Decimal(str(rates["bid"]))
        offer = Decimal(str(rates["offer"]))
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ValueError(
            f"API ноды вернул неожиданный формат: {data}"
        ) from exc

    if (
        not bid.is_finite()
        or not offer.is_finite()
        or bid <= 0
        or offer <= 0
    ):
        raise ValueError("API ноды вернул некорректный курс")

    return {"bid": bid, "offer": offer}


def get_tracked_rate(rates):
    return rates["bid"] if RATE_TYPE == "bid" else rates["offer"]


# =========================================================
# TELEGRAM
# =========================================================

async def send_message(app, message):
    await app.bot.send_message(chat_id=CHAT_ID, text=message)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Бот мониторинга курса юаня.\n\n"
        f"ID этого чата: {update.effective_chat.id}\n"
        f"Отслеживаемый курс: {RATE_TYPE.upper()}\n"
        "Тариф: до 500 000 ₽\n\n"
        "/status — последний курс и изменение\n"
        "/target 12.50 — задать целевой курс\n"
        "/target 0 — отключить целевой курс\n"
        "/help — помощь"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Команды:\n"
        "/start — информация о боте\n"
        "/status — последний полученный курс\n"
        "/target 12.50 — уведомлять при достижении порога\n"
        "/target 0 — отключить порог\n\n"
        f"Интервал проверки: {CHECK_INTERVAL} секунд."
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if state.get("rate") is None:
        await update.effective_message.reply_text(
            "Курс пока не получен. Дождись первого успешного ответа ноды."
        )
        return

    bid = Decimal(str(state["bid"]))
    offer = Decimal(str(state["offer"]))
    rate = Decimal(str(state["rate"]))

    previous = state.get("previous_rate")
    delta = state.get("last_delta")
    arrow = state.get("last_arrow") or "🔄"
    updated_at = state.get("last_updated") or "неизвестно"

    if previous is None or delta is None:
        previous_text = "—"
        delta_text = "—"
        heading = "ℹ️ Текущий курс"
    else:
        previous_text = f"{Decimal(str(previous)):.4f} ₽"
        delta_text = f"{Decimal(str(delta)):+.4f} ₽"
        heading = f"{arrow} Последнее изменение курса"

    await update.effective_message.reply_text(
        f"{heading}\n\n"
        f"Покупка: {bid:.4f} ₽\n"
        f"Продажа: {offer:.4f} ₽\n"
        f"Отслеживаемый курс ({RATE_TYPE}): {rate:.4f} ₽\n"
        f"Предыдущий: {previous_text}\n"
        f"Изменение: {delta_text}\n"
        f"Время: {updated_at}"
    )


async def target_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TARGET_RATE

    if not context.args:
        await update.effective_message.reply_text(
            f"Текущий целевой курс: {TARGET_RATE} ₽\n"
            f"Направление: {TARGET_DIRECTION}\n"
            "Пример: /target 12.50"
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
            f"Целевой курс установлен: {TARGET_RATE} ₽"
        )

    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text(
            "Укажи корректное число, например /target 12.50"
        )


# =========================================================
# МОНИТОРИНГ
# =========================================================

async def monitor(app):
    load_state()

    while True:
        try:
            rates = await asyncio.to_thread(get_yuan_rates)
            rate = get_tracked_rate(rates)
            now = datetime.now().astimezone()
            timestamp = now.strftime("%d.%m.%Y %H:%M:%S")

            previous = state.get("rate")
            previous_bid = state.get("bid")
            previous_offer = state.get("offer")

            if previous is None:
                state["rate"] = str(rate)
                state["bid"] = str(rates["bid"])
                state["offer"] = str(rates["offer"])
                state["last_updated"] = timestamp
                save_state()

                await send_message(
                    app,
                    "🟢 Мониторинг курса запущен\n\n"
                    f"Покупка: {rates['bid']:.4f} ₽\n"
                    f"Продажа: {rates['offer']:.4f} ₽\n"
                    f"Отслеживаемый курс ({RATE_TYPE}): {rate:.4f} ₽\n"
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

                    delta = rate - previous_rate

                    # Сохраняем данные последнего изменения для /status
                    state["previous_rate"] = str(previous_rate)
                    state["last_delta"] = str(delta)
                    state["last_arrow"] = arrow
                    state["last_updated"] = timestamp

                    await send_message(
                        app,
                        f"{arrow} Изменение курса юаня\n\n"
                        f"Покупка: {rates['bid']:.4f} ₽\n"
                        f"Продажа: {rates['offer']:.4f} ₽\n"
                        f"Отслеживаемый курс ({RATE_TYPE}): {rate:.4f} ₽\n"
                        f"Предыдущий: {previous_rate:.4f} ₽\n"
                        f"Изменение: {delta:+.4f} ₽\n"
                        f"Время: {timestamp}"
                    )

                state["rate"] = str(rate)
                state["bid"] = str(rates["bid"])
                state["offer"] = str(rates["offer"])
                state["last_updated"] = timestamp
                save_state()

            # Уведомление о достижении целевого курса
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
                        f"Время: {timestamp}"
                    )
                    state["target_triggered"] = True
                    save_state()

                elif not reached and state.get("target_triggered", False):
                    state["target_triggered"] = False
                    save_state()

            # Часовая сводка
            hour_key = now.strftime("%Y-%m-%d %H")

            if state.get("last_hour") != hour_key:
                await send_message(
                    app,
                    "🕐 Часовая сводка — курс юаня\n\n"
                    f"Покупка: {rates['bid']:.4f} ₽\n"
                    f"Продажа: {rates['offer']:.4f} ₽\n"
                    f"Отслеживаемый курс ({RATE_TYPE}): {rate:.4f} ₽\n"
                    f"Тариф: до {MAX_AMOUNT:.0f} ₽\n"
                    f"Время: {timestamp}"
                )

                state["last_hour"] = hour_key
                save_state()

        except Exception:
            log.exception("Ошибка получения курса или отправки сообщения")

        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(app):
    app.create_task(monitor(app))


# =========================================================
# ЗАПУСК
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    if not CHAT_ID:
        raise RuntimeError("Не задан CHAT_ID")

    if not RATES_API_URL:
        raise RuntimeError("Не задан RATES_API_URL")

    if RATE_TYPE not in ("bid", "offer"):
        raise RuntimeError("RATE_TYPE должен быть bid или offer")

    if TARGET_DIRECTION not in ("below", "above"):
        raise RuntimeError("TARGET_DIRECTION должен быть below или above")

    telegram_request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )

    updates_request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=40,
        write_timeout=30,
        pool_timeout=30,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(telegram_request)
        .get_updates_request(updates_request)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("target", target_command))

    log.info("Запуск Telegram-бота")
    application.run_polling()


if __name__ == "__main__":
    main()