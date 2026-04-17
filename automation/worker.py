import os
import asyncio
import datetime
import logging
import random
import threading
import time
from telethon import TelegramClient, events
from dotenv import load_dotenv

load_dotenv()

from config.bots_config import get_all_bots, messages as promo_messages
from state.state_manager import state_manager

SECURITY_KEYWORDS = ["security", "verify", "captcha", "check"]

last_group_sent_time = 0
MIN_GROUP_DELAY_RANGE = (4, 9)


def tweak_message(msg):
    variations = [
        msg,
        msg + " 🙂",
        msg + " 👍",
        msg.replace("India", "Indian"),
        msg.replace("chat", "talk"),
    ]
    return random.choice(variations)


def notify_security(bot_name: str):
    try:
        from control.controller import trigger_security_notification
        trigger_security_notification(bot_name)
    except ImportError as e:
        logger.warning(f"Could not import controller for notification: {e}")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

session_name = os.getenv("TG_SESSION", "session")
api_id = os.getenv("API_ID")
api_hash = os.getenv("API_HASH")

if not api_id:
    logger.error("API_ID missing from environment variables")
    raise ValueError("API_ID missing")
if not api_hash:
    logger.error("API_HASH missing from environment variables")
    raise ValueError("API_HASH missing")

api_id = int(api_id)
logger.info(f"Initializing Telethon client with session: {session_name}")

client = None


def get_client():
    global client
    if client is None:
        client = TelegramClient(session_name, api_id, api_hash)
    return client


client = get_client()


@client.on(events.NewMessage)
async def handle_new_message(event):
    if event.out:
        return

    bots = get_all_bots()
    
    chat = await event.get_chat()
    bot_username = getattr(chat, "username", None)
    
    if not bot_username:
        return
    
    if bot_username not in bots:
        return

    text = event.raw_text.lower()
    logger.info(f"[{bot_username}] Incoming: {text[:100]}")
    logger.info(f"[{bot_username}] Enabled: {state_manager.is_enabled(bot_username)}")

    if "left the chat" in text or "partner left" in text or "chat is over" in text:
        return

    config = bots[bot_username]
    triggers = config.get("triggers", [])

    trigger_found = any(t.lower() in text for t in triggers)
    logger.info(f"[{bot_username}] Triggers: {triggers}, Matched: {trigger_found}")

    if state_manager.is_security_paused(bot_username):
        logger.info(f"[{bot_username}] Security paused, skipping")
        return

    if trigger_found and state_manager.is_enabled(bot_username):
        if any(keyword in text for keyword in SECURITY_KEYWORDS):
            if state_manager.is_security_paused(bot_username):
                return

            logger.warning(f"Security detected in {bot_username}")

            state_manager.set_security_pause(bot_username, True)
            state_manager.disable_bot(bot_username)

            from control.controller import notify_security
            await notify_security(bot_username)

            return

        if state_manager.should_stop(bot_username):
            logger.info(f"[{bot_username}] Limit reached, stopping automation")
            state_manager.disable_bot(bot_username)
            return

        delay = random.uniform(*config.get("speed", (0.5, 2)))
        logger.info(f"[{bot_username}] Match found, waiting {delay:.2f}s before sending")
        await asyncio.sleep(delay)

        promo_msg = random.choice(promo_messages)
        await client.send_message(bot_username, promo_msg)
        logger.info(f"[{bot_username}] Sent promotion message")
        
        stop_delay = random.randint(*config.get("stop_delay", (5, 8)))
        await asyncio.sleep(stop_delay)
        
        await client.send_message(bot_username, config["stop_cmd"])
        logger.info(f"[{bot_username}] Sent stop command")
        
        restart_delay = random.randint(*config.get("restart_delay", (3, 6)))
        await asyncio.sleep(restart_delay)
        
        state_manager.increment_count(bot_username)
        count = state_manager.get_state(bot_username).get("count", 0)
        logger.info(f"[{bot_username}] Incremented count to {count}")
        
        await client.send_message(bot_username, config["start_cmd"])
        logger.info(f"[{bot_username}] Sent start command to continue")


async def start_automation():
    try:
        await client.start()
        logger.info("Telethon client started successfully")
        
        bots = get_all_bots().copy()
        for bot_username in list(bots.keys()):
            if state_manager.is_enabled(bot_username):
                logger.info(f"Starting automation for @{bot_username}")
                await client.send_message(bot_username, bots[bot_username]["start_cmd"])
    except Exception as e:
        logger.error(f"Failed to start automation: {e}")
        raise


async def run_automation():
    await start_automation()
    while True:
        try:
            await client.run_until_disconnected()
        except Exception as e:
            logger.error(f"Reconnect error: {e}")
            await asyncio.sleep(5)


def run_in_background():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_automation())
    except Exception as e:
        logger.error(f"Automation error: {e}")
    finally:
        loop.close()


def run_in_thread():
    automation_thread = threading.Thread(target=run_in_background, daemon=True)
    automation_thread.start()
    logger.info("Automation worker started in background thread")


async def stop_automation():
    try:
        bots = get_all_bots().copy()
        for bot_username in list(bots.keys()):
            if state_manager.is_enabled(bot_username):
                await client.send_message(bot_username, bots[bot_username]["stop_cmd"])
        await client.disconnect()
        logger.info("Automation stopped gracefully")
    except Exception as e:
        logger.error(f"Error stopping automation: {e}")


async def send_command(bot_username: str, command: str):
    try:
        if not client.is_connected():
            await client.connect()
        await client.send_message(bot_username, command)
        logger.info(f"Sent command {command} to {bot_username}")
    except Exception as e:
        logger.error(f"Failed to send command to {bot_username}: {e}")


async def send_group_messages():
    global last_group_sent_time
    logger.info("Group messaging worker started")
    while True:
        try:
            hour = datetime.datetime.now().hour
            if 1 <= hour <= 7:
                logger.info(f"Night hours ({hour}:00), sleeping...")
                await asyncio.sleep(random.randint(300, 900))
                continue
            
            if state_manager.is_group_enabled():
                settings = state_manager.get_group_settings()
                if settings.get("enabled", False):
                    if state_manager.should_stop_daily(limit=500):
                        logger.warning("Daily group message limit reached, pausing for rest of day")
                        await asyncio.sleep(3600)
                        continue
                    
                    max_groups = settings.get("max_groups_per_cycle", 5)
                    delay_range = settings.get("delay_range", (30, 90))
                    
                    selected_groups = state_manager.get_rotated_groups(max_groups)
                    
                    if selected_groups:
                        logger.info(f"Group run: {len(selected_groups)} groups | Delay: {delay_range} sec | Safe mode: {settings.get('safe_mode', True)}")
                        
                        for group in selected_groups:
                            if state_manager.should_stop_daily(limit=500):
                                logger.warning("Daily limit reached during cycle")
                                break
                            
                            if not state_manager.can_send_to_group(group, cooldown_seconds=300):
                                logger.info(f"Skipping {group} - cooldown not passed")
                                continue
                            
                            now = time.time()
                            delay = random.randint(*MIN_GROUP_DELAY_RANGE)
                            wait_time = delay - (now - last_group_sent_time)
                            if wait_time > 0:
                                await asyncio.sleep(wait_time)
                            last_group_sent_time = time.time()
                            
                            try:
                                await client.send_message(group, tweak_message(state_manager.get_next_message(promo_messages)))
                                logger.info(f"Sent message to group: {group}")
                                state_manager.update_group_sent(group)
                                state_manager.increment_daily_count()
                                
                                daily = state_manager.get_daily_count()
                                if daily < 50:
                                    delay = random.randint(8, 15)
                                elif daily < 200:
                                    delay = random.randint(5, 10)
                                else:
                                    delay = random.randint(*delay_range)
                                
                                if random.randint(1, 10) == 1:
                                    long_cooldown = random.randint(1800, 3600)
                                    logger.info(f"Random long cooldown: {long_cooldown}s")
                                    await asyncio.sleep(long_cooldown)
                                else:
                                    await asyncio.sleep(delay)
                            except Exception as e:
                                logger.error(f"Error sending to {group}: {e}")
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Group messaging error: {e}")
            await asyncio.sleep(60)


def get_client():
    return client