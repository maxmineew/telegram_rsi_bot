from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
from html import escape
from datetime import datetime

import ccxt
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from telegram_rsi_bot import config, db
from telegram_rsi_bot.errors_ru import explain_exception
from telegram_rsi_bot.monitor import SIGNAL_LABELS, _symbol_display, _tf_ru
from telegram_rsi_bot.rsi_chart import render_rsi_chart_png
from telegram_rsi_bot.rsi_snapshot import build_snapshot

log = logging.getLogger(__name__)

HTML = ParseMode.HTML

MAIN_MENU = (
    "<b>📡 RSI Monitor</b>\n"
    "Сигналы по пересечению RSI (14) уровней <b>30 · 50 · 70</b> для "
    "<b>BTC</b> и <b>ETH</b> на таймфреймах <b>1h / 4h / 1d</b>.\n\n"
    "<b>Команды</b>\n"
    "<code>/settings</code> — настройки подписки\n"
    "<code>/rsi</code> — RSI по текущей свече\n"
    "<code>/check</code> — последняя закрытая свеча\n"
    "<code>/chart</code> — график RSI\n"
    "<code>/privacy</code> — конфиденциальность\n"
    "<code>/status</code> · <code>/stop</code> · <code>/help</code>"
)

HELP_TEXT = (
    "<b>ℹ️ Как пользоваться</b>\n\n"
    "<b>/settings</b> — пары, таймфреймы и уровни, затем <b>Сохранить</b>.\n"
    "<b>/rsi</b> — RSI(14) по <b>текущей открытой</b> свече (бар ещё формируется).\n"
    "<b>/check</b> — RSI на <b>последней закрытой</b> свече и пересечения уровней на её закрытии.\n"
    "<b>/chart</b> — график RSI за последние бары (PNG).\n"
    "<b>/privacy</b> — политика конфиденциальности.\n"
    "<b>/status</b> — подписка. <b>/stop</b> — отключить сигналы.\n\n"
    "Пара и таймфрейм для RSI/графика — первая сохранённая в настройках; "
    "если настроек нет — <b>BTCUSDT 1h</b>.\n\n"
    "Сбои сети/биржи: мониторинг повторяется по расписанию."
)

PRIVACY_TEXT = (
    "<b>🔒 Политика конфиденциальности</b>\n\n"
    "1. Бот получает от Telegram ваш <b>user_id</b> и (если есть) <b>username</b> "
    "для работы команд и хранения настроек.\n"
    "2. Настройки подписки (пары, таймфреймы, уровни) хранятся в локальной базе "
    "на сервере, где запущен бот.\n"
    "3. Запросы к бирже (OKX и т.п.) используются только для котировок и расчёта RSI; "
    "API-ключи биржи боту <b>не передаются</b> и не хранятся.\n"
    "4. Мы не продаём и не передаём ваши данные третьим лицам. Сообщения обрабатываются "
    "через официальный Telegram Bot API.\n"
    "5. Команда <b>/stop</b> отключает рассылку и удаляет сохранённые настройки подписки.\n\n"
    "По вопросам данных обратитесь к владельцу развёртывания бота."
)

PRIVACY_GATE_INTRO = (
    "<b>Перед началом работы</b>\n\n"
    "Ознакомьтесь с политикой конфиденциальности ниже. "
    "Чтобы пользоваться ботом, нажмите <b>«Принять политику конфиденциальности»</b>.\n\n"
)

PRIVACY_GATE_FULL = PRIVACY_GATE_INTRO + PRIVACY_TEXT

PRIVACY_BLOCKED = (
    "<b>Доступ ограничен</b>\n\n"
    "Бот недоступен, пока вы не примете политику конфиденциальности. "
    "Отправьте команду /start и нажмите кнопку принятия."
)


def build_privacy_accept_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Принять политику конфиденциальности",
                    callback_data="privacy:accept",
                ),
            ],
        ]
    )


async def _send_privacy_blocked(update: Update) -> None:
    msg = update.effective_message
    if msg:
        await msg.reply_text(PRIVACY_BLOCKED, parse_mode=HTML)


async def _require_privacy(update: Update) -> bool:
    """False — пользователь не принял политику; отправлено пояснение."""
    u = update.effective_user
    if not u:
        return False
    if db.has_accepted_privacy(u.id):
        return True
    await _send_privacy_blocked(update)
    return False


def _default_draft() -> dict:
    return {
        "symbols": set(),
        "timeframes": set(),
        "levels": {"30": False, "50": False, "70": False},
    }


def _load_draft_from_db(user_id: int) -> dict:
    rows = db.load_settings_rows(user_id)
    if not rows:
        return _default_draft()
    st = db.settings_to_ui_state(rows)
    return {
        "symbols": set(st["symbols"]),
        "timeframes": set(st["timeframes"]),
        "levels": {k: bool(st["levels"].get(k)) for k in ("30", "50", "70")},
    }


def _load_draft_safe(user_id: int) -> dict:
    """Загрузка настроек из БД; при сбое SQLite — пустой черновик (чтобы меню не падало)."""
    try:
        return _load_draft_from_db(user_id)
    except sqlite3.Error:
        log.exception("SQLite: не удалось загрузить настройки user_id=%s", user_id)
        return _default_draft()


def _normalize_draft(draft: object) -> dict:
    """
    user_data после перезапуска/сериализации может отдать list вместо set —
    тогда .add() даёт AttributeError и срабатывает глобальный on_error.
    """
    if not isinstance(draft, dict):
        return _default_draft()
    out = _default_draft()
    sym = draft.get("symbols")
    if isinstance(sym, set):
        out["symbols"] = set(sym)
    elif isinstance(sym, (list, tuple)):
        out["symbols"] = set(sym)
    tf = draft.get("timeframes")
    if isinstance(tf, set):
        out["timeframes"] = set(tf)
    elif isinstance(tf, (list, tuple)):
        out["timeframes"] = set(tf)
    lv = draft.get("levels")
    if isinstance(lv, dict):
        for k in ("30", "50", "70"):
            out["levels"][k] = bool(lv.get(k, False))
    return out


def _draft_for_ui(draft: object) -> dict:
    """Гарантирует set/dict для текста и клавиатуры настроек."""
    if not draft:
        return _default_draft()
    return _normalize_draft(draft)


def _pill(on: bool, on_label: str, off_label: str) -> str:
    return on_label if on else off_label


def format_settings_html(draft: dict) -> str:
    d = _draft_for_ui(draft)
    s = d["symbols"]
    t = d["timeframes"]
    lv = d["levels"]
    lines = [
        "<b>🎛 Панель настроек RSI</b>",
        "",
        "Включите <b>пары</b>, <b>таймфреймы</b> и <b>уровни</b>, затем нажмите "
        "<b>Сохранить</b>. Бот отправит уведомление только при пересечении уровня.",
        "",
        "<b>Сводка</b>",
        f"· Пары: {_pill(bool(s), '✅ выбраны', '⚪ не выбраны')}",
        f"· Таймфреймы: {_pill(bool(t), '✅ выбраны', '⚪ не выбраны')}",
        f"· Уровни: {_pill(any(lv.values()), '✅ выбраны', '⚪ не выбраны')}",
    ]
    return "\n".join(lines)


def build_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎛 Настройки RSI", callback_data="menu:settings"),
                InlineKeyboardButton("📊 Мой статус", callback_data="menu:status"),
            ],
            [
                InlineKeyboardButton("📈 RSI", callback_data="tool:rsi"),
                InlineKeyboardButton("✓ Проверка свечи", callback_data="tool:check"),
            ],
            [
                InlineKeyboardButton("📉 График RSI", callback_data="tool:chart"),
                InlineKeyboardButton("🔒 Конфиденциальность", callback_data="tool:privacy"),
            ],
            [
                InlineKeyboardButton("ℹ️ Помощь", callback_data="menu:help"),
                InlineKeyboardButton("🛑 Отключить", callback_data="stop:ask"),
            ],
        ]
    )


def build_settings_keyboard(draft: dict) -> InlineKeyboardMarkup:
    d = _draft_for_ui(draft)
    s = d["symbols"]
    t = d["timeframes"]
    lv = d["levels"]
    row_pairs = [
        InlineKeyboardButton(
            f"{_pill('BTCUSDT' in s, '🟢', '⚪')}  BTC",
            callback_data="cfg:s:BTCUSDT",
        ),
        InlineKeyboardButton(
            f"{_pill('ETHUSDT' in s, '🟢', '⚪')}  ETH",
            callback_data="cfg:s:ETHUSDT",
        ),
    ]
    row_tf = [
        InlineKeyboardButton(
            f"{_pill(tf in t, '✅', '▫️')} 1h",
            callback_data="cfg:t:1h",
        ),
        InlineKeyboardButton(
            f"{_pill('4h' in t, '✅', '▫️')} 4h",
            callback_data="cfg:t:4h",
        ),
        InlineKeyboardButton(
            f"{_pill('1d' in t, '✅', '▫️')} 1d",
            callback_data="cfg:t:1d",
        ),
    ]
    row_lv = [
        InlineKeyboardButton(
            f"{_pill(lv.get('30', False), '📉', '▫️')} 30",
            callback_data="cfg:l:30",
        ),
        InlineKeyboardButton(
            f"{_pill(lv.get('50', False), '~', '▫️')} 50",
            callback_data="cfg:l:50",
        ),
        InlineKeyboardButton(
            f"{_pill(lv.get('70', False), '📈', '▫️')} 70",
            callback_data="cfg:l:70",
        ),
    ]
    row_save = [
        InlineKeyboardButton(
            "💾 Сохранить настройки",
            callback_data="cfg:save",
        )
    ]
    row_back = [
        InlineKeyboardButton("↩️ Главное меню", callback_data="menu:main"),
    ]
    return InlineKeyboardMarkup(
        [
            row_pairs,
            row_tf,
            row_lv,
            row_save,
            row_back,
        ]
    )


def build_saved_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Статус", callback_data="menu:status"),
                InlineKeyboardButton("🎛 Изменить снова", callback_data="menu:settings"),
            ],
            [InlineKeyboardButton("↩️ Главное меню", callback_data="menu:main")],
        ]
    )


def build_stop_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("↩️ Отмена", callback_data="menu:main"),
                InlineKeyboardButton("✅ Да, отключить", callback_data="stop:yes"),
            ],
        ]
    )


def _sync_prepare_settings(context: ContextTypes.DEFAULT_TYPE, uid: int) -> tuple[dict, str, InlineKeyboardMarkup]:
    """Черновик + текст + клавиатура панели RSI (общая для /settings и callback menu:settings)."""
    db.ensure_default_subscription_if_needed(uid)
    raw_draft = context.user_data.get("draft")
    draft = (
        _load_draft_safe(uid)
        if not raw_draft
        else _normalize_draft(raw_draft)
    )
    context.user_data["draft"] = draft
    return draft, format_settings_html(draft), build_settings_keyboard(draft)


async def _open_rsi_settings_panel(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    uid: int,
) -> None:
    """Показ панели настроек по callback; при сбое — новое сообщение, без «Ошибка /start»."""
    try:
        _, text, kb = _sync_prepare_settings(context, uid)
        await _safe_edit(query, text, kb)
    except Exception:
        log.exception("_open_rsi_settings_panel")
        try:
            draft = _default_draft()
            context.user_data["draft"] = draft
            text = format_settings_html(draft)
            kb = build_settings_keyboard(draft)
            if query.message:
                await query.message.reply_text(text, reply_markup=kb, parse_mode=HTML)
        except Exception:
            log.exception("_open_rsi_settings_panel: fallback")
            if query.message:
                await query.message.reply_text(
                    "<b>🎛 Настройки RSI</b>\n\n"
                    "Введите <code>/settings</code> или отправьте /start.",
                    parse_mode=HTML,
                )


async def _safe_edit(
    query,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Редактирует сообщение с кнопками. Если Telegram не даёт править текст
    (старый чат, тип сообщения и т.д.) — отправляет новое сообщение в ответ.
    Не пробрасывает исключения наружу (чтобы не срабатывал глобальный on_error).
    """
    try:
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode=HTML,
        )
    except BadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err or "not modified" in err:
            return
        log.warning("edit_message_text: %s — отправляю новое сообщение", e)
        if query.message:
            try:
                await query.message.reply_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=HTML,
                )
            except BadRequest as e2:
                log.warning("reply_text после edit: %s", e2)
            except Exception:
                log.exception("reply_text после edit")
        return
    except Exception:
        log.exception("_safe_edit: edit_message_text")
        if query.message:
            try:
                await query.message.reply_text(
                    text=text,
                    reply_markup=reply_markup,
                    parse_mode=HTML,
                )
            except Exception:
                log.exception("_safe_edit: reply_text fallback")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not db.has_accepted_privacy(u.id):
        await update.effective_message.reply_text(
            PRIVACY_GATE_FULL,
            reply_markup=build_privacy_accept_keyboard(),
            parse_mode=HTML,
        )
        return
    db.ensure_default_subscription_if_needed(u.id)
    await update.effective_message.reply_text(
        MAIN_MENU,
        reply_markup=build_main_keyboard(),
        parse_mode=HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not await _require_privacy(update):
        return
    await update.effective_message.reply_text(
        HELP_TEXT,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Главное меню", callback_data="menu:main")]]
        ),
        parse_mode=HTML,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not await _require_privacy(update):
        return
    _, text, kb = _sync_prepare_settings(context, u.id)
    await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=HTML)


async def on_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    u = update.effective_user
    if not u:
        await query.answer(
            "Откройте диалог с ботом в личных сообщениях.",
            show_alert=True,
        )
        return
    uid = u.id
    # callback_data: menu:settings → action = "settings" (не использовать split по первому ':' только)
    parts = query.data.split(":", 1)
    action = (parts[1] if len(parts) > 1 else "").strip()
    try:
        try:
            await query.answer()
        except BadRequest:
            pass
        db.upsert_user(u.id, u.username)
        if action in ("settings", "status"):
            db.ensure_default_subscription_if_needed(uid)
        if action == "main":
            await _safe_edit(query, MAIN_MENU, build_main_keyboard())
            return
        if action == "help":
            await _safe_edit(
                query,
                HELP_TEXT,
                InlineKeyboardMarkup(
                    [[InlineKeyboardButton("↩️ Главное меню", callback_data="menu:main")]]
                ),
            )
            return
        if action == "settings":
            await _open_rsi_settings_panel(query, context, uid)
            return
        if action == "status":
            status_plain = db.format_status(uid)
            await _safe_edit(
                query,
                f"<b>📊 Статус подписки</b>\n\n{escape(status_plain)}",
                InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "🎛 Настройки", callback_data="menu:settings"
                            ),
                            InlineKeyboardButton("↩️ Меню", callback_data="menu:main"),
                        ]
                    ]
                ),
            )
            return
    except BadRequest as e:
        log.warning("on_menu_callback BadRequest: %s", e)
        if query.message:
            await query.message.reply_text(
                "<b>Не удалось обновить экран.</b> Нажмите /start.",
                parse_mode=HTML,
            )
    except Forbidden as e:
        log.warning("on_menu_callback Forbidden: %s", e)
        if query.message:
            await query.message.reply_text(
                "<b>Нет доступа к сообщению.</b> Отправьте /start новым сообщением.",
                parse_mode=HTML,
            )
    except sqlite3.Error:
        log.exception("on_menu_callback: SQLite")
        if query.message:
            await query.message.reply_text(
                "<b>Ошибка базы данных.</b> Проверьте права на файл БД и путь DATABASE_PATH в .env, "
                "затем /start.",
                parse_mode=HTML,
            )
    except TelegramError as e:
        log.warning("on_menu_callback TelegramError: %s", e)
        if query.message:
            await query.message.reply_text(
                "<b>Сбой Telegram.</b> Повторите через минуту или /start.",
                parse_mode=HTML,
            )
    except Exception:
        log.exception("on_menu_callback")
        if query.message:
            await query.message.reply_text(
                "<b>Ошибка.</b> Попробуйте /start.",
                parse_mode=HTML,
            )


async def on_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    u = update.effective_user
    if not u:
        return
    uid = u.id
    part = query.data.split(":", 1)[-1]
    if part == "ask":
        await query.answer()
        await _safe_edit(
            query,
            "<b>🛑 Отключить рассылку?</b>\n\n"
            "Все сохранённые настройки будут сброшены. Подтвердите действие.",
            build_stop_confirm_keyboard(),
        )
        return
    if part == "yes":
        await query.answer("Рассылка отключена")
        db.deactivate_user(uid)
        context.user_data.pop("draft", None)
        await _safe_edit(
            query,
            "<b>Готово.</b> Рассылка отключена, настройки сброшены.\n"
            "Вы можете снова включить уведомления в <b>Настройках RSI</b>.",
            InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🎛 Настройки RSI", callback_data="menu:settings"),
                        InlineKeyboardButton("↩️ Меню", callback_data="menu:main"),
                    ]
                ]
            ),
        )
        return


async def on_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if not data.startswith("cfg:"):
        return
    u = update.effective_user
    if not u:
        try:
            await query.answer(
                "Откройте бота в личном чате.",
                show_alert=True,
            )
        except BadRequest:
            pass
        return
    try:
        await query.answer()
    except BadRequest:
        pass
    uid = u.id
    db.upsert_user(u.id, u.username)
    db.ensure_default_subscription_if_needed(uid)
    raw_draft = context.user_data.get("draft")
    draft = (
        _load_draft_safe(uid)
        if not raw_draft
        else _normalize_draft(raw_draft)
    )
    parts = data.split(":", 2)
    if len(parts) < 2:
        return
    kind = parts[1]
    if kind == "s" and len(parts) >= 3:
        sym = parts[2]
        if sym in draft["symbols"]:
            draft["symbols"].discard(sym)
        else:
            draft["symbols"].add(sym)
    elif kind == "t" and len(parts) >= 3:
        tf = parts[2]
        if tf in draft["timeframes"]:
            draft["timeframes"].discard(tf)
        else:
            draft["timeframes"].add(tf)
    elif kind == "l" and len(parts) >= 3:
        lev = parts[2]
        draft["levels"][lev] = not draft["levels"].get(lev, False)
    elif kind == "save":
        try:
            db.save_settings(
                uid,
                draft["symbols"],
                draft["timeframes"],
                draft["levels"],
            )
        except Exception:
            log.exception("save_settings failed")
            if query.message:
                await query.message.reply_text(
                    "<b>Не удалось сохранить.</b> Повторите или попробуйте позже.",
                    parse_mode=HTML,
                )
            return
        context.user_data["draft"] = draft
        await _safe_edit(
            query,
            "<b>✅ Настройки сохранены</b>\n\n"
            "Можно проверить подписку в разделе «Мой статус».",
            build_saved_keyboard(),
        )
        return
    context.user_data["draft"] = draft
    await _safe_edit(query, format_settings_html(draft), build_settings_keyboard(draft))


async def on_privacy_accept_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    u = update.effective_user
    if not u:
        return
    if query.data != "privacy:accept":
        return
    try:
        await query.answer()
    except BadRequest:
        pass
    db.upsert_user(u.id, u.username)
    db.accept_privacy(u.id)
    db.ensure_default_subscription_if_needed(u.id)
    await _safe_edit(query, MAIN_MENU, build_main_keyboard())


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Один обработчик на все callback_data — надёжнее, чем несколько Regex в разных версиях PTB."""
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    if data.startswith("privacy:"):
        await on_privacy_accept_callback(update, context)
        return
    u = update.effective_user
    if not u:
        return
    if not db.has_accepted_privacy(u.id):
        try:
            await query.answer(
                "Сначала примите политику конфиденциальности через /start.",
                show_alert=True,
            )
        except BadRequest:
            pass
        return
    if data.startswith("menu:"):
        await on_menu_callback(update, context)
    elif data.startswith("cfg:"):
        await on_settings_callback(update, context)
    elif data.startswith("tool:"):
        await on_tool_callback(update, context)
    elif data.startswith("stop:"):
        await on_stop_callback(update, context)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not await _require_privacy(update):
        return
    db.ensure_default_subscription_if_needed(u.id)
    await update.effective_message.reply_text(
        f"<b>📊 Статус подписки</b>\n\n{escape(db.format_status(u.id))}",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎛 Настройки", callback_data="menu:settings"),
                    InlineKeyboardButton("↩️ Меню", callback_data="menu:main"),
                ]
            ]
        ),
        parse_mode=HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not await _require_privacy(update):
        return
    db.deactivate_user(u.id)
    context.user_data.pop("draft", None)
    await update.effective_message.reply_text(
        "<b>Рассылка отключена.</b> Настройки сброшены.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🎛 Настройки RSI", callback_data="menu:settings"),
                    InlineKeyboardButton("↩️ Меню", callback_data="menu:main"),
                ]
            ]
        ),
        parse_mode=HTML,
    )


def _fmt_ms_display(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=config.display_timezone())
    return dt.strftime("%Y-%m-%d %H:%M МСК")


def _describe_crossovers(codes: list[str]) -> str:
    if not codes:
        return (
            "Пересечений уровней <b>30 / 50 / 70</b> между предыдущей и последней "
            "<b>закрытой</b> свечой по правилам бота не зафиксировано."
        )
    lines = [SIGNAL_LABELS[c][0] for c in codes if c in SIGNAL_LABELS]
    return "На закрытии последней свечи (относительно предыдущей): " + "; ".join(lines)


async def _fetch_snapshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u:
        return None, None, None
    ex = context.application.bot_data.get("exchange")
    if ex is None:
        await update.effective_message.reply_text(
            "<b>Биржа не инициализирована.</b> Перезапустите процесс бота.",
            parse_mode=HTML,
        )
        return None, None, None
    sym, tf = db.get_preferred_symbol_timeframe(u.id)
    try:
        snap = await asyncio.to_thread(build_snapshot, ex, sym, tf)
    except ccxt.BaseError as e:
        await update.effective_message.reply_text(
            f"<b>Ошибка биржи.</b> {explain_exception(e)}",
            parse_mode=HTML,
        )
        return None, None, None
    except Exception:
        log.exception("build_snapshot")
        await update.effective_message.reply_text(
            "<b>Не удалось получить данные.</b> Попробуйте позже.",
            parse_mode=HTML,
        )
        return None, None, None
    if not snap or snap.get("rsi_current") is None:
        await update.effective_message.reply_text(
            "<b>Недостаточно данных</b> для расчёта RSI.",
            parse_mode=HTML,
        )
        return None, None, None
    return snap, sym, tf


async def cmd_rsi(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not await _require_privacy(update):
        return
    snap, sym, tf = await _fetch_snapshot(update, context)
    if snap is None:
        return
    rc = snap["rsi_current"]
    pair = _symbol_display(sym)
    txt = (
        f"<b>📈 RSI — текущая свеча</b> (бар формируется)\n\n"
        f"Пара: <b>{pair}</b>, ТФ: <b>{_tf_ru(tf)}</b>\n"
        f"RSI(14): <b>{rc:.2f}</b>\n"
        f"Открытие бара (МСК): {_fmt_ms_display(snap['time_open_current_ms'])}\n\n"
        f"<i>Пара/ТФ — первая строка в /settings; если настроек нет — BTC 1h.</i>"
    )
    await update.effective_message.reply_text(txt, parse_mode=HTML)


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not await _require_privacy(update):
        return
    snap, sym, tf = await _fetch_snapshot(update, context)
    if snap is None:
        return
    rlc = snap["rsi_last_closed"]
    rbc = snap["rsi_before_closed"]
    if rlc is None:
        await update.effective_message.reply_text(
            "<b>Недостаточно данных</b> для RSI на закрытой свече.",
            parse_mode=HTML,
        )
        return
    codes = snap.get("crossover_codes_last_closed") or []
    pair = _symbol_display(sym)
    extra = ""
    if rbc is not None:
        extra = f"RSI на предыдущей закрытой: <b>{rbc:.2f}</b>\n"
    txt = (
        f"<b>✓ Последняя закрытая свеча</b>\n\n"
        f"Пара: <b>{pair}</b>, ТФ: <b>{_tf_ru(tf)}</b>\n"
        f"{extra}"
        f"RSI(14) на закрытии последней свечи: <b>{rlc:.2f}</b>\n"
        f"Время открытия этой свечи (МСК): {_fmt_ms_display(snap['time_open_last_closed_ms'])}\n\n"
        f"{_describe_crossovers(codes)}"
    )
    await update.effective_message.reply_text(txt, parse_mode=HTML)


async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not await _require_privacy(update):
        return
    snap, sym, tf = await _fetch_snapshot(update, context)
    if snap is None:
        return
    try:
        png = await asyncio.to_thread(
            render_rsi_chart_png,
            snap["times"],
            snap["rsi"],
            sym,
            tf,
        )
    except Exception:
        log.exception("render_rsi_chart_png")
        await update.effective_message.reply_text(
            "<b>Не удалось построить график.</b>",
            parse_mode=HTML,
        )
        return
    pair = _symbol_display(sym)
    cap = (
        f"<b>📉 RSI(14)</b> · {pair} · {_tf_ru(tf)}\n"
        f"Линии: 30 / 50 / 70. Данные с биржи."
    )
    await update.effective_message.reply_photo(
        photo=io.BytesIO(png),
        caption=cap,
        parse_mode=HTML,
    )


async def cmd_privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    if not u:
        return
    db.upsert_user(u.id, u.username)
    if not db.has_accepted_privacy(u.id):
        await update.effective_message.reply_text(
            PRIVACY_GATE_FULL,
            reply_markup=build_privacy_accept_keyboard(),
            parse_mode=HTML,
        )
        return
    await update.effective_message.reply_text(
        PRIVACY_TEXT,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩️ Главное меню", callback_data="menu:main")]]
        ),
        parse_mode=HTML,
    )


async def on_tool_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()
    action = query.data.split(":", 1)[-1]
    if action == "rsi":
        await cmd_rsi(update, context)
    elif action == "check":
        await cmd_check(update, context)
    elif action == "chart":
        await cmd_chart(update, context)
    elif action == "privacy":
        await cmd_privacy(update, context)


def register(application) -> None:
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("rsi", cmd_rsi))
    application.add_handler(CommandHandler("check", cmd_check))
    application.add_handler(CommandHandler("chart", cmd_chart))
    application.add_handler(CommandHandler("privacy", cmd_privacy))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CommandHandler("stop", cmd_stop))
    application.add_handler(CallbackQueryHandler(callback_router))
