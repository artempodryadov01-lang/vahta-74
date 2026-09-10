
**delete_webhook.py:**
```python
"""
Скрипт для удаления вебхука Telegram-бота.
Используйте при переходе с webhook на polling.
"""
import asyncio
import os
from dotenv import load_dotenv
from aiogram import Bot

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

async def delete_webhook():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN не установлен в .env")
        return

    bot = Bot(token=BOT_TOKEN)
    try:
        webhook_info = await bot.get_webhook_info()
        print(f"📡 Текущий вебхук: {webhook_info.url or 'не установлен'}")

        await bot.delete_webhook(drop_pending_updates=True)
        print("✅ Вебхук успешно удалён")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(delete_webhook())