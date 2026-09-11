import asyncio
import logging
import os
import sys

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import database as db
from handlers import router, handle_herosms_webhook, get_bot_instance, set_bot_instance

TOKEN = os.getenv("BOT_TOKEN", "8202597792:AAFO7lRfZXwBzQuvkiO4CBBegFiluI9dMz0")
PORT  = int(os.getenv("PORT", 8080))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

async def on_startup(bot: Bot):
    await db.init_db()
    if WEBHOOK_URL:
        # টেলিগ্রাম ওয়েবহুক সেট করা হচ্ছে
        await bot.set_webhook(f"{WEBHOOK_URL}/webhook/telegram")
        logging.info(f"Telegram Webhook set to: {WEBHOOK_URL}/webhook/telegram")
    else:
        logging.warning("WEBHOOK_URL is not set! Telegram updates will not work.")

async def on_shutdown(bot: Bot):
    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Webhook deleted on shutdown.")

async def handle_ping(request):
    return web.Response(text="Bot is alive and highly optimized!")

def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not WEBHOOK_URL:
        logging.error("You must set the WEBHOOK_URL environment variable to run this unified webhook server.")
        sys.exit(1)

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    set_bot_instance(bot)
    
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    app = web.Application()
    
    # Health Check
    app.router.add_get("/", handle_ping)
    
    # HeroSMS Webhook (Strictly POST as per API docs)
    app.router.add_post("/herosms_webhook", handle_herosms_webhook)

    # Telegram Webhook
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    ).register(app, path="/webhook/telegram")
    
    setup_application(app, dp, bot=bot)
    
    logging.info(f"Starting unified Web Server on port {PORT}...")
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot stopped.")
