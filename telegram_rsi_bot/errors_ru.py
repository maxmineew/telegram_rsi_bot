"""Короткие пояснения к ошибкам для логов и (при необходимости) пользователя."""

from __future__ import annotations

import html
import sqlite3

import ccxt


def explain_exception(exc: BaseException) -> str:
    # Подклассы NetworkError проверять раньше родителя
    if isinstance(exc, ccxt.RequestTimeout):
        return (
            "Таймаут запроса к бирже: соединение слишком медленное или сервер перегружен. "
            "Повторим попытку позже."
        )
    if isinstance(exc, ccxt.ExchangeNotAvailable):
        return (
            "Биржа временно недоступна (техработы или перегруз). Подождите и проверьте статус OKX."
        )
    ddos = getattr(ccxt, "DDoSProtection", None)
    if ddos is not None and isinstance(exc, ddos):
        return (
            "Сработала защита от частых запросов (rate limit). Увеличьте интервал опроса в настройках."
        )
    if isinstance(exc, ccxt.NetworkError):
        return (
            "Нет сети или биржа не отвечает. Проверьте интернет, VPN/файрвол и "
            "статус биржи; запрос будет повторён при следующем цикле."
        )
    if isinstance(exc, ccxt.ExchangeError):
        return f"Ответ биржи с ошибкой: {exc}. Проверьте символы и тип рынка (spot)."
    if isinstance(exc, ccxt.BaseError):
        return f"Ошибка ccxt: {exc}"
    return f"{type(exc).__name__}: {exc}"


def telegram_user_hint(exc: BaseException) -> str:
    from telegram.error import (
        BadRequest,
        Conflict,
        Forbidden,
        InvalidToken,
        NetworkError,
        RetryAfter,
        TelegramError,
        TimedOut,
    )

    def _short_exc(e: BaseException, n: int = 180) -> str:
        s = str(e).strip() or repr(e)
        return html.escape(s if len(s) <= n else s[: n - 1] + "…")

    if isinstance(exc, sqlite3.Error):
        return "Ошибка базы данных. Попробуйте позже или /start."
    if isinstance(exc, (AttributeError, ValueError, TypeError, KeyError)):
        return f"Сбой данных: {_short_exc(exc)}"
    mod = type(exc).__module__ or ""
    if mod.startswith("httpx") or mod.startswith("httpcore"):
        return f"Сетевой сбой (HTTP): {_short_exc(exc)}"

    if isinstance(exc, TimedOut):
        return "Таймаут Telegram: сеть нестабильна. Повторите действие."
    if isinstance(exc, NetworkError):
        return "Нет связи с серверами Telegram. Проверьте интернет и попробуйте снова."
    if isinstance(exc, RetryAfter):
        return f"Слишком много запросов. Подождите {exc.retry_after} с."
    if isinstance(exc, Forbidden):
        return "Бот заблокирован или чат недоступен."
    if isinstance(exc, Conflict):
        return "Конфликт: запущено несколько экземпляров бота с одним токеном."
    if isinstance(exc, InvalidToken):
        return "Неверный токен бота. Проверьте BOT_TOKEN в .env."
    if isinstance(exc, BadRequest):
        return f"Некорректный запрос к Telegram: {exc}"
    if isinstance(exc, TelegramError):
        return f"Ошибка Telegram: {exc}"
    return f"{type(exc).__name__}: {_short_exc(exc)}"
