from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Iterable, Sequence

from telegram_rsi_bot.config import DATABASE_PATH


def _ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    _ensure_parent(DATABASE_PATH)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS user_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                level_30 INTEGER NOT NULL DEFAULT 0,
                level_50 INTEGER NOT NULL DEFAULT 0,
                level_70 INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                UNIQUE (user_id, symbol, timeframe)
            );

            CREATE TABLE IF NOT EXISTS signal_dedup (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                bar_time_ms INTEGER NOT NULL,
                signal_code TEXT NOT NULL,
                UNIQUE (symbol, timeframe, bar_time_ms, signal_code)
            );

            CREATE INDEX IF NOT EXISTS idx_user_settings_user ON user_settings(user_id);
            CREATE INDEX IF NOT EXISTS idx_user_settings_sym_tf ON user_settings(symbol, timeframe);
            """
        )


def upsert_user(user_id: int, username: str | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, username, is_active)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username or ""),
        )


def set_user_active(user_id: int, active: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET is_active = ? WHERE user_id = ?",
            (1 if active else 0, user_id),
        )


def deactivate_user(user_id: int) -> None:
    set_user_active(user_id, False)
    with get_conn() as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))


def get_user_active(user_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT is_active FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return False
        return bool(row["is_active"])


def save_settings(
    user_id: int,
    symbols: Iterable[str],
    timeframes: Iterable[str],
    levels: dict[str, bool],
) -> None:
    sym_set = set(symbols)
    tf_set = set(timeframes)
    l30 = 1 if levels.get("30") else 0
    l50 = 1 if levels.get("50") else 0
    l70 = 1 if levels.get("70") else 0
    with get_conn() as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))
        for s in sym_set:
            for tf in tf_set:
                conn.execute(
                    """
                    INSERT INTO user_settings
                    (user_id, symbol, timeframe, level_30, level_50, level_70)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, s, tf, l30, l50, l70),
                )
        conn.execute(
            "UPDATE users SET is_active = 1 WHERE user_id = ?", (user_id,)
        )


def load_settings_rows(user_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return list(
            conn.execute(
                "SELECT symbol, timeframe, level_30, level_50, level_70 "
                "FROM user_settings WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        )


def get_preferred_symbol_timeframe(user_id: int) -> tuple[str, str]:
    """Пара и таймфрейм для запросов RSI / графика (первая сохранённая комбинация)."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT symbol, timeframe FROM user_settings
            WHERE user_id = ?
            ORDER BY symbol, timeframe
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if row is None:
        return "BTCUSDT", "1h"
    return str(row["symbol"]), str(row["timeframe"])


def settings_to_ui_state(rows: Sequence[sqlite3.Row]) -> dict:
    symbols: set[str] = set()
    timeframes: set[str] = set()
    levels = {"30": False, "50": False, "70": False}
    for r in rows:
        symbols.add(r["symbol"])
        timeframes.add(r["timeframe"])
        if r["level_30"]:
            levels["30"] = True
        if r["level_50"]:
            levels["50"] = True
        if r["level_70"]:
            levels["70"] = True
    return {"symbols": symbols, "timeframes": timeframes, "levels": levels}


def format_status(user_id: int) -> str:
    if not get_user_active(user_id):
        return "Рассылка отключена. Используйте /settings чтобы настроить подписку."
    rows = load_settings_rows(user_id)
    if not rows:
        return "Подписка не настроена. Откройте /settings."
    st = settings_to_ui_state(rows)
    sym_labels = sorted(_symbol_label(s) for s in st["symbols"])
    tf_labels = sorted(st["timeframes"])
    lvl = [k for k in ("30", "50", "70") if st["levels"].get(k)]
    lv_s = ", ".join(lvl) if lvl else ""
    if not sym_labels or not tf_labels or not lv_s:
        return "Настройки неполные. Откройте /settings и выберите валюту, таймфрейм и уровни."
    sym_str = ", ".join(sym_labels)
    tf_str = ", ".join(tf_labels)
    return f"Вы подписаны на сигналы: {sym_str} ({tf_str}) по уровням {lv_s}."


def _symbol_label(symbol: str) -> str:
    if symbol == "BTCUSDT":
        return "BTC"
    if symbol == "ETHUSDT":
        return "ETH"
    return symbol


def distinct_symbol_timeframes() -> list[tuple[str, str]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT us.symbol, us.timeframe
            FROM user_settings us
            JOIN users u ON u.user_id = us.user_id
            WHERE u.is_active = 1
            """
        ).fetchall()
    return [(r["symbol"], r["timeframe"]) for r in rows]


def user_ids_for_signal(
    symbol: str, timeframe: str, signal_code: str
) -> list[int]:
    level_col = {
        "30_up": "level_30",
        "70_down": "level_70",
        "50_up": "level_50",
        "50_down": "level_50",
    }.get(signal_code)
    if level_col is None:
        return []
    with get_conn() as conn:
        q = f"""
            SELECT DISTINCT us.user_id
            FROM user_settings us
            JOIN users u ON u.user_id = us.user_id
            WHERE u.is_active = 1
              AND us.symbol = ?
              AND us.timeframe = ?
              AND us.{level_col} = 1
        """
        rows = conn.execute(q, (symbol, timeframe)).fetchall()
    return [int(r["user_id"]) for r in rows]


def try_insert_dedup(
    symbol: str, timeframe: str, bar_time_ms: int, signal_code: str
) -> bool:
    """Returns True if inserted (new), False if duplicate."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO signal_dedup
            (symbol, timeframe, bar_time_ms, signal_code)
            VALUES (?, ?, ?, ?)
            """,
            (symbol, timeframe, bar_time_ms, signal_code),
        )
        return cur.rowcount > 0


def prune_old_dedup(keep_days: int = 14) -> None:
    import time

    cutoff_ms = int((time.time() - keep_days * 86400) * 1000)
    with get_conn() as conn:
        conn.execute("DELETE FROM signal_dedup WHERE bar_time_ms < ?", (cutoff_ms,))
