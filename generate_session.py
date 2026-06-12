import os

from dotenv import load_dotenv
from telethon.sessions import StringSession
from telethon.sync import TelegramClient


def main() -> None:
    load_dotenv()
    api_id = os.getenv("API_ID") or os.getenv("api_id")
    api_hash = os.getenv("API_HASH") or os.getenv("api_hash")

    if not api_id or not api_id.isdigit():
        raise RuntimeError("API_ID must be set to a numeric value")
    if not api_hash:
        raise RuntimeError("API_HASH must be set")

    with TelegramClient(StringSession(), int(api_id), api_hash) as client:
        print(client.session.save())


if __name__ == "__main__":
    main()
