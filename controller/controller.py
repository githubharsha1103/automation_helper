import asyncio
import logging
import os
import time
import traceback
from pathlib import Path

from dotenv import load_dotenv
from telethon import utils as telethon_utils
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from automation.worker import automation_service, telegram_service
from storage.analytics import get_scope_document, init_analytics, list_bot_documents
from storage.db import (
    add_bot,
    add_group,
    add_message,
    clear_group_special_message,
    delete_bot,
    delete_group,
    delete_message,
    get_bot,
    get_bots,
    get_group,
    get_message,
    get_setting,
    get_promotion_asset_channel,
    get_promotion_sticker_message_id,
    is_bot_enabled,
    is_bot_paused,
    list_groups,
    list_messages,
    set_group_special_message,
    set_promotion_asset_channel,
    set_promotion_sticker_message_id,
    set_bot_enabled,
    set_bot_paused,
    set_group_status,
    set_all_bots_enabled,
    set_all_groups_status,
    set_setting,
    update_group_delay,
    update_group_name,
    update_group_time_window,
)

load_dotenv()

logger = logging.getLogger(__name__)
STICKER_DIR = Path("stickers")
CONTROLLER_START_MONOTONIC = time.monotonic()

ADD_GROUP_CHAT_ID = 100
ADD_MESSAGE_CONTENT = 200
ADD_MESSAGE_DELAY = 201
DELETE_MESSAGE_PICK = 300
EDIT_GROUP_NAME = 400
EDIT_GROUP_DELAY = 401
SET_GROUP_MESSAGE = 402
EDIT_GROUP_TIME_START = 403
EDIT_GROUP_TIME_END = 404
ADD_BOT_USERNAME = 500
ADD_BOT_START_CMD = 501
ADD_BOT_STOP_CMD = 502
ADD_BOT_MATCH_TRIGGERS = 503
ADD_BOT_SECURITY_TRIGGERS = 504
ADD_BOT_AFTER_MATCH_DELAY = 505
ADD_BOT_AFTER_CHAT_DELAY = 506
EDIT_BOT_START_CMD = 507
EDIT_BOT_STOP_CMD = 508
EDIT_BOT_MATCH_TRIGGERS = 509
EDIT_BOT_SECURITY_TRIGGERS = 510
EDIT_BOT_AFTER_MATCH_DELAY = 511
EDIT_BOT_AFTER_CHAT_DELAY = 512
SET_PROMOTION_STICKER = 600
SET_PROMOTION_ASSET_CHANNEL = 601
TEST_PROMOTION_STICKER = 602
SET_PROMOTION_ASSET_CHANNEL = 601


def _env(name: str, default: str = "") -> str:
    return os.getenv(name) or os.getenv(name.lower(), default)


TOKEN = _env("CONTROL_BOT_TOKEN")
ALLOWED_USER_ID = int(_env("ALLOWED_USER_ID", "0") or "0")
_application: Application | None = None


def _is_allowed(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    user = update.effective_user
    return bool(user and user.id == ALLOWED_USER_ID)


async def _send_or_edit(
    update: Update,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    query = update.callback_query
    if query:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
            return
        except Exception:
            await query.message.reply_text(text, reply_markup=reply_markup)
            return
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def _safe_callback_answer(query) -> None:
    try:
        await query.answer()
    except BadRequest as exc:
        message = str(exc).lower()
        if "query is too old" in message or "query id is invalid" in message:
            logger.warning("Ignoring stale callback query: %s", exc)
            return
        raise


def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Bots", callback_data="menu:bots")],
            [InlineKeyboardButton("Groups", callback_data="menu:groups")],
            [InlineKeyboardButton("Messages", callback_data="menu:messages")],
            [InlineKeyboardButton("Automation", callback_data="menu:automation")],
        ]
    )


def _groups_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add Group", callback_data="group:add")],
            [InlineKeyboardButton("List Groups", callback_data="group:list")],
            [InlineKeyboardButton("Back", callback_data="menu:main")],
        ]
    )


def _bots_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add Bot", callback_data="bot:add")],
            [InlineKeyboardButton("List Bots", callback_data="bot:list")],
            [InlineKeyboardButton("Back", callback_data="menu:main")],
        ]
    )


def _messages_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Set Message", callback_data="message:add")],
            [InlineKeyboardButton("List Messages", callback_data="message:list")],
            [InlineKeyboardButton("Clear Message", callback_data="message:delete")],
            [InlineKeyboardButton("Back", callback_data="menu:main")],
        ]
    )


def _automation_bot_action_label() -> str:
    return "Disable All Bots" if any(is_bot_enabled(name, False) for name in get_bots()) else "Enable All Bots"


def _automation_group_action_label() -> str:
    return "Disable All Groups" if any(group.get("status") == "enabled" for group in list_groups()) else "Enable All Groups"


def _automation_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"🤖{_automation_bot_action_label()}", callback_data="automation:toggle_bots")],
            [InlineKeyboardButton(f"👥 {_automation_group_action_label()}", callback_data="automation:toggle_groups")],
            [InlineKeyboardButton("📊 Analytics Dashboard", callback_data="analytics:menu")],
            [InlineKeyboardButton("Promotion Settings", callback_data="promotion:settings")],
            [InlineKeyboardButton("Back", callback_data="menu:main")],
        ]
    )


def _analytics_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Today", callback_data="analytics:today")],
            [InlineKeyboardButton("📆 This Month", callback_data="analytics:month")],
            [InlineKeyboardButton("🌍 Overall", callback_data="analytics:overall")],
            [InlineKeyboardButton("🤖 Per Bot", callback_data="analytics:bot")],
            [InlineKeyboardButton("🟢 Live Status", callback_data="analytics:live")],
            [InlineKeyboardButton("⬅️ Back", callback_data="menu:automation")],
        ]
    )


def _refresh_back_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Refresh 🔄", callback_data=f"{prefix}:refresh")],
            [InlineKeyboardButton("⬅️ Back", callback_data="analytics:menu")],
        ]
    )


def _fmt_date(value: object) -> str:
    if not value:
        return "N/A"
    text = str(value)
    return text.replace("T", " ")[:19]


def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0%"
    return f"{round((numerator / denominator) * 100, 1)}%"


def _analytics_text(scope: str) -> str:
    if scope == "today":
        doc = get_scope_document("day", period=__import__("datetime").datetime.now().strftime("%Y-%m-%d")) or {}
        cycles_started = int(doc.get("cycle_started", 0) or 0)
        cycles_completed = int(doc.get("cycle_completed", 0) or 0)
        failures = int(doc.get("cycle_failed", 0) or 0)
        return (
            "Today Analytics\n"
            f"Total Cycles Started: {cycles_started}\n"
            f"Total Cycles Completed: {cycles_completed}\n"
            f"Matches Found: {int(doc.get('match_found', 0) or 0)}\n"
            f"Promotion Messages Sent: {int(doc.get('promotion_sent', 0) or 0)}\n"
            f"Promotion Stickers Sent: {int(doc.get('sticker_sent', 0) or 0)}\n"
            f"Stop Commands Sent: {int(doc.get('stop_command_sent', 0) or 0)}\n"
            f"Security Challenges Encountered: {int(doc.get('security_challenge', 0) or 0)}\n"
            f"Security Bypass Used: {int(doc.get('security_bypass_used', 0) or 0)}\n"
            f"Failed Cycles: {failures}\n"
            f"Success Rate: {_pct(cycles_completed, cycles_started)}\n"
            f"Runtime Today: {round(float(doc.get('runtime_seconds', 0) or 0) / 3600, 2)}h"
        )
    if scope == "month":
        doc = get_scope_document("month", period=__import__("datetime").datetime.now().strftime("%Y-%m")) or {}
        cycles_started = int(doc.get("cycle_started", 0) or 0)
        cycles_completed = int(doc.get("cycle_completed", 0) or 0)
        return (
            "This Month Analytics\n"
            f"Total Cycles Started: {cycles_started}\n"
            f"Total Cycles Completed: {cycles_completed}\n"
            f"Matches Found: {int(doc.get('match_found', 0) or 0)}\n"
            f"Promotion Messages Sent: {int(doc.get('promotion_sent', 0) or 0)}\n"
            f"Promotion Stickers Sent: {int(doc.get('sticker_sent', 0) or 0)}\n"
            f"Stop Commands Sent: {int(doc.get('stop_command_sent', 0) or 0)}\n"
            f"Security Challenges Encountered: {int(doc.get('security_challenge', 0) or 0)}\n"
            f"Security Bypass Used: {int(doc.get('security_bypass_used', 0) or 0)}\n"
            f"Failed Cycles: {int(doc.get('cycle_failed', 0) or 0)}\n"
            f"Success Rate: {_pct(cycles_completed, cycles_started)}\n"
            f"Runtime This Month: {round(float(doc.get('runtime_seconds', 0) or 0) / 3600, 2)}h\n"
            f"Average Cycles Per Day: {round(cycles_started / max(1, __import__('datetime').datetime.now().day), 2)}\n"
            f"Average Promotions Per Day: {round(int(doc.get('promotion_sent', 0) or 0) / max(1, __import__('datetime').datetime.now().day), 2)}"
        )
    if scope == "overall":
        doc = get_scope_document("overall") or {}
        return (
            "Overall Analytics\n"
            f"Total Cycles: {int(doc.get('cycle_started', 0) or 0)}\n"
            f"Total Matches: {int(doc.get('match_found', 0) or 0)}\n"
            f"Total Promotions: {int(doc.get('promotion_sent', 0) or 0)}\n"
            f"Total Stickers: {int(doc.get('sticker_sent', 0) or 0)}\n"
            f"Total Stop Commands: {int(doc.get('stop_command_sent', 0) or 0)}\n"
            f"Total Security Challenges: {int(doc.get('security_challenge', 0) or 0)}\n"
            f"Total Failed Cycles: {int(doc.get('cycle_failed', 0) or 0)}\n"
            f"Total Runtime: {round(float(doc.get('runtime_seconds', 0) or 0) / 3600, 2)}h\n"
            f"First Automation Start Date: {_fmt_date(doc.get('first_automation_start'))}"
        )
    if scope == "live":
        live = automation_service.live_status_snapshot()
        return (
            "Live Status\n"
            f"Automation Running / Stopped: {'Running' if live['automation_running'] else 'Stopped'}\n"
            f"Number of Active Bots: {live['active_bots']}\n"
            f"Bots Currently Searching: {live['bots_currently_searching']}\n"
            f"Active Conversations: {live['active_conversations']}\n"
            f"Bots Waiting for Match: {live['bots_waiting_for_match']}\n"
            f"Bots Waiting for Security Verification: {live['bots_waiting_for_security_verification']}\n"
            f"Current Cycle Count: {live['current_cycle_count']}\n"
            f"Current Uptime: {round((time.monotonic() - CONTROLLER_START_MONOTONIC) / 3600, 2)}h"
        )
    return "Analytics"


def _bot_analytics_text() -> str:
    docs = list_bot_documents()
    if not docs:
        return "Per Bot Analytics\nNo analytics available yet."
    lines = ["Per Bot Analytics"]
    for doc in docs:
        completed = int(doc.get("cycle_completed", 0) or 0)
        started = int(doc.get("cycle_started", 0) or 0)
        lines.extend(
            [
                f"Bot Name: {doc.get('bot_name', 'N/A')}",
                f"Cycles Completed: {completed}",
                f"Matches: {int(doc.get('match_found', 0) or 0)}",
                f"Promotions: {int(doc.get('promotion_sent', 0) or 0)}",
                f"Security Challenges: {int(doc.get('security_challenge', 0) or 0)}",
                f"Failed Cycles: {int(doc.get('cycle_failed', 0) or 0)}",
                f"Success Rate: {_pct(completed, started)}",
                f"Last Active Time: {_fmt_date(doc.get('last_activity_at'))}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _cancel_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="nav:cancel")]])


def _group_rows() -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for group in list_groups():
        status = "ON" if group["status"] == "enabled" else "OFF"
        rows.append(
            [InlineKeyboardButton(f"{status} {group['group_name']}", callback_data=f"group:view:{group['group_id']}")]
        )
    if not rows:
        rows.append([InlineKeyboardButton("No groups saved", callback_data="noop")])
    rows.append([InlineKeyboardButton("Add Group", callback_data="group:add")])
    rows.append([InlineKeyboardButton("Back", callback_data="menu:groups")])
    return rows


def _group_details_text(group: dict) -> str:
    status = "ON" if group.get("status") == "enabled" else "OFF"
    group_name = group.get("group_name") or group.get("group_id") or "N/A"
    special_message = group.get("special_message") or "None"
    last_status = group.get("last_status") or "N/A"
    last_error = group.get("last_error") or "None"
    fail_count = group.get("fail_count", 0)
    last_failed_at = group.get("last_failed_at") or "Never"
    cooldown_until = group.get("cooldown_until") or "None"
    last_sent_at = group.get("last_sent_at") or "Never"
    next_run_at = group.get("next_run_at") or "Not Scheduled"
    active_start_hour = group.get("active_start_hour")
    active_end_hour = group.get("active_end_hour")
    time_window = (
        f"{active_start_hour} -> {active_end_hour}"
        if active_start_hour is not None and active_end_hour is not None
        else "Not Set"
    )
    delay_min = group.get("delay_min", 4)
    delay_max = group.get("delay_max", 7)
    return (
        "Group Details\n\n"
        f"Name: {group_name}\n"
        f"Group ID: {group.get('group_id', 'N/A')}\n"
        f"Status: {status}\n"
        f"Delay Range: {delay_min}-{delay_max} min\n"
        f"Time Window: {time_window}\n"
        f"Special Message: {special_message}\n"
        f"Last Sent Time: {last_sent_at}\n"
        f"Next Run Time: {next_run_at}\n"
        f"Last Status: {last_status}\n"
        f"Last Error: {last_error}\n"
        f"Fail Count: {fail_count}\n"
        f"Last Failed At: {last_failed_at}\n"
        f"Cooldown Until: {cooldown_until}"
    )


def _group_details_keyboard(group_id: str, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Disable" if enabled else "Enable", callback_data=f"group:toggle:{group_id}")],
            [
                InlineKeyboardButton("Edit Name", callback_data=f"group:edit_name:{group_id}"),
                InlineKeyboardButton("Edit Delay", callback_data=f"group:edit_delay:{group_id}"),
            ],
            [InlineKeyboardButton("Time Window Settings", callback_data=f"group:time_window:{group_id}")],
            [InlineKeyboardButton("Set Message", callback_data=f"group:set_message:{group_id}")],
            [InlineKeyboardButton("Clear Message", callback_data=f"group:clear_message:{group_id}")],
            [InlineKeyboardButton("Delete Group", callback_data=f"group:delete:{group_id}")],
            [InlineKeyboardButton("Back to List", callback_data="group:list")],
        ]
    )


def _group_time_window_text(group: dict) -> str:
    start = group.get("active_start_hour")
    end = group.get("active_end_hour")
    time_window = f"{start} -> {end}" if start is not None and end is not None else "Not Set"
    return (
        "Time Window Settings\n\n"
        f"Group: {group.get('group_name') or group.get('group_id')}\n"
        f"Current Window: {time_window}\n\n"
        "Choose what to update."
    )


def _group_time_window_keyboard(group_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Set Start Hour", callback_data=f"group:time_start:{group_id}")],
            [InlineKeyboardButton("Set End Hour", callback_data=f"group:time_end:{group_id}")],
            [InlineKeyboardButton("Clear Time Window", callback_data=f"group:time_clear:{group_id}")],
            [InlineKeyboardButton("Back", callback_data=f"group:view:{group_id}")],
        ]
    )


async def _render_group_details(update: Update, group_id: str) -> None:
    group = get_group(group_id)
    if not group:
        await _send_or_edit(update, "Group not found", InlineKeyboardMarkup(_group_rows()))
        return
    await _send_or_edit(
        update,
        _group_details_text(group),
        _group_details_keyboard(group_id, group["status"] == "enabled"),
    )


async def _render_group_time_window(update: Update, group_id: str) -> None:
    group = get_group(group_id)
    if not group:
        await _send_or_edit(update, "Group not found", InlineKeyboardMarkup(_group_rows()))
        return
    await _send_or_edit(update, _group_time_window_text(group), _group_time_window_keyboard(group_id))


def _message_rows() -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for message in list_messages(active_only=False):
        snippet = message["content"].replace("\n", " ")[:28]
        rows.append(
            [InlineKeyboardButton(f"#{message['id']} [{message['delay_minutes']}m] {snippet}", callback_data=f"message:view:{message['id']}")]
        )
    if not rows:
        rows.append([InlineKeyboardButton("No messages saved", callback_data="noop")])
    rows.append([InlineKeyboardButton("Set Message", callback_data="message:add")])
    rows.append([InlineKeyboardButton("Back", callback_data="menu:messages")])
    return rows


def _bot_rows() -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for bot_name, bot in get_bots().items():
        enabled = is_bot_enabled(bot_name, False)
        status = "ON" if enabled else "OFF"
        rows.append(
            [InlineKeyboardButton(f"{status} {bot_name}", callback_data=f"bot:view:{bot_name}")]
        )
    if not rows:
        rows.append([InlineKeyboardButton("No bots saved", callback_data="noop")])
    rows.append([InlineKeyboardButton("Back", callback_data="menu:bots")])
    return rows


def _bot_details_text(bot_name: str, bot: dict) -> str:
    enabled = is_bot_enabled(bot_name, False)
    paused = is_bot_paused(bot_name, False)
    runtime_state = "RUNNING" if enabled and not paused else "IDLE"
    match_triggers = bot.get("match_triggers") or bot.get("triggers") or []
    security_triggers = bot.get("security_triggers") or []
    promotion_mode = get_setting("promotion_mode", "message") or "message"
    promotion_asset_channel = get_promotion_asset_channel()
    promotion_sticker_message_id = get_promotion_sticker_message_id()
    sticker_configured = "Yes" if promotion_sticker_message_id else "No"
    mode_label = {
        "message": "Message",
        "sticker": "Sticker",
        "both": "Message + Sticker",
    }.get(str(promotion_mode), "Message")
    return (
        f"Bot: {bot_name}\n"
        f"Status: {'ON' if enabled else 'OFF'}\n"
        f"Runtime state: {runtime_state}\n"
        f"Promotion Mode: {mode_label}\n"
        f"Asset Channel Configured: {'Yes' if promotion_asset_channel else 'No'}\n"
        f"Sticker Message ID: {promotion_sticker_message_id or 'N/A'}\n"
        f"Sticker Configured: {sticker_configured}\n"
        f"Paused: {'Yes' if paused else 'No'}\n"
        f"Start cmd: {bot.get('start_cmd', '-')}\n"
        f"Stop cmd: {bot.get('stop_cmd', '-')}\n"
        f"Match triggers: {', '.join(match_triggers) if match_triggers else 'None'}\n"
        f"Security triggers: {', '.join(security_triggers) if security_triggers else 'None'}\n"
        f"After match delay: {bot.get('after_match_delay', bot.get('speed', [0, 0])[0])} sec\n"
        f"After chat delay: {bot.get('after_chat_delay', bot.get('stop_delay', [0, 0])[0])} sec"
    )


def _bot_details_keyboard(bot_name: str, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Disable" if enabled else "Enable", callback_data=f"bot:toggle:{bot_name}"),
                InlineKeyboardButton("Edit Settings", callback_data=f"bot:edit:{bot_name}"),
            ],
            [InlineKeyboardButton("Delete Bot", callback_data=f"bot:delete:{bot_name}")],
            [InlineKeyboardButton("Back", callback_data="bot:list")],
        ]
    )


def _bot_settings_text(bot_name: str, bot: dict) -> str:
    match_triggers = bot.get("match_triggers") or bot.get("triggers") or []
    security_triggers = bot.get("security_triggers") or []
    return (
        f"Edit Settings: {bot_name}\n\n"
        f"Start cmd: {bot.get('start_cmd', '-')}\n"
        f"Stop cmd: {bot.get('stop_cmd', '-')}\n"
        f"Match triggers: {', '.join(match_triggers) if match_triggers else 'None'}\n"
        f"Security triggers: {', '.join(security_triggers) if security_triggers else 'None'}\n"
        f"After match delay: {bot.get('after_match_delay', 1)} sec\n"
        f"After chat delay: {bot.get('after_chat_delay', 10)} sec"
    )


def _bot_settings_keyboard(bot_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Start Command", callback_data=f"botcfg:start:{bot_name}")],
            [InlineKeyboardButton("Stop Command", callback_data=f"botcfg:stop:{bot_name}")],
            [InlineKeyboardButton("Match Triggers", callback_data=f"botcfg:match:{bot_name}")],
            [InlineKeyboardButton("Security Triggers", callback_data=f"botcfg:security:{bot_name}")],
            [InlineKeyboardButton("After Match Delay", callback_data=f"botcfg:after_match:{bot_name}")],
            [InlineKeyboardButton("After Chat Delay", callback_data=f"botcfg:after_chat:{bot_name}")],
            [InlineKeyboardButton("Back", callback_data=f"bot:view:{bot_name}")],
        ]
    )


def _normalize_command(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return cleaned
    return cleaned if cleaned.startswith("/") else f"/{cleaned}"


def _canonical_bot_config(bot_name: str, bot: dict) -> dict:
    existing_enabled = bool(bot.get("enabled", is_bot_enabled(bot_name, False)))
    match_triggers = bot.get("match_triggers") or bot.get("triggers") or []
    security_triggers = bot.get("security_triggers") or []
    return {
        "start_cmd": _normalize_command(bot.get("start_cmd", "")),
        "stop_cmd": _normalize_command(bot.get("stop_cmd", "")),
        "match_triggers": [item.strip().lower() for item in match_triggers if str(item).strip()],
        "triggers": [item.strip().lower() for item in match_triggers if str(item).strip()],
        "security_triggers": [item.strip().lower() for item in security_triggers if str(item).strip()],
        "after_match_delay": float(bot.get("after_match_delay", 1) or 1),
        "after_chat_delay": float(bot.get("after_chat_delay", 10) or 10),
        "enabled": existing_enabled,
    }


def _save_bot_config(bot_name: str, bot: dict) -> dict:
    normalized = _canonical_bot_config(bot_name, bot)
    add_bot(bot_name, normalized)
    set_bot_enabled(bot_name, normalized["enabled"])
    return normalized


def _fresh_bot(bot_name: str) -> dict | None:
    return get_bot(bot_name)


async def _render_bot_details(update: Update, bot_name: str) -> None:
    bot = _fresh_bot(bot_name)
    if not bot:
        await _send_or_edit(update, "Bot not found", InlineKeyboardMarkup(_bot_rows()))
        return
    await _send_or_edit(update, _bot_details_text(bot_name, bot), _bot_details_keyboard(bot_name, is_bot_enabled(bot_name, False)))


async def _render_bot_settings(update: Update, bot_name: str) -> None:
    bot = _fresh_bot(bot_name)
    if not bot:
        await _send_or_edit(update, "Bot not found", InlineKeyboardMarkup(_bot_rows()))
        return
    await _send_or_edit(update, _bot_settings_text(bot_name, bot), _bot_settings_keyboard(bot_name))


def _automation_status_text() -> str:
    state = get_setting("automation_state", "IDLE")
    enabled_groups = len(list_groups(enabled_only=True))
    messages_count = len(list_messages())
    active_bots = sum(1 for name in get_bots() if is_bot_enabled(name, False))
    last_execution_time = get_setting("automation_last_execution_time", "Never")
    promotion_mode = get_setting("promotion_mode", "message") or "message"
    promotion_asset_channel = get_promotion_asset_channel()
    promotion_sticker_message_id = get_promotion_sticker_message_id()
    mode_label = {
        "message": "Message",
        "sticker": "Sticker",
        "both": "Message + Sticker",
    }.get(str(promotion_mode), "Message")
    return (
        f"Automation is {'running' if state == 'RUNNING' else 'paused' if state == 'PAUSED' else 'idle'}\n"
        f"Runtime state: {state}\n"
        f"Promotion Mode: {mode_label}\n"
        f"Asset Channel Configured: {'Yes' if promotion_asset_channel else 'No'}\n"
        f"Sticker Message ID: {promotion_sticker_message_id or 'N/A'}\n"
        f"Enabled groups: {enabled_groups}\n"
        f"Active messages: {messages_count}\n"
        f"Active bot count: {active_bots}\n"
        f"Last execution time: {last_execution_time}"
    )


def _promotion_mode_label(mode: str | None) -> str:
    return {
        "message": "Message",
        "sticker": "Sticker",
        "both": "Message + Sticker",
    }.get(str(mode or "message"), "Message")


def _promotion_settings_text() -> str:
    mode = get_setting("promotion_mode", "message") or "message"
    asset_channel = get_promotion_asset_channel()
    sticker_message_id = get_promotion_sticker_message_id()
    return (
        "Promotion Settings\n\n"
        f"Current Mode: {mode}\n"
        f"Asset Channel Configured: {'Yes' if asset_channel else 'No'}\n"
        f"Asset Channel ID: {asset_channel or 'N/A'}\n"
        f"Sticker Message ID: {sticker_message_id or 'N/A'}\n\n"
        "Choose an option."
    )


def _promotion_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Promotion Mode", callback_data="promotion:mode")],
            [InlineKeyboardButton("Set Asset Channel", callback_data="promotion:set_channel")],
            [InlineKeyboardButton("Set Sticker Message ID", callback_data="promotion:set_message_id")],
            [InlineKeyboardButton("View Current Asset Settings", callback_data="promotion:view_asset")],
            [InlineKeyboardButton("Test Promotion Sticker", callback_data="promotion:test_sticker")],
            [InlineKeyboardButton("Back", callback_data="menu:automation")],
        ]
    )


def _promotion_mode_text() -> str:
    mode = get_setting("promotion_mode", "message") or "message"
    return (
        "Promotion Mode\n\n"
        f"Current Mode: {mode}\n\n"
        "Choose Promotion Type"
    )


def _promotion_mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Message", callback_data="promotion:mode:message")],
            [InlineKeyboardButton("Sticker", callback_data="promotion:mode:sticker")],
            [InlineKeyboardButton("Message + Sticker", callback_data="promotion:mode:both")],
            [InlineKeyboardButton("Back", callback_data="promotion:settings")],
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    context.user_data.clear()
    await _send_or_edit(update, "Telegram automation control panel", _main_menu())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await _safe_callback_answer(query)
    if not _is_allowed(update):
        return
    action = query.data
    if action == "menu:main":
        context.user_data.clear()
        await _send_or_edit(update, "Telegram automation control panel", _main_menu())
    elif action == "menu:groups":
        await _send_or_edit(update, "Group management", _groups_menu())
    elif action == "menu:bots":
        await _send_or_edit(update, "Bot management", _bots_menu())
    elif action == "menu:messages":
        await _send_or_edit(update, "Message management", _messages_menu())
    elif action == "menu:automation":
        await _send_or_edit(update, _automation_status_text(), _automation_menu())
    elif action == "analytics:menu":
        await _send_or_edit(update, "Analytics Dashboard", _analytics_menu())
    elif action == "analytics:today":
        await _send_or_edit(update, _analytics_text("today"), _refresh_back_keyboard("analytics:today"))
    elif action == "analytics:month":
        await _send_or_edit(update, _analytics_text("month"), _refresh_back_keyboard("analytics:month"))
    elif action == "analytics:overall":
        await _send_or_edit(update, _analytics_text("overall"), _refresh_back_keyboard("analytics:overall"))
    elif action == "analytics:live":
        await _send_or_edit(update, _analytics_text("live"), _refresh_back_keyboard("analytics:live"))
    elif action == "analytics:bot":
        await _send_or_edit(update, _bot_analytics_text(), _refresh_back_keyboard("analytics:bot"))
    elif action == "automation:toggle_bots":
        await automation_toggle_bots(update, context)
    elif action == "automation:toggle_groups":
        await automation_toggle_groups(update, context)
    elif action.startswith("analytics:") and action.endswith(":refresh"):
        key = action.split(":", 2)[1]
        if key == "today":
            await _send_or_edit(update, _analytics_text("today"), _refresh_back_keyboard("analytics:today"))
        elif key == "month":
            await _send_or_edit(update, _analytics_text("month"), _refresh_back_keyboard("analytics:month"))
        elif key == "overall":
            await _send_or_edit(update, _analytics_text("overall"), _refresh_back_keyboard("analytics:overall"))
        elif key == "live":
            await _send_or_edit(update, _analytics_text("live"), _refresh_back_keyboard("analytics:live"))
        elif key == "bot":
            await _send_or_edit(update, _bot_analytics_text(), _refresh_back_keyboard("analytics:bot"))


async def promotion_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, _promotion_settings_text(), _promotion_settings_keyboard())


async def promotion_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, _promotion_mode_text(), _promotion_mode_keyboard())


async def promotion_mode_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    mode = query.data.split(":", 2)[2]
    set_setting("promotion_mode", mode)
    await _send_or_edit(update, _promotion_settings_text(), _promotion_settings_keyboard())


async def promotion_asset_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    context.user_data.clear()
    context.user_data["promotion_asset_channel_flow"] = True
    await _send_or_edit(
        update,
        "Send the private Telegram channel @username or numeric channel id to use as the promotion asset repository.\n\nPress Cancel to abort.",
        _cancel_menu(),
    )


async def promotion_asset_channel_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    context.user_data.clear()
    context.user_data["promotion_asset_channel_flow"] = True
    await _send_or_edit(
        update,
        "Send the private Telegram channel @username or numeric channel id to use as the promotion asset repository.\n\nPress Cancel to abort.",
        _cancel_menu(),
    )
    return SET_PROMOTION_ASSET_CHANNEL


async def promotion_asset_channel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        await update.message.reply_text("Please send a channel username or channel id.")
        return SET_PROMOTION_ASSET_CHANNEL
    raw_value = update.message.text.strip()
    if not raw_value:
        await update.message.reply_text("Channel value cannot be empty.")
        return SET_PROMOTION_ASSET_CHANNEL
    set_promotion_asset_channel(raw_value)
    context.user_data.clear()
    await update.message.reply_text("Promotion asset channel updated successfully.")
    await update.message.reply_text(_promotion_settings_text(), reply_markup=_promotion_settings_keyboard())
    return ConversationHandler.END


async def promotion_sticker_message_id_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    context.user_data.clear()
    context.user_data["promotion_sticker_message_id_flow"] = True
    await _send_or_edit(
        update,
        "Send the Telegram message ID of the sticker stored in your asset channel.",
        _cancel_menu(),
    )
    return SET_PROMOTION_STICKER


async def promotion_sticker_message_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message or not update.message.text:
        await update.message.reply_text("Please send a numeric message ID.")
        return SET_PROMOTION_STICKER
    raw_value = update.message.text.strip()
    try:
        message_id = int(raw_value)
    except ValueError:
        await update.message.reply_text("Message ID must be a number.")
        return SET_PROMOTION_STICKER
    set_promotion_sticker_message_id(message_id)
    context.user_data.clear()
    await update.message.reply_text("Sticker message ID updated successfully.")
    await update.message.reply_text(_promotion_settings_text(), reply_markup=_promotion_settings_keyboard())
    return ConversationHandler.END


async def promotion_asset_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, _promotion_settings_text(), _promotion_settings_keyboard())


async def promotion_test_sticker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    asset_channel = get_promotion_asset_channel()
    sticker_message_id = get_promotion_sticker_message_id()
    if not asset_channel or not sticker_message_id:
        await _send_or_edit(update, "❌ Sticker Forward Test Failed Asset channel or sticker message ID is missing.", _promotion_settings_keyboard())
        return
    try:
        await telegram_service.ensure_connected()
        client = telegram_service._ensure_client()
        asset_entity = await telegram_service.resolve_entity(str(asset_channel))
        logger.info("ASSET STICKER FORWARD START channel_id=%s message_id=%s target=me", asset_channel, sticker_message_id)
        await client.forward_messages("me", int(sticker_message_id), from_peer=asset_entity)
        logger.info("ASSET STICKER FORWARD SUCCESS channel_id=%s message_id=%s target=me", asset_channel, sticker_message_id)
        await _send_or_edit(update, "✅ Sticker Forward Test Successful", _promotion_settings_keyboard())
    except Exception as exc:
        logger.exception("ASSET STICKER FORWARD FAILED channel_id=%s message_id=%s target=me error=%s", asset_channel, sticker_message_id, exc)
        await _send_or_edit(update, f"❌ Sticker Forward Test Failed {exc}", _promotion_settings_keyboard())


async def promotion_settings_back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def list_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, "Saved groups", InlineKeyboardMarkup(_group_rows()))


async def list_bots_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, "Saved bots", InlineKeyboardMarkup(_bot_rows()))


async def view_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    await _render_bot_details(update, bot_name)


async def toggle_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    bot = get_bots().get(bot_name)
    if not bot:
        await _send_or_edit(update, "Bot not found", InlineKeyboardMarkup(_bot_rows()))
        return
    new_enabled = not is_bot_enabled(bot_name, False)
    set_bot_enabled(bot_name, new_enabled)
    if not new_enabled:
        set_bot_paused(bot_name, False)
        stop_cmd = _normalize_command(bot.get("stop_cmd"))
        if stop_cmd:
            try:
                await telegram_service.client.send_message(bot_name, stop_cmd)
            except Exception:
                logger.exception("Failed to send stop command to %s", bot_name)
    else:
        start_cmd = _normalize_command(bot.get("start_cmd"))
        if start_cmd:
            try:
                await telegram_service.client.send_message(bot_name, start_cmd)
            except Exception:
                logger.exception("Failed to send start command to %s", bot_name)
    await _render_bot_details(update, bot_name)


async def _set_bot_enabled_runtime(bot_name: str, enabled: bool, bot: dict | None = None) -> None:
    bot = bot or get_bots().get(bot_name) or {}
    set_bot_enabled(bot_name, enabled)
    if not enabled:
        set_bot_paused(bot_name, False)
        stop_cmd = _normalize_command(bot.get("stop_cmd"))
        if stop_cmd:
            try:
                await telegram_service.client.send_message(bot_name, stop_cmd)
            except Exception:
                logger.exception("Failed to send stop command to %s", bot_name)
    else:
        start_cmd = _normalize_command(bot.get("start_cmd"))
        if start_cmd:
            try:
                await telegram_service.client.send_message(bot_name, start_cmd)
            except Exception:
                logger.exception("Failed to send start command to %s", bot_name)


async def _apply_group_status(group_id: str, status: str) -> None:
    set_group_status(group_id, status)
    if status == "enabled":
        automation_service.start()
        set_setting("automation_running", True)


async def delete_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    delete_bot(bot_name)
    await _send_or_edit(update, "Bot deleted", InlineKeyboardMarkup(_bot_rows()))


async def edit_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    bot = get_bots().get(bot_name)
    if not bot:
        await _send_or_edit(update, "Bot not found", InlineKeyboardMarkup(_bot_rows()))
        return
    context.user_data.clear()
    await _render_bot_settings(update, bot_name)


async def add_bot_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    context.user_data.clear()
    context.user_data["bot_flow_mode"] = "add"
    context.user_data["bot_config"] = {}
    await _send_or_edit(update, "Enter bot username", _cancel_menu())
    return ADD_BOT_USERNAME


async def bot_username_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    username = update.message.text.strip().lstrip("@")
    if not username:
        await update.message.reply_text("Bot username cannot be empty.")
        return ADD_BOT_USERNAME
    context.user_data.setdefault("bot_config", {})["username"] = username
    await update.message.reply_text("Enter start command (e.g. /match)", reply_markup=_cancel_menu())
    return ADD_BOT_START_CMD


async def bot_start_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    start_cmd = _normalize_command(update.message.text)
    if not start_cmd:
        await update.message.reply_text("Start command cannot be empty.")
        return ADD_BOT_START_CMD
    context.user_data["bot_config"]["start_cmd"] = start_cmd
    await update.message.reply_text("Enter stop command (e.g. /stop)", reply_markup=_cancel_menu())
    return ADD_BOT_STOP_CMD


async def bot_stop_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    stop_cmd = _normalize_command(update.message.text)
    if not stop_cmd:
        await update.message.reply_text("Stop command cannot be empty.")
        return ADD_BOT_STOP_CMD
    context.user_data["bot_config"]["stop_cmd"] = stop_cmd
    await update.message.reply_text("Enter match triggers (comma-separated text)", reply_markup=_cancel_menu())
    return ADD_BOT_MATCH_TRIGGERS


async def bot_match_triggers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    triggers = [item.strip().lower() for item in update.message.text.split(",") if item.strip()]
    if not triggers:
        await update.message.reply_text("Enter at least one match trigger.")
        return ADD_BOT_MATCH_TRIGGERS
    context.user_data["bot_config"]["match_triggers"] = triggers
    await update.message.reply_text("Enter security triggers (comma-separated text)", reply_markup=_cancel_menu())
    return ADD_BOT_SECURITY_TRIGGERS


async def bot_security_triggers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    triggers = [item.strip().lower() for item in update.message.text.split(",") if item.strip()]
    if not triggers:
        await update.message.reply_text("Enter at least one security trigger.")
        return ADD_BOT_SECURITY_TRIGGERS
    context.user_data["bot_config"]["security_triggers"] = triggers
    await update.message.reply_text("Enter after-match delay (seconds)", reply_markup=_cancel_menu())
    return ADD_BOT_AFTER_MATCH_DELAY


async def bot_after_match_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        delay = float(update.message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid number of seconds.")
        return ADD_BOT_AFTER_MATCH_DELAY
    context.user_data["bot_config"]["after_match_delay"] = delay
    await update.message.reply_text("Enter after-chat delay (seconds)", reply_markup=_cancel_menu())
    return ADD_BOT_AFTER_CHAT_DELAY


async def bot_after_chat_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        delay = float(update.message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid number of seconds.")
        return ADD_BOT_AFTER_CHAT_DELAY

    config = context.user_data.get("bot_config", {})
    config["after_chat_delay"] = delay
    bot_name = config["username"]
    existing = get_bots().get(bot_name, {})
    enabled = existing.get("enabled", False) if context.user_data.get("bot_flow_mode") == "edit" else False
    saved_config = {
        **existing,
        "start_cmd": config["start_cmd"],
        "stop_cmd": config["stop_cmd"],
        "match_triggers": config["match_triggers"],
        "security_triggers": config["security_triggers"],
        "after_match_delay": config["after_match_delay"],
        "after_chat_delay": config["after_chat_delay"],
        "enabled": enabled,
    }
    saved_config = _save_bot_config(bot_name, saved_config)
    context.user_data.clear()
    await update.message.reply_text(
        _bot_details_text(bot_name, saved_config),
        reply_markup=_bot_details_keyboard(bot_name, is_bot_enabled(bot_name, False)),
    )
    return ConversationHandler.END


def _get_bot_or_end(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    return context.user_data.get("edit_bot_name")


async def bot_settings_start_cmd_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    await _send_or_edit(update, "Enter start command (e.g. /match)", _cancel_menu())
    return EDIT_BOT_START_CMD


async def bot_settings_start_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = _get_bot_or_end(context)
    if not bot_name:
        return ConversationHandler.END
    start_cmd = _normalize_command(update.message.text)
    if not start_cmd:
        await update.message.reply_text("Start command cannot be empty.")
        return EDIT_BOT_START_CMD
    bot = _fresh_bot(bot_name) or {}
    bot["start_cmd"] = start_cmd
    _save_bot_config(bot_name, bot)
    context.user_data.clear()
    await update.message.reply_text("Updated.")
    await _render_bot_settings(update, bot_name)
    return ConversationHandler.END


async def bot_settings_stop_cmd_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    await _send_or_edit(update, "Enter stop command (e.g. /stop)", _cancel_menu())
    return EDIT_BOT_STOP_CMD


async def bot_settings_stop_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = _get_bot_or_end(context)
    if not bot_name:
        return ConversationHandler.END
    stop_cmd = _normalize_command(update.message.text)
    if not stop_cmd:
        await update.message.reply_text("Stop command cannot be empty.")
        return EDIT_BOT_STOP_CMD
    bot = _fresh_bot(bot_name) or {}
    bot["stop_cmd"] = stop_cmd
    _save_bot_config(bot_name, bot)
    context.user_data.clear()
    await update.message.reply_text("Updated.")
    await _render_bot_settings(update, bot_name)
    return ConversationHandler.END


async def bot_settings_match_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    await _send_or_edit(update, "Enter match triggers (comma-separated text)", _cancel_menu())
    return EDIT_BOT_MATCH_TRIGGERS


async def bot_settings_match_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = _get_bot_or_end(context)
    if not bot_name:
        return ConversationHandler.END
    triggers = [item.strip().lower() for item in update.message.text.split(",") if item.strip()]
    if not triggers:
        await update.message.reply_text("Enter at least one match trigger.")
        return EDIT_BOT_MATCH_TRIGGERS
    bot = _fresh_bot(bot_name) or {}
    bot["match_triggers"] = triggers
    bot["triggers"] = triggers
    _save_bot_config(bot_name, bot)
    context.user_data.clear()
    await update.message.reply_text("Updated.")
    await _render_bot_settings(update, bot_name)
    return ConversationHandler.END


async def bot_settings_security_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    await _send_or_edit(update, "Enter security triggers (comma-separated text)", _cancel_menu())
    return EDIT_BOT_SECURITY_TRIGGERS


async def bot_settings_security_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = _get_bot_or_end(context)
    if not bot_name:
        return ConversationHandler.END
    triggers = [item.strip().lower() for item in update.message.text.split(",") if item.strip()]
    if not triggers:
        await update.message.reply_text("Enter at least one security trigger.")
        return EDIT_BOT_SECURITY_TRIGGERS
    bot = _fresh_bot(bot_name) or {}
    bot["security_triggers"] = triggers
    _save_bot_config(bot_name, bot)
    context.user_data.clear()
    await update.message.reply_text("Updated.")
    await _render_bot_settings(update, bot_name)
    return ConversationHandler.END


async def bot_settings_after_match_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    await _send_or_edit(update, "Enter after-match delay (seconds)", _cancel_menu())
    return EDIT_BOT_AFTER_MATCH_DELAY


async def bot_settings_after_match_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = _get_bot_or_end(context)
    if not bot_name:
        return ConversationHandler.END
    try:
        delay = float(update.message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid number of seconds.")
        return EDIT_BOT_AFTER_MATCH_DELAY
    bot = _fresh_bot(bot_name) or {}
    bot["after_match_delay"] = delay
    _save_bot_config(bot_name, bot)
    context.user_data.clear()
    await update.message.reply_text("Updated.")
    await _render_bot_settings(update, bot_name)
    return ConversationHandler.END


async def bot_settings_after_chat_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    await _send_or_edit(update, "Enter after-chat delay (seconds)", _cancel_menu())
    return EDIT_BOT_AFTER_CHAT_DELAY


async def bot_settings_after_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = _get_bot_or_end(context)
    if not bot_name:
        return ConversationHandler.END
    try:
        delay = float(update.message.text.strip())
        if delay < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Enter a valid number of seconds.")
        return EDIT_BOT_AFTER_CHAT_DELAY
    bot = _fresh_bot(bot_name) or {}
    bot["after_chat_delay"] = delay
    _save_bot_config(bot_name, bot)
    context.user_data.clear()
    await update.message.reply_text("Updated.")
    await _render_bot_settings(update, bot_name)
    return ConversationHandler.END


async def view_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    await _render_group_details(update, group_id)


async def toggle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    group = get_group(group_id)
    if not group:
        await list_groups_callback(update, context)
        return
    new_status = "disabled" if group["status"] == "enabled" else "enabled"
    await _apply_group_status(group_id, new_status)
    await _render_group_details(update, group_id)


async def delete_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    delete_group(group_id)
    await _send_or_edit(update, "Group deleted", InlineKeyboardMarkup(_group_rows()))


async def group_edit_name_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_group_id"] = group_id
    await _send_or_edit(update, "Send the new group name.", _cancel_menu())
    return EDIT_GROUP_NAME


async def group_edit_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_id = context.user_data.get("edit_group_id")
    if not group_id:
        return ConversationHandler.END
    new_name = update.message.text.strip()
    if not new_name:
        await update.message.reply_text("Group name cannot be empty.")
        return EDIT_GROUP_NAME
    update_group_name(group_id, new_name)
    context.user_data.clear()
    group = get_group(group_id)
    await update.message.reply_text(
        _group_details_text(group),
        reply_markup=_group_details_keyboard(group_id, group["status"] == "enabled"),
    )
    return ConversationHandler.END


async def group_edit_delay_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_group_id"] = group_id
    await _send_or_edit(update, "Send delay range in minutes as min,max", _cancel_menu())
    return EDIT_GROUP_DELAY


async def group_edit_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_id = context.user_data.get("edit_group_id")
    if not group_id:
        return ConversationHandler.END
    try:
        parts = [part.strip() for part in update.message.text.split(",")]
        if len(parts) != 2:
            raise ValueError
        delay_min = int(parts[0])
        delay_max = int(parts[1])
        if delay_min < 1 or delay_max < delay_min:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid format. Use min,max in minutes, for example 3,6")
        return EDIT_GROUP_DELAY
    update_group_delay(group_id, delay_min, delay_max)
    context.user_data.clear()
    group = get_group(group_id)
    await update.message.reply_text(
        _group_details_text(group),
        reply_markup=_group_details_keyboard(group_id, group["status"] == "enabled"),
    )
    return ConversationHandler.END


async def group_time_window_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_group_id"] = group_id
    context.user_data["group_back_view"] = "time_window"
    await _render_group_time_window(update, group_id)


async def group_time_start_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_group_id"] = group_id
    context.user_data["group_back_view"] = "time_window"
    await _send_or_edit(update, "Send the start hour from 0 to 23.", _cancel_menu())
    return EDIT_GROUP_TIME_START


async def group_time_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_id = context.user_data.get("edit_group_id")
    if not group_id:
        return ConversationHandler.END
    try:
        start_hour = int(update.message.text.strip())
        if start_hour < 0 or start_hour > 23:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Start hour must be a number from 0 to 23.")
        return EDIT_GROUP_TIME_START
    group = get_group(group_id) or {}
    update_group_time_window(group_id, start_hour, group.get("active_end_hour"))
    context.user_data["group_back_view"] = "time_window"
    await update.message.reply_text("Start hour saved.")
    await _render_group_time_window(update, group_id)
    return ConversationHandler.END


async def group_time_end_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_group_id"] = group_id
    context.user_data["group_back_view"] = "time_window"
    await _send_or_edit(update, "Send the end hour from 0 to 23.", _cancel_menu())
    return EDIT_GROUP_TIME_END


async def group_time_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_id = context.user_data.get("edit_group_id")
    if not group_id:
        return ConversationHandler.END
    try:
        end_hour = int(update.message.text.strip())
        if end_hour < 0 or end_hour > 23:
            raise ValueError
    except ValueError:
        await update.message.reply_text("End hour must be a number from 0 to 23.")
        return EDIT_GROUP_TIME_END
    group = get_group(group_id) or {}
    update_group_time_window(group_id, group.get("active_start_hour"), end_hour)
    context.user_data["group_back_view"] = "time_window"
    await update.message.reply_text("End hour saved.")
    await _render_group_time_window(update, group_id)
    return ConversationHandler.END


async def group_time_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_group_id"] = group_id
    context.user_data["group_back_view"] = "time_window"
    update_group_time_window(group_id, None, None)
    await _render_group_time_window(update, group_id)
    return None


async def group_set_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_group_id"] = group_id
    await _send_or_edit(update, "Send the special message for this group.", _cancel_menu())
    return SET_GROUP_MESSAGE


async def group_set_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    group_id = context.user_data.get("edit_group_id")
    if not group_id:
        return ConversationHandler.END
    message_text = update.message.text.strip()
    if not message_text:
        await update.message.reply_text("Special message cannot be empty.")
        return SET_GROUP_MESSAGE
    set_group_special_message(group_id, message_text)
    context.user_data.clear()
    group = get_group(group_id)
    await update.message.reply_text(
        _group_details_text(group),
        reply_markup=_group_details_keyboard(group_id, group["status"] == "enabled"),
    )
    return ConversationHandler.END


async def clear_group_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    group_id = query.data.split(":", 2)[2]
    clear_group_special_message(group_id)
    await _render_group_details(update, group_id)


async def add_group_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    context.user_data.clear()
    await _send_or_edit(
        update,
        "Send the group chat_id or @username. I will fetch the group name automatically.",
        _cancel_menu(),
    )
    return ADD_GROUP_CHAT_ID


async def add_group_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        return ConversationHandler.END
    raw_value = update.message.text.strip()
    try:
        entity = await telegram_service.resolve_entity(raw_value)
    except Exception as exc:
        logger.warning("Failed to resolve group %s: %s", raw_value, exc)
        await update.message.reply_text("I could not access that group. Make sure the account is in the group.")
        return ADD_GROUP_CHAT_ID

    group_id = str(telethon_utils.get_peer_id(entity))
    group_name = getattr(entity, "title", None) or getattr(entity, "username", None) or raw_value
    add_group(group_id=group_id, group_name=group_name, status="enabled")
    await update.message.reply_text(
        f"Saved group: {group_name}",
        reply_markup=InlineKeyboardMarkup(_group_rows()),
    )
    return ConversationHandler.END


async def messages_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, "Saved messages", InlineKeyboardMarkup(_message_rows()))


async def message_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    message_id = int(query.data.split(":", 2)[2])
    message = get_message(message_id)
    if not message:
        await messages_list_callback(update, context)
        return
    media = message["media_type"] or "none"
    text = (
        f"Message #{message['id']}\n"
        f"Delay: {message['delay_minutes']} minute(s)\n"
        f"Media: {media}\n\n"
        f"{message['content']}"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Delete", callback_data=f"message:delete_one:{message_id}")],
            [InlineKeyboardButton("Back", callback_data="message:list")],
        ]
    )
    await _send_or_edit(update, text, keyboard)


async def add_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    context.user_data.clear()
    await _send_or_edit(
        update,
        "Send the message text. You can attach one photo, video, or document with the same message if needed.",
        _cancel_menu(),
    )
    return ADD_MESSAGE_CONTENT


async def add_message_content(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        return ConversationHandler.END
    message = update.message
    media_type = None
    media_file_id = None

    if message.photo:
        media_type = "photo"
        media_file_id = message.photo[-1].file_id
    elif message.video:
        media_type = "video"
        media_file_id = message.video.file_id
    elif message.document:
        media_type = "document"
        media_file_id = message.document.file_id

    content = (message.caption or message.text or "").strip()
    if not content:
        await message.reply_text("Message content cannot be empty.")
        return ADD_MESSAGE_CONTENT

    context.user_data["message_content"] = content
    context.user_data["media_type"] = media_type
    context.user_data["media_file_id"] = media_file_id
    await message.reply_text("Enter delay in minutes for this message.", reply_markup=_cancel_menu())
    return ADD_MESSAGE_DELAY


async def add_message_delay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        delay_minutes = int(update.message.text.strip())
        if delay_minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Delay must be a whole number of minutes greater than 0.")
        return ADD_MESSAGE_DELAY

    add_message(
        content=context.user_data["message_content"],
        delay_minutes=delay_minutes,
        media_type=context.user_data.get("media_type"),
        media_file_id=context.user_data.get("media_file_id"),
    )
    context.user_data.clear()
    await update.message.reply_text(
        "Message saved.",
        reply_markup=InlineKeyboardMarkup(_message_rows()),
    )
    return ConversationHandler.END


async def delete_message_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    rows = []
    for message in list_messages(active_only=False):
        rows.append(
            [InlineKeyboardButton(f"Delete #{message['id']}", callback_data=f"message:delete_one:{message['id']}")]
        )
    rows.append([InlineKeyboardButton("Back", callback_data="menu:messages")])
    await _send_or_edit(update, "Choose a message to delete", InlineKeyboardMarkup(rows))
    return ConversationHandler.END


async def delete_one_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    message_id = int(query.data.split(":", 3)[3])
    delete_message(message_id)
    await _send_or_edit(update, "Message deleted", InlineKeyboardMarkup(_message_rows()))


async def automation_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    automation_service.start()
    set_setting("automation_running", True)
    set_setting("automation_paused", False)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    automation_service.stop()
    set_setting("automation_running", False)
    set_setting("automation_paused", False)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    automation_service.pause()
    set_setting("automation_running", True)
    set_setting("automation_paused", True)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    automation_service.resume()
    set_setting("automation_running", True)
    set_setting("automation_paused", False)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_toggle_bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    bots = get_bots()
    bot_names = list(bots.keys())
    if not bot_names:
        await _send_or_edit(update, _automation_status_text(), _automation_menu())
        return
    should_enable = not any(is_bot_enabled(name, False) for name in bot_names)
    for bot_name, bot in bots.items():
        await _set_bot_enabled_runtime(bot_name, should_enable, bot)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_toggle_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    groups = list_groups()
    if not groups:
        await _send_or_edit(update, _automation_status_text(), _automation_menu())
        return
    should_enable = not any(group.get("status") == "enabled" for group in groups)
    for group in groups:
        await _apply_group_status(str(group.get("group_id")), "enabled" if should_enable else "disabled")
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def notify_security(bot_name: str) -> None:
    global _application
    if _application is None or ALLOWED_USER_ID == 0:
        return
    await _application.bot.send_message(
        chat_id=ALLOWED_USER_ID,
        text=f"Security check detected for {bot_name}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Bypassed", callback_data=f"bot:bypass:{bot_name}")]]
        ),
    )


async def bypass_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    set_bot_paused(bot_name, False)
    automation_service.arm_security_bypass_wait(bot_name)
    await _send_or_edit(update, f"Resumed {bot_name}", _main_menu())


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await _safe_callback_answer(update.callback_query)
    bot_name = context.user_data.get("edit_bot_name")
    group_id = context.user_data.get("edit_group_id")
    group_back_view = context.user_data.get("group_back_view")
    promotion_asset_channel_flow = context.user_data.get("promotion_asset_channel_flow")
    promotion_sticker_message_id_flow = context.user_data.get("promotion_sticker_message_id_flow")
    context.user_data.clear()
    if bot_name:
        await _render_bot_settings(update, bot_name)
        return ConversationHandler.END
    if group_id:
        group = get_group(group_id)
        if group and group_back_view == "time_window":
            await _render_group_time_window(update, group_id)
            return ConversationHandler.END
        if group:
            await _render_group_details(update, group_id)
            return ConversationHandler.END
    if promotion_asset_channel_flow or promotion_sticker_message_id_flow:
        await _send_or_edit(update, _promotion_settings_text(), _promotion_settings_keyboard())
        return ConversationHandler.END
    await _send_or_edit(update, "Cancelled.", _main_menu())
    return ConversationHandler.END


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)


async def start_controller() -> None:
    global _application
    if not TOKEN:
        raise ValueError("CONTROL_BOT_TOKEN is required")

    init_analytics()
    print("[STARTUP] Building Telegram control application...")
    logger.info("[STARTUP] Building Telegram control application...")
    _application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_group_entry, pattern="^group:add$"),
            CallbackQueryHandler(add_message_entry, pattern="^message:add$"),
            CallbackQueryHandler(add_bot_entry, pattern="^bot:add$"),
            CallbackQueryHandler(group_edit_name_entry, pattern="^group:edit_name:"),
            CallbackQueryHandler(group_edit_delay_entry, pattern="^group:edit_delay:"),
            CallbackQueryHandler(group_set_message_entry, pattern="^group:set_message:"),
            CallbackQueryHandler(group_time_start_entry, pattern="^group:time_start:"),
            CallbackQueryHandler(group_time_end_entry, pattern="^group:time_end:"),
            CallbackQueryHandler(bot_settings_start_cmd_entry, pattern="^botcfg:start:"),
            CallbackQueryHandler(bot_settings_stop_cmd_entry, pattern="^botcfg:stop:"),
            CallbackQueryHandler(bot_settings_match_entry, pattern="^botcfg:match:"),
            CallbackQueryHandler(bot_settings_security_entry, pattern="^botcfg:security:"),
            CallbackQueryHandler(bot_settings_after_match_entry, pattern="^botcfg:after_match:"),
            CallbackQueryHandler(bot_settings_after_chat_entry, pattern="^botcfg:after_chat:"),
            CallbackQueryHandler(promotion_settings_callback, pattern="^promotion:settings$"),
            CallbackQueryHandler(promotion_mode_callback, pattern="^promotion:mode$"),
            CallbackQueryHandler(promotion_mode_set_callback, pattern="^promotion:mode:"),
            CallbackQueryHandler(promotion_asset_channel_entry, pattern="^promotion:set_channel$"),
            CallbackQueryHandler(promotion_asset_view_callback, pattern="^promotion:view_asset$"),
            CallbackQueryHandler(promotion_test_sticker_callback, pattern="^promotion:test_sticker$"),
            CallbackQueryHandler(promotion_sticker_message_id_entry, pattern="^promotion:set_message_id$"),
        ],
        states={
            ADD_GROUP_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_chat_id)],
            ADD_MESSAGE_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
                    & ~filters.COMMAND,
                    add_message_content,
                )
            ],
            ADD_MESSAGE_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_message_delay)],
            ADD_BOT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_username_handler)],
            ADD_BOT_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_start_cmd_handler)],
            ADD_BOT_STOP_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_stop_cmd_handler)],
            ADD_BOT_MATCH_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_match_triggers_handler)],
            ADD_BOT_SECURITY_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_security_triggers_handler)],
            ADD_BOT_AFTER_MATCH_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_after_match_delay_handler)],
            ADD_BOT_AFTER_CHAT_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_after_chat_delay_handler)],
            EDIT_BOT_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_start_cmd_handler)],
            EDIT_BOT_STOP_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_stop_cmd_handler)],
            EDIT_BOT_MATCH_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_match_handler)],
            EDIT_BOT_SECURITY_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_security_handler)],
            EDIT_BOT_AFTER_MATCH_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_after_match_handler)],
            EDIT_BOT_AFTER_CHAT_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_after_chat_handler)],
            EDIT_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_edit_name_handler)],
            EDIT_GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_edit_delay_handler)],
            SET_GROUP_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_set_message_handler)],
            EDIT_GROUP_TIME_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_time_start_handler)],
            EDIT_GROUP_TIME_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_time_end_handler)],
            SET_PROMOTION_ASSET_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, promotion_asset_channel_handler)],
            SET_PROMOTION_STICKER: [MessageHandler(filters.TEXT & ~filters.COMMAND, promotion_sticker_message_id_handler)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_callback, pattern="^nav:cancel$"),
            CommandHandler("cancel", cancel_callback),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    _application.add_handler(CommandHandler("start", start_command))
    _application.add_handler(conv_handler)
    _application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    _application.add_handler(CallbackQueryHandler(menu_callback, pattern="^analytics:"))
    _application.add_handler(CallbackQueryHandler(menu_callback, pattern="^automation:"))
    _application.add_handler(CallbackQueryHandler(list_bots_callback, pattern="^bot:list$"))
    _application.add_handler(CallbackQueryHandler(view_bot_callback, pattern="^bot:view:"))
    _application.add_handler(CallbackQueryHandler(toggle_bot_callback, pattern="^bot:toggle:"))
    _application.add_handler(CallbackQueryHandler(edit_bot_callback, pattern="^bot:edit:"))
    _application.add_handler(CallbackQueryHandler(bypass_bot_callback, pattern="^bot:bypass:"))
    _application.add_handler(CallbackQueryHandler(delete_bot_callback, pattern="^bot:delete:"))
    _application.add_handler(CallbackQueryHandler(list_groups_callback, pattern="^group:list$"))
    _application.add_handler(CallbackQueryHandler(view_group_callback, pattern="^group:view:"))
    _application.add_handler(CallbackQueryHandler(toggle_group_callback, pattern="^group:toggle:"))
    _application.add_handler(CallbackQueryHandler(group_time_window_menu, pattern="^group:time_window:"))
    _application.add_handler(CallbackQueryHandler(group_time_clear_callback, pattern="^group:time_clear:"))
    _application.add_handler(CallbackQueryHandler(clear_group_message_callback, pattern="^group:clear_message:"))
    _application.add_handler(CallbackQueryHandler(delete_group_callback, pattern="^group:delete:"))
    _application.add_handler(CallbackQueryHandler(messages_list_callback, pattern="^message:list$"))
    _application.add_handler(CallbackQueryHandler(message_view_callback, pattern="^message:view:"))
    _application.add_handler(CallbackQueryHandler(delete_message_menu, pattern="^message:delete$"))
    _application.add_handler(CallbackQueryHandler(delete_one_message, pattern="^message:delete_one:"))
    _application.add_handler(CallbackQueryHandler(automation_start, pattern="^automation:start$"))
    _application.add_handler(CallbackQueryHandler(automation_stop, pattern="^automation:stop$"))
    _application.add_handler(CallbackQueryHandler(automation_pause, pattern="^automation:pause$"))
    _application.add_handler(CallbackQueryHandler(automation_resume, pattern="^automation:resume$"))
    _application.add_handler(CallbackQueryHandler(noop_callback, pattern="^noop$"))

    try:
        print("[STARTUP] Initializing Telegram control application...")
        logger.info("[STARTUP] Initializing Telegram control application...")
        await _application.initialize()
        print("[STARTUP] Starting Telegram control application...")
        logger.info("[STARTUP] Starting Telegram control application...")
        await _application.start()
        print("[STARTUP] Starting Telegram polling...")
        logger.info("[STARTUP] Starting Telegram polling...")
        await _application.updater.start_polling()
    except Exception:
        print("FATAL ERROR: Telegram controller failed to start")
        print(traceback.format_exc())
        logger.exception("Telegram controller failed to start")
        raise

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await _application.updater.stop()
        await _application.stop()
        await _application.shutdown()


