import asyncio
import logging
import html
import time
import re
import uuid
from datetime import datetime, timezone, timedelta
from aiohttp import web
from aiogram import Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.utils.chat_action import ChatActionSender
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest

import database as db
from api_client import HeroSMSClient, check_telegram_numbers
import keyboards as kb
from states import BotStates
from engine import pipeline_engine, memory_cache

router = Router()
global_bot = None

def set_bot_instance(bot):
    global global_bot
    global_bot = bot
    pipeline_engine.start_pipeline()

def get_bot_instance():
    return global_bot

ADMIN_ID    = 7266067201
COLOMBIA_ID = 33
TG_SERVICE  = "tg"

DEFAULT_EXCLUDE_LIST = ["57350", "57351"]
DEFAULT_OPERATOR = "any"

processed_otps = {}
phone_otp_counter = {}
fresh_batches_cache = {}

MENU_BUTTONS = ["Bulk Buy Numbers", "Finish"]
SETTINGS_CACHE = {}

async def get_cached_setting(key: str, default: str = "") -> str:
    if key in SETTINGS_CACHE:
        return SETTINGS_CACHE[key]
    val = await db.get_setting(key)
    res = str(val) if val is not None else default
    SETTINGS_CACHE[key] = res
    return res

def set_cached_setting(key: str, val: str):
    SETTINGS_CACHE[key] = str(val)

async def get_dynamic_max_price() -> float:
    saved = await get_cached_setting("max_price", "0.180")
    try:
        return float(saved)
    except Exception:
        return 0.180

def clean_error_text(raw_err: any) -> str:
    if not raw_err:
        return "Unknown Error"
    text = str(raw_err).strip()
    text = text.replace("phone_number_", "").replace("error_", "")
    text = text.replace("_", " ")
    return html.escape(text.title())

def cleanup_expired_cache():
    now = time.time()
    for k in list(processed_otps.keys()):
        if now - processed_otps[k] > 1800:
            del processed_otps[k]
    for b_id in list(fresh_batches_cache.keys()):
        if now - fresh_batches_cache[b_id].get("created_at", 0) > 1800:
            del fresh_batches_cache[b_id]
    memory_cache.prune_expired(1800)

async def start_periodic_janitor():
    while True:
        try:
            await asyncio.sleep(600)
            cleanup_expired_cache()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"Janitor error: {e}")

# --- Sub-50ms Fast Webhook Handler ---
async def handle_herosms_webhook(request: web.Request):
    data = {}
    try:
        if request.method == "POST":
            try:
                data = await request.json()
            except Exception:
                data = dict(await request.post())
        if not data:
            data = dict(request.query)
    except Exception as e:
        logging.error(f"Error reading webhook request: {e}")
        return web.Response(text="Bad Request", status=400)

    aid = str(data.get("activationId") or data.get("id") or data.get("activation_id") or "").strip()
    raw_code = data.get("code") or data.get("smsCode") or data.get("text") or data.get("sms") or ""
    
    code = ""
    if raw_code:
        match = re.search(r'\b\d{4,8}\b', str(raw_code))
        code = match.group(0) if match else str(raw_code).strip()

    direct_phone = data.get("phoneNumber") or data.get("phone") or ""

    if not aid or not code:
        return web.Response(text="OK", status=200)

    cache_key = f"{aid}:{code}"
    if cache_key in processed_otps:
        return web.Response(text="OK", status=200)

    processed_otps[cache_key] = time.time()
    cleanup_expired_cache()

    try:
        user_id = None
        phone = direct_phone

        cached_item = memory_cache.get_activation_by_aid(aid)
        if cached_item:
            user_id = cached_item["user_id"]
            if not phone:
                phone = cached_item["phone"]
        else:
            row = await db.get_activation_user(aid)
            if row:
                user_id = row[0]
                if not phone:
                    phone = row[1]
        
        if user_id and phone:
            clean_p = str(phone).lstrip("+").strip()
            count = phone_otp_counter.get(clean_p, 0) + 1
            phone_otp_counter[clean_p] = count
            is_second = (count > 1)

            bot = get_bot_instance()
            if bot:
                msg_text = format_otp_text(phone, code, is_second=is_second)
                try:
                    await bot.send_message(
                        user_id,
                        msg_text,
                        reply_markup=kb.otp_copy_menu(code),
                        parse_mode=ParseMode.HTML,
                        disable_notification=False
                    )
                except Exception as e:
                    logging.error(f"Failed to send webhook OTP: {e}")
    except Exception as e:
        logging.error(f"Error processing webhook: {e}")

    return web.Response(text="OK", status=200)

# --- Live Safe Sniper Function ---
async def start_live_sniper_process(message: Message, target_op: str):
    user, client = await get_valid_user_client(message.from_user.id)[cite: 1]
    if not user or not client:
        return await message.answer("API Key not found.")[cite: 1]

    dynamic_max = await get_dynamic_max_price()[cite: 1]
    current_exc = await get_excluded_prefixes_str()[cite: 1]
    api_exc = current_exc if current_exc else None[cite: 1]
    clean_op = target_op.strip().lower()[cite: 1]

    status_msg = await message.answer(
        f"🎯 <b>Sniper Active: {clean_op.upper()}</b> (Max: ${dynamic_max:.3f})\n"
        f"🔍 Searching & attempting to grab... (Attempt #1)",
        parse_mode=ParseMode.HTML
    )[cite: 1]

    await pipeline_engine.submit_hunt_job(
        user_id=message.from_user.id,
        client=client,
        target_op=clean_op,
        max_price=dynamic_max,
        exc_prefixes=api_exc,
        status_msg=status_msg
    )

async def auto_cancel_bad_numbers(client: HeroSMSClient, bad_items: list):
    if not bad_items:
        return[cite: 1]
    await asyncio.sleep(125)[cite: 1]
    for item in bad_items:[cite: 1]
        aid = item.get("aid")[cite: 1]
        if not aid:
            continue[cite: 1]
        try:
            r = await client.set_status(aid, 8)[cite: 1]
            if (isinstance(r, str) and (r.startswith("ACCESS_CANCEL") or r.startswith("STATUS_CANCEL"))) or (isinstance(r, dict) and r.get("status") == "success"):[cite: 1]
                await db.delete_activation(aid)[cite: 1]
                memory_cache.remove_activation(aid)
                for k in list(processed_otps.keys()):[cite: 1]
                    if k.startswith(f"{aid}:"):[cite: 1]
                        del processed_otps[k][cite: 1]
        except Exception as e:
            logging.warning(f"Auto-cancel failed for {aid}: {e}")[cite: 1]

def get_colombia_operator(phone: str) -> str:
    clean = str(phone).lstrip("+").strip()[cite: 1]
    if clean.startswith("57"):[cite: 1]
        clean = clean[2:][cite: 1]
    prefix = clean[:3][cite: 1]
    if prefix in ["310", "311", "312", "313", "314", "320", "321", "322", "323"]:[cite: 1]
        return "Claro"[cite: 1]
    elif prefix in ["300", "301", "302", "304", "305", "324"]:[cite: 1]
        return "Tigo"[cite: 1]
    elif prefix in ["315", "316", "317", "318"]:[cite: 1]
        return "Movistar"[cite: 1]
    elif prefix in ["350", "351", "333"]:[cite: 1]
        return "WOM"[cite: 1]
    elif prefix in ["319"]:[cite: 1]
        return "Virgin"[cite: 1]
    return "Unknown"[cite: 1]

def format_otp_text(phone: str, code: str, is_second: bool = False) -> str:
    clean_phone = str(phone).lstrip("+").strip()[cite: 1]
    safe_phone = html.escape(clean_phone)[cite: 1]
    suffix = " (2nd SMS)" if is_second else ""[cite: 1]
    return f"🇨🇴 <b>Telegram</b> <code>{safe_phone}</code>{suffix}"[cite: 1]

def format_tg_status(raw_status: any) -> dict:
    if raw_status is None:[cite: 1]
        return {"badge": "⚠️ Check Failed", "priority": 5, "is_fresh": False, "is_error": True}[cite: 1]

    if isinstance(raw_status, dict):[cite: 1]
        st = str(raw_status.get("status") or raw_status.get("result") or raw_status.get("msg") or raw_status).strip().lower()[cite: 1]
    elif isinstance(raw_status, bool):[cite: 1]
        st = "occupied" if raw_status else "unoccupied"[cite: 1]
    else:
        st = str(raw_status).strip().lower()[cite: 1]

    if "api_error" in st or "check_failed" in st or not st:[cite: 1]
        return {"badge": "⚠️ Check Failed", "priority": 5, "is_fresh": False, "is_error": True}[cite: 1]

    fresh_signals = [[cite: 1]
        "unoccupied", "phone_number_unoccupied",[cite: 1]
        "unregistered", "not_registered", "not registered", "non_registered",[cite: 1]
        "not_occupied", "not occupied", "free", "fresh", "available",[cite: 1]
        "ready", "allow", "ok", "valid", "clean", "false", "0",[cite: 1]
        "no_account", "no account", "does_not_exist", "not_exists"[cite: 1]
    ]
    if any(w in st for w in fresh_signals):[cite: 1]
        if "banned" in st and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):[cite: 1]
            return {"badge": "🚫 Banned", "priority": 4, "is_fresh": False, "is_error": False}[cite: 1]
        return {"badge": "✅", "priority": 1, "is_fresh": True, "is_error": False}[cite: 1]

    locked_signals = ["flood", "locked", "lock", "wait", "restricted", "2fa", "password", "has_password"][cite: 1]
    if any(w in st for w in locked_signals):[cite: 1]
        return {"badge": "🔒", "priority": 2, "is_fresh": False, "is_error": False}[cite: 1]

    occupied_signals = ["occupied", "registered", "taken", "used", "true", "1"][cite: 1]
    if any(w in st for w in occupied_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):[cite: 1]
        return {"badge": "❌", "priority": 3, "is_fresh": False, "is_error": False}[cite: 1]

    banned_signals = ["banned", "ban", "blocked"][cite: 1]
    if any(w in st for w in banned_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "without", "false"]):[cite: 1]
        return {"badge": "🚫", "priority": 4, "is_fresh": False, "is_error": False}[cite: 1]

    clean = clean_error_text(st)[cite: 1]
    return {"badge": f"⚠ {clean}", "priority": 5, "is_fresh": False, "is_error": False}[cite: 1]

async def get_excluded_prefixes_str() -> str:
    return await get_cached_setting("excluded_prefixes", ",".join(DEFAULT_EXCLUDE_LIST))[cite: 1]

async def get_preferred_operator_str() -> str:
    return await get_cached_setting("preferred_operator", DEFAULT_OPERATOR)[cite: 1]

async def get_valid_user_client(user_id: int):
    user = await db.get_user(user_id)[cite: 1]
    if not user:[cite: 1]
        return None, None[cite: 1]
    try:
        api_key = user["api_key"][cite: 1]
        if not api_key:[cite: 1]
            return None, None[cite: 1]
        return user, HeroSMSClient(api_key)[cite: 1]
    except Exception:
        return None, None[cite: 1]

async def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID:[cite: 1]
        return True[cite: 1]
    user = await db.get_user(user_id)[cite: 1]
    if not user or user["is_banned"] or not user["is_approved"]:[cite: 1]
        return False[cite: 1]
    maintenance = await get_cached_setting("maintenance", "0")[cite: 1]
    return maintenance != "1"[cite: 1]

# --- Start Command ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()[cite: 1]
    uid = message.from_user.id[cite: 1]

    if uid == ADMIN_ID:[cite: 1]
        try:
            await db.add_user(uid)[cite: 1]
            await db.set_approval_status(uid, True)[cite: 1]
        except Exception:
            pass[cite: 1]

        user = await db.get_user(uid)[cite: 1]
        has_api_key = bool(user and user["api_key"])[cite: 1]
        if not has_api_key:[cite: 1]
            await message.answer([cite: 1]
                "👑 <b>Welcome Admin!</b>\n\nPlease send your HeroSMS API Key to get started:",[cite: 1]
                reply_markup=ReplyKeyboardRemove(),[cite: 1]
                parse_mode=ParseMode.HTML[cite: 1]
            )
            await state.set_state(BotStates.waiting_for_api_key)[cite: 1]
        else:
            await message.answer("👑 Welcome back Admin!", reply_markup=kb.main_reply_menu())[cite: 1]
        return[cite: 1]

    user = await db.get_user(uid)[cite: 1]
    if not user or not user["is_approved"] or user["is_banned"]:[cite: 1]
        return[cite: 1]

    maintenance = await get_cached_setting("maintenance", "0")[cite: 1]
    if maintenance == "1":[cite: 1]
        return[cite: 1]

    has_api_key = bool(user and user["api_key"])[cite: 1]
    if not has_api_key:[cite: 1]
        await message.answer([cite: 1]
            "Welcome to HeroSMS Bot!\n\nPlease send your HeroSMS API Key to get started.",[cite: 1]
            reply_markup=ReplyKeyboardRemove()[cite: 1]
        )
        await state.set_state(BotStates.waiting_for_api_key)[cite: 1]
    else:
        await message.answer("Welcome back!", reply_markup=kb.main_reply_menu())[cite: 1]

# --- Direct /api Command & Input Handler ---
@router.message(Command("api"))
async def cmd_direct_api(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id):[cite: 1]
        return[cite: 1]
    await state.clear()[cite: 1]
    args = message.text.split()[cite: 1]
    if len(args) >= 2:[cite: 1]
        api_key = args[1].strip("\"'").strip()[cite: 1]
        client = HeroSMSClient(api_key)[cite: 1]
        balance = await client.get_balance()[cite: 1]
        if balance is not None:[cite: 1]
            await db.update_api_key(message.from_user.id, api_key)[cite: 1]
            return await message.answer([cite: 1]
                f"✅ <b>API Key Updated!</b>\n💰 Balance: <code>{balance:.4f} USD</code>",[cite: 1]
                reply_markup=kb.main_reply_menu(),[cite: 1]
                parse_mode=ParseMode.HTML[cite: 1]
            )
        else:
            return await message.answer("❌ Invalid API Key. Please verify your key.")[cite: 1]

    await message.answer([cite: 1]
        "🔑 <b>Send New API Key:</b>\n\nNiche apnar new HeroSMS API Key paste kore pathan:",[cite: 1]
        reply_markup=kb.api_key_cancel_menu(),[cite: 1]
        parse_mode=ParseMode.HTML[cite: 1]
    )
    await state.set_state(BotStates.waiting_for_api_key)[cite: 1]

@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    text = message.text.strip()[cite: 1]
    if text in MENU_BUTTONS or text.lower() == "t":[cite: 1]
        await state.clear()[cite: 1]
        return await message.answer("Action cancelled.")[cite: 1]

    api_key = text.strip("\"'").strip()[cite: 1]
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):[cite: 1]
        client = HeroSMSClient(api_key)[cite: 1]
        balance = await client.get_balance()[cite: 1]
        if balance is not None:[cite: 1]
            await db.update_api_key(message.from_user.id, api_key)[cite: 1]
            await state.clear()[cite: 1]
            await message.answer([cite: 1]
                f"✅ <b>API Key Saved Successfully!</b>\n💰 Balance: <code>{balance:.4f} USD</code>",[cite: 1]
                reply_markup=kb.main_reply_menu(),[cite: 1]
                parse_mode=ParseMode.HTML[cite: 1]
            )
        else:
            await message.answer("❌ Invalid API Key. Please send a valid key (or send 't' to cancel):")[cite: 1]

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()[cite: 1]
    if not await is_allowed(callback.from_user.id):[cite: 1]
        return[cite: 1]
    try: 
        await callback.message.delete()[cite: 1]
    except Exception: 
        pass[cite: 1]
    await callback.message.answer("Main Menu:", reply_markup=kb.main_reply_menu())[cite: 1]

# --- Quick Tools Dashboard ---
@router.message(Command("t", "tools", "admin"))
@router.message(F.text.casefold() == "t")
async def open_tools_menu(message: Message, state: FSMContext):
    await state.clear()[cite: 1]
    if not await is_allowed(message.from_user.id):[cite: 1]
        return[cite: 1]
    
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):[cite: 1]
        max_p = await get_dynamic_max_price()[cite: 1]
        op = await get_preferred_operator_str()[cite: 1]
        sniper_on = memory_cache.is_user_sniper_active(message.from_user.id)
        sniper_op = await get_cached_setting("sniper_op", "claro")[cite: 1]
        
        snapshot = (
            f"🛠️ <b>Control Center & Tools (Page 1/2)</b>\n\n"
            f"📊 <b>Quick Settings Snapshot:</b>\n"
            f"• Max Price Limit: <code>${max_p:.3f}</code>\n"
            f"• Active Operator: <code>{op.upper()}</code>\n"
            f"• Auto Sniper: <code>{'ACTIVE (' + sniper_op.upper() + ')' if sniper_on else 'OFF'}</code>\n\n"
            f"Select any tool or action from the buttons below:"
        )[cite: 1]
        await message.answer(snapshot, reply_markup=kb.tools_menu_page_1(sniper_on, sniper_op), parse_mode=ParseMode.HTML)[cite: 1]

@router.callback_query(F.data == "tools_page_1")
async def cb_tools_page_1(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id):[cite: 1]
        return[cite: 1]
    max_p = await get_dynamic_max_price()[cite: 1]
    op = await get_preferred_operator_str()[cite: 1]
    sniper_on = memory_cache.is_user_sniper_active(callback.from_user.id)
    sniper_op = await get_cached_setting("sniper_op", "claro")[cite: 1]
    
    snapshot = (
        f"🛠️ <b>Control Center & Tools (Page 1/2)</b>\n\n"
        f"📊 <b>Quick Settings Snapshot:</b>\n"
        f"• Max Price Limit: <code>${max_p:.3f}</code>\n"
        f"• Active Operator: <code>{op.upper()}</code>\n"
        f"• Auto Sniper: <code>{'ACTIVE (' + sniper_op.upper() + ')' if sniper_on else 'OFF'}</code>\n\n"
        f"Select any tool or action from the buttons below:"
    )[cite: 1]
    await callback.message.edit_text(snapshot, reply_markup=kb.tools_menu_page_1(sniper_on, sniper_op), parse_mode=ParseMode.HTML)[cite: 1]

@router.callback_query(F.data == "tools_page_2")
async def cb_tools_page_2(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id):[cite: 1]
        return[cite: 1]
    maintenance = (await get_cached_setting("maintenance", "0")) == "1"[cite: 1]
    snapshot = (
        f"🛠️️ <b>Control Center & Tools (Page 2/2)</b>\n\n"
        f"⚙️ <b>Advanced & Administrative Controls:</b>\n"
        f"• HeroSMS API Key Management\n"
        f"• Order Statistics & User Management\n"
        f"• System Maintenance & Broadcast\n\n"
        f"Select an action from below:"
    )[cite: 1]
    await callback.message.edit_text(snapshot, reply_markup=kb.tools_menu_page_2(maintenance), parse_mode=ParseMode.HTML)[cite: 1]

# --- Sniper Toggle ---
@router.callback_query(F.data == "tool_toggle_sniper")
async def cb_tool_toggle_sniper(callback: CallbackQuery, state: FSMContext):
    is_active = memory_cache.is_user_sniper_active(callback.from_user.id)
    if is_active:
        memory_cache.set_user_sniper_state(callback.from_user.id, False)
        await db.set_setting("sniper_active", "0")[cite: 1]
        set_cached_setting("sniper_active", "0")[cite: 1]
        await callback.answer("🛑 Auto Sniper Stopped!", show_alert=True)[cite: 1]
        return await cb_tools_page_1(callback)[cite: 1]
    else:
        await callback.answer()[cite: 1]
        await callback.message.edit_text([cite: 1]
            "🎯 <b>Auto Sniper Direct Grabber</b>\n\n"[cite: 1]
            "Kon operator-er number continuous try korte chan? Operator name likhun (e.g. <code>claro</code>, <code>tigo</code>, <code>movistar</code>):",[cite: 1]
            reply_markup=kb.back_button("tools_page_1"),[cite: 1]
            parse_mode=ParseMode.HTML[cite: 1]
        )
        await state.set_state(BotStates.waiting_for_sniper_operator)[cite: 1]

@router.message(BotStates.waiting_for_sniper_operator)
async def process_sniper_operator(message: Message, state: FSMContext):
    target_op = message.text.strip().lower()[cite: 1]
    if target_op in MENU_BUTTONS or target_op.lower() == "t":[cite: 1]
        await state.clear()[cite: 1]
        return await message.answer("Sniper configuration cancelled.")[cite: 1]

    await state.clear()[cite: 1]
    await db.set_setting("sniper_active", "1")[cite: 1]
    set_cached_setting("sniper_active", "1")[cite: 1]
    await db.set_setting("sniper_op", target_op)[cite: 1]
    set_cached_setting("sniper_op", target_op)[cite: 1]

    asyncio.create_task(start_live_sniper_process(message, target_op))[cite: 1]

# --- Balance & Settings ---
@router.callback_query(F.data == "tool_balance")
async def cb_tool_balance(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)[cite: 1]
    if not user or not client:[cite: 1]
        return await callback.answer("API Key not found.", show_alert=True)[cite: 1]
    bal = await client.get_balance()[cite: 1]
    if bal is not None:[cite: 1]
        alert = "\n\n⚠️ <b>Warning:</b> Balance is below $0.50! Please recharge soon." if bal < 0.50 else ""[cite: 1]
        text = f"💰 <b>Your Current Balance:</b> <code>{bal:.4f} USD</code>{alert}"[cite: 1]
        await callback.message.edit_text(text, reply_markup=kb.back_button("tools_page_1"), parse_mode=ParseMode.HTML)[cite: 1]
    else:
        await callback.answer("Error fetching balance.", show_alert=True)[cite: 1]

@router.callback_query(F.data == "tool_view_api_key")
async def cb_tool_view_api_key(callback: CallbackQuery, state: FSMContext):
    await state.clear()[cite: 1]
    user = await db.get_user(callback.from_user.id)[cite: 1]
    api_k = user.get("api_key") if user else None[cite: 1]
    if api_k:[cite: 1]
        masked = f"{api_k[:6]}...{api_k[-4:]}"[cite: 1]
        text = f"🔑 <b>Your HeroSMS API Key:</b>\n\n<code>{api_k}</code>\n\n• Preview: <code>{masked}</code>\n• Status: 🟢 <b>Configured</b>"[cite: 1]
    else:
        text = "🔑 <b>API Key Not Set!</b>"[cite: 1]
    await callback.message.edit_text(text, reply_markup=kb.api_key_view_menu(), parse_mode=ParseMode.HTML)[cite: 1]

@router.callback_query(F.data == "tool_change_api_key")
async def cb_tool_change_api_key(callback: CallbackQuery, state: FSMContext):
    await callback.answer()[cite: 1]
    await state.set_state(BotStates.waiting_for_api_key)[cite: 1]
    await callback.message.edit_text("🔑 <b>Update HeroSMS API Key</b>\n\nPlease send your new API Key:", reply_markup=kb.api_key_cancel_menu(), parse_mode=ParseMode.HTML)[cite: 1]

@router.callback_query(F.data == "tool_cancel_api_change")
async def cb_tool_cancel_api_change(callback: CallbackQuery, state: FSMContext):
    await state.clear()[cite: 1]
    await callback.answer("Cancelled", show_alert=False)[cite: 1]
    await cb_tool_view_api_key(callback, state)[cite: 1]

@router.callback_query(F.data == "tool_set_max_price")
async def cb_tool_set_max_price(callback: CallbackQuery, state: FSMContext):
    cur_p = await get_dynamic_max_price()[cite: 1]
    await callback.message.edit_text(f"💵 <b>Current Max Price:</b> <code>${cur_p:.3f}</code>\n\nSend new price (e.g. 0.18):", reply_markup=kb.back_button("tools_page_1"), parse_mode=ParseMode.HTML)[cite: 1]
    await state.set_state(BotStates.waiting_for_max_price)[cite: 1]

@router.message(BotStates.waiting_for_max_price)
async def process_new_max_price(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): [cite: 1]
        return[cite: 1]
    text = message.text.strip()[cite: 1]
    if text in MENU_BUTTONS or text.lower() == "t":[cite: 1]
        await state.clear()[cite: 1]
        return await message.answer("Action cancelled.")[cite: 1]
    try:
        new_val = float(text)[cite: 1]
        if new_val <= 0: raise ValueError[cite: 1]
        await db.set_setting("max_price", str(new_val))[cite: 1]
        set_cached_setting("max_price", str(new_val))[cite: 1]
        await state.clear()[cite: 1]
        await message.answer(f"✅ Max Purchase Price updated to <b>${new_val:.3f}</b>!", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)[cite: 1]
    except Exception:
        await message.answer("Enter a valid decimal number (e.g. 0.18):")[cite: 1]

@router.message(Command("max"))
async def cmd_quick_max_price(message: Message):
    if not await is_allowed(message.from_user.id):[cite: 1]
        return[cite: 1]
    args = message.text.split()[cite: 1]
    if len(args) < 2:[cite: 1]
        cur_p = await get_dynamic_max_price()[cite: 1]
        return await message.answer(f"Current Max Price: <code>${cur_p:.3f}</code>\nUsage: <code>/max 0.18</code>", parse_mode=ParseMode.HTML)
