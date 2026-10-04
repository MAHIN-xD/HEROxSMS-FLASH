import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import BotCommand, BotCommandScopeDefault

import database as db
from handlers import (
    router, 
    handle_herosms_webhook, 
    set_bot_instance, 
    start_periodic_janitor
)

TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.getenv("PORT", 8080))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

async def on_startup(bot: Bot):
    await db.init_db()[cite: 3]
    asyncio.create_task(start_periodic_janitor())[cite: 3]
    
    # Shudhu ei 2-ti command suggestion hishebe dekhabe
    commands = [
        BotCommand(command="start", description="Start or Restart Bot"),
        BotCommand(command="t", description="Open Control Tools Dashboard")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logging.info("Telegram command suggestions registered (start & t).")

    if WEBHOOK_URL:
        webhook_path = f"{WEBHOOK_URL}/webhook/telegram"[cite: 3]
        await bot.set_webhook(webhook_path)[cite: 3]
        logging.info(f"Telegram webhook set to: {webhook_path}")[cite: 3]
    else:
        await bot.delete_webhook(drop_pending_updates=True)[cite: 3]
        logging.info("Running in polling mode, deleted webhooks")[cite: 3]

async def handle_ping(request):
    return web.Response(text="HeroSMS Bot is alive!")

def main():
    if not TOKEN:
        raise ValueError("BOT_TOKEN environment variable is missing!")[cite: 3]

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")[cite: 3]

    bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))[cite: 3]
    set_bot_instance(bot)[cite: 3]
    
    dp = Dispatcher()[cite: 3]
    dp.include_router(router)[cite: 3]
    dp.startup.register(on_startup)[cite: 3]

    app = web.Application()[cite: 3]
    app.router.add_get("/", handle_ping)[cite: 3]
    app.router.add_get("/webhook", handle_herosms_webhook)[cite: 3]
    app.router.add_post("/webhook", handle_herosms_webhook)[cite: 3]
    app.router.add_get("/herosms_webhook", handle_herosms_webhook)[cite: 3]
    app.router.add_post("/herosms_webhook", handle_herosms_webhook)[cite: 3]

    if WEBHOOK_URL:
        SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
        ).register(app, path="/webhook/telegram")[cite: 3]
        setup_application(app, dp, bot=bot)[cite: 3]
        
        logging.info(f"Starting web server on port {PORT} with Telegram Webhook")[cite: 3]
        web.run_app(app, host="0.0.0.0", port=PORT)[cite: 3]
    else:
        async def start_polling_and_server():
            runner = web.AppRunner(app)[cite: 3]
            await runner.setup()[cite: 3]
            site = web.TCPSite(runner, "0.0.0.0", PORT)[cite: 3]
            await site.start()[cite: 3]
            logging.info(f"Web server running on port {PORT} (HeroSMS Webhook active, Telegram Polling active)")[cite: 3]
            
            await bot.delete_webhook(drop_pending_updates=True)[cite: 3]
            await dp.start_polling(bot)[cite: 3]
            
        asyncio.run(start_polling_and_server())[cite: 3]

if __name__ == "__main__":
    try:
        main()[cite: 3]
    except KeyboardInterrupt:
        print("Bot stopped.")[cite: 3]
