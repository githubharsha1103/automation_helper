import asyncio
import logging
import os
import sys
import threading
import traceback

print("[BOOT] Starting main.py import sequence")

try:
    print("[BOOT] Importing automation.worker")
    from automation.worker import telegram_service, start_group_worker, start_worker
    print("[BOOT] Importing controller.controller")
    from controller.controller import TOKEN as CONTROL_BOT_TOKEN, start_controller
    print("[BOOT] Importing storage.db")
    from storage.db import init_db
    print("[BOOT] Importing web.server")
    from web.server import run_flask_in_thread
except Exception:
    print("[BOOT] Import failure")
    print(traceback.format_exc())
    raise


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)


def _configure_global_exception_handlers() -> None:
    def handle_uncaught_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        print("FATAL ERROR: Uncaught exception")
        print("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)))

    def handle_thread_exception(args):
        print(f"FATAL ERROR: Uncaught exception in thread {args.thread.name}")
        print("".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = handle_uncaught_exception
    threading.excepthook = handle_thread_exception


def _log_startup(step: str) -> None:
    print(f"[STARTUP] {step}")
    logger.info("[STARTUP] %s", step)


def _log_environment() -> None:
    _log_startup("Loading environment")
    for key in [
        "API_ID",
        "API_HASH",
        "SESSION_STRING",
        "BOT_TOKEN",
        "CONTROL_BOT_TOKEN",
        "MONGO_URI",
        "DATABASE_PATH",
        "PORT",
    ]:
        value = os.getenv(key)
        status = "set" if value else "missing"
        print(f"[STARTUP] {key}={status}")
        logger.info("[STARTUP] %s=%s", key, status)


async def _run_named_task(name: str, coroutine) -> None:
    try:
        _log_startup(f"Starting {name}")
        await coroutine
    except Exception:
        print(f"FATAL ERROR: {name} failed")
        print(traceback.format_exc())
        raise


async def main() -> None:
    _configure_global_exception_handlers()
    _log_environment()
    try:
        _log_startup("Initializing database")
        init_db()
        _log_startup("Database initialized")
    except Exception:
        print("FATAL ERROR: database initialization failed")
        print(traceback.format_exc())
        raise

    try:
        _log_startup("Starting Flask")
        run_flask_in_thread()
    except Exception:
        print("FATAL ERROR: Flask startup failed")
        print(traceback.format_exc())
        raise

    tasks = []
    if telegram_service._configured:
        tasks.append(asyncio.create_task(_run_named_task("worker", start_worker())))
        from storage.db import list_enabled_groups
        enabled_groups = list_enabled_groups()
        if enabled_groups:
            tasks.append(asyncio.create_task(_run_named_task("group worker", start_group_worker())))
        else:
            _log_startup("GROUP WORKER NOT STARTED: enabled_groups=0")
            logger.info("GROUP WORKER NOT STARTED: enabled_groups=0")
    else:
        _log_startup("Skipping worker startup because API_ID/API_HASH are missing")

    if CONTROL_BOT_TOKEN:
        tasks.append(asyncio.create_task(_run_named_task("controller", start_controller())))
    else:
        _log_startup("Skipping controller startup because CONTROL_BOT_TOKEN is missing")

    if not tasks:
        _log_startup("No background services configured; waiting for process health")
        await asyncio.Event().wait()
        return

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in done:
        task.result()
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print("FATAL ERROR")
        print(traceback.format_exc())
        raise
