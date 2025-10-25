import os
import asyncio
from telegram import Bot

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

async def main():
    assert TOKEN and CHAT_ID, "ENV vars missing"
    bot = Bot(TOKEN)
    await bot.send_message(chat_id=CHAT_ID, text="✅ Railway test OK. Bot can post to your channel.")
    print("Test message sent successfully.")

if __name__ == "__main__":
    asyncio.run(main())
