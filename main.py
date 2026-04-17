#!/usr/bin/env python3

import os
import sys
import asyncio
import threading
import logging
import random
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from web.server import run_in_background as run_flask
from control.controller import run_in_background as run_controller
from automation.worker import run_in_background as run_automation, send_group_messages


def run_group_worker():
    asyncio.run(send_group_messages())


def main():
    logger.info("=" * 50)
    logger.info("Starting Telegram Automation System")
    logger.info("=" * 50)

    startup_delay = random.randint(10, 60)
    logger.info(f"Waiting {startup_delay}s before starting automation...")
    threading.Event().wait(startup_delay)

    run_flask()
    run_controller()
    run_automation()
    
    group_thread = threading.Thread(target=run_group_worker, daemon=True)
    group_thread.start()
    logger.info("Group messaging worker started")

    logger.info("All services started successfully")
    logger.info("  - Flask server: http://localhost:5000")
    logger.info("  - Control bot: Send /start command")
    logger.info("  - Automation: Running in background")
    logger.info("Press Ctrl+C to stop")

    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()