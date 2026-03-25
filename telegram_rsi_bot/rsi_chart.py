"""PNG-график RSI для команды /chart."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from telegram_rsi_bot.config import SYMBOLS


def _tf_title(tf: str) -> str:
    return {"1h": "1ч", "4h": "4ч", "1d": "1д"}.get(tf, tf)


def render_rsi_chart_png(
    times_ms: list[int],
    rsi: np.ndarray,
    symbol: str,
    timeframe: str,
    max_bars: int = 80,
) -> bytes:
    """Строит последние max_bаров RSI с уровнями 30/50/70."""
    n = min(len(times_ms), len(rsi), max_bars)
    t = times_ms[-n:]
    r = rsi[-n:]
    xs = list(range(n))
    labels = []
    for ms in t:
        dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        labels.append(dt.strftime("%d.%m %H:%M"))

    pair = SYMBOLS.get(symbol, symbol)
    fig, ax = plt.subplots(figsize=(10, 4), dpi=120)
    r_plot = np.ma.masked_invalid(np.asarray(r, dtype=float))
    ax.plot(xs, r_plot, color="#2563eb", linewidth=1.2, label="RSI(14)")
    for lvl, color in ((30, "#16a34a"), (50, "#737373"), (70, "#dc2626")):
        ax.axhline(lvl, color=color, linewidth=0.8, linestyle="--", alpha=0.85)

    ax.set_ylim(0, 100)
    ax.set_xlim(0, max(n - 1, 1))
    step = max(1, n // 8)
    ax.set_xticks(xs[::step])
    ax.set_xticklabels([labels[i] for i in range(0, n, step)], rotation=35, ha="right", fontsize=7)
    ax.set_ylabel("RSI")
    ax.set_title(f"{pair} · {_tf_title(timeframe)}")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
