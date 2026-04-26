import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import utils as telethon_utils
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
    is_bot_enabled,
    is_bot_paused,
    list_groups,
    list_messages,
    set_group_special_message,
    set_bot_enabled,
    set_bot_paused,
    set_group_status,
    set_setting,
    update_group_delay,
    update_group_name,
)

load_dotenv()

logger = logging.getLogger(__name__)

ADD_GROUP_CHAT_ID = 100
ADD_MESSAGE_CONTENT = 200
ADD_MESSAGE_DELAY = 201
DELETE_MESSAGE_PICK = 300
EDIT_GROUP_NAME = 400
EDIT_GROUP_DELAY = 401
SET_GROUP_MESSAGE = 402
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


def _automation_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Stop Automation", callback_data="automation:stop")],
            [InlineKeyboardButton("Pause", callback_data="automation:pause")],
            [InlineKeyboardButton("Resume", callback_data="automation:resume")],
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
    delay_min = group.get("delay_min", 4)
    delay_max = group.get("delay_max", 7)
    return (
        "Group Details\n\n"
        f"Name: {group_name}\n"
        f"Group ID: {group.get('group_id', 'N/A')}\n"
        f"Status: {status}\n"
        f"Delay Range: {delay_min}-{delay_max} min\n"
        f"Special Message: {special_message}\n"
        f"Last Status: {last_status}\n"
        f"Last Error: {last_error}"
    )


def _group_details_keyboard(group_id: str, enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Disable" if enabled else "Enable", callback_data=f"group:toggle:{group_id}")],
            [
                InlineKeyboardButton("Edit Name", callback_data=f"group:edit_name:{group_id}"),
                InlineKeyboardButton("Edit Delay", callback_data=f"group:edit_delay:{group_id}"),
            ],
            [InlineKeyboardButton("Set Message", callback_data=f"group:set_message:{group_id}")],
            [InlineKeyboardButton("Clear Message", callback_data=f"group:clear_message:{group_id}")],
            [InlineKeyboardButton("Delete Group", callback_data=f"group:delete:{group_id}")],
            [InlineKeyboardButton("Back to List", callback_data="group:list")],
        ]
    )


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
    return (
        f"Bot: {bot_name}\n"
        f"Status: {'ON' if enabled else 'OFF'}\n"
        f"Runtime state: {runtime_state}\n"
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
    return (
        f"Automation is {'running' if state == 'RUNNING' else 'paused' if state == 'PAUSED' else 'idle'}\n"
        f"Runtime state: {state}\n"
        f"Enabled groups: {enabled_groups}\n"
        f"Active messages: {messages_count}\n"
        f"Active bot count: {active_bots}\n"
        f"Last execution time: {last_execution_time}"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    context.user_data.clear()
    await _send_or_edit(update, "Telegram automation control panel", _main_menu())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await query.answer()
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


async def list_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await _send_or_edit(update, "Saved groups", InlineKeyboardMarkup(_group_rows()))


async def list_bots_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    await _send_or_edit(update, "Saved bots", InlineKeyboardMarkup(_bot_rows()))


async def view_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    bot_name = query.data.split(":", 2)[2]
    await _render_bot_details(update, bot_name)


async def toggle_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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


async def delete_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    bot_name = query.data.split(":", 2)[2]
    delete_bot(bot_name)
    await _send_or_edit(update, "Bot deleted", InlineKeyboardMarkup(_bot_rows()))


async def edit_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    bot_name = query.data.split(":", 2)[2]
    bot = get_bots().get(bot_name)
    if not bot:
        await _send_or_edit(update, "Bot not found", InlineKeyboardMarkup(_bot_rows()))
        return
    context.user_data.clear()
    await _render_bot_settings(update, bot_name)


async def add_bot_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
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
    await query.answer()
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
    await query.answer()
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
    await query.answer()
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
    await query.answer()
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
    await query.answer()
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
    await query.answer()
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
    await query.answer()
    group_id = query.data.split(":", 2)[2]
    group = get_group(group_id)
    if not group:
        await _send_or_edit(update, "Group not found", InlineKeyboardMarkup(_group_rows()))
        return
    await _send_or_edit(
        update,
        _group_details_text(group),
        _group_details_keyboard(group_id, group["status"] == "enabled"),
    )


async def toggle_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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
    refreshed = get_group(group_id)
    await _send_or_edit(
        update,
        _group_details_text(refreshed),
        _group_details_keyboard(group_id, refreshed["status"] == "enabled"),
    )


async def delete_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = query.data.split(":", 2)[2]
    delete_group(group_id)
    await _send_or_edit(update, "Group deleted", InlineKeyboardMarkup(_group_rows()))


async def group_edit_name_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
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
    await query.answer()
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


async def group_set_message_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
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
    await query.answer()
    group_id = query.data.split(":", 2)[2]
    clear_group_special_message(group_id)
    group = get_group(group_id)
    await _send_or_edit(
        update,
        _group_details_text(group),
        _group_details_keyboard(group_id, group["status"] == "enabled"),
    )


async def add_group_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.answer()
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
    await update.callback_query.answer()
    await _send_or_edit(update, "Saved messages", InlineKeyboardMarkup(_message_rows()))


async def message_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
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
    await update.callback_query.answer()
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
    await update.callback_query.answer()
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
    await query.answer()
    message_id = int(query.data.split(":", 3)[3])
    delete_message(message_id)
    await _send_or_edit(update, "Message deleted", InlineKeyboardMarkup(_message_rows()))


async def automation_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    automation_service.start()
    set_setting("automation_running", True)
    set_setting("automation_paused", False)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    automation_service.stop()
    set_setting("automation_running", False)
    set_setting("automation_paused", False)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    automation_service.pause()
    set_setting("automation_running", True)
    set_setting("automation_paused", True)
    await _send_or_edit(update, _automation_status_text(), _automation_menu())


async def automation_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
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
    await query.answer()
    bot_name = query.data.split(":", 2)[2]
    set_bot_paused(bot_name, False)
    bot = get_bots().get(bot_name)
    start_cmd = _normalize_command(bot.get("start_cmd")) if bot else None
    if bot and is_bot_enabled(bot_name, False) and start_cmd:
        try:
            await telegram_service.client.send_message(bot_name, start_cmd)
        except Exception:
            logger.exception("Failed to resume bot %s after bypass", bot_name)
    await _send_or_edit(update, f"Resumed {bot_name}", _main_menu())


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await update.callback_query.answer()
    bot_name = context.user_data.get("edit_bot_name")
    group_id = context.user_data.get("edit_group_id")
    context.user_data.clear()
    if bot_name:
        await _render_bot_settings(update, bot_name)
        return ConversationHandler.END
    if group_id:
        group = get_group(group_id)
        if group:
            await _send_or_edit(
                update,
                _group_details_text(group),
                _group_details_keyboard(group_id, group["status"] == "enabled"),
            )
            return ConversationHandler.END
    await _send_or_edit(update, "Cancelled.", _main_menu())
    return ConversationHandler.END


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def start_controller() -> None:
    global _application
    if not TOKEN:
        raise ValueError("CONTROL_BOT_TOKEN is required")

    _application = Application.builder().token(TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_group_entry, pattern="^group:add$"),
            CallbackQueryHandler(add_message_entry, pattern="^message:add$"),
            CallbackQueryHandler(add_bot_entry, pattern="^bot:add$"),
            CallbackQueryHandler(group_edit_name_entry, pattern="^group:edit_name:"),
            CallbackQueryHandler(group_edit_delay_entry, pattern="^group:edit_delay:"),
            CallbackQueryHandler(group_set_message_entry, pattern="^group:set_message:"),
            CallbackQueryHandler(bot_settings_start_cmd_entry, pattern="^botcfg:start:"),
            CallbackQueryHandler(bot_settings_stop_cmd_entry, pattern="^botcfg:stop:"),
            CallbackQueryHandler(bot_settings_match_entry, pattern="^botcfg:match:"),
            CallbackQueryHandler(bot_settings_security_entry, pattern="^botcfg:security:"),
            CallbackQueryHandler(bot_settings_after_match_entry, pattern="^botcfg:after_match:"),
            CallbackQueryHandler(bot_settings_after_chat_entry, pattern="^botcfg:after_chat:"),
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
    _application.add_handler(CallbackQueryHandler(list_bots_callback, pattern="^bot:list$"))
    _application.add_handler(CallbackQueryHandler(view_bot_callback, pattern="^bot:view:"))
    _application.add_handler(CallbackQueryHandler(toggle_bot_callback, pattern="^bot:toggle:"))
    _application.add_handler(CallbackQueryHandler(edit_bot_callback, pattern="^bot:edit:"))
    _application.add_handler(CallbackQueryHandler(bypass_bot_callback, pattern="^bot:bypass:"))
    _application.add_handler(CallbackQueryHandler(delete_bot_callback, pattern="^bot:delete:"))
    _application.add_handler(CallbackQueryHandler(list_groups_callback, pattern="^group:list$"))
    _application.add_handler(CallbackQueryHandler(view_group_callback, pattern="^group:view:"))
    _application.add_handler(CallbackQueryHandler(toggle_group_callback, pattern="^group:toggle:"))
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

    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling()

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await _application.updater.stop()
        await _application.stop()
        await _application.shutdown()
