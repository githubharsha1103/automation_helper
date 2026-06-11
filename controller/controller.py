import logging
import os
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from storage.db import (
    add_bot,
    add_group,
    add_message,
    delete_bot,
    delete_group,
    delete_message,
    get_bot,
    get_bot_settings,
    get_bots,
    get_group,
    get_message,
    get_setting,
    is_bot_enabled,
    is_bot_paused,
    list_groups,
    list_messages,
    set_bot_enabled,
    set_bot_paused,
    set_bot_settings,
    set_group_status,
    set_setting,
    update_group_delay,
    update_group_name,
)

load_dotenv()

logger = logging.getLogger(__name__)

TOKEN = os.getenv("CONTROL_BOT_TOKEN") or os.getenv("control_bot_token") or ""
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0") or "0")

WAITING_FOR_VALUE = "waiting_for_value"
_application: Application | None = None


def _is_allowed(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    user = update.effective_user
    return bool(user and user.id == ALLOWED_USER_ID)


async def _safe_answer(query) -> None:
    try:
        await query.answer()
    except BadRequest:
        return


async def _send(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await update.callback_query.message.reply_text(text, reply_markup=markup)
        return
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)


def _main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Bots", callback_data="nav:bots:main")],
            [InlineKeyboardButton("Groups", callback_data="nav:groups:main")],
            [InlineKeyboardButton("Messages", callback_data="nav:messages:main")],
            [InlineKeyboardButton("Promotion", callback_data="nav:promotion:main")],
            [InlineKeyboardButton("Settings", callback_data="nav:settings:main")],
        ]
    )


def _bots_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("List Bots", callback_data="bot:list")],
            [InlineKeyboardButton("Add Bot", callback_data="bot:add:bots_menu")],
            [InlineKeyboardButton("Back", callback_data="nav:main:bots")],
        ]
    )


def _groups_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("List Groups", callback_data="group:list")],
            [InlineKeyboardButton("Add Group", callback_data="group:add:groups_menu")],
            [InlineKeyboardButton("Back", callback_data="nav:main:groups")],
        ]
    )


def _messages_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("List Messages", callback_data="message:list")],
            [InlineKeyboardButton("Add Message", callback_data="message:add:messages_menu")],
            [InlineKeyboardButton("Back", callback_data="nav:main:messages")],
        ]
    )


def _promotion_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Promotion Settings", callback_data="promotion:settings")],
            [InlineKeyboardButton("Back", callback_data="nav:main:promotion")],
        ]
    )


def _settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="nav:main:settings")]])


def _bot_detail_menu(bot_name: str) -> InlineKeyboardMarkup:
    enabled = is_bot_enabled(bot_name, True)
    paused = is_bot_paused(bot_name, False)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Disable" if enabled else "Enable", callback_data=f"bot:toggle:{bot_name}"),
                InlineKeyboardButton("Pause" if not paused else "Resume", callback_data=f"bot:pause:{bot_name}"),
            ],
            [InlineKeyboardButton("Settings", callback_data=f"bot:settings:{bot_name}")],
            [InlineKeyboardButton("Delete", callback_data=f"bot:delete:{bot_name}")],
            [InlineKeyboardButton("Back", callback_data=f"nav:bot_list:{bot_name}")],
        ]
    )


def _group_detail_menu(group_id: str) -> InlineKeyboardMarkup:
    group = get_group(group_id) or {}
    enabled = group.get("status") == "enabled"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Disable" if enabled else "Enable", callback_data=f"group:toggle:{group_id}"),
                InlineKeyboardButton("Edit", callback_data=f"group:edit:{group_id}"),
            ],
            [InlineKeyboardButton("Delete", callback_data=f"group:delete:{group_id}")],
            [InlineKeyboardButton("Back", callback_data=f"nav:group_list:{group_id}")],
        ]
    )


def _message_detail_menu(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Delete", callback_data=f"message:delete:{message_id}")],
            [InlineKeyboardButton("Back", callback_data=f"nav:message_list:{message_id}")],
        ]
    )


def _promotion_settings_menu() -> InlineKeyboardMarkup:
    mode = str(get_setting("promotion_mode", "message") or "message").lower()
    mode_label = {"message": "Message", "sticker": "Sticker", "both": "Message + Sticker"}.get(mode, "Message")
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"Mode: {mode_label}", callback_data="promotion:mode")],
            [InlineKeyboardButton("Set Asset Channel", callback_data="promotion:asset_channel")],
            [InlineKeyboardButton("Set Sticker Message ID", callback_data="promotion:sticker_id")],
            [InlineKeyboardButton("Back", callback_data="nav:promotion:settings")],
        ]
    )


def _bot_settings_menu(bot_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("No Response Timeout", callback_data=f"setting:bot:{bot_name}:no_response_timeout")],
            [InlineKeyboardButton("Conversation Sequence", callback_data=f"setting:bot:{bot_name}:conversation_sequence")],
            [InlineKeyboardButton("Conversation Delay Min", callback_data=f"setting:bot:{bot_name}:conversation_delay_min")],
            [InlineKeyboardButton("Conversation Delay Max", callback_data=f"setting:bot:{bot_name}:conversation_delay_max")],
            [InlineKeyboardButton("Promotion Delay Min", callback_data=f"setting:bot:{bot_name}:promotion_delay_min")],
            [InlineKeyboardButton("Promotion Delay Max", callback_data=f"setting:bot:{bot_name}:promotion_delay_max")],
            [InlineKeyboardButton("Promotion Mode", callback_data=f"setting:bot:{bot_name}:promotion_mode")],
            [InlineKeyboardButton("Back", callback_data=f"nav:bot_view:{bot_name}")],
        ]
    )


def _format_bot(bot_name: str, bot: dict[str, Any]) -> str:
    settings = get_bot_settings(bot_name)
    return (
        f"Bot Details\n\n"
        f"Name: {bot_name}\n"
        f"Enabled: {'Yes' if bot.get('enabled', True) else 'No'}\n"
        f"Paused: {'Yes' if bot.get('paused', False) else 'No'}\n"
        f"Promotion Mode: {settings.get('promotion_mode', 'MESSAGE')}\n"
        f"No Response Timeout: {settings.get('no_response_timeout', 0)}\n"
        f"Conversation Sequence: {settings.get('conversation_sequence', [])}\n"
        f"Conversation Delay: {settings.get('conversation_delay_min', 0)}-{settings.get('conversation_delay_max', 0)}\n"
        f"Promotion Delay: {settings.get('promotion_delay_min', 0)}-{settings.get('promotion_delay_max', 0)}"
    )


def _format_group(group: dict[str, Any]) -> str:
    return (
        f"Group Details\n\n"
        f"Group ID: {group.get('group_id')}\n"
        f"Group Name: {group.get('group_name')}\n"
        f"Status: {group.get('status', 'enabled')}\n"
        f"Delay: {group.get('delay_min', 4)}-{group.get('delay_max', 7)}\n"
        f"Special Message: {group.get('special_message') or 'None'}"
    )


def _format_message(message: dict[str, Any]) -> str:
    return (
        f"Message Details\n\n"
        f"ID: {message.get('id')}\n"
        f"Active: {'Yes' if message.get('is_active', True) else 'No'}\n"
        f"Delay Minutes: {message.get('delay_minutes', 0)}\n"
        f"Content: {message.get('content') or 'N/A'}"
    )


def _validate_setting_value(setting: str, raw_value: str) -> tuple[bool, Any, str | None]:
    raw = raw_value.strip()
    if setting == "conversation_sequence":
        try:
            values = [int(part.strip()) for part in raw.split(",") if part.strip()]
        except ValueError:
            return False, None, "Enter comma-separated integers."
        return True, values, None
    if setting == "promotion_mode":
        value = raw.lower()
        if value not in {"message", "sticker", "both"}:
            return False, None, "Use message, sticker, or both."
        return True, value.upper(), None
    try:
        value = int(raw)
    except ValueError:
        return False, None, "Enter a whole number."
    if value < 0:
        return False, None, "Value must be zero or greater."
    return True, value, None


def _save_setting_target(scope: str, target: str, setting: str, value: Any) -> None:
    if scope == "bot":
        current = get_bot_settings(target)
        current[setting] = value
        set_bot_settings(target, **current)
    elif scope == "global":
        set_setting(setting, value)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await _send(update, "Main Menu", _main_menu())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    parts = query.data.split(":")
    route = parts[1] if len(parts) > 1 else "main"
    if route == "main":
        await _send(update, "Main Menu", _main_menu())
    elif route == "bots":
        await _send(update, "Bots Menu", _bots_menu())
    elif route == "groups":
        await _send(update, "Groups Menu", _groups_menu())
    elif route == "messages":
        await _send(update, "Messages Menu", _messages_menu())
    elif route == "promotion":
        await _send(update, "Promotion Menu", _promotion_menu())
    elif route == "settings":
        await _send(update, "Settings Menu", _settings_menu())


async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    parts = query.data.split(":")
    destination = parts[1] if len(parts) > 1 else "main"
    if destination == "main":
        await _send(update, "Main Menu", _main_menu())
    elif destination == "bots":
        await _send(update, "Bots Menu", _bots_menu())
    elif destination == "groups":
        await _send(update, "Groups Menu", _groups_menu())
    elif destination == "messages":
        await _send(update, "Messages Menu", _messages_menu())
    elif destination == "promotion":
        await _send(update, "Promotion Menu", _promotion_menu())
    elif destination == "settings":
        await _send(update, "Settings Menu", _settings_menu())
    elif destination == "bot_list":
        await list_bots_callback(update, context)
    elif destination == "group_list":
        await list_groups_callback(update, context)
    elif destination == "message_list":
        await list_messages_callback(update, context)
    elif destination == "bot_view" and len(parts) > 2:
        await view_bot_callback(update, context)


async def list_bots_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    bots = get_bots()
    rows: list[list[InlineKeyboardButton]] = []
    for bot_name in sorted(bots):
        bot = bots[bot_name]
        status = "ON" if bot.get("enabled", True) else "OFF"
        pause = "PAUSED" if bot.get("paused") else "RUNNING"
        rows.append([InlineKeyboardButton(f"{status} {pause} {bot_name}", callback_data=f"bot:view:{bot_name}")])
    rows.append([InlineKeyboardButton("Add Bot", callback_data="bot:add:bot_list")])
    rows.append([InlineKeyboardButton("Back", callback_data="nav:bots:bot_list")])
    await _send(update, "Bots", InlineKeyboardMarkup(rows))


async def view_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    bot_name = query.data.split(":", 2)[2]
    bot = get_bot(bot_name)
    if not bot:
        await _send(update, "Bot not found.", _bots_menu())
        return
    await _send(update, _format_bot(bot_name, bot), _bot_detail_menu(bot_name))


async def bot_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await _safe_answer(query)
    context.user_data.clear()
    context.user_data.update({"action": "add_bot", "field": "bot_name"})
    await _send(update, "Send the bot name.")


async def toggle_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    bot_name = query.data.split(":", 2)[2]
    enabled = is_bot_enabled(bot_name, True)
    set_bot_enabled(bot_name, not enabled)
    bot = get_bot(bot_name) or {}
    await _send(update, _format_bot(bot_name, bot), _bot_detail_menu(bot_name))


async def pause_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    bot_name = query.data.split(":", 2)[2]
    paused = is_bot_paused(bot_name, False)
    set_bot_paused(bot_name, not paused)
    bot = get_bot(bot_name) or {}
    await _send(update, _format_bot(bot_name, bot), _bot_detail_menu(bot_name))


async def delete_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    bot_name = query.data.split(":", 2)[2]
    delete_bot(bot_name)
    await _send(update, "Bot deleted.", _bots_menu())


async def bot_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    bot_name = query.data.split(":", 2)[2]
    await _send(update, f"Bot Settings: {bot_name}", _bot_settings_menu(bot_name))


async def bot_setting_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    _, _, bot_name, setting = query.data.split(":", 3)
    context.user_data.clear()
    context.user_data.update({"action": "update_setting", "scope": "bot", "target": bot_name, "setting": setting})
    prompts = {
        "no_response_timeout": "Send the no response timeout.",
        "conversation_sequence": "Send comma-separated message IDs, for example 1,2,3.",
        "conversation_delay_min": "Send the conversation delay minimum.",
        "conversation_delay_max": "Send the conversation delay maximum.",
        "promotion_delay_min": "Send the promotion delay minimum.",
        "promotion_delay_max": "Send the promotion delay maximum.",
        "promotion_mode": "Send message, sticker, or both.",
    }
    await _send(update, prompts.get(setting, "Send a value."))


async def list_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    groups = list_groups()
    rows: list[list[InlineKeyboardButton]] = []
    for group in groups:
        rows.append([InlineKeyboardButton(f"{group.get('group_name')} ({group.get('group_id')})", callback_data=f"group:view:{group.get('group_id')}")])
    rows.append([InlineKeyboardButton("Add Group", callback_data="group:add:group_list")])
    rows.append([InlineKeyboardButton("Back", callback_data="nav:groups:group_list")])
    await _send(update, "Groups", InlineKeyboardMarkup(rows))


async def group_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    group_id = query.data.split(":", 2)[2]
    group = get_group(group_id)
    if not group:
        await _send(update, "Group not found.", _groups_menu())
        return
    await _send(update, _format_group(group), _group_detail_menu(group_id))


async def group_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await _safe_answer(query)
    context.user_data.clear()
    context.user_data.update({"action": "add_group", "field": "group_id"})
    await _send(update, "Send the group ID.")


async def group_toggle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    group_id = query.data.split(":", 2)[2]
    group = get_group(group_id) or {}
    new_status = "disabled" if group.get("status") == "enabled" else "enabled"
    set_group_status(group_id, new_status)
    await _send(update, _format_group(get_group(group_id) or {}), _group_detail_menu(group_id))


async def group_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    group_id = query.data.split(":", 2)[2]
    context.user_data.clear()
    context.user_data.update({"action": "update_group", "group_id": group_id, "field": "group_name"})
    await _send(update, "Send the new group name.")


async def delete_group_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    group_id = query.data.split(":", 2)[2]
    delete_group(group_id)
    await _send(update, "Group deleted.", _groups_menu())


async def list_messages_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    messages = list_messages(active_only=False)
    rows: list[list[InlineKeyboardButton]] = []
    for message in messages:
        rows.append([InlineKeyboardButton(f"Message #{message.get('id')}", callback_data=f"message:view:{message.get('id')}")])
    rows.append([InlineKeyboardButton("Add Message", callback_data="message:add:message_list")])
    rows.append([InlineKeyboardButton("Back", callback_data="nav:messages:message_list")])
    await _send(update, "Messages", InlineKeyboardMarkup(rows))


async def message_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    message_id = int(query.data.split(":", 2)[2])
    message = get_message(message_id)
    if not message:
        await _send(update, "Message not found.", _messages_menu())
        return
    await _send(update, _format_message(message), _message_detail_menu(message_id))


async def message_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await _safe_answer(query)
    context.user_data.clear()
    context.user_data.update({"action": "add_message", "field": "content"})
    await _send(update, "Send the message content.")


async def delete_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    message_id = int(query.data.split(":", 2)[2])
    delete_message(message_id)
    await _send(update, "Message deleted.", _messages_menu())


async def promotion_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    text = (
        "Promotion Settings\n\n"
        f"Mode: {get_setting('promotion_mode', 'message')}\n"
        f"Asset Channel: {get_setting('promotion_asset_channel', None) or 'N/A'}\n"
        f"Sticker Message ID: {get_setting('promotion_sticker_message_id', None) or 'N/A'}"
    )
    await _send(update, text, _promotion_settings_menu())


async def promotion_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await _safe_answer(query)
    context.user_data.clear()
    context.user_data.update({"action": "update_setting", "scope": "global", "target": "promotion_mode", "setting": "promotion_mode"})
    await _send(update, "Send message, sticker, or both.")


async def promotion_asset_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await _safe_answer(query)
    context.user_data.clear()
    context.user_data.update({"action": "update_setting", "scope": "global", "target": "promotion_asset_channel", "setting": "promotion_asset_channel"})
    await _send(update, "Send the asset channel ID or username.")


async def promotion_sticker_id_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query:
        await _safe_answer(query)
    context.user_data.clear()
    context.user_data.update({"action": "update_setting", "scope": "global", "target": "promotion_sticker_message_id", "setting": "promotion_sticker_message_id"})
    await _send(update, "Send the sticker message ID.")


async def _handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = dict(context.user_data)
    if data.get("action") not in {"add_bot", "add_group", "add_message", "update_setting", "update_group"}:
        return
    text = (update.message.text or "").strip()
    if data["action"] == "add_bot":
        bot_name = text
        add_bot(bot_name, {"enabled": True, "paused": False})
        context.user_data.clear()
        await _send(update, f"Bot saved: {bot_name}", _bots_menu())
        return
    if data["action"] == "add_group":
        if data.get("field") == "group_id":
            context.user_data["group_id"] = text
            context.user_data["field"] = "group_name"
            await _send(update, "Send the group name.")
            return
        group_id = str(data.get("group_id"))
        add_group(group_id, text)
        context.user_data.clear()
        await _send(update, f"Group saved: {group_id}", _groups_menu())
        return
    if data["action"] == "add_message":
        if data.get("field") == "content":
            context.user_data["content"] = text
            context.user_data["field"] = "delay"
            await _send(update, "Send the message delay in minutes.")
            return
        content = str(data.get("content", ""))
        try:
            delay = int(text)
        except ValueError:
            await _send(update, "Enter a whole number for delay.")
            return
        add_message(content, delay)
        context.user_data.clear()
        await _send(update, "Message saved.", _messages_menu())
        return
    if data["action"] == "update_group":
        group_id = str(data["group_id"])
        if data.get("field") == "group_name":
            update_group_name(group_id, text)
            context.user_data.clear()
            await _send(update, "Group updated.", _groups_menu())
            return
    if data["action"] == "update_setting":
        scope = str(data.get("scope"))
        target = str(data.get("target"))
        setting = str(data.get("setting"))
        if scope == "global" and setting == "promotion_asset_channel":
            set_setting(setting, text)
            context.user_data.clear()
            await _send(update, "Saved.", _settings_menu())
            return
        if scope == "global" and setting == "promotion_sticker_message_id":
            try:
                value = int(text)
            except ValueError:
                await _send(update, "Enter a whole number.")
                return
            set_setting(setting, value)
            context.user_data.clear()
            await _send(update, "Saved.", _settings_menu())
            return
        ok, value, error = _validate_setting_value(setting, text)
        if not ok:
            await _send(update, error or "Invalid value.")
            return
        if scope == "bot":
            current = get_bot_settings(target)
            current[setting] = value
            set_bot_settings(target, **current)
        elif scope == "global":
            set_setting(setting, value)
        context.user_data.clear()
        await _send(update, "Saved.", _settings_menu())


def _register_handlers() -> None:
    assert _application is not None
    _application.add_handler(CommandHandler("start", start_command))
    _application.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^nav:[^:]+:.*$"))
    _application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:(main|bots|groups|messages|promotion|settings)$"))
    _application.add_handler(CallbackQueryHandler(list_bots_callback, pattern=r"^bot:list$"))
    _application.add_handler(CallbackQueryHandler(view_bot_callback, pattern=r"^bot:view:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(bot_add_callback, pattern=r"^bot:add:.*$"))
    _application.add_handler(CallbackQueryHandler(toggle_bot_callback, pattern=r"^bot:toggle:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(pause_bot_callback, pattern=r"^bot:pause:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(bot_settings_callback, pattern=r"^bot:settings:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(delete_bot_callback, pattern=r"^bot:delete:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(bot_setting_entry, pattern=r"^setting:bot:[^:]+:[a-z_]+$"))
    _application.add_handler(CallbackQueryHandler(list_groups_callback, pattern=r"^group:list$"))
    _application.add_handler(CallbackQueryHandler(group_view_callback, pattern=r"^group:view:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(group_add_callback, pattern=r"^group:add:.*$"))
    _application.add_handler(CallbackQueryHandler(group_toggle_callback, pattern=r"^group:toggle:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(group_edit_callback, pattern=r"^group:edit:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(delete_group_callback, pattern=r"^group:delete:[^:]+$"))
    _application.add_handler(CallbackQueryHandler(list_messages_callback, pattern=r"^message:list$"))
    _application.add_handler(CallbackQueryHandler(message_view_callback, pattern=r"^message:view:\d+$"))
    _application.add_handler(CallbackQueryHandler(message_add_callback, pattern=r"^message:add:.*$"))
    _application.add_handler(CallbackQueryHandler(delete_message_callback, pattern=r"^message:delete:\d+$"))
    _application.add_handler(CallbackQueryHandler(promotion_settings_callback, pattern=r"^promotion:settings$"))
    _application.add_handler(CallbackQueryHandler(promotion_mode_callback, pattern=r"^promotion:mode$"))
    _application.add_handler(CallbackQueryHandler(promotion_asset_channel_callback, pattern=r"^promotion:asset_channel$"))
    _application.add_handler(CallbackQueryHandler(promotion_sticker_id_callback, pattern=r"^promotion:sticker_id$"))
    _application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_text))


async def start_controller() -> None:
    global _application
    if not TOKEN:
        raise RuntimeError("CONTROL_BOT_TOKEN is missing")
    _application = Application.builder().token(TOKEN).build()
    _register_handlers()
    await _application.initialize()
    await _application.start()
    await _application.updater.start_polling()
    await _application.updater.idle()
