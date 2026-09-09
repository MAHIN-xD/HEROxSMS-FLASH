import asyncio
import logging
from aiohttp import web
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext

import database as db
from api_client import HeroSMSClient
import keyboards as kb
from states import BotStates

router = Router()
global_bot = None

def set_bot_instance(bot):
    global global_bot
    global_bot = bot

def get_bot_instance():
    return global_bot

ADMIN_ID    = 7266067201
COLOMBIA_ID = 33
TG_SERVICE  = "tg"
MAX_PRICE   = 0.135

ALLOWED_WEBHOOK_IPS = ["84.32.223.53", "185.138.88.87"]

# --- Webhook Handler for HeroSMS ---
async def handle_herosms_webhook(request):
    """
    HeroSMS will call this when an SMS arrives.
    Example GET params: ?action=STATUS_OK&activationId=12345&code=99999
    """
    # IP Whitelist check (optional if proxy hides IP, but good to have)
    # peer_ip = request.remote
    # if peer_ip not in ALLOWED_WEBHOOK_IPS:
    #    logging.warning(f"Webhook hit from unauthorized IP: {peer_ip}")
    #    return web.Response(text="Unauthorized", status=403)

    action = request.query.get("action")
    aid = request.query.get("activationId") or request.query.get("id")
    code = request.query.get("code")

    if not action or not aid:
        return web.Response(text="Missing parameters", status=400)

    if action == "STATUS_OK" and code:
        row = await db.get_activation_user(aid)
        if row:
            user_id = row[0]
            phone = row[1]
            text = f"📱 **Number:** `+{phone}`\n💬 **OTP:** `{code}`"
            bot = get_bot_instance()
            if bot:
                try:
                    await bot.send_message(
                        user_id,
                        text,
                        reply_markup=kb.otp_copy_menu(code),
                        parse_mode="Markdown"
                    )
                    user = await db.get_user(user_id)
                    client = HeroSMSClient(user["api_key"])
                    await client.set_status(aid, 6)
                    await db.delete_activation(aid)
                except Exception as e:
                    logging.error(f"Failed to send webhook OTP to {user_id}: {e}")
        return web.Response(text="OK")
    return web.Response(text="Ignored")

async def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    user = await db.get_user(user_id)
    if user and user["is_banned"]: return False
    maintenance = await db.get_setting("maintenance")
    if maintenance == "1": return False
    return True

# --- Polling Fallback (in case webhook fails) ---
async def poll_sms(bot, chat_id: int, activation_id: str, phone: str, client: HeroSMSClient):
    for _ in range(200): # ~10 mins
        await asyncio.sleep(3)
        # Check if already processed by webhook
        row = await db.get_activation_user(activation_id)
        if not row:
            return # Already handled and deleted from DB
            
        try:
            res = await client.get_status(activation_id)
            if isinstance(res, str):
                if res.startswith("STATUS_OK:"):
                    code = res.split(":", 1)[1]
                    text = f"📱 **Number:** `+{phone}`\n💬 **OTP:** `{code}`"
                    await bot.send_message(
                        chat_id, text,
                        reply_markup=kb.otp_copy_menu(code),
                        parse_mode="Markdown"
                    )
                    await client.set_status(activation_id, 6)
                    await db.delete_activation(activation_id)
                    return
                elif res.startswith("STATUS_CANCEL"):
                    await db.delete_activation(activation_id)
                    return
        except Exception:
            pass

# --- /start ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id)
    user = await db.get_user(message.from_user.id)

    if user and user["is_banned"]:
        await message.answer("You are banned from using this bot.")
        return

    maintenance = await db.get_setting("maintenance")
    if maintenance == "1" and message.from_user.id != ADMIN_ID:
        await message.answer("🛠 Bot is under maintenance. Contact @Syedmahinislam.")
        return

    if not user or not user["api_key"]:
        await message.answer("Welcome to HeroSMS Bot!\n\nPlease send your HeroSMS API Key to get started.")
        await state.set_state(BotStates.waiting_for_api_key)
    else:
        await message.answer("Welcome back! 👋", reply_markup=kb.main_reply_menu())

# --- API Key Setup ---
@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    api_key = message.text.strip().strip("\"'").strip()
    client  = HeroSMSClient(api_key)
    balance = await client.get_balance()
    if balance is not None:
        await db.update_api_key(message.from_user.id, api_key)
        await state.clear()
        await message.answer(
            f"✅ API Key saved successfully!\n💰 **Balance:** `{balance:.4f} USD`",
            reply_markup=kb.main_reply_menu(),
            parse_mode="Markdown"
        )
    else:
        res = await client._get("getBalance")
        err_msg = ""
        if isinstance(res, dict):
            err_msg = res.get("title") or res.get("details") or str(res)
        elif isinstance(res, str):
            err_msg = res
        
        if err_msg and err_msg != "None":
            await message.answer(f"❌ Invalid API Key (`{err_msg}`). Please check and try again.", parse_mode="Markdown")
        else:
            await message.answer("❌ Invalid API Key. Please check and try again.")

# --- Menus ---
@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_allowed(callback.from_user.id): return
    try: await callback.message.delete()
    except: pass
    await callback.message.answer("Welcome back! 👋", reply_markup=kb.main_reply_menu())

@router.message(F.text == "Profile")
async def text_profile(message: Message):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]: return
    client = HeroSMSClient(user["api_key"])
    balance = await client.get_balance()
    bal_str = f"{balance:.4f} USD" if balance is not None else "Error"
    text = f"👤 **Profile**\n\n💰 **Balance:** {bal_str}\n🔑 **API Key:** `{user['api_key'][:12]}...`"
    await message.answer(text, reply_markup=kb.profile_menu(), parse_mode="Markdown")

@router.callback_query(F.data == "profile_change_key")
async def cb_change_key(callback: CallbackQuery, state: FSMContext):
    if not await is_allowed(callback.from_user.id): return
    await callback.message.edit_text("Please send your new HeroSMS API Key.", reply_markup=kb.back_button())
    await state.set_state(BotStates.waiting_for_api_key)

@router.message(F.text == "Balance")
async def text_balance(message: Message):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    client = HeroSMSClient(user["api_key"])
    balance = await client.get_balance()
    if balance is not None:
        await message.answer(f"💰 **Balance:** `{balance:.4f} USD`", parse_mode="Markdown")
    else:
        await message.answer("❌ Error fetching balance.")

@router.message(F.text == "Support")
async def text_support(message: Message):
    await message.answer("📞 **Support:** @Syedmahinislam", parse_mode="Markdown")

# --- Single Buy ---
@router.message(F.text == "Buy Telegram Number")
async def text_buy_tg_number(message: Message):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    client = HeroSMSClient(user["api_key"])
    prices = await client.get_prices(country=COLOMBIA_ID, service=TG_SERVICE)
    try:
        cost = prices[str(COLOMBIA_ID)][TG_SERVICE]["cost"]
        count = prices[str(COLOMBIA_ID)][TG_SERVICE]["count"]
    except:
        await message.answer("❌ Pricing not available right now.")
        return
    text = (f"🛒 **Purchase Info**\n\n🌍 Country: Colombia\n📱 Service: Telegram\n\n"
            f"💰 Price: `{cost} USD`\n📦 Available: `{count}` numbers\n\nDo you want to buy?")
    await message.answer(text, reply_markup=kb.confirm_number_menu(COLOMBIA_ID, TG_SERVICE), parse_mode="Markdown")

@router.callback_query(F.data.startswith("buy_"))
async def cb_buy_number(callback: CallbackQuery):
    parts = callback.data.split("_")
    country_id, service = parts[1], parts[2]
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    await callback.message.edit_text("⏳ Buying number...")
    res = await client.get_number(service=service, country=country_id, max_price=MAX_PRICE)

    if not isinstance(res, dict) or "activationId" not in res:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.message.edit_text(f"❌ Failed: {err}")
        return

    aid = str(res["activationId"])
    phone = res.get("phoneNumber", "Unknown")
    cost = res.get("activationCost", "?")

    await db.save_activation(aid, callback.from_user.id, phone)
    text = f"✅ **Number Purchased!**\n\n📱 **Phone:** `+{phone}`\n💰 **Cost:** `{cost} USD`\n\nWaiting for SMS..."
    await callback.message.edit_text(text, reply_markup=kb.number_action_menu(aid), parse_mode="Markdown")
    asyncio.create_task(poll_sms(callback.bot, callback.message.chat.id, aid, phone, client))

# --- Single Cancel Command ---
@router.message(Command("cancel"))
async def cmd_cancel_number(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Please specify the number or activation ID.\nUsage: `/cancel +573...` or `/cancel 12345`", parse_mode="Markdown")
        return
    
    target = args[1].replace("+", "").strip()
    user = await db.get_user(message.from_user.id)
    client = HeroSMSClient(user["api_key"])
    
    # Need to find activation ID if phone is provided
    res = await client.get_active_activations()
    aid_to_cancel = target
    
    if isinstance(res, dict) and res.get("status") == "success":
        for act in res.get("data", []):
            if str(act.get("phoneNumber")) == target or str(act.get("activationId")) == target:
                aid_to_cancel = str(act.get("activationId"))
                break
                
    cancel_res = await client.set_status(aid_to_cancel, 8)
    if isinstance(cancel_res, str) and (cancel_res.startswith("ACCESS_CANCEL") or cancel_res.startswith("STATUS_CANCEL")):
        await db.delete_activation(aid_to_cancel)
        await message.answer(f"✅ Successfully cancelled `{target}`. Balance refunded.", parse_mode="Markdown")
    elif isinstance(cancel_res, str) and "EARLY_CANCEL_DENIED" in cancel_res:
        await message.answer("❌ Cannot cancel within first 2 minutes.")
    else:
        err = cancel_res.get("title", str(cancel_res)) if isinstance(cancel_res, dict) else str(cancel_res)
        await message.answer(f"❌ Failed to cancel `{target}`: {err}", parse_mode="Markdown")

@router.callback_query(F.data.startswith("single_cancel_"))
async def cb_cancel_single(callback: CallbackQuery):
    aid = callback.data[len("single_cancel_"):]
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    res = await client.set_status(aid, 8)
    if isinstance(res, str) and res.startswith("ACCESS_CANCEL"):
        await db.delete_activation(aid)
        await callback.message.edit_text("✅ Cancelled. Balance refunded.", reply_markup=kb.back_button())
    elif isinstance(res, str) and "EARLY_CANCEL_DENIED" in res:
        await callback.answer("Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.answer(f"Error: {err}", show_alert=True)

# --- Check SMS Manually ---
@router.callback_query(F.data.startswith("check_"))
async def cb_check_sms(callback: CallbackQuery):
    aid = callback.data[len("check_"):]
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    res = await client.get_status(aid)
    if isinstance(res, str):
        if res.startswith("STATUS_OK:"):
            code = res.split(":", 1)[1]
            await callback.message.edit_text(f"📩 **OTP Received!**\n\n`{code}`", reply_markup=kb.otp_copy_menu(code), parse_mode="Markdown")
            await client.set_status(aid, 6)
            await db.delete_activation(aid)
        elif res.startswith("STATUS_WAIT_CODE"):
            await callback.answer("Still waiting for SMS...", show_alert=True)
        elif res.startswith("STATUS_CANCEL"):
            await db.delete_activation(aid)
            await callback.message.edit_text("❌ Activation cancelled.", reply_markup=kb.back_button())
        else:
            await callback.answer(f"Status: {res}", show_alert=True)
    else:
        await callback.answer("Error checking status.", show_alert=True)

# --- Bulk Buy ---
@router.message(F.text == "Bulk Buy Numbers")
async def text_bulk_buy(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]: return
    await message.answer("🔢 **Bulk Purchase**\n\nHow many numbers do you want to buy? (1-500)", parse_mode="Markdown")
    await state.set_state(BotStates.waiting_for_bulk_amount)

@router.message(BotStates.waiting_for_bulk_amount)
async def process_bulk_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        if not (1 <= amount <= 500): raise ValueError
    except:
        await message.answer("⚠️ Enter a number between 1 and 500.")
        return

    await state.clear()
    user = await db.get_user(message.from_user.id)
    client = HeroSMSClient(user["api_key"])

    status_msg = await message.answer(f"⏳ Buying **{amount}** numbers... 0%", parse_mode="Markdown")
    
    purchased = []
    for i in range(amount):
        res = await client.get_number(service=TG_SERVICE, country=COLOMBIA_ID, max_price=MAX_PRICE)
        if isinstance(res, dict) and "activationId" in res:
            aid = str(res["activationId"])
            phone = res.get("phoneNumber", "Unknown")
            purchased.append(phone)
            await db.save_activation(aid, message.from_user.id, phone)
            asyncio.create_task(poll_sms(message.bot, message.chat.id, aid, phone, client))
            
            # Live Update
            try:
                lines = "\n".join(f"{n}. `+{p}`" for n, p in enumerate(purchased, 1))
                upd_text = f"⏳ **Buying {amount} numbers...** ({len(purchased)}/{amount})\n\n{lines}"
                if len(upd_text) > 4000: upd_text = upd_text[:3990] + "..."
                await status_msg.edit_text(upd_text, parse_mode="Markdown")
            except:
                pass
            await asyncio.sleep(0.3)
        else:
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message.answer(f"❌ Stopped at #{i+1}: {err}")
            break

    if purchased:
        lines = "\n".join(f"{n}. `+{p}`" for n, p in enumerate(purchased, 1))
        final = f"✅ **Bulk Order Done!**\n\nPurchased **{len(purchased)}** numbers:\n\n{lines}"
        if len(final) > 4000:
            for part in [final[i:i+4000] for i in range(0, len(final), 4000)]:
                await message.answer(part, parse_mode="Markdown")
            await status_msg.delete()
        else:
            await status_msg.edit_text(final, parse_mode="Markdown")
    else:
        await status_msg.edit_text("❌ Could not purchase any numbers.")

# --- Active Numbers & Cancel All ---
@router.message(F.text == "Active Numbers")
async def text_active_numbers(message: Message):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]: return
    client = HeroSMSClient(user["api_key"])
    status_msg = await message.answer("⏳ Fetching active numbers...")
    res = await client.get_active_activations()

    if not (isinstance(res, dict) and res.get("status") == "success"):
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await status_msg.edit_text(f"❌ Error: {err}")
        return

    activations = res.get("data", [])
    if not activations:
        await status_msg.edit_text("✅ No active numbers.")
        return

    await status_msg.edit_text(
        f"📱 **Active Numbers ({len(activations)}):**",
        reply_markup=kb.active_numbers_menu(activations),
        parse_mode="Markdown"
    )

@router.callback_query(F.data == "cancel_all_active")
async def cb_cancel_all_active(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id): return
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    res = await client.get_active_activations()
    if not (isinstance(res, dict) and res.get("status") == "success"):
        await callback.answer("Failed to fetch active numbers.", show_alert=True)
        return
    activations = res.get("data", [])
    if not activations:
        await callback.answer("No active numbers to cancel.", show_alert=True)
        return

    await callback.message.edit_text(f"⏳ Cancelling {len(activations)} numbers... please wait.", parse_mode="Markdown")

    async def cancel_one(act):
        aid = str(act.get("activationId", ""))
        if not aid: return False
        try:
            r = await client.set_status(aid, 8)
            if isinstance(r, str) and (r.startswith("ACCESS_CANCEL") or r.startswith("STATUS_CANCEL")):
                await db.delete_activation(aid)
                return True
            if isinstance(r, dict) and r.get("status") == "success":
                await db.delete_activation(aid)
                return True
        except: pass
        return False

    results = await asyncio.gather(*[cancel_one(a) for a in activations])
    ok = sum(1 for x in results if x)
    await callback.message.edit_text(f"✅ Cancelled **{ok}/{len(activations)}** numbers. Balance refunded.", parse_mode="Markdown")

@router.callback_query(F.data.startswith("active_cancel_"))
async def cb_active_cancel(callback: CallbackQuery):
    aid = callback.data[len("active_cancel_"):]
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    r = await client.set_status(aid, 8)
    if isinstance(r, str) and (r.startswith("ACCESS_CANCEL") or r.startswith("STATUS_CANCEL")):
        await db.delete_activation(aid)
        await callback.answer("✅ Cancelled!", show_alert=True)
        # re-fetch and update menu
        res = await client.get_active_activations()
        if isinstance(res, dict) and res.get("status") == "success":
            acts = res.get("data", [])
            if not acts:
                await callback.message.edit_text("✅ No active numbers left.")
            else:
                await callback.message.edit_reply_markup(reply_markup=kb.active_numbers_menu(acts))
    elif isinstance(r, str) and "EARLY_CANCEL_DENIED" in r:
        await callback.answer("❌ Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        await callback.answer("❌ Failed to cancel.", show_alert=True)

# --- Admin ---
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID: return
    maintenance = await db.get_setting("maintenance")
    await message.answer("Admin Panel", reply_markup=kb.admin_menu(maintenance == "1"))

@router.callback_query(F.data == "admin_maintenance")
async def cb_admin_maintenance(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    current = await db.get_setting("maintenance")
    new_val = "0" if current == "1" else "1"
    await db.set_setting("maintenance", new_val)
    await callback.message.edit_reply_markup(reply_markup=kb.admin_menu(new_val == "1"))
    await callback.answer("Maintenance updated.")

@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("Send the broadcast message:", reply_markup=kb.back_button())
    await state.set_state(BotStates.waiting_for_broadcast)

@router.message(BotStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    users = await db.get_all_users()
    sent = 0
    for uid in users:
        try:
            await message.bot.send_message(uid, f"📢 **Broadcast:**\n\n{message.text}", parse_mode="Markdown")
            sent += 1
        except: pass
    await message.answer(f"✅ Sent to {sent} users.")
    await state.clear()

@router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer("Send the user ID to ban/unban:", reply_markup=kb.back_button())
    await state.set_state(BotStates.waiting_for_ban_id)

@router.message(BotStates.waiting_for_ban_id)
async def process_ban_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try: target = int(message.text.strip())
    except:
        await message.answer("Invalid ID.")
        return
    user = await db.get_user(target)
    if not user:
        await message.answer("User not found.")
        return
    new_status = not bool(user["is_banned"])
    await db.set_ban_status(target, new_status)
    label = "Banned" if new_status else "Unbanned"
    await message.answer(f"✅ User {target} {label}.")
    await state.clear()

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()
