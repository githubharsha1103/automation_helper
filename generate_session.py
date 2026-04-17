import os
from telethon import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("Enter API_ID: "))
api_hash = input("Enter API_HASH: ")

print("Generating session... Login with OTP/Telegram app when prompted.")

with TelegramClient(StringSession(), api_id, api_hash) as client:
    client.start()
    session_string = client.session.save()
    print("\n" + "="*50)
    print("SESSION_STRING (copy this to Render):")
    print(session_string)
    print("="*50)