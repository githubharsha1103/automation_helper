import asyncio
import logging

from automation.worker import telegram_service, start_group_worker, start_worker
from controller.controller import start_controller
from storage.db import init_db
from web.server import run_flask_in_thread


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)


async def main() -> None:
    init_db()
    await telegram_service.ensure_authorized_session()
    run_flask_in_thread()
    await asyncio.gather(
        start_worker(),
        start_group_worker(),
        start_controller(),
    )


if __name__ == "__main__":
    asyncio.run(main())
