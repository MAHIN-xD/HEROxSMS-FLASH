import asyncio
import logging
from aiohttp import web
from aiogram import Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

import database as db
from api_client import HeroSMSClient
import keyboards as kb
from states import BotStates

router = Router()
global_bot = None

ADMIN_ID = 7266067201
COLOMBIA_ID = 33
TG_SERVICE = "tg"
MAX_PRICE = 0.135

def set_bot_instance(bot):
    global global_bot
    global_bot = bot

def get_bot_instance():
    return global_bot

# --- Webhook Handler (HeroSMS থেকে OTP গ্রহণের জন্য) ---
async def handle_herosms_webhook(request):
    aid = request.query.get("activationId") or request.query.get("id")
    code = request.query.get("code")
    action = request.query.get("action")

    if not aid:
        return web.Response(text="Missing activationId", status=400)

    if action == "STATUS_OK" or code:
        row = await db.get_activation_user(aid)
        if row:
            user_id, phone = row[0], row[1]
            text = f"🇨🇴 <b>Colombia Telegram OTP</b>\n\n📱 <b>Number:</b> <code>+{phone}</code>\n💬 <b>OTP Code:</b> <code>{code}</code>"
            
            bot = get_bot_instance()
            if bot:
                try:
                    await bot.send_message(user_id, text, reply_markup=kb.otp_copy_menu(code))
                    user = await db.get_user(user_id)
                    if user and user["api_key"]:
                        client = HeroSMSClient(user["api_key"])
                        await client.set_status(aid, 6) # অটোম্যাটিক কমপ্লিট স্ট্যাটাস পাঠানো
                    await db.delete_activation(aid)
                except Exception as e:
                    logging.error(f"Error handling OTP for user {user_id}: {e}")
        return web.Response(text="OK")
    
    return web.Response(text="Ignored")

# --- Telegram Bot Command Handlers ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id)
    user = await db.get_user(message.from_user.id)

    if user and user.get("is_banned"):
        await message.answer("❌ You are banned from using this bot.")
        return

    if not user or not user.get("api_key"):
        await message.answer(
            "Welcome to Colombia Telegram SMS Bot! 🇨🇴\n\nPlease send your <b>HeroSMS API Key</b> to start.",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(BotStates.waiting_for_api_key)
    else:
        await message.answer("Welcome back! 👋", reply_markup=kb.main_reply_menu())

@router.message(F.text == "Buy Colombia Telegram Number")
async def buy_colombia_number(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or not user.get("api_key"):
        await message.answer("❌ Please set your API key first.")
        return

    client = HeroSMSClient(user["api_key"])
    res = await client.buy_colombia_telegram_number(max_price=MAX_PRICE)

    # HeroSMS API Response Verification
    if isinstance(res, dict) and res.get("status") == "SUCCESS":
        act_id = str(res.get("activationId"))
        phone = str(res.get("phoneNumber"))
        
        await db.add_activation(act_id, message.from_user.id, phone, TG_SERVICE, COLOMBIA_ID)
        
        msg = (
            f"✅ <b>Colombia Number Ordered!</b> 🇨🇴\n\n"
            f"📱 <b>Number:</b> <code>+{phone}</code>\n"
            f"🆔 <b>ID:</b> <code>{act_id}</code>\n\n"
            f"Waiting for OTP via Webhook..."
        )
        await message.answer(msg, reply_markup=kb.cancel_activation_menu(act_id))
    else:
        error_msg = res.get("message", "NO_NUMBERS") if isinstance(res, dict) else str(res)
        await message.answer(f"❌ Failed to get Colombia number. Error: <code>{error_msg}</code>")
