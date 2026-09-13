import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

import database as db
from handlers import router, handle_herosms_webhook, set_bot_instance

# টোকেন Environment থেকে আসবে, কোনো হার্ডকোডেড টোকেন থাকবে না
TOKEN = os.getenv("BOT_TOKEN")
PORT  = int(os.getenv("PORT", 8080))
# শেষে স্ল্যাশ থাকলে তা কেটে ফেলা হবে
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

async def on_startup(bot: Bot):
    await db.init_db()
    if WEBHOOK_URL:
        webhook_path = f"{WEBHOOK_URL}/webhook/telegram"
        await bot.set_webhook(webhook_path)
        logging.info(f"Telegram webhook set to: {webhook_path}")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Running in polling mode, deleted webhooks")

async def handle_ping(request):
    return web.Response(text="Bot is alive!")

def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN environment variable is missing!")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    set_bot_instance(bot)
    
    dp = Dispatcher()
    dp.include_router(router)
    dp.startup.register(on_startup)

    app = web.Application()
    
    app.router.add_get("/", handle_ping)
    
    # দুটো পাথই চালু রাখা হলো যাতে যেকোনো লিংক দিলেই কাজ করে
    app.router.add_get("/webhook", handle_herosms_webhook)
    app.router.add_post("/webhook", handle_herosms_webhook)
    app.router.add_get("/herosms_webhook", handle_herosms_webhook)
    app.router.add_post("/herosms_webhook", handle_herosms_webhook)

    if WEBHOOK_URL:
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
        ).register(app, path="/webhook/telegram")
        setup_application(app, dp, bot=bot)
        
        logging.info(f"Starting web server on port {PORT} with Telegram Webhook")
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:
        async def start_polling_and_server():
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", PORT)
            await site.start()
            logging.info(f"Web server running on port {PORT} (HeroSMS Webhook active, Telegram Polling active)")
            
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
            
        asyncio.run(start_polling_and_server())

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Bot stopped.")
