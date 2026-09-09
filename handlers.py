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

# --- Webhook Handler ---
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
            text = f"Number: +{phone}\nOTP: {code}"
            
            bot = get_bot_instance()
            if bot:
                try:
                    await bot.send_message(user_id, text, reply_markup=kb.otp_copy_menu(code))
                    user = await db.get_user(user_id)
                    if user and user["api_key"]:
                        client = HeroSMSClient(user["api_key"])
                        await client.set_status(aid, 6)
                    await db.delete_activation(aid)
                except Exception as e:
                    logging.error(f"Error sending OTP to user {user_id}: {e}")
        return web.Response(text="OK")
    
    return web.Response(text="Ignored")

# --- Command & Start Handlers ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id)
    user = await db.get_user(message.from_user.id)

    if user and user["is_banned"]:
        await message.answer("You are banned from using this bot.")
        return

    if not user or not user["api_key"]:
        await message.answer(
            "Welcome!\n\nPlease send your HeroSMS API Key to start.",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(BotStates.waiting_for_api_key)
    else:
        await message.answer("Welcome back!", reply_markup=kb.main_reply_menu())

# --- Process API Key Input ---
@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    api_key = message.text.strip().strip("\"'").strip()
    
    client = HeroSMSClient(api_key)
    balance = await client.get_balance()
    
    if balance is not None:
        await db.update_api_key(message.from_user.id, api_key)
        await state.clear()
        await message.answer(
            f"API Key saved successfully!\nBalance: {balance:.4f} USD",
            reply_markup=kb.main_reply_menu()
        )
    else:
        await message.answer("Invalid API Key. Please check and send again.")

# --- Reply Keyboard Button Handlers ---

@router.message(F.text.in_(["Buy Telegram Number", "Buy Number"]))
async def buy_colombia_number(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]:
        await message.answer("Please set your API key first using /start.")
        return

    client = HeroSMSClient(user["api_key"])
    res = await client.buy_colombia_telegram_number(max_price=MAX_PRICE)

    if isinstance(res, dict) and res.get("status") == "SUCCESS":
        act_id = str(res.get("activationId"))
        phone = str(res.get("phoneNumber"))
        
        await db.add_activation(act_id, message.from_user.id, phone, TG_SERVICE, COLOMBIA_ID)
        
        msg = f"Number: +{phone}\nID: {act_id}\n\nWaiting for OTP..."
        await message.answer(msg, reply_markup=kb.cancel_activation_menu(act_id))
    else:
        error_msg = res.get("message", "NO_NUMBERS") if isinstance(res, dict) else str(res)
        await message.answer(f"Failed to get number. Error: {error_msg}")

@router.message(F.text == "Balance")
async def check_balance(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]:
        await message.answer("Please set your API key first using /start.")
        return

    client = HeroSMSClient(user["api_key"])
    balance = await client.get_balance()
    if balance is not None:
        await message.answer(f"Balance: {balance:.4f} USD")
    else:
        await message.answer("Error checking balance. Check your API Key.")

@router.message(F.text == "Profile")
async def show_profile(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.answer("User profile not found.")
        return
    
    api_status = "Set" if user["api_key"] else "Not Set"
    await message.answer(f"User ID: {message.from_user.id}\nAPI Key Status: {api_status}")

# --- Callback Handlers (Cancel Activation) ---

@router.callback_query(F.data.startswith("cancel_"))
async def cancel_activation_callback(query: CallbackQuery):
    act_id = query.data.split("_")[1]
    user = await db.get_user(query.from_user.id)
    
    if user and user["api_key"]:
        client = HeroSMSClient(user["api_key"])
        await client.set_status(act_id, 8) # Status 8 = Cancel
        await db.delete_activation(act_id)
        await query.message.edit_text(f"Activation {act_id} cancelled successfully.")
    else:
        await query.answer("Failed to cancel activation.", show_alert=True)
