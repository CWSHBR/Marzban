import importlib.util
from os.path import dirname
from threading import Thread

from telebot import TeleBot, apihelper

from app import app, ha, logger
from config import TELEGRAM_API_TOKEN, TELEGRAM_PROXY_URL

bot = None
if TELEGRAM_API_TOKEN:
    apihelper.proxy = {'http': TELEGRAM_PROXY_URL, 'https': TELEGRAM_PROXY_URL}
    bot = TeleBot(TELEGRAM_API_TOKEN)

handler_names = ["admin", "report", "user"]

# A Thread cannot be restarted, so re-promotion needs a fresh one.
_polling_thread = None


def _load_handlers():
    """Register the bot's command handlers exactly once per process.

    Purely local work with no network calls, and loading twice would register
    every handler twice, so this is deliberately not tied to the role.
    """
    handler_dir = dirname(__file__) + "/handlers/"
    for name in handler_names:
        spec = importlib.util.spec_from_file_location(name, f"{handler_dir}{name}.py")
        spec.loader.exec_module(importlib.util.module_from_spec(spec))

    from app.telegram import utils  # setup custom handlers
    utils.setup()


def start_polling():
    """Leader-only: Telegram allows one getUpdates poller per bot token.

    Two pollers means a hard 409 and updates landing on whichever panel won
    the race, so this follows the lease.
    """
    global _polling_thread

    if not bot or (_polling_thread is not None and _polling_thread.is_alive()):
        return

    logger.info("Starting Telegram bot polling")
    _polling_thread = Thread(target=bot.infinity_polling, daemon=True)
    _polling_thread.start()


def stop_polling():
    global _polling_thread

    if not bot or _polling_thread is None:
        return

    logger.info("Stopping Telegram bot polling")
    try:
        bot.stop_polling()
    except Exception:
        pass
    _polling_thread = None


if bot:
    ha.on_promote(start_polling)
    ha.on_demote(stop_polling)


@app.on_event("startup")
def start_bot():
    if bot:
        _load_handlers()


# Outbound reports are safe on both roles -- they only send messages, they
# never consume updates -- so a user created on the follower still notifies.
from .handlers.report import (  # noqa
    report,
    report_new_user,
    report_user_modification,
    report_user_deletion,
    report_status_change,
    report_user_usage_reset,
    report_user_data_reset_by_next,
    report_user_subscription_revoked,
    report_login
)

__all__ = [
    "bot",
    "report",
    "report_new_user",
    "report_user_modification",
    "report_user_deletion",
    "report_status_change",
    "report_user_usage_reset",
    "report_user_data_reset_by_next",
    "report_user_subscription_revoked",
    "report_login",
    "start_polling",
    "stop_polling",
]
