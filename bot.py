import asyncio
import logging
import os

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import BotCommand, BotCommandScopeDefault

import database as db
from handlers import router, handle_herosms_webhook, set_bot_instance

TOKEN = os.getenv("BOT_TOKEN")
PORT  = int(os.getenv("PORT", 8080))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").rstrip("/")

async def on_startup(bot: Bot):
    await db.init_db()
    
    # টেলিগ্রামে '/' চাপলে যে পূর্ণাঙ্গ কমান্ড মেনু পপ-আপ হবে
    commands = [
        BotCommand(command="start", description="বট শুরু করুন / মেনু দেখুন"),
        BotCommand(command="stats", description="আজকের সাকসেস রেট ও পরিসংখ্যান দেখুন"),
        BotCommand(command="getallsms", description="সবগুলো ওটিপি দেখুন (/getallsms <ID/নম্বর>)"),
        BotCommand(command="history", description="সাম্প্রতিক অ্যাক্টিভেশন হিস্ট্রি দেখুন"),
        BotCommand(command="act_history", description="বিগত ৭ দিনের মোট খরচ ও সামারি রিপোর্ট"),
        BotCommand(command="retry", description="কোড পুনরায় চাইতে (/retry <নম্বর/ID>)"),
        BotCommand(command="cancel", description="চলমান নম্বর বাতিল করুন (/cancel <নম্বর/ID>)"),
        BotCommand(command="exclude", description="প্রিফিক্স ব্লকলিস্টে যোগ করুন (/exclude 57300)"),
        BotCommand(command="unexclude", description="প্রিফিক্স ব্লকলিস্ট থেকে সরান (/unexclude 57350)"),
        BotCommand(command="exclude_list", description="বাদ থাকা প্রিফিক্স তালিকা দেখুন"),
        BotCommand(command="reset_exclude", description="ব্লকলিস্ট রিসেট করে ডিফল্ট 57350 করুন"),
        BotCommand(command="operator", description="অপারেটর সেট করুন (/operator claro,tigo বা any)"),
        BotCommand(command="operator_list", description="বর্তমান অপারেটর দেখুন"),
        BotCommand(command="reset_operator", description="অপারেটর রিসেট করে any করুন"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    logging.info("Telegram bot command menu registered successfully.")

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
    
    # HeroSMS Webhook এন্ডিং রাউট
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
