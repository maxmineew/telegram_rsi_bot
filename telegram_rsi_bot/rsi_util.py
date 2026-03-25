from __future__ import annotations

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator


def compute_rsi(closes: list[float], period: int = 14) -> np.ndarray:
    s = pd.Series(closes, dtype=float)
    rsi = RSIIndicator(close=s, window=period).rsi()
    return rsi.values


def detect_signals(prev_rsi: float, curr_rsi: float) -> list[str]:
    """Crossover between last two *closed* candles. Returns signal_code list."""
    out: list[str] = []
    if np.isnan(prev_rsi) or np.isnan(curr_rsi):
        return out
    # 30: prev below 30, cross up
    if prev_rsi < 30 and curr_rsi > 30:
        out.append("30_up")
    # 70: prev above 70, cross down
    if prev_rsi > 70 and curr_rsi < 70:
        out.append("70_down")
    # 50
    if prev_rsi < 50 and curr_rsi > 50:
        out.append("50_up")
    if prev_rsi > 50 and curr_rsi < 50:
        out.append("50_down")
    return out
