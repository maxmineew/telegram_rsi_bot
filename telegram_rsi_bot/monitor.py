from __future__ import annotations

import asyncio
import logging

import ccxt
from telegram import Bot
from telegram.error import Forbidden, TelegramError

from telegram_rsi_bot import db
from telegram_rsi_bot.config import OHLCV_LIMIT, SYMBOLS, display_timezone
from telegram_rsi_bot.errors_ru import explain_exception
from telegram_rsi_bot.exchange import fetch_closes
from telegram_rsi_bot.rsi_util import compute_rsi, detect_signals

log = logging.getLogger(__name__)

SIGNAL_LABELS: dict[str, tuple[str, str]] = {
    "30_up": ("Пересечение 30 вверх (Покупка / выход из перепроданности)", "Покупка"),
    "70_down": ("Пересечение 70 вниз (Продажа / выход из перекупленности)", "Продажа"),
    "50_up": ("Пересечение 50 вверх (бычий)", "Бычий"),
    "50_down": ("Пересечение 50 вниз (медвежий)", "Медвежий"),
}


def _tf_ru(tf: str) -> str:
    return {"1h": "1ч", "4h": "4ч", "1d": "1д"}.get(tf, tf)


def _symbol_display(sym: str) -> str:
    return SYMBOLS.get(sym, sym.replace("USDT", "/USDT"))


def format_signal_message(
    symbol: str, timeframe: str, signal_code: str, rsi_value: float, bar_time_ms: int
) -> str:
    action, _ = SIGNAL_LABELS[signal_code]
    from datetime import datetime

    dt = datetime.fromtimestamp(bar_time_ms / 1000, tz=display_timezone())
    t_str = dt.strftime("%Y-%m-%d %H:%M МСК")
    pair = _symbol_display(symbol)
    return (
        f"🚨 *Сигнал RSI*\n"
        f"Пара: {pair}\n"
        f"Таймфрейм: {_tf_ru(timeframe)}\n"
        f"Действие: {action}\n"
        f"Текущий RSI: {rsi_value:.1f}\n"
        f"Время: {t_str}"
    )


def analyze_pair_tf(
    exchange: ccxt.Exchange, symbol: str, timeframe: str, ohlcv_limit: int | None = None
) -> list[tuple[str, float, int]]:
    limit = ohlcv_limit if ohlcv_limit is not None else OHLCV_LIMIT
    market = SYMBOLS.get(symbol)
    if not market:
        return []
    closes, times = fetch_closes(exchange, market, timeframe, limit)
    if len(closes) < 4:
        return []
    rsi = compute_rsi(closes)
    prev_rsi = float(rsi[-3])
    curr_rsi = float(rsi[-2])
    bar_time_ms = times[-2]
    codes = detect_signals(prev_rsi, curr_rsi)
    return [(c, curr_rsi, bar_time_ms) for c in codes]


async def send_to_subscribers(
    bot: Bot,
    symbol: str,
    timeframe: str,
    signal_code: str,
    rsi_value: float,
    bar_time_ms: int,
) -> None:
    if not db.try_insert_dedup(symbol, timeframe, bar_time_ms, signal_code):
        return
    user_ids = db.user_ids_for_signal(symbol, timeframe, signal_code)
    text = format_signal_message(symbol, timeframe, signal_code, rsi_value, bar_time_ms)
    for uid in user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode="Markdown",
            )
        except Forbidden:
            log.warning("User %s blocked bot, deactivating", uid)
            db.deactivate_user(uid)
        except TelegramError as e:
            log.warning("Telegram error for %s: %s", uid, e)


async def run_monitor_cycle_async(
    bot: Bot, exchange: ccxt.Exchange, ohlcv_limit: int
) -> None:
    pairs = db.distinct_symbol_timeframes()
    if not pairs:
        return
    for symbol, tf in pairs:
        try:
            events = await asyncio.to_thread(
                analyze_pair_tf, exchange, symbol, tf, ohlcv_limit
            )
        except ccxt.BaseError as e:
            log.warning(
                "Биржа · %s %s: %s",
                symbol,
                tf,
                explain_exception(e),
            )
            continue
        except Exception:
            log.exception("fetch/analyze %s %s", symbol, tf)
            continue
        for signal_code, rsi_val, bar_ms in events:
            await send_to_subscribers(
                bot, symbol, tf, signal_code, rsi_val, bar_ms
            )
