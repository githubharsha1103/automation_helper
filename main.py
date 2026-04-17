#!/usr/bin/env python3

print("🔥 MAIN STARTED")

import os
import sys
import asyncio
import threading
import logging
import random
import time
from dotenv import load_dotenv

load_dotenv()

print("ENV API_ID:", os.getenv("API_ID"))
print("ENV API_HASH:", os.getenv("API_HASH"))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

try:
    from web.server import run_in_background as run_flask
    print("✅ web.server imported")
except Exception as e:
    print("❌ web.server import failed:", e)
    raise

try:
    from control.controller import run_controller
    print("✅ controller imported")
except Exception as e:
    print("❌ controller import failed:", e)
    raise

try:
    from automation.worker import run_in_background as run_automation, send_group_messages
    print("✅ worker imported")
except Exception as e:
    print("❌ worker import failed:", e)
    raise


def run_group_worker():
    asyncio.run(send_group_messages())


def main():
    import os
    mode = os.getenv("SERVICE_MODE", "all")
    print(f"🚀 Starting Telegram Automation System... (PID: {os.getpid()}, MODE: {mode})")
    print("Environment loaded")
    
    logger.info("=" * 50)
    logger.info(f"Starting Telegram Automation System (MODE: {mode})")
    logger.info("=" * 50)

    if mode == "web":
        print("🌐 Running Flask server only...")
        run_flask()
        print("✅ Flask server started")
    elif mode == "bot":
        print("🤖 Running control bot only...")
        run_controller()
    elif mode == "worker":
        startup_delay = random.randint(10, 60)
        logger.info(f"Waiting {startup_delay}s before starting automation...")
        threading.Event().wait(startup_delay)
        
        print("⚙️ Starting automation worker...")
        run_automation()
        print("✅ Automation worker started")
        
        group_thread = threading.Thread(target=run_group_worker, daemon=True)
        group_thread.start()
        logger.info("Group messaging worker started")
    else:
        startup_delay = random.randint(10, 60)
        logger.info(f"Waiting {startup_delay}s before starting automation...")
        threading.Event().wait(startup_delay)

        print("▶️ Starting Flask server...")
        run_flask()
        print("✅ Flask server started")

        print("🤖 Starting control bot...")
        run_controller()
        print("✅ Control bot started")

        print("⚙️ Starting automation worker...")
        run_automation()
        print("✅ Automation worker started")
        
        group_thread = threading.Thread(target=run_group_worker, daemon=True)
        group_thread.start()
        logger.info("Group messaging worker started")

    logger.info("All services started successfully")
    logger.info("Press Ctrl+C to stop")

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("Startup Error:", e)
        import traceback
        traceback.print_exc()
        raise