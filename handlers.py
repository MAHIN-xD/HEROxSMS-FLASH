import asyncio
import logging
import html
import re
from aiohttp import web
from aiogram import Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext

import database as db
from api_client import HeroSMSClient
import keyboards as kb
from states import BotStates

# ==================== PREMIUM EMOJI SYSTEM ====================
CUSTOM_EMOJI_MAP = {
    "💎": "6271537028307881531", "👑": "6269556155031228243",
    "🔥": "6100575060521653786", "⚡️": "6100400465806104855",
    "💸": "6098101064869893253", "✅": "6097949525538772310",
    "❌": "6100304499056844996", "🏆": "6194737030165959506",
    "👤": "5352861489541714456", "💰": "6233367447789899509",
    "🔑": "6176966310920983412", "🌍": "6098236425059178463",
    "📶": "5231200819986047254", "🚀": "5188481279963715781",
    "⚙️": "5341715473882955310", "🛡": "6100129320225741245",
    "🌐": "6098236425059178463", "📢": "5251671501702196837",
    "💬": "6097930030682215910", "📈": "6098163741327629439",
    "📉": "6098266184887572126", "🔔": "6098419394960955857",
    "📞": "5375338737028841420", "🆔": "5352861489541714456",
    "📩": "6269255258212404947", "📦": "6257812301399725616",
    "📋": "5429483843541284898", "➕": "5397916757333654639",
    "➖": "5244837092042750681", "🔗": "6100307857721267700",
    "⏳": "6217721388736712699", "📱": "5337010556253543833",
    "🛒": "6257812301399725616", "🚫": "6100388225149310843",
    "⚠️": "6098337704682984714", "🇨🇴": "5913773060074246009",
    "ℹ️": "6100619775426173201", "✈️": "5271801931814165886",
    "🐙": "5417836094098007862", "☑️": "5427009714745511004"
}
_CUSTOM_EMOJI_KEYS = sorted(CUSTOM_EMOJI_MAP.keys(), key=len, reverse=True)
_TAG_SPLIT_RE = re.compile(r'(<[^>]+>)')

def _wrap_plain_segment(segment):
    out, i, n = [], 0, len(segment)
    while i < n:
        for k in _CUSTOM_EMOJI_KEYS:
            if segment.startswith(k, i):
                out.append(f'<tg-emoji emoji-id="{CUSTOM_EMOJI_MAP[k]}">{k}</tg-emoji>')
                i += len(k)
                break
        else:
            out.append(segment[i])
            i += 1
    return ''.join(out)

def pe(text):
    if not text: return text
    parts = _TAG_SPLIT_RE.split(text)
    out, in_tg_emoji = [], False
    for part in parts:
        if part.startswith('<tg-emoji'):
            in_tg_emoji = True
            out.append(part)
        elif part == '</tg-emoji>':
            in_tg_emoji = False
            out.append(part)
        elif part.startswith('<') and part.endswith('>'):
            out.append(part)
        elif in_tg_emoji:
            out.append(part)
        else:
            out.append(_wrap_plain_segment(part))
    return ''.join(out)
# ==============================================================

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

MENU_BUTTONS = ["Buy Telegram Number", "Bulk Buy Numbers", "Active Numbers", "Balance", "Profile"]

def format_otp_text(phone: str, code: str) -> str:
    clean_phone = str(phone).replace('+', '')
    return pe(
        f"🇨🇴 | <b>COLOMBIA</b> | TG ✈️\n\n"
        f"☑️ | Number : <b><code>+{clean_phone}</code></b>\n"
        f"🐙 | Code : <code>{code}</code>"
    )

def _add_tick_to_markup(markup):
    # Safely intercepts the markup to add the ✅ to the OTP button text
    try:
        if markup and hasattr(markup, 'inline_keyboard') and markup.inline_keyboard:
            markup.inline_keyboard[0][0].text = f"✅ {markup.inline_keyboard[0][0].text}"
    except Exception:
        pass
    return markup

async def handle_herosms_webhook(request):
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
            text = format_otp_text(phone, code)
            bot = get_bot_instance()
            if bot:
                try:
                    markup = _add_tick_to_markup(kb.otp_copy_menu(code))
                    await bot.send_message(user_id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
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

async def poll_sms(bot, chat_id: int, activation_id: str, phone: str, client: HeroSMSClient):
    # ৩ সেকেন্ড পর পর ৪০০ বার ট্রাই করবে = ২০ মিনিট
    for _ in range(400):
        await asyncio.sleep(3)
        row = await db.get_activation_user(activation_id)
        if not row:
            return 
            
        try:
            res = await client.get_status(activation_id)
            if isinstance(res, str):
                if res.startswith("STATUS_OK:"):
                    code = res.split(":", 1)[1]
                    text = format_otp_text(phone, code)
                    markup = _add_tick_to_markup(kb.otp_copy_menu(code))
                    await bot.send_message(chat_id, text, reply_markup=markup, parse_mode=ParseMode.HTML)
                    await client.set_status(activation_id, 6)
                    await db.delete_activation(activation_id)
                    return
                elif res.startswith("STATUS_CANCEL"):
                    await db.delete_activation(activation_id)
                    return
        except Exception as e:
            logging.error(f"Polling error for {activation_id}: {e}")

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id)
    user = await db.get_user(message.from_user.id)

    if user and user["is_banned"]:
        await message.answer(pe("🚫 <b>You are banned from using this bot.</b>"), parse_mode=ParseMode.HTML)
        return

    maintenance = await db.get_setting("maintenance")
    if maintenance == "1" and message.from_user.id != ADMIN_ID:
        await message.answer(pe("⚙️ <b>Bot is under maintenance.</b> Contact Admin."), parse_mode=ParseMode.HTML)
        return

    if not user or not user["api_key"]:
        await message.answer(pe("👋 <b>Welcome to HeroSMS Bot!</b>\n\n🔑 Please send your <b>HeroSMS API Key</b> to get started."), reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)
        await state.set_state(BotStates.waiting_for_api_key)
    else:
        await message.answer(pe("✅ <b>Welcome back!</b>"), reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    text = message.text.strip()
    
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer(pe("❌ <b>Action cancelled.</b> Please try again."), parse_mode=ParseMode.HTML)

    api_key = text.strip("\"'").strip()
    client = HeroSMSClient(api_key)
    balance = await client.get_balance()
    
    if balance is not None:
        await db.update_api_key(message.from_user.id, api_key)
        await state.clear()
        await message.answer(
            pe(f"✅ <b>API Key saved successfully!</b>\n💰 Balance: <b>{balance:.4f} USD</b>"),
            reply_markup=kb.main_reply_menu(),
            parse_mode=ParseMode.HTML
        )
    else:
        res = await client._get("getBalance")
        err_msg = ""
        if isinstance(res, dict):
            err_msg = res.get("title") or res.get("details") or str(res)
        elif isinstance(res, str):
            err_msg = res
        
        err_msg = html.escape(err_msg)
        if err_msg and err_msg != "None":
            await message.answer(pe(f"❌ <b>Invalid API Key</b> (<code>{err_msg}</code>). Please check and try again."), parse_mode=ParseMode.HTML)
        else:
            await message.answer(pe("❌ <b>Invalid API Key.</b> Please check and try again."), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_allowed(callback.from_user.id): return
    try: await callback.message.delete()
    except: pass
    await callback.message.answer(pe("✅ <b>Welcome back!</b>"), reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

@router.message(F.text == "Profile")
async def text_profile(message: Message):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]: return
    client = HeroSMSClient(user["api_key"])
    balance = await client.get_balance()
    bal_str = f"{balance:.4f} USD" if balance is not None else "Error"
    text = f"👤 <b>Profile</b>\n\n💰 Balance: <b>{bal_str}</b>\n🔑 API Key: <code>{user['api_key'][:12]}...</code>"
    await message.answer(pe(text), reply_markup=kb.profile_menu(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "profile_change_key")
async def cb_change_key(callback: CallbackQuery, state: FSMContext):
    if not await is_allowed(callback.from_user.id): return
    await callback.message.edit_text(pe("🔑 <b>Please send your new HeroSMS API Key.</b>"), reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_api_key)

@router.message(F.text == "Balance")
async def text_balance(message: Message):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    client = HeroSMSClient(user["api_key"])
    balance = await client.get_balance()
    if balance is not None:
        await message.answer(pe(f"💰 <b>Balance:</b> {balance:.4f} USD"), parse_mode=ParseMode.HTML)
    else:
        await message.answer(pe("❌ <b>Error fetching balance.</b>"), parse_mode=ParseMode.HTML)

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
        await message.answer(pe("❌ <b>Pricing not available right now.</b>"), parse_mode=ParseMode.HTML)
        return
    text = (f"🛒 <b>Purchase Info</b>\n\n🌍 Country: <b>Colombia</b>\n💬 Service: <b>Telegram</b>\n\n"
            f"💸 Price: <b>{cost} USD</b>\n📦 Available: <b>{count} numbers</b>\n\n❓ <b>Do you want to buy?</b>")
    await message.answer(pe(text), reply_markup=kb.confirm_number_menu(COLOMBIA_ID, TG_SERVICE), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("buy_"))
async def cb_buy_number(callback: CallbackQuery):
    parts = callback.data.split("_")
    country_id, service = parts[1], parts[2]
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    await callback.message.edit_text(pe("⏳ <i>Buying number...</i>"), parse_mode=ParseMode.HTML)
    res = await client.get_number(service=service, country=country_id, max_price=MAX_PRICE)

    if not isinstance(res, dict) or "activationId" not in res:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.message.edit_text(pe(f"❌ <b>Failed:</b> <code>{html.escape(err)}</code>"), parse_mode=ParseMode.HTML)
        return

    aid = str(res["activationId"])
    phone = res.get("phoneNumber", "Unknown")

    await db.save_activation(aid, callback.from_user.id, phone)
    text = f"✅ <b>Number Purchased!</b>\n\n📱 Number: <b>+{phone}</b>\n🆔 ID: <code>{aid}</code>\n\n⏳ <i>Waiting for OTP...</i>"
    await callback.message.edit_text(pe(text), reply_markup=kb.number_action_menu(aid), parse_mode=ParseMode.HTML)
    asyncio.create_task(poll_sms(callback.bot, callback.message.chat.id, aid, phone, client))

@router.message(Command("cancel"))
async def cmd_cancel_number(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        await message.answer(pe("⚠️ <b>Please specify the number or activation ID.</b>\nUsage: <code>/cancel +573...</code> or <code>/cancel 12345</code>"), parse_mode=ParseMode.HTML)
        return
    
    target = args[1].replace("+", "").strip()
    user = await db.get_user(message.from_user.id)
    client = HeroSMSClient(user["api_key"])
    
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
        await message.answer(pe(f"✅ <b>Successfully cancelled <code>{target}</code>. Balance refunded.</b>"), parse_mode=ParseMode.HTML)
    elif isinstance(cancel_res, str) and "EARLY_CANCEL_DENIED" in cancel_res:
        await message.answer(pe("⚠️ <b>Cannot cancel within first 2 minutes.</b>"), parse_mode=ParseMode.HTML)
    else:
        err = cancel_res.get("title", str(cancel_res)) if isinstance(cancel_res, dict) else str(cancel_res)
        await message.answer(pe(f"❌ <b>Failed to cancel <code>{target}</code>:</b> <code>{html.escape(err)}</code>"), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("single_cancel_"))
async def cb_cancel_single(callback: CallbackQuery):
    aid = callback.data[len("single_cancel_"):]
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    res = await client.set_status(aid, 8)
    if isinstance(res, str) and res.startswith("ACCESS_CANCEL"):
        await db.delete_activation(aid)
        await callback.message.edit_text(pe("✅ <b>Cancelled. Balance refunded.</b>"), reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    elif isinstance(res, str) and "EARLY_CANCEL_DENIED" in res:
        await callback.answer("⚠️ Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.answer(f"❌ Error: {err}", show_alert=True)

@router.callback_query(F.data.startswith("check_"))
async def cb_check_sms(callback: CallbackQuery):
    aid = callback.data[len("check_"):]
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    row = await db.get_activation_user(aid)
    phone = row[1] if row else "Unknown"

    res = await client.get_status(aid)
    if isinstance(res, str):
        if res.startswith("STATUS_OK:"):
            code = res.split(":", 1)[1]
            text = format_otp_text(phone, code)
            markup = _add_tick_to_markup(kb.otp_copy_menu(code))
            await callback.message.edit_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
            await client.set_status(aid, 6)
            await db.delete_activation(aid)
        elif res.startswith("STATUS_WAIT_CODE"):
            await callback.answer("⏳ Still waiting for SMS...", show_alert=True)
        elif res.startswith("STATUS_CANCEL"):
            await db.delete_activation(aid)
            await callback.message.edit_text(pe("🚫 <b>Activation cancelled.</b>"), reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
        else:
            await callback.answer(f"ℹ️ Status: {res}", show_alert=True)
    else:
        await callback.answer("❌ Error checking status.", show_alert=True)

@router.message(F.text == "Bulk Buy Numbers")
async def text_bulk_buy(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]: return
    await message.answer(pe("📦 <b>Bulk Purchase</b>\n\n❓ <b>How many numbers do you want to buy? (1-500)</b>"), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_bulk_amount)

@router.message(BotStates.waiting_for_bulk_amount)
async def process_bulk_amount(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer(pe("❌ <b>Bulk buy cancelled.</b>"), parse_mode=ParseMode.HTML)

    try:
        amount = int(text)
        if not (1 <= amount <= 500): raise ValueError
    except:
        await message.answer(pe("⚠️ <b>Enter a valid number between 1 and 500.</b>"), parse_mode=ParseMode.HTML)
        return

    await state.clear()
    user = await db.get_user(message.from_user.id)
    client = HeroSMSClient(user["api_key"])

    status_msg = await message.answer(pe(f"⏳ <i>Buying {amount} numbers...</i>"), parse_mode=ParseMode.HTML)
    
    purchased = []
    for i in range(amount):
        res = await client.get_number(service=TG_SERVICE, country=COLOMBIA_ID, max_price=MAX_PRICE)
        if isinstance(res, dict) and "activationId" in res:
            aid = str(res["activationId"])
            phone = res.get("phoneNumber", "Unknown")
            purchased.append(phone)
            await db.save_activation(aid, message.from_user.id, phone)
            asyncio.create_task(poll_sms(message.bot, message.chat.id, aid, phone, client))
            
            # দ্রুত মেসেজ আপডেট এর জন্য (প্রতি ৩টি পারচেজে বা শেষে আপডেট হবে)
            if i % 3 == 0 or i == amount - 1:
                try:
                    lines = "\n".join(f"<b>{n}.</b> <code>+{p}</code>" for n, p in enumerate(purchased, 1))
                    upd_text = f"⏳ <i>Buying {amount} numbers... ({len(purchased)}/{amount})</i>\n\n{lines}"
                    if len(upd_text) > 3990: upd_text = upd_text[:3985] + "..."
                    await status_msg.edit_text(pe(upd_text), parse_mode=ParseMode.HTML)
                except:
                    pass
            await asyncio.sleep(0.05)  # সর্বনিম্ন ডিলে দিয়ে দ্রুততম কেনা নিশ্চিত করে
        else:
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message.answer(pe(f"❌ <b>Stopped at #{i+1}:</b> <code>{html.escape(err)}</code>"), parse_mode=ParseMode.HTML)
            break

    if purchased:
        lines = "\n".join(f"<b>{n}.</b> <code>+{p}</code>" for n, p in enumerate(purchased, 1))
        final = f"✅ <b>Bulk Order Done!</b>\n\n📦 Purchased <b>{len(purchased)}</b> numbers:\n\n{lines}"
        if len(final) > 4000:
            for part in [final[i:i+4000] for i in range(0, len(final), 4000)]:
                await message.answer(pe(part), parse_mode=ParseMode.HTML)
            await status_msg.delete()
        else:
            await status_msg.edit_text(pe(final), parse_mode=ParseMode.HTML)
    else:
        await status_msg.edit_text(pe("❌ <b>Could not purchase any numbers.</b>"), parse_mode=ParseMode.HTML)

@router.message(F.text == "Active Numbers")
async def text_active_numbers(message: Message):
    if not await is_allowed(message.from_user.id): return
    user = await db.get_user(message.from_user.id)
    if not user or not user["api_key"]: return
    client = HeroSMSClient(user["api_key"])
    status_msg = await message.answer(pe("⏳ <i>Fetching active numbers...</i>"), parse_mode=ParseMode.HTML)
    res = await client.get_active_activations()

    if not (isinstance(res, dict) and res.get("status") == "success"):
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await status_msg.edit_text(pe(f"❌ <b>Error:</b> <code>{html.escape(err)}</code>"), parse_mode=ParseMode.HTML)
        return

    activations = res.get("data", [])
    if not activations:
        await status_msg.edit_text(pe("⚠️ <b>No active numbers.</b>"), parse_mode=ParseMode.HTML)
        return

    total = len(activations)
    await status_msg.edit_text(
        pe(f"📋 <b>Active Numbers ({total})</b> - Page <b>1/{(total+9)//10}</b>:"),
        reply_markup=kb.active_numbers_menu(activations, page=0),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data.startswith("act_page_"))
async def cb_active_page(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    res = await client.get_active_activations()

    if isinstance(res, dict) and res.get("status") == "success":
        activations = res.get("data", [])
        total = len(activations)
        if not activations:
            await callback.message.edit_text(pe("⚠️ <b>No active numbers left.</b>"), parse_mode=ParseMode.HTML)
            return
        await callback.message.edit_text(
            pe(f"📋 <b>Active Numbers ({total})</b> - Page <b>{page+1}/{(total+9)//10}</b>:"),
            reply_markup=kb.active_numbers_menu(activations, page=page),
            parse_mode=ParseMode.HTML
        )

@router.callback_query(F.data == "cancel_all_active")
async def cb_cancel_all_active(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id): return
    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    res = await client.get_active_activations()
    if not (isinstance(res, dict) and res.get("status") == "success"):
        await callback.answer("❌ Failed to fetch active numbers.", show_alert=True)
        return
    activations = res.get("data", [])
    if not activations:
        await callback.answer("⚠️ No active numbers to cancel.", show_alert=True)
        return

    await callback.message.edit_text(pe(f"⏳ <i>Cancelling <b>{len(activations)}</b> numbers... please wait.</i>"), parse_mode=ParseMode.HTML)

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
    await callback.message.edit_text(pe(f"✅ <b>Cancelled {ok}/{len(activations)} numbers.</b> Balance refunded."), parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("active_cancel_"))
async def cb_active_cancel(callback: CallbackQuery):
    parts = callback.data.split("_")
    aid = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0

    user = await db.get_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    r = await client.set_status(aid, 8)
    if isinstance(r, str) and (r.startswith("ACCESS_CANCEL") or r.startswith("STATUS_CANCEL")):
        await db.delete_activation(aid)
        await callback.answer("✅ Cancelled!", show_alert=True)
        res = await client.get_active_activations()
        if isinstance(res, dict) and res.get("status") == "success":
            acts = res.get("data", [])
            if not acts:
                await callback.message.edit_text(pe("⚠️ <b>No active numbers left.</b>"), parse_mode=ParseMode.HTML)
            else:
                total = len(acts)
                max_page = (total - 1) // 10
                current_page = min(page, max_page)
                await callback.message.edit_text(
                    pe(f"📋 <b>Active Numbers ({total})</b> - Page <b>{current_page+1}/{(total+9)//10}</b>:"),
                    reply_markup=kb.active_numbers_menu(acts, page=current_page),
                    parse_mode=ParseMode.HTML
                )
    elif isinstance(r, str) and "EARLY_CANCEL_DENIED" in r:
        await callback.answer("⚠️ Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        await callback.answer("❌ Failed to cancel.", show_alert=True)

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID: return
    maintenance = await db.get_setting("maintenance")
    await message.answer(pe("⚙️ <b>Admin Panel</b>"), reply_markup=kb.admin_menu(maintenance == "1"), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "admin_maintenance")
async def cb_admin_maintenance(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    current = await db.get_setting("maintenance")
    new_val = "0" if current == "1" else "1"
    await db.set_setting("maintenance", new_val)
    await callback.message.edit_reply_markup(reply_markup=kb.admin_menu(new_val == "1"))
    await callback.answer("⚙️ Maintenance updated.")

@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer(pe("📢 <b>Send the broadcast message:</b>"), reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_broadcast)

@router.message(BotStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    
    if message.text.strip() in MENU_BUTTONS:
        await state.clear()
        return await message.answer(pe("❌ <b>Broadcast cancelled.</b>"), parse_mode=ParseMode.HTML)

    users = await db.get_all_users()
    sent = 0
    for uid in users:
        try:
            await message.bot.send_message(uid, pe(f"📢 <b>Broadcast:</b>\n\n{message.text}"), parse_mode=ParseMode.HTML)
            sent += 1
        except: pass
    await message.answer(pe(f"✅ <b>Sent to {sent} users.</b>"), parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    await callback.message.answer(pe("👤 <b>Send the user ID to ban/unban:</b>"), reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_ban_id)

@router.message(BotStates.waiting_for_ban_id)
async def process_ban_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try: target = int(message.text.strip())
    except:
        await message.answer(pe("❌ <b>Invalid ID.</b>"), parse_mode=ParseMode.HTML)
        return
    user = await db.get_user(target)
    if not user:
        await message.answer(pe("❌ <b>User not found.</b>"), parse_mode=ParseMode.HTML)
        return
    new_status = not bool(user["is_banned"])
    await db.set_ban_status(target, new_status)
    label = "Banned" if new_status else "Unbanned"
    await message.answer(pe(f"✅ <b>User <code>{target}</code> {label}.</b>"), parse_mode=ParseMode.HTML)
    await state.clear()

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()
