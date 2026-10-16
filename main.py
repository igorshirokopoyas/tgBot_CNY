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

