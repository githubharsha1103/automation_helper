import asyncio
import logging
import os
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
from storage.db import (
    add_bot,
    add_group,
    add_category_message,
    add_promotion_sticker,
    add_message,
    clear_group_special_message,
    delete_category_message,
    delete_bot,
    delete_group,
    delete_message,
    get_bot,
    get_bots,
    get_group,
    get_category_message,
    get_bot_settings,
    get_bot_runtime,
    get_message,
    get_setting,
    get_promotion_asset_channel,
    get_promotion_sticker_message_id,
    is_bot_enabled,
    is_bot_paused,
    list_groups,
    list_category_messages,
    list_messages,
    get_message_performance,
    set_category_message_enabled,
    is_category_message_enabled,
    set_group_special_message,
    set_promotion_asset_channel,
    set_promotion_sticker_message_id,
    update_category_message,
    set_bot_enabled,
    set_bot_paused,
    set_group_status,
    set_bot_settings,
    update_bot_runtime,
    increment_bot_runtime,
    set_setting,
    update_group_delay,
    update_group_name,
    update_group_time_window,
)

load_dotenv()

logger = logging.getLogger(__name__)
STICKER_DIR = Path("stickers")

ADD_GROUP_CHAT_ID = 100
ADD_CONVERSATIONAL_MESSAGE = 800
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
SET_BOT_PROMOTION_MODE = 700
SET_BOT_CONVERSATION_SEQUENCE = 701
SET_BOT_NO_RESPONSE_TIMEOUT = 702
SET_BOT_CONV_DELAY_MIN = 703
SET_BOT_CONV_DELAY_MAX = 704
SET_BOT_PROMO_DELAY_MIN = 705
SET_BOT_PROMO_DELAY_MAX = 706


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
            [InlineKeyboardButton("Message Management", callback_data="menu:messages")],
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
            [InlineKeyboardButton("Conversational Messages", callback_data="menu:messages:conversational")],
            [InlineKeyboardButton("Bot Messages", callback_data="menu:messages:bot")],
            [InlineKeyboardButton("Group Messages", callback_data="menu:messages:group")],
            [InlineKeyboardButton("Stickers", callback_data="menu:messages:stickers")],
            [InlineKeyboardButton("Back", callback_data="menu:main")],
        ]
    )


def _automation_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Stop Automation", callback_data="automation:stop")],
            [InlineKeyboardButton("Pause", callback_data="automation:pause")],
            [InlineKeyboardButton("Resume", callback_data="automation:resume")],
            [InlineKeyboardButton("Promotion Settings", callback_data="promotion:settings")],
            [InlineKeyboardButton("Back", callback_data="menu:main")],
        ]
    )


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


def _category_label(category: str) -> str:
    return {
        "conversational_messages": "Conversational Messages",
        "bot_messages": "Bot Messages",
        "group_messages": "Group Messages",
        "promotion_stickers": "Promotion Stickers",
    }[category]


def _category_back_target(category: str) -> str:
    return "menu:messages"


def _category_rows(category: str) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    messages = list_category_messages(category, active_only=False)
    top_message = None
    top_reply_rate = -1.0
    if category == "conversational_messages":
        for message in messages:
            perf = get_message_performance(category, int(message["id"]))
            sent = int(perf.get("times_sent", 0) or 0)
            replies = int(perf.get("replies_received", 0) or 0)
            reply_rate = (replies / sent) if sent else 0.0
            if reply_rate > top_reply_rate:
                top_reply_rate = reply_rate
                top_message = message
    for message in messages:
        enabled = bool(message.get("enabled", True))
        status = "Enabled" if enabled else "Disabled"
        if category == "promotion_stickers":
            label = f"#{message['id']} Sticker"
        else:
            snippet = str(message.get("content", "")).replace("\n", " ")[:28]
            label = f"#{message['id']} {snippet}\nStatus: {status}"
            if category == "conversational_messages":
                perf = get_message_performance(category, int(message["id"]))
                sent = int(perf.get("times_sent", 0) or 0)
                replies = int(perf.get("replies_received", 0) or 0)
                reply_rate = f"{(replies / sent * 100):.1f}%" if sent else "0.0%"
                label = f"{label}\nSent: {sent}\nReplies: {replies}\nReply Rate: {reply_rate}"
            elif category == "bot_messages":
                perf = get_message_performance(category, int(message["id"]))
                sent = int(perf.get("times_sent", 0) or 0)
                label = f"{label}\nSent: {sent}"
        action_buttons = [InlineKeyboardButton(label, callback_data=f"msg:{category}:view:{message['id']}")]
        if category in {"conversational_messages", "bot_messages"}:
            if enabled:
                action_buttons.append(InlineKeyboardButton("Disable", callback_data=f"msg:{category}:disable:{message['id']}"))
            else:
                action_buttons.append(InlineKeyboardButton("Enable", callback_data=f"msg:{category}:enable:{message['id']}"))
        rows.append(action_buttons)
    if not rows:
        rows.append([InlineKeyboardButton("No messages saved", callback_data="noop")])
    if category == "conversational_messages" and top_message:
        rows.insert(0, [InlineKeyboardButton(f"Top Performer: #{top_message['id']}", callback_data=f"msg:{category}:view:{top_message['id']}")])
    if category == "conversational_messages":
        rows.append([InlineKeyboardButton("Add", callback_data="msg:conversational:add")])
    else:
        rows.append([InlineKeyboardButton("Add", callback_data=f"msg:{category}:add")])
    rows.append([InlineKeyboardButton("Back", callback_data=_category_back_target(category))])
    return rows


def _bot_rows() -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    for bot_name, bot in get_bots().items():
        enabled = bool(bot.get("enabled", False))
        status = "ON" if enabled else "OFF"
        rows.append([InlineKeyboardButton(f"{status} {bot_name}", callback_data=f"bot:view:{bot_name}")])
    if not rows:
        rows.append([InlineKeyboardButton("No bots saved", callback_data="noop")])
    rows.append([InlineKeyboardButton("Back", callback_data="menu:bots")])
    return rows


def _bot_details_text(bot_name: str, bot: dict) -> str:
    enabled = bool(bot.get("enabled", False))
    paused = bool(get_setting(f"bot_paused_{bot_name}", False))
    runtime_state = "RUNNING" if enabled and not paused else "IDLE"
    match_triggers = bot.get("match_triggers") or bot.get("triggers") or []
    security_triggers = bot.get("security_triggers") or []
    promotion_mode = get_setting("promotion_mode", "message") or "message"
    promotion_asset_channel = get_promotion_asset_channel()
    promotion_sticker_message_id = get_promotion_sticker_message_id()
    runtime = get_bot_runtime(bot_name)
    conversations_started = int(runtime.get("conversations_started", 0) or 0)
    partner_replies = int(runtime.get("partner_replies", 0) or 0)
    promotions_sent = int(runtime.get("promotions_sent", 0) or 0)
    error_count = int(runtime.get("error_count", 0) or 0)
    reply_rate = round(partner_replies / conversations_started, 4) if conversations_started else 0.0
    promotion_coverage = round(promotions_sent / conversations_started, 4) if conversations_started else 0.0
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
        f"Current Stage: {runtime.get('current_stage', 'IDLE')}\n"
        f"Last Activity Timestamp: {runtime.get('last_activity_ts') or 'N/A'}\n"
        f"Last Failure Reason: {runtime.get('last_failure_reason') or 'None'}\n"
        f"Last Failure Time: {runtime.get('last_failure_ts') or 'N/A'}\n"
        f"Conversations Started: {conversations_started}\n"
        f"Partner Replies: {partner_replies}\n"
        f"Promotions Sent: {promotions_sent}\n"
        f"Error Count: {error_count}\n"
        f"Reply Rate: {reply_rate}\n"
        f"Promotion Coverage: {promotion_coverage}\n"
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
            [InlineKeyboardButton("Refresh Status", callback_data=f"bot:refresh:{bot_name}")],
            [InlineKeyboardButton("Message Management", callback_data=f"botmsg:view:{bot_name}")],
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


def _bot_message_settings_text(bot_name: str) -> str:
    settings = get_bot_settings(bot_name)
    return (
        f"Automation Settings: {bot_name}\n\n"
        f"Promotion Mode: {settings.get('promotion_mode', 'MESSAGE')}\n"
        f"Conversation Sequence: {','.join(map(str, settings.get('conversation_sequence', []))) or 'None'}\n"
        f"No Response Timeout: {settings.get('no_response_timeout', 0)} sec\n"
        f"Conversation Delay: {settings.get('conversation_delay_min', 0)}-{settings.get('conversation_delay_max', 0)} sec\n"
        f"Promotion Delay: {settings.get('promotion_delay_min', 0)}-{settings.get('promotion_delay_max', 0)} sec"
    )


def _bot_message_settings_keyboard(bot_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Promotion Mode", callback_data=f"botmsg:mode:{bot_name}")],
            [InlineKeyboardButton("Conversation Sequence", callback_data=f"botmsg:sequence:{bot_name}")],
            [InlineKeyboardButton("No Response Timeout", callback_data=f"botmsg:timeout:{bot_name}")],
            [InlineKeyboardButton("Conversation Delay Min", callback_data=f"botmsg:conv_min:{bot_name}")],
            [InlineKeyboardButton("Conversation Delay Max", callback_data=f"botmsg:conv_max:{bot_name}")],
            [InlineKeyboardButton("Promotion Delay Min", callback_data=f"botmsg:promo_min:{bot_name}")],
            [InlineKeyboardButton("Promotion Delay Max", callback_data=f"botmsg:promo_max:{bot_name}")],
            [InlineKeyboardButton("Back", callback_data=f"bot:view:{bot_name}")],
        ]
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


def _state_name(state: int) -> str:
    state_names = {
        ADD_CONVERSATIONAL_MESSAGE: "ADD_CONVERSATIONAL_MESSAGE",
        ADD_BOT_USERNAME: "ADD_BOT_USERNAME",
        ADD_BOT_START_CMD: "ADD_BOT_START_CMD",
        ADD_BOT_STOP_CMD: "ADD_BOT_STOP_CMD",
        ADD_BOT_MATCH_TRIGGERS: "ADD_BOT_MATCH_TRIGGERS",
        ADD_BOT_SECURITY_TRIGGERS: "ADD_BOT_SECURITY_TRIGGERS",
        ADD_BOT_AFTER_MATCH_DELAY: "ADD_BOT_AFTER_MATCH_DELAY",
        ADD_BOT_AFTER_CHAT_DELAY: "ADD_BOT_AFTER_CHAT_DELAY",
        EDIT_BOT_START_CMD: "EDIT_BOT_START_CMD",
        EDIT_BOT_STOP_CMD: "EDIT_BOT_STOP_CMD",
        EDIT_BOT_MATCH_TRIGGERS: "EDIT_BOT_MATCH_TRIGGERS",
        EDIT_BOT_SECURITY_TRIGGERS: "EDIT_BOT_SECURITY_TRIGGERS",
        EDIT_BOT_AFTER_MATCH_DELAY: "EDIT_BOT_AFTER_MATCH_DELAY",
        EDIT_BOT_AFTER_CHAT_DELAY: "EDIT_BOT_AFTER_CHAT_DELAY",
        SET_BOT_PROMOTION_MODE: "SET_BOT_PROMOTION_MODE",
        SET_BOT_CONVERSATION_SEQUENCE: "SET_BOT_CONVERSATION_SEQUENCE",
        SET_BOT_NO_RESPONSE_TIMEOUT: "SET_BOT_NO_RESPONSE_TIMEOUT",
        SET_BOT_CONV_DELAY_MIN: "SET_BOT_CONV_DELAY_MIN",
        SET_BOT_CONV_DELAY_MAX: "SET_BOT_CONV_DELAY_MAX",
        SET_BOT_PROMO_DELAY_MIN: "SET_BOT_PROMO_DELAY_MIN",
        SET_BOT_PROMO_DELAY_MAX: "SET_BOT_PROMO_DELAY_MAX",
        SET_GROUP_MESSAGE: "SET_GROUP_MESSAGE",
        EDIT_GROUP_NAME: "EDIT_GROUP_NAME",
        EDIT_GROUP_DELAY: "EDIT_GROUP_DELAY",
        EDIT_GROUP_TIME_START: "EDIT_GROUP_TIME_START",
        EDIT_GROUP_TIME_END: "EDIT_GROUP_TIME_END",
        SET_PROMOTION_ASSET_CHANNEL: "SET_PROMOTION_ASSET_CHANNEL",
        SET_PROMOTION_STICKER: "SET_PROMOTION_STICKER",
    }
    return state_names.get(state, f"STATE_{state}")


async def _audit_state_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    state: int,
    handler,
) -> int:
    state_label = _state_name(state)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    logger.info("ENTER STATE %s user_id=%s chat_id=%s", state_label, user_id, chat_id)
    try:
        user_message = getattr(getattr(update, "message", None), "text", None) or getattr(getattr(update, "message", None), "caption", None) or ""
        logger.info("RECEIVED USER MESSAGE %s text=%s", state_label, user_message)
        logger.info("HANDLER EXECUTED %s handler=%s", state_label, getattr(handler, "__name__", str(handler)))
        try:
            result = await handler(update, context)
        except Exception:
            logger.exception("MESSAGE_CONTENT_HANDLER_CRASH")
            raise
        logger.info("SAVE SUCCESS %s result=%s", state_label, result)
        logger.info("STATE TRANSITION state=%s next_state=%s", state_label, "END" if result == ConversationHandler.END else _state_name(result) if isinstance(result, int) else result)
        return result
    except Exception:
        logger.exception("ERROR state=%s user_id=%s chat_id=%s", state_label, user_id, chat_id)
        raise
    finally:
        logger.info("EXIT STATE %s", state_label)


def _audit_state_transition(state: int, next_state: int | None) -> None:
    logger.info("STATE TRANSITION state=%s next_state=%s", _state_name(state), "END" if next_state == ConversationHandler.END else _state_name(next_state) if next_state is not None else "None")


def _canonical_bot_config(bot_name: str, bot: dict) -> dict:
    existing = get_bots().get(bot_name, {})
    existing_enabled = bool(bot.get("enabled", existing.get("enabled", True)))
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


def _group_assigned_bot(group: dict | None) -> str | None:
    if not group:
        return None
    assigned_bot = group.get("assigned_bot") or group.get("bot_username")
    if assigned_bot:
        return str(assigned_bot).strip().lstrip("@") or None
    return None


def _save_bot_config(bot_name: str, bot: dict) -> dict:
    normalized = _canonical_bot_config(bot_name, bot)
    add_bot(bot_name, normalized)
    logger.warning("BOT SAVED: name=%s enabled=%s", bot_name, normalized.get("enabled"))
    set_bot_enabled(bot_name, normalized["enabled"])
    return normalized


def _fresh_bot(bot_name: str) -> dict | None:
    return get_bot(bot_name)


async def _render_bot_details(update: Update, bot_name: str) -> None:
    bot = _fresh_bot(bot_name)
    if not bot:
        await _send_or_edit(update, "Bot not found", InlineKeyboardMarkup(_bot_rows()))
        return
    await _send_or_edit(update, _bot_details_text(bot_name, bot), _bot_details_keyboard(bot_name, bool(bot.get("enabled", False))))


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
    active_bots = sum(1 for bot in get_bots().values() if bool(bot.get("enabled", False)))
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
    elif action == "menu:messages:conversational":
        await _send_or_edit(update, "Conversational Messages", InlineKeyboardMarkup(_category_rows("conversational_messages")))
    elif action == "menu:messages:bot":
        await _send_or_edit(update, "Bot Messages", InlineKeyboardMarkup(_category_rows("bot_messages")))
    elif action == "menu:messages:group":
        await _send_or_edit(update, "Group Messages", InlineKeyboardMarkup(_category_rows("group_messages")))
    elif action == "menu:messages:stickers":
        await _send_or_edit(update, "Promotion Stickers", InlineKeyboardMarkup(_category_rows("promotion_stickers")))


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


async def promotion_asset_channel_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, SET_PROMOTION_ASSET_CHANNEL, promotion_asset_channel_handler)


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


async def promotion_sticker_message_id_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, SET_PROMOTION_STICKER, promotion_sticker_message_id_handler)


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


async def refresh_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    new_enabled = not bool(bot.get("enabled", False))
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


async def bot_message_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    bot_name = query.data.split(":", 2)[2]
    await _send_or_edit(update, _bot_message_settings_text(bot_name), _bot_message_settings_keyboard(bot_name))


async def botmsg_mode_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    bot_name = update.callback_query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    context.user_data["state"] = SET_BOT_PROMOTION_MODE
    await _send_or_edit(update, "Send promotion mode: MESSAGE, STICKER, BOTH, RANDOM, or DISABLED.", _cancel_menu())
    logger.info("CALLBACK EXECUTED botmsg_mode_entry bot=%s returned_state=%s conversation_active=%s", bot_name, SET_BOT_PROMOTION_MODE, True)
    return SET_BOT_PROMOTION_MODE


async def botmsg_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = context.user_data.get("edit_bot_name")
    logger.info("TEXT RECEIVED botmsg_mode_handler bot=%s text=%s", bot_name, getattr(getattr(update, "message", None), "text", None))
    mode = update.message.text.strip().upper()
    if mode not in {"MESSAGE", "STICKER", "BOTH", "RANDOM", "DISABLED"}:
        await update.message.reply_text("Choose MESSAGE, STICKER, BOTH, RANDOM, or DISABLED.")
        return SET_BOT_PROMOTION_MODE
    settings = get_bot_settings(bot_name)
    settings["promotion_mode"] = mode
    settings.pop("bot_name", None)
    set_bot_settings(bot_name, **settings)
    context.user_data.clear()
    await update.message.reply_text(_bot_message_settings_text(bot_name), reply_markup=_bot_message_settings_keyboard(bot_name))
    logger.info("SAVE SUCCESS botmsg_mode_handler bot=%s field=promotion_mode state=%s", bot_name, ConversationHandler.END)
    return ConversationHandler.END


async def botmsg_value_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str, prompt: str, state: int) -> int:
    await _safe_callback_answer(update.callback_query)
    bot_name = update.callback_query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data["edit_bot_name"] = bot_name
    context.user_data["botmsg_field"] = field
    context.user_data["state"] = state
    await _send_or_edit(update, prompt, _cancel_menu())
    logger.info("CALLBACK EXECUTED botmsg_value_entry bot=%s field=%s returned_state=%s conversation_active=%s", bot_name, field, state, True)
    return state


async def botmsg_sequence_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_value_entry(update, context, "conversation_sequence", "Send comma-separated conversational message IDs, e.g. 1,2,3", SET_BOT_CONVERSATION_SEQUENCE)


async def botmsg_timeout_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_value_entry(update, context, "no_response_timeout", "Send no-response timeout in seconds.", SET_BOT_NO_RESPONSE_TIMEOUT)


async def botmsg_conv_min_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_value_entry(update, context, "conversation_delay_min", "Send conversation delay minimum in seconds.", SET_BOT_CONV_DELAY_MIN)


async def botmsg_conv_max_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_value_entry(update, context, "conversation_delay_max", "Send conversation delay maximum in seconds.", SET_BOT_CONV_DELAY_MAX)


async def botmsg_promo_min_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_value_entry(update, context, "promotion_delay_min", "Send promotion delay minimum in seconds.", SET_BOT_PROMO_DELAY_MIN)


async def botmsg_promo_max_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_value_entry(update, context, "promotion_delay_max", "Send promotion delay maximum in seconds.", SET_BOT_PROMO_DELAY_MAX)


async def botmsg_generic_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    bot_name = context.user_data.get("edit_bot_name")
    field = context.user_data.get("botmsg_field")
    if not bot_name or not field:
        return ConversationHandler.END
    logger.info("TEXT RECEIVED botmsg_generic_value_handler bot=%s field=%s text=%s", bot_name, field, getattr(getattr(update, "message", None), "text", None))
    raw = update.message.text.strip()
    if field == "conversation_sequence":
        try:
            value = [int(item.strip()) for item in raw.split(",") if item.strip()]
        except ValueError:
            await update.message.reply_text("Use comma-separated numeric IDs.")
            return context.user_data.get("state", ConversationHandler.END)
    else:
        try:
            value = int(raw)
        except ValueError:
            await update.message.reply_text("Send a whole number.")
            return context.user_data.get("state", ConversationHandler.END)
    settings = get_bot_settings(bot_name)
    settings[field] = value
    settings.pop("bot_name", None)
    set_bot_settings(bot_name, **settings)
    context.user_data.clear()
    await update.message.reply_text(_bot_message_settings_text(bot_name), reply_markup=_bot_message_settings_keyboard(bot_name))
    logger.info("SAVE SUCCESS botmsg_generic_value_handler bot=%s field=%s state=%s", bot_name, field, ConversationHandler.END)
    return ConversationHandler.END


async def botmsg_generic_value_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = int(context.user_data.get("state", ConversationHandler.END))
    logger.info("TEXT RECEIVED FOR STATE %s", state)
    result = await _audit_state_handler(update, context, state, botmsg_generic_value_handler)
    logger.info("STATE EXIT state=%s next_state=%s", state, "END" if result == ConversationHandler.END else result)
    return result


async def _set_bot_setting_value(update: Update, context: ContextTypes.DEFAULT_TYPE, key: str, field: str, cast=str) -> int:
    bot_name = context.user_data.get("edit_bot_name")
    if not bot_name:
        return ConversationHandler.END
    try:
        value = cast(update.message.text.strip())
    except Exception:
        await update.message.reply_text("Invalid value.")
        return context.user_data.get("state", ConversationHandler.END)
    settings = get_bot_settings(bot_name)
    settings[field] = value
    settings.pop("bot_name", None)
    set_bot_settings(bot_name, **settings)
    context.user_data.clear()
    await update.message.reply_text(_bot_message_settings_text(bot_name), reply_markup=_bot_message_settings_keyboard(bot_name))
    return ConversationHandler.END


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
    enabled = existing.get("enabled", True) if context.user_data.get("bot_flow_mode") == "edit" else True
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
        reply_markup=_bot_details_keyboard(bot_name, bool(bot.get("enabled", False))),
    )
    return ConversationHandler.END


async def bot_username_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_BOT_USERNAME, bot_username_handler)


async def bot_start_cmd_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_BOT_START_CMD, bot_start_cmd_handler)


async def bot_stop_cmd_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_BOT_STOP_CMD, bot_stop_cmd_handler)


async def bot_match_triggers_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_BOT_MATCH_TRIGGERS, bot_match_triggers_handler)


async def bot_security_triggers_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_BOT_SECURITY_TRIGGERS, bot_security_triggers_handler)


async def bot_after_match_delay_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_BOT_AFTER_MATCH_DELAY, bot_after_match_delay_handler)


async def bot_after_chat_delay_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_BOT_AFTER_CHAT_DELAY, bot_after_chat_delay_handler)


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


async def botmsg_mode_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, SET_BOT_PROMOTION_MODE, botmsg_mode_handler)


async def bot_settings_start_cmd_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_BOT_START_CMD, bot_settings_start_cmd_handler)


async def bot_settings_stop_cmd_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_BOT_STOP_CMD, bot_settings_stop_cmd_handler)


async def bot_settings_match_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_BOT_MATCH_TRIGGERS, bot_settings_match_handler)


async def bot_settings_security_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_BOT_SECURITY_TRIGGERS, bot_settings_security_handler)


async def bot_settings_after_match_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_BOT_AFTER_MATCH_DELAY, bot_settings_after_match_handler)


async def bot_settings_after_chat_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_BOT_AFTER_CHAT_DELAY, bot_settings_after_chat_handler)


async def botmsg_timeout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_generic_value_handler(update, context)


async def botmsg_conv_min_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_generic_value_handler(update, context)


async def botmsg_conv_max_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_generic_value_handler(update, context)


async def botmsg_promo_min_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_generic_value_handler(update, context)


async def botmsg_promo_max_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await botmsg_generic_value_handler(update, context)


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
    set_group_status(group_id, new_status)
    if new_status == "enabled":
        automation_service.start()
        set_setting("automation_running", True)
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


async def group_edit_name_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_GROUP_NAME, group_edit_name_handler)


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


async def group_edit_delay_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_GROUP_DELAY, group_edit_delay_handler)


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


async def group_time_start_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_GROUP_TIME_START, group_time_start_handler)


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


async def group_time_end_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, EDIT_GROUP_TIME_END, group_time_end_handler)


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


async def group_set_message_handler_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, SET_GROUP_MESSAGE, group_set_message_handler)


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


async def add_group_chat_id_audited(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _audit_state_handler(update, context, ADD_GROUP_CHAT_ID, add_group_chat_id)


def set_group_assigned_bot(group_id: str, bot_name: str | None) -> bool:
    group = get_group(group_id) or {}
    group["assigned_bot"] = bot_name
    group["bot_username"] = bot_name
    return bool(
        __import__("storage.db", fromlist=["update_group_runtime"])._groups_collection().update_one(  # type: ignore[attr-defined]
            {"_id": str(group_id)},
            {"$set": {"assigned_bot": bot_name, "bot_username": bot_name, "updated_at": __import__("datetime").datetime.utcnow()}},
        )
    )


async def _message_list_callback(update: Update, category: str) -> None:
    await _send_or_edit(update, _category_label(category), InlineKeyboardMarkup(_category_rows(category)))


async def _message_view_callback(update: Update, category: str, message_id: int) -> None:
    message = get_category_message(category, message_id)
    if not message:
        await _message_list_callback(update, category)
        return
    text = f"Message #{message['id']}\n\n{message.get('content', '') or message.get('media_file_id', '')}"
    edit_callback = f"msg:{category}:edit:{message_id}"
    if category == "conversational_messages":
        edit_callback = f"msg:conversational:edit:{message_id}"
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Edit", callback_data=edit_callback)],
            [InlineKeyboardButton("Delete", callback_data=f"msg:{category}:delete:{message_id}")],
            [InlineKeyboardButton("Back", callback_data=f"menu:messages:{'stickers' if category == 'promotion_stickers' else category.split('_')[0]}")],
        ]
    )
    await _send_or_edit(update, text, keyboard)


async def _message_add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str) -> int:
    await _safe_callback_answer(update.callback_query)
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    context.user_data.clear()
    context.user_data["message_category"] = category
    context.user_data["message_action"] = "add"
    logger.warning(
        "MESSAGE ADD ENTRY user_id=%s chat_id=%s category=%s current_state=%s",
        user_id,
        chat_id,
        category,
        ADD_CONVERSATIONAL_MESSAGE,
    )
    await _send_or_edit(
        update,
        "Send message in this format:\n\n<delay_minutes>|<message>\n\nExample:\n5|Hello everyone",
        _cancel_menu(),
    )
    logger.warning(
        "MESSAGE ADD ENTRY RETURN user_id=%s chat_id=%s category=%s returned_state=%s",
        user_id,
        chat_id,
        category,
        ADD_CONVERSATIONAL_MESSAGE,
    )
    return ADD_CONVERSATIONAL_MESSAGE


def _parse_conversational_message_payload(value: str) -> tuple[int, str]:
    raw = value.strip()
    if "|" not in raw:
        raise ValueError("Use <delay_minutes>|<message>")
    delay_text, content = raw.split("|", 1)
    delay_text = delay_text.strip()
    content = content.strip()
    if not delay_text or not delay_text.isdigit():
        raise ValueError("Delay minutes must be a whole number.")
    delay_minutes = int(delay_text)
    if delay_minutes < 1:
        raise ValueError("Delay minutes must be greater than 0.")
    if not content:
        raise ValueError("Message content cannot be empty.")
    return delay_minutes, content


async def conversational_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await _safe_callback_answer(query)
    parts = query.data.split(":")
    action = parts[2]
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    context.user_data.clear()
    context.user_data["message_category"] = "conversational_messages"
    context.user_data["message_action"] = action
    if action == "edit":
        message_id = int(parts[3])
        message = get_category_message("conversational_messages", message_id)
        if not message:
            await _send_or_edit(update, "Message not found.", InlineKeyboardMarkup(_category_rows("conversational_messages")))
            return ConversationHandler.END
        context.user_data["message_id"] = message_id
        current_value = f"{int(message.get('delay_minutes', 0) or 0)}|{message.get('content', '') or ''}"
        await _send_or_edit(
            update,
            "Send replacement value in format:\n\n<delay_minutes>|<message>\n\nCurrent value:\n"
            f"{current_value}",
            _cancel_menu(),
        )
    else:
        await _send_or_edit(
            update,
            "Send conversational message in this format:\n\n<delay_minutes>|<message>\n\nExample:\n5|Hello everyone",
            _cancel_menu(),
        )
    logger.warning(
        "CONVERSATIONAL MESSAGE ENTRY user_id=%s chat_id=%s action=%s returned_state=%s",
        user_id,
        chat_id,
        action,
        ADD_CONVERSATIONAL_MESSAGE,
    )
    return ADD_CONVERSATIONAL_MESSAGE


async def add_conversational_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not _is_allowed(update):
        return ConversationHandler.END
    message = update.message
    user_id = getattr(getattr(update, "effective_user", None), "id", None)
    chat_id = getattr(getattr(update, "effective_chat", None), "id", None)
    raw_text = (message.text or message.caption or "").strip()
    logger.warning(
        "CONVERSATIONAL MESSAGE INPUT user_id=%s chat_id=%s raw_text=%s user_data=%s",
        user_id,
        chat_id,
        raw_text,
        dict(context.user_data),
    )
    try:
        delay_minutes, content = _parse_conversational_message_payload(raw_text)
    except ValueError as exc:
        await message.reply_text(str(exc))
        return ADD_CONVERSATIONAL_MESSAGE

    action = context.user_data.get("message_action")
    category = context.user_data.get("message_category", "conversational_messages")
    try:
        if action == "edit":
            message_id = int(context.user_data["message_id"])
            update_category_message(
                category,
                message_id,
                content=content,
                delay_minutes=delay_minutes,
                media_type=None,
                media_file_id=None,
            )
        else:
            add_category_message(category, content=content, delay_minutes=delay_minutes)
    except Exception:
        logger.exception(
            "CONVERSATIONAL MESSAGE SAVE FAILED user_id=%s chat_id=%s action=%s",
            user_id,
            chat_id,
            action,
        )
        await message.reply_text("Failed to save the message. Please try again.")
        return ConversationHandler.END

    context.user_data.clear()
    await message.reply_text("Message saved successfully.")
    return ConversationHandler.END


async def messages_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)
    await _message_list_callback(update, "bot_messages")


async def message_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    parts = query.data.split(":")
    category = parts[1]
    message_id = int(parts[3]) if len(parts) > 3 else int(parts[-1])
    await _message_view_callback(update, category, message_id)


async def message_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    parts = query.data.split(":")
    category = parts[1]
    action = parts[2]
    message_id = int(parts[3])
    enabled = action == "enable"
    set_category_message_enabled(category, message_id, enabled)
    await _send_or_edit(update, "Updated.", InlineKeyboardMarkup(_category_rows(category)))


async def add_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    state = await _message_add_entry(update, context, "bot_messages")
    logger.warning("ADD MESSAGE ENTRY RETURNED STATE=%s", state)
    return state


async def delete_message_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await _safe_callback_answer(update.callback_query)
    await _send_or_edit(update, "Choose a message to delete", InlineKeyboardMarkup(_category_rows("bot_messages")))
    return ConversationHandler.END


async def delete_one_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await _safe_callback_answer(query)
    parts = query.data.split(":")
    category = parts[1]
    message_id = int(parts[3])
    delete_category_message(category, message_id)
    await _send_or_edit(update, "Message deleted", InlineKeyboardMarkup(_category_rows(category)))


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
    bot = get_bots().get(bot_name)
    start_cmd = _normalize_command(bot.get("start_cmd")) if bot else None
    if bot and bool(bot.get("enabled", False)) and start_cmd:
        try:
            await telegram_service.client.send_message(bot_name, start_cmd)
        except Exception:
            logger.exception("Failed to resume bot %s after bypass", bot_name)
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


def _conversation_state_map() -> list[tuple[int, str]]:
    return [
        (ADD_GROUP_CHAT_ID, "add_group_chat_id_audited"),
        (ADD_CONVERSATIONAL_MESSAGE, "add_conversational_message_handler"),
        (ADD_BOT_USERNAME, "bot_username_handler_audited"),
        (ADD_BOT_START_CMD, "bot_start_cmd_handler_audited"),
        (ADD_BOT_STOP_CMD, "bot_stop_cmd_handler_audited"),
        (ADD_BOT_MATCH_TRIGGERS, "bot_match_triggers_handler_audited"),
        (ADD_BOT_SECURITY_TRIGGERS, "bot_security_triggers_handler_audited"),
        (ADD_BOT_AFTER_MATCH_DELAY, "bot_after_match_delay_handler_audited"),
        (ADD_BOT_AFTER_CHAT_DELAY, "bot_after_chat_delay_handler_audited"),
        (EDIT_BOT_START_CMD, "bot_settings_start_cmd_handler_audited"),
        (EDIT_BOT_STOP_CMD, "bot_settings_stop_cmd_handler_audited"),
        (EDIT_BOT_MATCH_TRIGGERS, "bot_settings_match_handler_audited"),
        (EDIT_BOT_SECURITY_TRIGGERS, "bot_settings_security_handler_audited"),
        (EDIT_BOT_AFTER_MATCH_DELAY, "bot_settings_after_match_handler_audited"),
        (EDIT_BOT_AFTER_CHAT_DELAY, "bot_settings_after_chat_handler_audited"),
        (SET_BOT_PROMOTION_MODE, "botmsg_mode_handler_audited"),
        (SET_BOT_CONVERSATION_SEQUENCE, "botmsg_generic_value_handler_audited"),
        (SET_BOT_NO_RESPONSE_TIMEOUT, "botmsg_generic_value_handler_audited"),
        (SET_BOT_CONV_DELAY_MIN, "botmsg_generic_value_handler_audited"),
        (SET_BOT_CONV_DELAY_MAX, "botmsg_generic_value_handler_audited"),
        (SET_BOT_PROMO_DELAY_MIN, "botmsg_generic_value_handler_audited"),
        (SET_BOT_PROMO_DELAY_MAX, "botmsg_generic_value_handler_audited"),
        (EDIT_GROUP_NAME, "group_edit_name_handler_audited"),
        (EDIT_GROUP_DELAY, "group_edit_delay_handler_audited"),
        (SET_GROUP_MESSAGE, "group_set_message_handler_audited"),
        (EDIT_GROUP_TIME_START, "group_time_start_handler_audited"),
        (EDIT_GROUP_TIME_END, "group_time_end_handler_audited"),
        (SET_PROMOTION_ASSET_CHANNEL, "promotion_asset_channel_handler_audited"),
        (SET_PROMOTION_STICKER, "promotion_sticker_message_id_handler_audited"),
    ]


def _log_conversation_audit() -> None:
    state_map = _conversation_state_map()
    declared_state_ids = {
        ADD_GROUP_CHAT_ID,
        ADD_CONVERSATIONAL_MESSAGE,
        ADD_BOT_USERNAME,
        ADD_BOT_START_CMD,
        ADD_BOT_STOP_CMD,
        ADD_BOT_MATCH_TRIGGERS,
        ADD_BOT_SECURITY_TRIGGERS,
        ADD_BOT_AFTER_MATCH_DELAY,
        ADD_BOT_AFTER_CHAT_DELAY,
        EDIT_BOT_START_CMD,
        EDIT_BOT_STOP_CMD,
        EDIT_BOT_MATCH_TRIGGERS,
        EDIT_BOT_SECURITY_TRIGGERS,
        EDIT_BOT_AFTER_MATCH_DELAY,
        EDIT_BOT_AFTER_CHAT_DELAY,
        SET_PROMOTION_STICKER,
        SET_PROMOTION_ASSET_CHANNEL,
        TEST_PROMOTION_STICKER,
        SET_BOT_PROMOTION_MODE,
        SET_BOT_CONVERSATION_SEQUENCE,
        SET_BOT_NO_RESPONSE_TIMEOUT,
        SET_BOT_CONV_DELAY_MIN,
        SET_BOT_CONV_DELAY_MAX,
        SET_BOT_PROMO_DELAY_MIN,
        SET_BOT_PROMO_DELAY_MAX,
        EDIT_GROUP_NAME,
        EDIT_GROUP_DELAY,
        SET_GROUP_MESSAGE,
        EDIT_GROUP_TIME_START,
        EDIT_GROUP_TIME_END,
    }
    logger.info("CONVERSATION STATE MAP START")
    for state_id, handler_name in state_map:
        logger.info("STATE_ID=%s -> HANDLER_NAME=%s", state_id, handler_name)
    logger.info("CONVERSATION STATE MAP END count=%s", len(state_map))
    mapped_ids = {state for state, _ in state_map}
    orphaned_declared = sorted(declared_state_ids - mapped_ids)
    logger.info("DECLARED STATE CONSTANTS=%s", sorted(declared_state_ids))
    logger.info("ORPHANED STATE CONSTANTS=%s", orphaned_declared)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _safe_callback_answer(update.callback_query)


async def start_controller() -> None:
    global _application
    if not TOKEN:
        raise ValueError("CONTROL_BOT_TOKEN is required")
    print("[STARTUP] Building Telegram control application...")
    logger.info("[STARTUP] Building Telegram control application...")
    _application = Application.builder().token(TOKEN).build()
    _log_conversation_audit()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_group_entry, pattern="^group:add$"),
            CallbackQueryHandler(add_message_entry, pattern="^message:add$"),
            CallbackQueryHandler(add_bot_entry, pattern="^bot:add$"),
            CallbackQueryHandler(conversational_message_callback, pattern="^msg:conversational:add$|^msg:conversational:edit:"),
            CallbackQueryHandler(group_edit_name_entry, pattern="^group:edit_name:"),
            CallbackQueryHandler(group_edit_delay_entry, pattern="^group:edit_delay:"),
            CallbackQueryHandler(group_set_message_entry, pattern="^group:set_message:"),
            CallbackQueryHandler(group_time_start_entry, pattern="^group:time_start:"),
            CallbackQueryHandler(group_time_end_entry, pattern="^group:time_end:"),
            CallbackQueryHandler(botmsg_mode_entry, pattern="^botmsg:mode:"),
            CallbackQueryHandler(botmsg_sequence_entry, pattern="^botmsg:sequence:"),
            CallbackQueryHandler(botmsg_timeout_entry, pattern="^botmsg:timeout:"),
            CallbackQueryHandler(botmsg_conv_min_entry, pattern="^botmsg:conv_min:"),
            CallbackQueryHandler(botmsg_conv_max_entry, pattern="^botmsg:conv_max:"),
            CallbackQueryHandler(botmsg_promo_min_entry, pattern="^botmsg:promo_min:"),
            CallbackQueryHandler(botmsg_promo_max_entry, pattern="^botmsg:promo_max:"),
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
            ADD_GROUP_CHAT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_chat_id_audited)],
            ADD_CONVERSATIONAL_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_conversational_message_handler)
            ],
            ADD_BOT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_username_handler_audited)],
            ADD_BOT_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_start_cmd_handler_audited)],
            ADD_BOT_STOP_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_stop_cmd_handler_audited)],
            ADD_BOT_MATCH_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_match_triggers_handler_audited)],
            ADD_BOT_SECURITY_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_security_triggers_handler_audited)],
            ADD_BOT_AFTER_MATCH_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_after_match_delay_handler_audited)],
            ADD_BOT_AFTER_CHAT_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_after_chat_delay_handler_audited)],
            EDIT_BOT_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_start_cmd_handler_audited)],
            EDIT_BOT_STOP_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_stop_cmd_handler_audited)],
            EDIT_BOT_MATCH_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_match_handler_audited)],
            EDIT_BOT_SECURITY_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_security_handler_audited)],
            EDIT_BOT_AFTER_MATCH_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_after_match_handler_audited)],
            EDIT_BOT_AFTER_CHAT_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot_settings_after_chat_handler_audited)],
            SET_BOT_PROMOTION_MODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, botmsg_mode_handler_audited)],
            SET_BOT_CONVERSATION_SEQUENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, botmsg_generic_value_handler_audited)],
            SET_BOT_NO_RESPONSE_TIMEOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, botmsg_generic_value_handler_audited)],
            SET_BOT_CONV_DELAY_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, botmsg_generic_value_handler_audited)],
            SET_BOT_CONV_DELAY_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, botmsg_generic_value_handler_audited)],
            SET_BOT_PROMO_DELAY_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, botmsg_generic_value_handler_audited)],
            SET_BOT_PROMO_DELAY_MAX: [MessageHandler(filters.TEXT & ~filters.COMMAND, botmsg_generic_value_handler_audited)],
            EDIT_GROUP_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_edit_name_handler_audited)],
            EDIT_GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_edit_delay_handler_audited)],
            SET_GROUP_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_set_message_handler_audited)],
            EDIT_GROUP_TIME_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_time_start_handler_audited)],
            EDIT_GROUP_TIME_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, group_time_end_handler_audited)],
            SET_PROMOTION_ASSET_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, promotion_asset_channel_handler_audited)],
            SET_PROMOTION_STICKER: [MessageHandler(filters.TEXT & ~filters.COMMAND, promotion_sticker_message_id_handler_audited)],
        },
        fallbacks=[
            CallbackQueryHandler(cancel_callback, pattern="^nav:cancel$"),
            CommandHandler("cancel", cancel_callback),
            CommandHandler("back", cancel_callback),
        ],
        per_chat=True,
        per_user=True,
        per_message=False,
        allow_reentry=False,
    )
    logger.info(
        "CONVERSATION HANDLER CONFIG per_chat=%s per_user=%s per_message=%s allow_reentry=%s fallbacks=%s",
        True,
        True,
        False,
        False,
        ["nav:cancel", "/cancel", "/back"],
    )
    logger.info(
        "HANDLER ORDER=%s",
        [
            "CommandHandler(start)",
            "ConversationHandler",
            "CallbackQueryHandler(menu:)",
            "CallbackQueryHandler(bot:list)",
            "CallbackQueryHandler(bot:view:)",
            "CallbackQueryHandler(bot:refresh:)",
            "CallbackQueryHandler(bot:toggle:)",
            "CallbackQueryHandler(bot:edit:)",
            "CallbackQueryHandler(bot:bypass:)",
            "CallbackQueryHandler(bot:delete:)",
            "CallbackQueryHandler(botmsg:view:)",
            "CallbackQueryHandler(botmsg:mode:)",
            "CallbackQueryHandler(botmsg:sequence:)",
            "CallbackQueryHandler(botmsg:timeout:)",
            "CallbackQueryHandler(botmsg:conv_min:)",
            "CallbackQueryHandler(botmsg:conv_max:)",
            "CallbackQueryHandler(botmsg:promo_min:)",
            "CallbackQueryHandler(botmsg:promo_max:)",
            "CallbackQueryHandler(group:list)",
            "CallbackQueryHandler(group:view:)",
            "CallbackQueryHandler(group:toggle:)",
            "CallbackQueryHandler(group:time_window:)",
            "CallbackQueryHandler(group:time_clear:)",
            "CallbackQueryHandler(group:clear_message:)",
            "CallbackQueryHandler(group:delete:)",
            "CallbackQueryHandler(message:list)",
            "CallbackQueryHandler(msg:view:)",
            "CallbackQueryHandler(msg:enable/disable)",
            "CallbackQueryHandler(message:delete)",
            "CallbackQueryHandler(msg:delete:)",
            "CallbackQueryHandler(automation:start/stop/pause/resume)",
            "CallbackQueryHandler(msg:conversational:add/edit)",
            "CallbackQueryHandler(noop)",
        ],
    )
    registered_states = sorted(conv_handler.states.keys())
    mapped_states = {state for state, _ in _conversation_state_map()}
    missing_in_map = sorted(set(registered_states) - mapped_states)
    orphaned_map = sorted(mapped_states - set(registered_states))
    logger.info("REGISTERED CONVERSATION STATES=%s", registered_states)
    logger.info("STATE MAP CHECK missing_in_map=%s orphaned_map=%s", missing_in_map, orphaned_map)
    if missing_in_map or orphaned_map:
        logger.warning("CONVERSATION HANDLER AUDIT MISMATCH missing_in_map=%s orphaned_map=%s", missing_in_map, orphaned_map)

    _application.add_handler(CommandHandler("start", start_command))
    _application.add_handler(conv_handler)
    _application.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu:"))
    _application.add_handler(CallbackQueryHandler(list_bots_callback, pattern="^bot:list$"))
    _application.add_handler(CallbackQueryHandler(view_bot_callback, pattern="^bot:view:"))
    _application.add_handler(CallbackQueryHandler(refresh_bot_callback, pattern="^bot:refresh:"))
    _application.add_handler(CallbackQueryHandler(toggle_bot_callback, pattern="^bot:toggle:"))
    _application.add_handler(CallbackQueryHandler(edit_bot_callback, pattern="^bot:edit:"))
    _application.add_handler(CallbackQueryHandler(bypass_bot_callback, pattern="^bot:bypass:"))
    _application.add_handler(CallbackQueryHandler(delete_bot_callback, pattern="^bot:delete:"))
    _application.add_handler(CallbackQueryHandler(bot_message_settings_callback, pattern="^botmsg:view:"))
    _application.add_handler(CallbackQueryHandler(list_groups_callback, pattern="^group:list$"))
    _application.add_handler(CallbackQueryHandler(view_group_callback, pattern="^group:view:"))
    _application.add_handler(CallbackQueryHandler(toggle_group_callback, pattern="^group:toggle:"))
    _application.add_handler(CallbackQueryHandler(group_time_window_menu, pattern="^group:time_window:"))
    _application.add_handler(CallbackQueryHandler(group_time_clear_callback, pattern="^group:time_clear:"))
    _application.add_handler(CallbackQueryHandler(clear_group_message_callback, pattern="^group:clear_message:"))
    _application.add_handler(CallbackQueryHandler(delete_group_callback, pattern="^group:delete:"))
    _application.add_handler(CallbackQueryHandler(messages_list_callback, pattern="^message:list$"))
    _application.add_handler(CallbackQueryHandler(message_view_callback, pattern="^msg:[a-z_]+:view:"))
    _application.add_handler(CallbackQueryHandler(message_toggle_callback, pattern="^msg:[a-z_]+:(enable|disable):"))
    _application.add_handler(CallbackQueryHandler(delete_message_menu, pattern="^message:delete$"))
    _application.add_handler(CallbackQueryHandler(delete_one_message, pattern="^msg:[a-z_]+:delete:"))
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


