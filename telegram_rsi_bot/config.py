import os
from pathlib import Path

from dotenv import load_dotenv

_pkg_dir = Path(__file__).resolve().parent
_root_dir = _pkg_dir.parent
# Локально часто кладут .env рядом с пакетом; на сервере — в корне проекта.
_env_pkg = _pkg_dir / ".env"
_env_root = _root_dir / ".env"
# Сначала корень проекта, затем пакет с override=True — токен в telegram_rsi_bot/.env
# не затирается пустым значением из корня и наоборот приоритет у более «локального» файла.
if _env_root.is_file():
    load_dotenv(_env_root)
if _env_pkg.is_file():
    load_dotenv(_env_pkg, override=True)


def _clean_secret(value: str | None) -> str:
    s = (value or "").strip()
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff").strip()
    return s


BOT_TOKEN = _clean_secret(os.environ.get("BOT_TOKEN"))
# ccxt: okx (OKX, ранее OKEx). В .env можно задать okx или okex.
EXCHANGE_ID = os.environ.get("EXCHANGE_ID", "okx").strip().lower()
DATABASE_PATH = os.environ.get(
    "DATABASE_PATH",
    str(Path(__file__).resolve().parent.parent / "data" / "bot.sqlite3"),
).strip()
# Относительный путь в .env считается от корня проекта, а не от текущей папки в shell.
_dp = Path(DATABASE_PATH)
if not _dp.is_absolute():
    DATABASE_PATH = str((_root_dir / _dp).resolve())
MONITOR_INTERVAL_SEC = int(os.environ.get("MONITOR_INTERVAL_SEC", "60"))
OHLCV_LIMIT = int(os.environ.get("OHLCV_LIMIT", "100"))
# Таймаут HTTP к бирже (мс), на VPS Beget при медленном канале можно поднять до 45000–60000.
EXCHANGE_TIMEOUT_MS = int(os.environ.get("EXCHANGE_TIMEOUT_MS", "30000"))
# Опционально: прокси для Telegram Bot API (если на сервере блокируют api.telegram.org).
# Формат: http://user:pass@host:port или socks5://… (нужен pip install python-telegram-bot[socks])
TELEGRAM_PROXY_URL = os.environ.get("TELEGRAM_PROXY_URL", "").strip()
# Секунды для httpx к api.telegram.org (при медленном интернете или блокировках).
TELEGRAM_CONNECT_TIMEOUT = float(os.environ.get("TELEGRAM_CONNECT_TIMEOUT", "60"))
TELEGRAM_READ_TIMEOUT = float(os.environ.get("TELEGRAM_READ_TIMEOUT", "60"))

SYMBOLS = {
    "BTCUSDT": "BTC/USDT",
    "ETHUSDT": "ETH/USDT",
}
TIMEFRAMES = ("1h", "4h", "1d")
LEVEL_KEYS = ("30", "50", "70")


def validate() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не задан. Укажите его в файле .env в корне проекта "
            f"({_root_dir / '.env'}) или в {_env_pkg}, строка вида BOT_TOKEN=123456:ABC..."
        )
    bot_id, sep, secret = BOT_TOKEN.partition(":")
    if sep != ":" or not bot_id.isdigit() or not secret:
        raise RuntimeError(
            "BOT_TOKEN похож на некорректный (нужен токен от @BotFather: «id_bot:secret», "
            "без кавычек и пробелов вокруг значения)."
        )
