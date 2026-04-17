import os
import asyncio
import logging
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters

from dotenv import load_dotenv
load_dotenv()

from config.bots_config import get_all_bots
from state.state_manager import state_manager
from automation.worker import get_client

bots = get_all_bots()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("CONTROL_BOT_TOKEN", "")
allowed_id = os.getenv("ALLOWED_USER_ID")

print("DEBUG CONTROL_BOT_TOKEN:", TOKEN)
print("DEBUG ALLOWED_USER_ID:", allowed_id)

if not TOKEN:
    logger.error("CONTROL_BOT_TOKEN not set in environment variables")
    raise ValueError("CONTROL_BOT_TOKEN is required")

if not allowed_id:
    logger.error("ALLOWED_USER_ID missing from environment variables")
    raise ValueError("ALLOWED_USER_ID missing")

if not allowed_id.isdigit():
    logger.error(f"ALLOWED_USER_ID must be numeric, got: {allowed_id}")
    raise ValueError(f"ALLOWED_USER_ID must be numeric, got: {allowed_id}")

ALLOWED_USER_ID = int(allowed_id)

print("🤖 Control bot configured for user ID:", ALLOWED_USER_ID)
logger.info(f"Control bot configured for user ID: {ALLOWED_USER_ID}")

SET_LIMIT = range(1)
ADD_GROUP = range(1, 2)
ADD_BOT_NAME = 10
ADD_START_CMD = 11
ADD_STOP_CMD = 12
ADD_TRIGGERS = 13
ADD_SPEED = 14
ADD_STOP_DELAY = 15
ADD_RESTART_DELAY = 16

SET_MAX_GROUPS = 30
SET_GROUP_DELAY = 31
VIEW_SAFE_LIMITS = 32


def check_user(update: Update) -> bool:
    if ALLOWED_USER_ID == 0:
        return True
    user_id = update.effective_user.id
    return user_id == ALLOWED_USER_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        await update.message.reply_text("⛔ Access denied")
        logger.warning(f"Unauthorized access attempt from user {update.effective_user.id}")
        return
    
    keyboard = [
        [InlineKeyboardButton("🤖 Manage Bots", callback_data="manage_bots")],
        [InlineKeyboardButton("📢 Manage Groups", callback_data="manage_groups")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎛️ <b>Telegram Automation Control</b>\n\nSelect an option:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )
    logger.info(f"User {update.effective_user.id} accessed main menu")


async def manage_bots_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    keyboard = []
    from db.mongo import get_bots
    all_bots = get_bots()
    
    if not all_bots:
        keyboard.append([InlineKeyboardButton("⚠️ No bots added yet", callback_data="ignore")])
    else:
        for bot_name in all_bots.keys():
            if bot_name.startswith('_'):
                continue
            state = state_manager.get_state(bot_name)
            status = "✅" if state.get("enabled") else "❌"
            keyboard.append([
                InlineKeyboardButton(f"{status} {bot_name}", callback_data=f"bot_{bot_name}")
            ])
    
    keyboard.append([InlineKeyboardButton("➕ Add Bot", callback_data="add_bot")])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="back_main")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🤖 <b>Manage Bots</b>\nSelect a bot to manage:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def bot_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    bot_name = query.data.replace("bot_", "")
    state = state_manager.get_state(bot_name)
    
    keyboard = [
        [
            InlineKeyboardButton("✅ Enable" if not state.get("enabled") else "❌ Disable", 
                              callback_data=f"toggle_{bot_name}"),
        ],
        [
            InlineKeyboardButton("📊 Set Limit", callback_data=f"limit_{bot_name}"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Count", callback_data=f"reset_{bot_name}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete Bot", callback_data=f"delete_{bot_name}"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Bot", callback_data=f"edit_{bot_name}"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_bots")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status_text = "ON" if state.get("enabled") else "OFF"
    count = state.get("count", 0)
    limit = state.get("limit", 0)
    
    await query.edit_message_text(
        f"🤖 <b>{bot_name}</b>\n"
        f"Status: {status_text}\n"
        f"Count: {count}/{limit}\n",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def toggle_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    bot_name = query.data.replace("toggle_", "")
    current_state = state_manager.get_state(bot_name)
    
    if current_state.get("enabled"):
        state_manager.disable_bot(bot_name)
        message = f"❌ Disabled {bot_name}"
    else:
        state_manager.enable_bot(bot_name)
        message = f"✅ Enabled {bot_name}"
    
    logger.info(f"Toggled bot {bot_name}: {message}")
    await query.edit_message_text(message)
    
    state = state_manager.get_state(bot_name)
    keyboard = [
        [
            InlineKeyboardButton("✅ Enable" if not state.get("enabled") else "❌ Disable", 
                              callback_data=f"toggle_{bot_name}"),
        ],
        [
            InlineKeyboardButton("📊 Set Limit", callback_data=f"limit_{bot_name}"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Count", callback_data=f"reset_{bot_name}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete Bot", callback_data=f"delete_{bot_name}"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Bot", callback_data=f"edit_{bot_name}"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_bots")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status_text = "ON" if state.get("enabled") else "OFF"
    count = state.get("count", 0)
    limit = state.get("limit", 0)
    
    await query.edit_message_text(
        f"🤖 <b>{bot_name}</b>\n"
        f"Status: {status_text}\n"
        f"Count: {count}/{limit}\n",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def limit_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    bot_name = query.data.replace("limit_", "")
    context.user_data["limit_bot"] = bot_name
    
    await query.edit_message_text(f"📊 Enter new limit for {bot_name}:")
    return SET_LIMIT


async def limit_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_name = context.user_data.get("limit_bot")
    
    if not bot_name or not check_user(update):
        return ConversationHandler.END
    
    try:
        new_limit = int(update.message.text)
        if new_limit > 0:
            state_manager.set_limit(bot_name, new_limit)
            await update.message.reply_text(f"✅ Set limit to {new_limit} for {bot_name}")
            logger.info(f"Set limit for {bot_name} to {new_limit}")
        else:
            await update.message.reply_text("❌ Limit must be positive")
    except ValueError:
        await update.message.reply_text("❌ Invalid number")
    
    return ConversationHandler.END


async def reset_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    bot_name = query.data.replace("reset_", "")
    state_manager.reset_count(bot_name)
    
    logger.info(f"Reset count for {bot_name}")
    await query.edit_message_text(f"🔄 Reset count for {bot_name}")
    
    state = state_manager.get_state(bot_name)
    keyboard = [
        [
            InlineKeyboardButton("✅ Enable" if not state.get("enabled") else "❌ Disable", 
                              callback_data=f"toggle_{bot_name}"),
        ],
        [
            InlineKeyboardButton("📊 Set Limit", callback_data=f"limit_{bot_name}"),
        ],
        [
            InlineKeyboardButton("🔄 Reset Count", callback_data=f"reset_{bot_name}"),
        ],
        [
            InlineKeyboardButton("🗑 Delete Bot", callback_data=f"delete_{bot_name}"),
        ],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_bots")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status_text = "ON" if state.get("enabled") else "OFF"
    count = state.get("count", 0)
    limit = state.get("limit", 0)
    
    await query.edit_message_text(
        f"🤖 <b>{bot_name}</b>\n"
        f"Status: {status_text}\n"
        f"Count: {count}/{limit}\n",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    all_state = state_manager.get_all_state()
    
    text = "⚙️ <b>Current State</b>\n\n"
    for bot_name, state in all_state.items():
        status = "✅" if state.get("enabled") else "❌"
        text += f"{status} <b>{bot_name}</b>\n"
        text += f"   Count: {state.get('count', 0)}/{state.get('limit', 0)}\n\n"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="back_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def back_main_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🤖 Manage Bots", callback_data="manage_bots")],
        [InlineKeyboardButton("📢 Manage Groups", callback_data="manage_groups")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎛️ <b>Telegram Automation Control</b>\n\nSelect an option:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def manage_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Group", callback_data="add_group_btn")],
        [InlineKeyboardButton("📋 View Groups", callback_data="view_groups")],
        [InlineKeyboardButton("▶️ Enable Sending", callback_data="enable_groups")],
        [InlineKeyboardButton("⏹ Disable Sending", callback_data="disable_groups")],
        [InlineKeyboardButton("⚙️ Group Settings", callback_data="group_settings")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status = "ON" if state_manager.is_group_enabled() else "OFF"
    group_count = len(state_manager.get_groups())
    
    await query.edit_message_text(
        f"📢 <b>Manage Groups</b>\n\n"
        f"Status: {status}\n"
        f"Groups: {group_count}",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def add_group_btn_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("add_group_btn_callback triggered")
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    await query.edit_message_text("➕ Send me the group username or ID (e.g., @groupname or -1001234567890):")
    return ADD_GROUP


async def add_group_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("add_group_input_handler triggered")
    if not check_user(update):
        return ConversationHandler.END
    
    group_id = update.message.text.strip()
    if state_manager.add_group(group_id):
        await update.message.reply_text(f"✅ Added group: {group_id}")
        logger.info(f"Group added: {group_id}")
    else:
        await update.message.reply_text(f"⚠️ Group already exists: {group_id}")
    
    return ConversationHandler.END


async def view_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    groups = state_manager.get_groups()
    if groups:
        text = "📋 <b>Group List</b>\n\n" + "\n".join([f"• {g}" for g in groups])
    else:
        text = "📋 No groups added yet"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="manage_groups")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def enable_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    state_manager.enable_group_messaging()
    state_manager.update_group_settings({"enabled": True})
    await query.edit_message_text("✅ Group messaging enabled")
    await manage_groups_callback(update, context)


async def disable_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    state_manager.disable_group_messaging()
    state_manager.update_group_settings({"enabled": False})
    await query.edit_message_text("⏹ Group messaging disabled")
    await manage_groups_callback(update, context)


async def group_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    settings = state_manager.get_group_settings()
    
    keyboard = [
        [InlineKeyboardButton("🔢 Set Max Groups", callback_data="set_max_groups")],
        [InlineKeyboardButton("⏱ Set Delay", callback_data="set_group_delay")],
        [InlineKeyboardButton(f"🛡 Safe Mode: {'ON' if settings.get('safe_mode') else 'OFF'}", callback_data="toggle_safe_mode")],
        [InlineKeyboardButton("📊 View Safe Limits", callback_data="view_safe_limits")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_groups")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚙️ <b>Group Settings</b>\n\n"
        f"Max Groups: {settings.get('max_groups_per_cycle', 5)}\n"
        f"Delay: {settings.get('delay_range', (30, 90))} sec\n"
        f"Safe Mode: {'ON' if settings.get('safe_mode') else 'OFF'}",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def set_max_groups_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        return
    
    settings = state_manager.get_group_settings()
    await query.message.reply_text(
        f"🔢 Enter max groups per cycle (recommended 3–10):\nCurrent: {settings.get('max_groups_per_cycle', 5)}"
    )
    return SET_MAX_GROUPS


async def set_max_groups_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    try:
        max_groups = int(update.message.text.strip())
        if max_groups < 1:
            await update.message.reply_text("⚠️ Must be at least 1")
            return SET_MAX_GROUPS
        
        state_manager.update_group_settings({"max_groups_per_cycle": max_groups})
        state_manager.enforce_safe_mode()
        await update.message.reply_text(f"✅ Set max groups to {max_groups}")
    except ValueError:
        await update.message.reply_text("⚠️ Invalid number")
    
    return ConversationHandler.END


async def set_group_delay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        return
    
    settings = state_manager.get_group_settings()
    current = settings.get('delay_range', (30, 90))
    await query.message.reply_text(
        f"⏱️ Enter delay range (min,max) in seconds (recommended 30,90):\nCurrent: {current}"
    )
    return SET_GROUP_DELAY


async def set_group_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split(",")
        if len(parts) != 2:
            raise ValueError("Format must be min,max")
        delay_range = (int(parts[0].strip()), int(parts[1].strip()))
        if delay_range[0] < 1 or delay_range[1] < delay_range[0]:
            raise ValueError("Invalid range")
        
        state_manager.update_group_settings({"delay_range": delay_range})
        state_manager.enforce_safe_mode()
        await update.message.reply_text(f"✅ Set delay range to {delay_range}")
    except Exception:
        await update.message.reply_text("⚠️ Invalid format! Use: min,max (e.g., 30,90)")
    
    return ConversationHandler.END


async def toggle_safe_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    settings = state_manager.get_group_settings()
    new_mode = not settings.get("safe_mode", True)
    state_manager.update_group_settings({"safe_mode": new_mode})
    
    if new_mode:
        state_manager.enforce_safe_mode()
        await query.edit_message_text("🛡 Safe Mode enabled - limits enforced")
    else:
        await query.edit_message_text("⚠️ Safe Mode disabled - limits removed (risk!)")
    
    await group_settings_callback(update, context)


async def view_safe_limits_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    settings = state_manager.get_group_settings()
    delay_range = settings.get('delay_range', (30, 90))
    max_groups = settings.get('max_groups_per_cycle', 5)
    safe_mode = settings.get('safe_mode', True)
    
    delay = delay_range[0]
    risk = "🟢 LOW"
    if delay < 30 or max_groups > 10:
        risk = "🔴 HIGH"
    elif delay < 45:
        risk = "🟡 MEDIUM"
    
    text = "📊 <b>Safe Limits</b>\n\n"
    text += "• Max groups per cycle: 5-10\n"
    text += "• Delay between messages: 30-90 sec\n"
    text += "• Daily messages: < 500 recommended\n\n"
    text += "<b>Current Settings</b>\n"
    text += f"• Max groups: {max_groups}\n"
    text += f"• Delay: {delay_range} sec\n"
    text += f"• Safe Mode: {'ON' if safe_mode else 'OFF'}\n\n"
    text += f"<b>Risk Level:</b> {risk}"
    
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data="group_settings")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="HTML")


async def add_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    await query.edit_message_text("➕ Enter bot username (without @):")
    return ADD_BOT_NAME


async def add_bot_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    bot_name = update.message.text.strip()
    if state_manager.bot_exists(bot_name):
        await update.message.reply_text("⚠️ Bot already exists! Choose a different name.")
        return ConversationHandler.END
    
    context.user_data["new_bot_name"] = bot_name
    await update.message.reply_text("✅ Enter start command (e.g., /match or /next):")
    return ADD_START_CMD


async def add_start_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    context.user_data["new_bot_start_cmd"] = update.message.text.strip()
    await update.message.reply_text("✅ Enter stop command (e.g., /stop):")
    return ADD_STOP_CMD


async def add_stop_cmd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    context.user_data["new_bot_stop_cmd"] = update.message.text.strip()
    await update.message.reply_text("✅ Enter trigger keywords (comma separated, e.g., found,match,connected):")
    return ADD_TRIGGERS


async def add_triggers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    triggers = [t.strip().lower() for t in update.message.text.split(",") if t.strip()]
    if not triggers:
        await update.message.reply_text("⚠️ At least one trigger keyword required!")
        return ConversationHandler.END
    
    context.user_data["new_bot_triggers"] = triggers
    await update.message.reply_text("✅ Enter message delay (min,max) e.g., 0.5,2:")
    return ADD_SPEED


async def add_speed_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split(",")
        if len(parts) != 2:
            raise ValueError("Format must be min,max")
        speed = (float(parts[0].strip()), float(parts[1].strip()))
        context.user_data["new_bot_speed"] = speed
    except Exception:
        await update.message.reply_text("⚠️ Invalid format! Use: min,max (e.g., 0.5,2)")
        return ADD_SPEED
    
    await update.message.reply_text("✅ Enter stop delay (min,max) e.g., 5,8:")
    return ADD_STOP_DELAY


async def add_stop_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split(",")
        if len(parts) != 2:
            raise ValueError("Format must be min,max")
        stop_delay = (int(parts[0].strip()), int(parts[1].strip()))
        context.user_data["new_bot_stop_delay"] = stop_delay
    except Exception:
        await update.message.reply_text("⚠️ Invalid format! Use: min,max (e.g., 5,8)")
        return ADD_STOP_DELAY
    
    await update.message.reply_text("✅ Enter restart delay (min,max) e.g., 3,6:")
    return ADD_RESTART_DELAY


async def add_restart_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split(",")
        if len(parts) != 2:
            raise ValueError("Format must be min,max")
        restart_delay = (int(parts[0].strip()), int(parts[1].strip()))
    except Exception:
        await update.message.reply_text("⚠️ Invalid format! Use: min,max (e.g., 3,6)")
        return ADD_RESTART_DELAY
    
    bot_name = context.user_data.get("new_bot_name")
    config = {
        "start_cmd": context.user_data.get("new_bot_start_cmd"),
        "stop_cmd": context.user_data.get("new_bot_stop_cmd"),
        "triggers": context.user_data.get("new_bot_triggers"),
        "speed": context.user_data.get("new_bot_speed"),
        "stop_delay": context.user_data.get("new_bot_stop_delay"),
        "restart_delay": restart_delay
    }
    
    if state_manager.add_bot(bot_name, config):
        state_manager.enable_bot(bot_name)
        await update.message.reply_text(f"✅ Bot '{bot_name}' added and enabled!")
        logger.info(f"Added dynamic bot: {bot_name}")
        
        try:
            from automation.worker import send_command
            await send_command(bot_name, config["start_cmd"])
            await update.message.reply_text(f"🚀 Started automation for {bot_name}")
        except Exception as e:
            logger.warning(f"Could not send start command: {e}")
    else:
        await update.message.reply_text("❌ Failed to add bot")
    
    context.user_data.clear()
    return ConversationHandler.END


def trigger_security_notification(bot_name: str):
    try:
        asyncio.get_event_loop().run_until_complete(send_security_notification(bot_name))
    except Exception as e:
        logger.warning(f"Could not send security notification: {e}")


async def send_security_notification(bot_name: str):
    try:
        application = Application.builder().token(TOKEN).build()
        await application.initialize()
        keyboard = [
            [InlineKeyboardButton("✅ Bypassed", callback_data=f"bypass_{bot_name}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await application.bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=f"⚠️ Security check detected in {bot_name}\nPlease bypass manually and click below.",
            reply_markup=reply_markup
        )
        await application.shutdown()
        logger.info(f"Sent security notification for {bot_name}")
    except Exception as e:
        logger.error(f"Failed to send security notification: {e}")


async def notify_security(bot_name: str):
    await send_security_notification(bot_name)


async def bypass_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    bot_name = query.data.replace("bypass_", "")
    
    state_manager.set_security_pause(bot_name, False)
    state_manager.enable_bot(bot_name)
    
    await asyncio.sleep(2)

    from config.bots_config import get_all_bots
    all_bots = get_all_bots()
    await send_command(bot_name, all_bots[bot_name]["start_cmd"])

    await query.edit_message_text(f"✅ Resumed {bot_name}")


async def delete_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    bot_name = query.data.replace("delete_", "")
    
    from db.mongo import delete_bot as mongo_delete_bot
    try:
        mongo_delete_bot(bot_name)
    except Exception as e:
        logger.error(f"MongoDB delete error: {e}")
    
    state_manager.remove_bot(bot_name)
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Bot", callback_data="add_bot")],
        [InlineKeyboardButton("🔙 Back", callback_data="manage_bots")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🗑 Bot <b>{bot_name}</b> deleted successfully",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


EDIT_TRIGGERS = 20
EDIT_SPEED = 21
EDIT_STOP_DELAY = 22
EDIT_RESTART_DELAY = 23


async def edit_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        await query.edit_message_text("⛔ Access denied")
        return
    
    bot_name = query.data.replace("edit_", "")
    
    keyboard = [
        [InlineKeyboardButton("🏷️ Triggers", callback_data=f"edit_triggers_{bot_name}")],
        [InlineKeyboardButton("⏱️ Speed", callback_data=f"edit_speed_{bot_name}")],
        [InlineKeyboardButton("⏹ Stop Delay", callback_data=f"edit_stop_delay_{bot_name}")],
        [InlineKeyboardButton("🔄 Restart Delay", callback_data=f"edit_restart_delay_{bot_name}")],
        [InlineKeyboardButton("🔙 Back", callback_data=f"bot_{bot_name}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✏️ Edit <b>{bot_name}</b>\n\nSelect what to edit:",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )


async def edit_triggers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        return
    
    bot_name = query.data.replace("edit_triggers_", "")
    context.user_data["edit_bot"] = bot_name
    
    await query.message.reply_text("🏷️ Enter new triggers (comma separated, e.g., found,match,connected):")
    return EDIT_TRIGGERS


async def edit_triggers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    bot_name = context.user_data.get("edit_bot")
    if not bot_name:
        return ConversationHandler.END
    
    triggers = [t.strip().lower() for t in update.message.text.split(",") if t.strip()]
    if not triggers:
        await update.message.reply_text("⚠️ At least one trigger required!")
        return EDIT_TRIGGERS
    
    state_manager.update_bot(bot_name, {"triggers": triggers})
    await update.message.reply_text(f"✅ Updated triggers for {bot_name}: {', '.join(triggers)}")
    
    context.user_data.clear()
    return ConversationHandler.END


async def edit_speed_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        return
    
    bot_name = query.data.replace("edit_speed_", "")
    context.user_data["edit_bot"] = bot_name
    
    await query.message.reply_text("⏱️ Enter speed delay (min,max) e.g., 0.5,2:")
    return EDIT_SPEED


async def edit_speed_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    bot_name = context.user_data.get("edit_bot")
    if not bot_name:
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split(",")
        if len(parts) != 2:
            raise ValueError("Format must be min,max")
        speed = (float(parts[0].strip()), float(parts[1].strip()))
    except Exception:
        await update.message.reply_text("⚠️ Invalid format! Use: min,max (e.g., 0.5,2)")
        return EDIT_SPEED
    
    state_manager.update_bot(bot_name, {"speed": speed})
    await update.message.reply_text(f"✅ Updated speed for {bot_name}: {speed}")
    
    context.user_data.clear()
    return ConversationHandler.END


async def edit_stop_delay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        return
    
    bot_name = query.data.replace("edit_stop_delay_", "")
    context.user_data["edit_bot"] = bot_name
    
    await query.message.reply_text("⏹️ Enter stop delay (min,max) e.g., 5,8:")
    return EDIT_STOP_DELAY


async def edit_stop_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    bot_name = context.user_data.get("edit_bot")
    if not bot_name:
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split(",")
        if len(parts) != 2:
            raise ValueError("Format must be min,max")
        delay = (int(parts[0].strip()), int(parts[1].strip()))
    except Exception:
        await update.message.reply_text("⚠️ Invalid format! Use: min,max (e.g., 5,8)")
        return EDIT_STOP_DELAY
    
    state_manager.update_bot(bot_name, {"stop_delay": delay})
    await update.message.reply_text(f"✅ Updated stop delay for {bot_name}: {delay}")
    
    context.user_data.clear()
    return ConversationHandler.END


async def edit_restart_delay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if not check_user(update):
        return
    
    bot_name = query.data.replace("edit_restart_delay_", "")
    context.user_data["edit_bot"] = bot_name
    
    await query.message.reply_text("🔄 Enter restart delay (min,max) e.g., 3,6:")
    return EDIT_RESTART_DELAY


async def edit_restart_delay_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_user(update):
        return ConversationHandler.END
    
    bot_name = context.user_data.get("edit_bot")
    if not bot_name:
        return ConversationHandler.END
    
    try:
        parts = update.message.text.split(",")
        if len(parts) != 2:
            raise ValueError("Format must be min,max")
        delay = (int(parts[0].strip()), int(parts[1].strip()))
    except Exception:
        await update.message.reply_text("⚠️ Invalid format! Use: min,max (e.g., 3,6)")
        return EDIT_RESTART_DELAY
    
    state_manager.update_bot(bot_name, {"restart_delay": delay})
    await update.message.reply_text(f"✅ Updated restart delay for {bot_name}: {delay}")
    
    context.user_data.clear()
    return ConversationHandler.END


async def start_bot():
    print("🚀 Starting control bot polling...")
    
    application = Application.builder().token(TOKEN).build()
    
    await application.bot.delete_webhook()
    print("✅ Webhook cleared")
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(manage_bots_callback, pattern="manage_bots"))
    application.add_handler(CallbackQueryHandler(bot_details_callback, pattern="^bot_"))
    application.add_handler(CallbackQueryHandler(toggle_bot_callback, pattern="^toggle_"))
    application.add_handler(CallbackQueryHandler(limit_button_callback, pattern="^limit_"))
    application.add_handler(CallbackQueryHandler(reset_count_callback, pattern="^reset_"))
    application.add_handler(CallbackQueryHandler(settings_callback, pattern="settings"))
    application.add_handler(CallbackQueryHandler(back_main_callback, pattern="back_main"))
    application.add_handler(CallbackQueryHandler(bypass_callback, pattern="^bypass_"))
    application.add_handler(CallbackQueryHandler(delete_bot_callback, pattern="^delete_"))
    application.add_handler(CallbackQueryHandler(edit_bot_callback, pattern="^edit_"))
    application.add_handler(CallbackQueryHandler(edit_triggers_callback, pattern="^edit_triggers_"))
    application.add_handler(CallbackQueryHandler(edit_speed_callback, pattern="^edit_speed_"))
    application.add_handler(CallbackQueryHandler(edit_stop_delay_callback, pattern="^edit_stop_delay_"))
    application.add_handler(CallbackQueryHandler(edit_restart_delay_callback, pattern="^edit_restart_delay_"))
    application.add_handler(CallbackQueryHandler(manage_groups_callback, pattern="manage_groups"))
    application.add_handler(CallbackQueryHandler(add_group_btn_callback, pattern="add_group_btn"))
    application.add_handler(CallbackQueryHandler(view_groups_callback, pattern="view_groups"))
    application.add_handler(CallbackQueryHandler(enable_groups_callback, pattern="enable_groups"))
    application.add_handler(CallbackQueryHandler(disable_groups_callback, pattern="disable_groups"))
    application.add_handler(CallbackQueryHandler(group_settings_callback, pattern="group_settings"))
    application.add_handler(CallbackQueryHandler(set_max_groups_callback, pattern="set_max_groups"))
    application.add_handler(CallbackQueryHandler(set_group_delay_callback, pattern="set_group_delay"))
    application.add_handler(CallbackQueryHandler(toggle_safe_mode_callback, pattern="toggle_safe_mode"))
    application.add_handler(CallbackQueryHandler(view_safe_limits_callback, pattern="view_safe_limits"))
    application.add_handler(CallbackQueryHandler(add_bot_callback, pattern="add_bot"))

        conv_handler = ConversationHandler(
            entry_points=[
                CallbackQueryHandler(limit_button_callback, pattern="^limit_"),
                CallbackQueryHandler(add_group_btn_callback, pattern="add_group_btn"),
                CallbackQueryHandler(add_bot_callback, pattern="add_bot"),
                CallbackQueryHandler(edit_triggers_callback, pattern="^edit_triggers_"),
                CallbackQueryHandler(edit_speed_callback, pattern="^edit_speed_"),
                CallbackQueryHandler(edit_stop_delay_callback, pattern="^edit_stop_delay_"),
                CallbackQueryHandler(edit_restart_delay_callback, pattern="^edit_restart_delay_")
            ],
            states={
                SET_LIMIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, limit_input_handler)],
                ADD_GROUP: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_group_input_handler)],
                ADD_BOT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_bot_name_handler)],
                ADD_START_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_start_cmd_handler)],
                ADD_STOP_CMD: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stop_cmd_handler)],
                ADD_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_triggers_handler)],
                ADD_SPEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_speed_handler)],
                ADD_STOP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stop_delay_handler)],
                ADD_RESTART_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_restart_delay_handler)],
                EDIT_TRIGGERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_triggers_handler)],
                EDIT_SPEED: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_speed_handler)],
                EDIT_STOP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_stop_delay_handler)],
                EDIT_RESTART_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_restart_delay_handler)],
                SET_MAX_GROUPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_max_groups_handler)],
                SET_GROUP_DELAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_group_delay_handler)],
            },
            fallbacks=[],
        )
        application.add_handler(conv_handler)

        logger.info("Control bot is running...")

        await application.initialize()
        await application.start()
        await application.bot.initialize()
        await application.updater.start_polling(drop_pending_updates=True)
        
        print("✅ Control bot polling started")


_controller_started = False


def run_controller():
    import asyncio
    
    logger.info("Starting control bot...")
    
    async def run():
        await start_bot()
    
    asyncio.run(run())


def run_in_background():
    global _controller_started
    if _controller_started:
        logger.warning("Control bot already running, skipping...")
        print("⚠️ Control bot already running")
        return
    
    _controller_started = True
    print("📦 Starting controller thread...")
    controller_thread = threading.Thread(target=run_controller, daemon=True)
    controller_thread.start()
    logger.info("Control bot started in background thread")