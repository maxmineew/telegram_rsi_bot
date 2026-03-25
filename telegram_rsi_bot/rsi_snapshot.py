"""Снимки RSI для команд /rsi и /check (не путать с логикой сигналов в monitor)."""

from __future__ import annotations

import numpy as np

from telegram_rsi_bot.config import OHLCV_LIMIT, SYMBOLS
from telegram_rsi_bot.exchange import fetch_closes
from telegram_rsi_bot.rsi_util import compute_rsi, detect_signals


def _finite_at(rsi: np.ndarray, idx: int) -> float | None:
    i = idx
    while i >= 0:
        v = float(rsi[i])
        if not np.isnan(v):
            return v
        i -= 1
    return None


def build_snapshot(
    exchange, symbol: str, timeframe: str, ohlcv_limit: int | None = None
) -> dict | None:
    """
    · rsi_current — по последнему бару (текущая открытая свеча).
    · rsi_last_closed — по предпоследнему бару (последняя закрытая).
    · rsi_before_closed — бар [-3] для сравнения с закрытой.
    """
    limit = ohlcv_limit if ohlcv_limit is not None else OHLCV_LIMIT
    market = SYMBOLS.get(symbol)
    if not market:
        return None
    closes, times = fetch_closes(exchange, market, timeframe, limit)
    if len(closes) < 4:
        return None
    rsi = compute_rsi(closes)
    rsi_current = _finite_at(rsi, len(rsi) - 1)
    rsi_last_closed = _finite_at(rsi, len(rsi) - 2)
    rsi_before_closed = _finite_at(rsi, len(rsi) - 3)
    codes: list[str] = []
    if rsi_before_closed is not None and rsi_last_closed is not None:
        codes = detect_signals(rsi_before_closed, rsi_last_closed)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "closes": closes,
        "times": times,
        "rsi": rsi,
        "rsi_current": rsi_current,
        "rsi_last_closed": rsi_last_closed,
        "rsi_before_closed": rsi_before_closed,
        "time_open_current_ms": times[-1],
        "time_open_last_closed_ms": times[-2],
        "crossover_codes_last_closed": codes,
    }
