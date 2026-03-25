from __future__ import annotations

import ccxt

from telegram_rsi_bot.config import EXCHANGE_TIMEOUT_MS


def _normalize_exchange_id(exchange_id: str) -> str:
    e = exchange_id.lower().strip()
    if e == "okex":
        return "okx"
    return e


def make_exchange(exchange_id: str) -> ccxt.Exchange:
    ex_id = _normalize_exchange_id(exchange_id)
    if not hasattr(ccxt, ex_id):
        raise ValueError(f"Unknown exchange: {exchange_id}")
    params: dict = {
        "enableRateLimit": True,
        "timeout": EXCHANGE_TIMEOUT_MS,
    }
    if ex_id == "okx":
        params["options"] = {"defaultType": "spot"}
    ex = getattr(ccxt, ex_id)(params)
    return ex


def fetch_closes(
    exchange: ccxt.Exchange, market: str, timeframe: str, limit: int
) -> tuple[list[float], list[int]]:
    ohlcv = exchange.fetch_ohlcv(market, timeframe=timeframe, limit=limit)
    if not ohlcv or len(ohlcv) < 4:
        return [], []
    closes = [float(x[4]) for x in ohlcv]
    times = [int(x[0]) for x in ohlcv]
    return closes, times


def check_exchange_reachable(exchange: ccxt.Exchange) -> tuple[bool, str]:
    """
    Лёгкая проверка до первого реального запроса к API (время сервера / здоровье).
    """
    try:
        if hasattr(exchange, "fetch_time"):
            exchange.fetch_time()
        else:
            exchange.load_markets()
        return True, "ok"
    except Exception as e:
        return False, str(e)
