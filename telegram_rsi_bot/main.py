from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict
from telegram.ext import Application, ContextTypes

from telegram_rsi_bot import config, db
from telegram_rsi_bot.errors_ru import telegram_user_hint
from telegram_rsi_bot.exchange import check_exchange_reachable, make_exchange
from telegram_rsi_bot.handlers import register
from telegram_rsi_bot.monitor import run_monitor_cycle_async

def _configure_logging() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    # Наш код — явно INFO (на случай смены уровня root в другом месте).
    logging.getLogger("telegram_rsi_bot").setLevel(logging.INFO)
    # httpx по умолчанию пишет INFO с полным URL (в нём токен Bot API).
    # Уровень WARNING: успешные запросы не спамят; предупреждения и ошибки HTTP видны.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger(__name__)


async def _monitor_job(application: Application) -> None:
    ex = application.bot_data.get("exchange")
    if ex is None:
        return
    try:
        await run_monitor_cycle_async(
            application.bot,
            ex,
            config.OHLCV_LIMIT,
        )
        db.prune_old_dedup()
    except Exception:
        log.exception(
            "Цикл мониторинга завершился с ошибкой (сеть/биржа/Telegram). "
            "Следующая попытка по расписанию."
        )


async def post_init(application: Application) -> None:
    db.init_db()
    exchange = make_exchange(config.EXCHANGE_ID)
    application.bot_data["exchange"] = exchange

    ok, err_detail = await asyncio.to_thread(check_exchange_reachable, exchange)
    if ok:
        log.info(
            "Подключение к бирже «%s» проверено: связь установлена, мониторинг будет по расписанию.",
            exchange.id,
        )
    else:
        log.warning(
            "Первичная проверка биржи «%s» не удалась: %s. "
            "Бот всё равно запущен — при восстановлении сети запросы возобновятся автоматически.",
            config.EXCHANGE_ID,
            err_detail,
        )

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        _monitor_job,
        "interval",
        seconds=config.MONITOR_INTERVAL_SEC,
        args=(application,),
        id="rsi_monitor",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    application.bot_data["scheduler"] = scheduler
    log.info(
        "Планировщик запущен: опрос каждые %s с, биржа=%s.",
        config.MONITOR_INTERVAL_SEC,
        config.EXCHANGE_ID,
    )


async def post_shutdown(application: Application) -> None:
    sch = application.bot_data.get("scheduler")
    if sch:
        sch.shutdown(wait=False)
    ex = application.bot_data.get("exchange")
    if ex is not None:
        close = getattr(ex, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                log.exception("Закрытие соединения с биржей")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if err is None:
        return
    log.error("Ошибка при обработке апдейта: %s", err, exc_info=err)
    if isinstance(err, Conflict):
        log.error(
            "Конфликт Telegram: с одним токеном запущено несколько процессов. "
            "Остановите лишние копии бота на сервере (systemctl stop / второй SSH).",
        )
        return
    if isinstance(err, BadRequest):
        low = str(err).lower()
        if "message is not modified" in low or "not modified" in low:
            log.debug("Игнорируем BadRequest: сообщение не изменилось. %s", err)
            return
    hint = telegram_user_hint(err)
    chat_id = None
    if update is not None:
        ec = getattr(update, "effective_chat", None)
        if ec is not None:
            chat_id = ec.id
        else:
            u = getattr(update, "effective_user", None)
            if u is not None:
                chat_id = u.id
    if chat_id is None:
        log.warning("Не удалось отправить подсказку: нет chat_id. %s", hint)
        return
    try:
        await context.application.bot.send_message(
            chat_id=chat_id,
            text=f"<b>⚠️ Что-то пошло не так</b>\n\n{hint}\n\nПопробуйте /start.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as send_exc:
        log.warning(
            "Не удалось отправить сообщение об ошибке пользователю: %s. Подсказка: %s",
            send_exc,
            hint,
        )


def _build_application() -> Application:
    b = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if hasattr(b, "connect_timeout"):
        b = b.connect_timeout(config.TELEGRAM_CONNECT_TIMEOUT).read_timeout(
            config.TELEGRAM_READ_TIMEOUT
        )
    p = config.TELEGRAM_PROXY_URL
    if p:
        safe = p.split("@")[-1] if "@" in p else p
        log.info("Прокси для Telegram Bot API: %s", safe)
        if hasattr(b, "proxy"):
            b = b.proxy(p)
        elif hasattr(b, "proxy_url"):
            b = b.proxy_url(p)
        else:
            log.warning(
                "TELEGRAM_PROXY_URL задан, но эта версия PTB не поддерживает proxy(); "
                "задайте HTTP_PROXY/HTTPS_PROXY в окружении или обновите python-telegram-bot.",
            )
        if hasattr(b, "get_updates_proxy"):
            b = b.get_updates_proxy(p)
    return b.build()


def main() -> None:
    config.validate()
    log.info(
        "Запуск RSI Monitor… Токен задан, биржа по умолчанию: %s. "
        "Ожидание соединения с Telegram…",
        config.EXCHANGE_ID,
    )
    application = _build_application()
    register(application)
    application.add_error_handler(on_error)
    log.info("Бот в режиме polling — интерфейс и мониторинг активны.")
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
