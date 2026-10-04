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

DEFAULT_EXCLUDE_LIST = ["57350", "57351"]
DEFAULT_OPERATOR = "any"

processed_otps = {}
phone_otp_counter = {}
fresh_batches_cache = {}
last_known_stocks = {}

MENU_BUTTONS = ["Bulk Buy Numbers", "Finish", "🛠 Tools"]

# --- Fast RAM Cache ---
USER_CACHE = {}
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

async def start_periodic_janitor():
    while True:
        try:
            await asyncio.sleep(600)
            cleanup_expired_cache()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"Janitor error: {e}")

async def start_restock_monitor():
    await asyncio.sleep(15)
    first_run = True
    while True:
        try:
            is_enabled = await get_cached_setting("restock_monitor", "0")
            if is_enabled == "1":
                admin_user = await db.get_user(ADMIN_ID)
                if admin_user and admin_user.get("api_key"):
                    client = HeroSMSClient(admin_user["api_key"])
                    raw_res = await client.get_prices(country=COLOMBIA_ID, service=TG_SERVICE)
                    
                    c_dict = {}
                    if isinstance(raw_res, dict):
                        if "33" in raw_res:
                            c_dict = raw_res["33"].get("tg", raw_res["33"])
                        elif 33 in raw_res:
                            c_dict = raw_res[33].get("tg", raw_res[33])
                        elif "tg" in raw_res:
                            c_dict = raw_res["tg"].get("33", raw_res["tg"])
                        else:
                            for k, v in raw_res.items():
                                if str(k) == "33" and isinstance(v, dict):
                                    c_dict = v.get("tg", v)
                                    break

                    if isinstance(c_dict, dict):
                        current_max = await get_dynamic_max_price()
                        for op_key, op_info in c_dict.items():
                            op_name = str(op_key).lower()
                            cnt = 0
                            cost = 0.0
                            if isinstance(op_info, dict):
                                cnt = int(op_info.get("count") or op_info.get("amount") or op_info.get("qty") or 0)
                                cost = float(op_info.get("cost") or op_info.get("price") or op_info.get("rate") or 0.0)
                            elif isinstance(op_info, (int, str)) and str(op_info).isdigit():
                                cnt = int(op_info)

                            prev_cnt = last_known_stocks.get(op_name, 0)
                            is_initial = first_run and cnt >= 5
                            is_restocked = (cnt - prev_cnt) >= 5

                            if (is_initial or is_restocked) and cost <= current_max:
                                bot = get_bot_instance()
                                if bot:
                                    alert_msg = (
                                        f"🚨 <b>RESTOCK ALERT!</b>\n\n"
                                        f"📡 Operator: <b>{op_name.upper()}</b>\n"
                                        f"💵 Rate: <b>${cost:.3f}</b>\n"
                                        f"📦 Available Stock: <b>{cnt} numbers</b>\n\n"
                                        f"💡 <i>Purchase now from Bulk Buy Numbers!</i>"
                                    )
                                    try:
                                        await bot.send_message(ADMIN_ID, alert_msg, parse_mode=ParseMode.HTML)
                                    except Exception:
                                        pass
                            last_known_stocks[op_name] = cnt
                        first_run = False
        except Exception as e:
            logging.error(f"Restock worker error: {e}")
        await asyncio.sleep(60)

async def auto_cancel_bad_numbers(client: HeroSMSClient, bad_items: list):
    if not bad_items:
        return
    await asyncio.sleep(125)
    for item in bad_items:
        aid = item.get("aid")
        if not aid:
            continue
        try:
            r = await client.set_status(aid, 8)
            if (isinstance(r, str) and (r.startswith("ACCESS_CANCEL") or r.startswith("STATUS_CANCEL"))) or (isinstance(r, dict) and r.get("status") == "success"):
                await db.delete_activation(aid)
                for k in list(processed_otps.keys()):
                    if k.startswith(f"{aid}:"):
                        del processed_otps[k]
        except Exception as e:
            logging.warning(f"Auto-cancel failed for {aid}: {e}")

def get_colombia_operator(phone: str) -> str:
    clean = str(phone).lstrip("+").strip()
    if clean.startswith("57"):
        clean = clean[2:]
    prefix = clean[:3]
    if prefix in ["310", "311", "312", "313", "314", "320", "321", "322", "323"]:
        return "Claro"
    elif prefix in ["300", "301", "302", "304", "305", "324"]:
        return "Tigo"
    elif prefix in ["315", "316", "317", "318"]:
        return "Movistar"
    elif prefix in ["350", "351", "333"]:
        return "WOM"
    elif prefix in ["319"]:
        return "Virgin"
    return "Unknown"

def format_otp_text(phone: str, code: str, is_second: bool = False) -> str:
    clean_phone = str(phone).lstrip("+").strip()
    safe_phone = html.escape(clean_phone)
    suffix = " (2nd SMS)" if is_second else ""
    return f"🇨🇴 <b>Telegram</b> <code>{safe_phone}</code>{suffix}"

def format_tg_status(raw_status: any) -> dict:
    if raw_status is None:
        return {"badge": "⚠️ Check Failed", "priority": 5, "is_fresh": False, "is_error": True}

    if isinstance(raw_status, dict):
        st = str(raw_status.get("status") or raw_status.get("result") or raw_status.get("msg") or raw_status).strip().lower()
    elif isinstance(raw_status, bool):
        st = "occupied" if raw_status else "unoccupied"
    else:
        st = str(raw_status).strip().lower()

    if "api_error" in st or "check_failed" in st or not st:
        return {"badge": "⚠️ Check Failed", "priority": 5, "is_fresh": False, "is_error": True}

    fresh_signals = [
        "unoccupied", "phone_number_unoccupied",
        "unregistered", "not_registered", "not registered", "non_registered",
        "not_occupied", "not occupied", "free", "fresh", "available",
        "ready", "allow", "ok", "valid", "clean", "false", "0",
        "no_account", "no account", "does_not_exist", "not_exists"
    ]
    if any(w in st for w in fresh_signals):
        if "banned" in st and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):
            return {"badge": "🚫 Banned", "priority": 4, "is_fresh": False, "is_error": False}
        return {"badge": "✅", "priority": 1, "is_fresh": True, "is_error": False}

    locked_signals = ["flood", "locked", "lock", "wait", "restricted", "2fa", "password", "has_password"]
    if any(w in st for w in locked_signals):
        return {"badge": "🔒 Locked", "priority": 2, "is_fresh": False, "is_error": False}

    occupied_signals = ["occupied", "registered", "taken", "used", "true", "1"]
    if any(w in st for w in occupied_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):
        return {"badge": "❌ Registered", "priority": 3, "is_fresh": False, "is_error": False}

    banned_signals = ["banned", "ban", "blocked"]
    if any(w in st for w in banned_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "without", "false"]):
        return {"badge": "🚫 Banned", "priority": 4, "is_fresh": False, "is_error": False}

    clean = clean_error_text(st)
    return {"badge": f"⚠ {clean}", "priority": 5, "is_fresh": False, "is_error": False}

async def get_excluded_prefixes_str() -> str:
    return await get_cached_setting("excluded_prefixes", ",".join(DEFAULT_EXCLUDE_LIST))

async def get_preferred_operator_str() -> str:
    return await get_cached_setting("preferred_operator", DEFAULT_OPERATOR)

async def get_valid_user_client(user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return None, None
    try:
        api_key = user["api_key"]
        if not api_key:
            return None, None
        return user, HeroSMSClient(api_key)
    except Exception:
        return None, None

async def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID: 
        return True
    
    user = await db.get_user(user_id)
    if not user or user["is_banned"] or not user["is_approved"]:
        return False
        
    maintenance = await get_cached_setting("maintenance", "0")
    return maintenance != "1"

# --- Webhook Handler (Supports 2nd OTP Seamlessly) ---
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
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logging.error(f"Failed to send webhook OTP to user {user_id}: {e}")
    except Exception as e:
        logging.error(f"Error processing webhook database lookup for {aid}: {e}")

    return web.Response(text="OK", status=200)

# --- Start Command (Super Admin Instant Pass + Dead Bot Silence for Unauthorized) ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    # 1. SUPER ADMIN BYPASS: Admin kokhono dead mode ba unauthorized hobe na
    if uid == ADMIN_ID:
        try:
            await db.add_user(uid)
            await db.set_approval_status(uid, True)
        except Exception:
            pass

        user = await db.get_user(uid)
        has_api_key = bool(user and user["api_key"])
        if not has_api_key:
            await message.answer(
                "👑 <b>Welcome Admin!</b>\n\nPlease send your HeroSMS API Key to get started:",
                reply_markup=ReplyKeyboardRemove(),
                parse_mode=ParseMode.HTML
            )
            await state.set_state(BotStates.waiting_for_api_key)
        else:
            await message.answer("👑 Welcome back Admin!", reply_markup=kb.main_reply_menu())
        return

    # 2. GENERAL USERS: Dead Bot Mode (Silent Drop for unauthorized/banned)
    user = await db.get_user(uid)
    if not user or not user["is_approved"] or user["is_banned"]:
        return

    maintenance = await get_cached_setting("maintenance", "0")
    if maintenance == "1":
        return

    has_api_key = bool(user and user["api_key"])
    if not has_api_key:
        await message.answer(
            "Welcome to HeroSMS Bot!\n\nPlease send your HeroSMS API Key to get started.",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(BotStates.waiting_for_api_key)
    else:
        await message.answer("Welcome back!", reply_markup=kb.main_reply_menu())

@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled. Please try again.")

    api_key = text.strip("\"'").strip()
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        client = HeroSMSClient(api_key)
        balance = await client.get_balance()
        
        if balance is not None:
            await db.update_api_key(message.from_user.id, api_key)
            await state.clear()
            await message.answer(
                f"✅ API Key saved successfully!\n💰 Balance: <code>{balance:.4f} USD</code>",
                reply_markup=kb.main_reply_menu(),
                parse_mode=ParseMode.HTML
            )
        else:
            await message.answer("❌ Invalid API Key. Please check and try again.")

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_allowed(callback.from_user.id): 
        return
    try: 
        await callback.message.delete()
    except Exception: 
        pass
    await callback.message.answer("Main Menu:", reply_markup=kb.main_reply_menu())

# --- Quick Tools Dashboard ---
@router.message(F.text == "🛠 Tools")
@router.message(Command("admin"))
async def open_tools_menu(message: Message):
    if not await is_allowed(message.from_user.id): 
        return
    
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        maintenance = (await get_cached_setting("maintenance", "0")) == "1"
        restock = (await get_cached_setting("restock_monitor", "0")) == "1"
        max_p = await get_dynamic_max_price()
        op = await get_preferred_operator_str()
        exc = await get_excluded_prefixes_str()
        exc_count = len([x for x in exc.split(",") if x.strip()])
        
        snapshot = (
            f"🛠️ <b>Control Center & Tools</b>\n\n"
            f"📊 <b>Quick Stats Snapshot:</b>\n"
            f"• Max Price Limit: <code>${max_p:.3f}</code>\n"
            f"• Active Operator: <code>{op.upper()}</code>\n"
            f"• Blacklisted Prefixes: <code>{exc_count} Active</code>\n"
            f"• Restock Monitor: <code>{'ACTIVE' if restock else 'MUTED'}</code>\n\n"
            f"Select any tool or action from the buttons below:"
        )
        await message.answer(snapshot, reply_markup=kb.tools_menu(maintenance, restock), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tools_main")
async def cb_tools_main(callback: CallbackQuery, state: FSMContext):
    if state:
        await state.clear()
    if not await is_allowed(callback.from_user.id): 
        return
    maintenance = (await get_cached_setting("maintenance", "0")) == "1"
    restock = (await get_cached_setting("restock_monitor", "0")) == "1"
    max_p = await get_dynamic_max_price()
    op = await get_preferred_operator_str()
    exc = await get_excluded_prefixes_str()
    exc_count = len([x for x in exc.split(",") if x.strip()])
    
    snapshot = (
        f"🛠️ <b>Control Center & Tools</b>\n\n"
        f"📊 <b>Quick Stats Snapshot:</b>\n"
        f"• Max Price Limit: <code>${max_p:.3f}</code>\n"
        f"• Active Operator: <code>{op.upper()}</code>\n"
        f"• Blacklisted Prefixes: <code>{exc_count} Active</code>\n"
        f"• Restock Monitor: <code>{'ACTIVE' if restock else 'MUTED'}</code>\n\n"
        f"Select any tool or action from the buttons below:"
    )
    await callback.message.edit_text(snapshot, reply_markup=kb.tools_menu(maintenance, restock), parse_mode=ParseMode.HTML)

# --- Button: View / Change API Key ---
@router.callback_query(F.data == "tool_view_api_key")
async def cb_tool_view_api_key(callback: CallbackQuery):
    user = await db.get_user(callback.from_user.id)
    api_k = user.get("api_key") if user else None
    
    if api_k:
        masked_preview = f"{api_k[:6]}...{api_k[-4:]}"
        text = (
            f"🔑 <b>Your HeroSMS API Key:</b>\n\n"
            f"<code>{api_k}</code>\n\n"
            f"• Preview: <code>{masked_preview}</code>\n"
            f"• Status: 🟢 <b>Configured</b>\n\n"
            f"<i>Tap above to copy your key or click below to update it.</i>"
        )
    else:
        text = (
            f"🔑 <b>API Key Not Set!</b>\n\n"
            f"You have not configured your HeroSMS API key yet."
        )
    
    await callback.message.edit_text(text, reply_markup=kb.api_key_view_menu(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_change_api_key")
async def cb_tool_change_api_key(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔑 <b>Update API Key:</b>\n\nPlease send your new HeroSMS API Key:",
        reply_markup=kb.back_button(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_api_key)

# --- Button: Check Balance ---
@router.callback_query(F.data == "tool_balance")
async def cb_tool_balance(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client:
        return await callback.answer("API Key not found.", show_alert=True)
    bal = await client.get_balance()
    if bal is not None:
        alert = "\n\n⚠️ <b>Warning:</b> Balance is below $0.50! Please recharge soon." if bal < 0.50 else ""
        text = f"💰 <b>Your Current Balance:</b> <code>{bal:.4f} USD</code>{alert}"
        await callback.message.edit_text(text, reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    else:
        await callback.answer("Error fetching balance.", show_alert=True)

# --- Button: Live Stock & True Rates ---
@router.callback_query(F.data == "tool_live_stock")
async def cb_tool_live_stock(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client:
        return await callback.answer("Set your API Key first.", show_alert=True)

    await callback.answer("Fetching live stock from HeroSMS...")
    raw_res = await client.get_prices(country=COLOMBIA_ID, service=TG_SERVICE)
    
    if not isinstance(raw_res, dict):
        return await callback.message.edit_text(f"❌ Failed to load live stock: {html.escape(str(raw_res))}", reply_markup=kb.back_button())

    c_dict = {}
    if "33" in raw_res:
        c_dict = raw_res["33"].get("tg", raw_res["33"])
    elif 33 in raw_res:
        c_dict = raw_res[33].get("tg", raw_res[33])
    elif "tg" in raw_res:
        c_dict = raw_res["tg"].get("33", raw_res["tg"])
    else:
        for k, v in raw_res.items():
            if str(k) == "33" and isinstance(v, dict):
                c_dict = v.get("tg", v)
                break
        if not c_dict and "claro" in str(raw_res).lower():
            c_dict = raw_res

    found_ops = {}
    total_available = 0

    if isinstance(c_dict, dict):
        for op_key, op_info in c_dict.items():
            op_name = str(op_key).lower()
            cnt = 0
            cost = 0.0
            if isinstance(op_info, dict):
                cnt = int(op_info.get("count") or op_info.get("amount") or op_info.get("qty") or 0)
                cost = float(op_info.get("cost") or op_info.get("price") or op_info.get("rate") or 0.0)
            elif isinstance(op_info, (int, str)) and str(op_info).isdigit():
                cnt = int(op_info)
            if cnt > 0:
                found_ops[op_name] = {"count": cnt, "cost": cost}
                total_available += cnt

    lines = ["🇨🇴 <b>Live Colombia Telegram Stock:</b>\n"]
    common_ops = ["claro", "tigo", "movistar", "wom", "virgin", "exito", "flash", "any"]

    if found_ops:
        for op, val in found_ops.items():
            cost_str = f"${val['cost']:.3f}" if val['cost'] > 0 else "Market"
            lines.append(f"🟢 <b>{op.upper()}</b>: <b>{val['count']} pcs</b> | Rate: <code>{cost_str}</code>")

    for op in common_ops:
        if op not in found_ops:
            lines.append(f"🔴 <b>{op.upper()}</b>: <b>0 pcs</b> | <i>Out of stock</i>")

    lines.append(f"\n📦 <b>Total Stock:</b> <code>{total_available} numbers</code>")
    await callback.message.edit_text("\n".join(lines), reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)

# --- Button: Restock Alert Toggle ---
@router.callback_query(F.data == "tool_toggle_restock")
async def cb_tool_toggle_restock(callback: CallbackQuery):
    cur = await get_cached_setting("restock_monitor", "0")
    new_val = "0" if cur == "1" else "1"
    await db.set_setting("restock_monitor", new_val)
    set_cached_setting("restock_monitor", new_val)
    status_label = "ENABLED ✅" if new_val == "1" else "MUTED 🔕"
    await callback.answer(f"Restock Monitor is now {status_label}!", show_alert=True)
    return await cb_tools_main(callback, None)

# --- Button: Set Max Price ---
@router.callback_query(F.data == "tool_set_max_price")
async def cb_tool_set_max_price(callback: CallbackQuery, state: FSMContext):
    cur_p = await get_dynamic_max_price()
    await callback.message.edit_text(
        f"💵 <b>Current Max Price:</b> <code>${cur_p:.3f}</code>\n\n"
        f"Please send the new max price (e.g. <code>0.18</code> or <code>0.20</code>):",
        reply_markup=kb.back_button(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_max_price)

@router.message(BotStates.waiting_for_max_price)
async def process_new_max_price(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): 
        return
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    try:
        new_val = float(text)
        if new_val <= 0:
            raise ValueError
        await db.set_setting("max_price", str(new_val))
        set_cached_setting("max_price", str(new_val))
        await state.clear()
        await message.answer(f"✅ Max Purchase Price updated to <b>${new_val:.3f}</b>!", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    except Exception:
        await message.answer("Enter a valid decimal number (e.g. 0.18):")

@router.message(Command("max"))
async def cmd_quick_max_price(message: Message):
    if not await is_allowed(message.from_user.id):
        return
    args = message.text.split()
    if len(args) < 2:
        cur_p = await get_dynamic_max_price()
        return await message.answer(f"Current Max Price: <code>${cur_p:.3f}</code>\nUsage: <code>/max 0.18</code>", parse_mode=ParseMode.HTML)
    try:
        new_val = float(args[1].strip())
        await db.set_setting("max_price", str(new_val))
        set_cached_setting("max_price", str(new_val))
        await message.answer(f"✅ Max Purchase Price updated to <b>${new_val:.3f}</b>!", parse_mode=ParseMode.HTML)
    except Exception:
        await message.answer("Invalid price format. Usage: <code>/max 0.18</code>", parse_mode=ParseMode.HTML)

# --- Button: HeroSMS Stats ---
@router.callback_query(F.data == "tool_stats")
async def cb_tool_stats(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client:
        return await callback.answer("Set your API Key first.", show_alert=True)

    await callback.answer("Fetching HeroSMS Stats...")
    res = await client.get_stats()
    if not res or not isinstance(res, dict) or "data" not in res:
        return await callback.message.edit_text("HeroSMS is compiling statistics. Please check back shortly.", reply_markup=kb.back_button())

    data = res.get("data", {})
    display_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"📊 <b>HeroSMS Stats ({display_date}):</b>\n"]
    total_purchased = 0
    total_success = 0

    for country_key, services in data.items():
        if isinstance(services, dict):
            for service_name, s_data in services.items():
                if isinstance(s_data, dict):
                    c_count = s_data.get("count", 0)
                    c_success = s_data.get("success", 0)
                    c_percent = float(s_data.get("percent", 0.0))
                    total_purchased += c_count
                    total_success += c_success
                    lines.append(
                        f"• {service_name.upper()} (ID: {country_key}): "
                        f"Total <b>{c_count}</b> | Success <b>{c_success}</b> ({c_percent:.1f}%)"
                    )

    overall_rate = (total_success / total_purchased * 100) if total_purchased > 0 else 0.0
    lines.append(
        f"\n-------------------\n"
        f"<b>Summary:</b>\n"
        f"• Total Orders: <b>{total_purchased}</b>\n"
        f"• Total Success: <b>{total_success}</b>\n"
        f"• Average Rate: <b>{overall_rate:.1f}%</b>"
    )
    final_text = "\n".join(lines)
    if len(final_text) > 4000:
        final_text = final_text[:3990] + "..."
    await callback.message.edit_text(final_text, reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)

# --- Button: Active Numbers ---
@router.callback_query(F.data == "tool_active_numbers")
async def cb_tool_active_numbers(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client: 
        return await callback.answer("Set your API Key first.", show_alert=True)

    await callback.answer("Loading active numbers...")
    res = await client.get_active_activations()
    if not (isinstance(res, dict) and res.get("status") == "success"):
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        return await callback.message.edit_text(f"Error: {clean_error_text(err)}", reply_markup=kb.back_button())

    activations = res.get("data", [])
    if not activations:
        return await callback.message.edit_text("ℹ️ No active numbers found.", reply_markup=kb.back_button())

    total = len(activations)
    await callback.message.edit_text(
        f"Active Numbers ({total}) - Page 1/{(total+9)//10}:",
        reply_markup=kb.active_numbers_menu(activations, page=0)
    )

# --- Operator Management ---
@router.callback_query(F.data == "tool_set_operator")
async def cb_tool_set_op(callback: CallbackQuery, state: FSMContext):
    cur = await get_preferred_operator_str()
    await callback.message.edit_text(
        f"📡 <b>Current Operator:</b> <code>{cur.upper()}</code>\n\n"
        f"Please send the operator name (e.g. <code>claro</code>, <code>tigo</code>, <code>movistar</code>, or <code>any</code>):",
        reply_markup=kb.back_button(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_operator)

@router.message(BotStates.waiting_for_operator)
async def process_operator_input(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    await db.set_setting("preferred_operator", text)
    set_cached_setting("preferred_operator", text)
    await state.clear()
    await message.answer(f"✅ Preferred operator set to: <b>{text.upper()}</b>", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_operator_list")
async def cb_tool_op_list(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client:
        return await callback.answer("Set your API Key first.", show_alert=True)
    res = await client.get_operators(country=COLOMBIA_ID)
    cur = await get_preferred_operator_str()
    if isinstance(res, dict) and res.get("status") == "success":
        colombia_ops = res.get("countryOperators", {}).get(str(COLOMBIA_ID), [])
        op_str = ", ".join(colombia_ops) if colombia_ops else "None"
        text = (
            f"📡 <b>Live Colombia Operators:</b>\n<code>{op_str}</code>\n\n"
            f"⚙️ <b>Currently Active:</b> <code>{cur.upper()}</code>"
        )
        await callback.message.edit_text(text, reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    else:
        await callback.answer("Could not load operators.", show_alert=True)

@router.callback_query(F.data == "tool_reset_operator")
async def cb_tool_reset_op(callback: CallbackQuery):
    await db.set_setting("preferred_operator", DEFAULT_OPERATOR)
    set_cached_setting("preferred_operator", DEFAULT_OPERATOR)
    await callback.answer(f"Operator reset to {DEFAULT_OPERATOR.upper()}!", show_alert=True)
    return await cb_tools_main(callback, None)

# --- Exclude / Blacklist Management ---
@router.callback_query(F.data == "tool_exclude")
async def cb_tool_exclude(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➕ <b>Exclude Prefix:</b>\n\nSend prefix to blacklist (e.g. <code>57350</code> or <code>57300,57301</code>):",
        reply_markup=kb.back_button(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_exclude)

@router.message(BotStates.waiting_for_exclude)
async def process_exclude_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    new_items = [p.replace("+", "").strip() for p in text.split(",") if p.strip()]
    cur_str = await get_excluded_prefixes_str()
    cur_list = cur_str.split(",") if cur_str else list(DEFAULT_EXCLUDE_LIST)
    for it in new_items:
        if it not in cur_list:
            cur_list.append(it)
    saved_str = ",".join(cur_list)
    await db.set_setting("excluded_prefixes", saved_str)
    set_cached_setting("excluded_prefixes", saved_str)
    await state.clear()
    await message.answer(f"✅ Added to blacklist!\nCurrent: <code>{saved_str}</code>", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_unexclude")
async def cb_tool_unexclude(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➖ <b>Unexclude Prefix:</b>\n\nSend prefix to remove from blacklist (e.g. <code>57350</code>):",
        reply_markup=kb.back_button(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_unexclude)

@router.message(BotStates.waiting_for_unexclude)
async def process_unexclude_input(message: Message, state: FSMContext):
    text = message.text.strip().replace("+", "")
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    cur_str = await get_excluded_prefixes_str()
    cur_list = cur_str.split(",") if cur_str else list(DEFAULT_EXCLUDE_LIST)
    if text in cur_list:
        cur_list.remove(text)
        saved_str = ",".join(cur_list)
        await db.set_setting("excluded_prefixes", saved_str)
        set_cached_setting("excluded_prefixes", saved_str)
        await state.clear()
        await message.answer(f"✅ Removed <code>{text}</code>!\nCurrent: <code>{saved_str}</code>", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(f"Prefix <code>{text}</code> was not found in blacklist.", parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_exclude_list")
async def cb_tool_exclude_list(callback: CallbackQuery):
    cur_str = await get_excluded_prefixes_str()
    prefixes = cur_str.split(",") if cur_str else []
    formatted = "\n".join(f"- <code>+{p}</code>" for p in prefixes) if prefixes else "Empty"
    await callback.message.edit_text(f"📜 <b>Currently Blacklisted Prefixes:</b>\n\n{formatted}", reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_reset_exclude")
async def cb_tool_reset_exclude(callback: CallbackQuery):
    saved_str = ",".join(DEFAULT_EXCLUDE_LIST)
    await db.set_setting("excluded_prefixes", saved_str)
    set_cached_setting("excluded_prefixes", saved_str)
    await callback.answer("Blacklist reset to default (57350, 57351)!", show_alert=True)
    return await cb_tools_main(callback, None)

# --- Retry & Cancel Specific Number ---
@router.callback_query(F.data == "tool_retry")
async def cb_tool_retry(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔁 <b>Retry Number:</b>\n\nSend the Number or Activation ID to resend SMS:",
        reply_markup=kb.back_button(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_retry)

@router.message(BotStates.waiting_for_retry)
async def process_retry_input(message: Message, state: FSMContext):
    target = message.text.strip().replace("+", "")
    if target in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client: 
        return
    res = await client.set_status(target, 3)
    await state.clear()
    if isinstance(res, str) and ("ACCESS_RETRY" in res or "STATUS_WAIT" in res):
        await message.answer(f"✅ Retry mode activated for <code>+{target}</code>!", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await message.answer(f"Failed to retry: {clean_error_text(err)}", reply_markup=kb.main_reply_menu())

@router.callback_query(F.data == "tool_cancel_number")
async def cb_tool_cancel_number(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "❌ <b>Cancel Specific Number:</b>\n\nSend the Number or Activation ID to cancel and refund:",
        reply_markup=kb.back_button(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_cancel)

@router.message(BotStates.waiting_for_cancel)
async def process_cancel_input(message: Message, state: FSMContext):
    target = message.text.strip().replace("+", "")
    if target in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client: 
        return
    res = await client.set_status(target, 8)
    await state.clear()
    if isinstance(res, str) and ("ACCESS_CANCEL" in res or "STATUS_CANCEL" in res):
        await db.delete_activation(target)
        await message.answer(f"✅ Successfully cancelled <code>+{target}</code> and refunded.", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await message.answer(f"Failed to cancel: {clean_error_text(err)}", reply_markup=kb.main_reply_menu())

# --- Stealth Access Commands (/add & /d) ---
@router.callback_query(F.data == "tool_add_user")
async def cb_tool_add_user(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Unauthorized", show_alert=True)
    await callback.message.edit_text("👥 Send the <b>User ID</b> to approve access:", reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_add_id)

@router.message(BotStates.waiting_for_add_id)
async def process_add_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: 
        return
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    try:
        target = int(text)
    except Exception:
        return await message.answer("Enter a valid numeric User ID:")
    
    await db.set_approval_status(target, True)
    await state.clear()
    await message.answer(f"✅ User <code>{target}</code> approved with full access!", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    bot = get_bot_instance()
    if bot:
        try:
            await bot.send_message(
                target,
                "🎉 <b>Your account has been authorized!</b>\n\nPlease send your HeroSMS API Key to get started:",
                parse_mode=ParseMode.HTML
            )
        except Exception:
            pass

@router.message(Command("add"))
async def cmd_quick_add_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: <code>/add 123456789</code>", parse_mode=ParseMode.HTML)
    target = int(args[1])
    await db.set_approval_status(target, True)
    await message.answer(f"✅ User <code>{target}</code> authorized!", parse_mode=ParseMode.HTML)
    bot = get_bot_instance()
    if bot:
        try:
            await bot.send_message(target, "🎉 <b>Your account has been authorized!</b>\nPlease send your HeroSMS API Key to start:", parse_mode=ParseMode.HTML)
        except Exception:
            pass

@router.callback_query(F.data == "tool_revoke_user")
async def cb_tool_revoke_user(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Unauthorized", show_alert=True)
    await callback.message.edit_text("❌ Send the <b>User ID</b> to secretly revoke access:", reply_markup=kb.back_button(), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_revoke_id)

@router.message(BotStates.waiting_for_revoke_id)
async def process_revoke_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")
    try:
        target = int(text)
    except Exception:
        return await message.answer("Enter a valid numeric User ID:")
    await db.set_approval_status(target, False)
    await state.clear()
    await message.answer(f"🔒 Access revoked for user <code>{target}</code> (User was not alerted).", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

@router.message(Command("d"))
async def cmd_quick_revoke_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: <code>/d 123456789</code>", parse_mode=ParseMode.HTML)
    target = int(args[1])
    await db.set_approval_status(target, False)
    await message.answer(f"🔒 Access revoked for user <code>{target}</code> silently.", parse_mode=ParseMode.HTML)

# --- Button: Finish (Replacement of /ok) ---
@router.message(F.text == "Finish")
@router.message(Command("ok"))
async def btn_finish_activations(message: Message):
    if not await is_allowed(message.from_user.id): 
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user or not client:
            return await message.answer("Please set your API key first.")

        res = await client.get_active_activations()
        if not (isinstance(res, dict) and res.get("status") == "success"):
            return await message.answer("❌ Failed to fetch active numbers.")

        activations = res.get("data", [])
        to_finish = [act for act in activations if bool(act.get("smsCode")) or str(act.get("activationStatus")) in ["4", "6"]]

        if not to_finish:
            return await message.answer("ℹ️ No completed activations found with SMS to finish.")

        finished_lines = []
        for act in to_finish:
            aid = str(act.get("activationId"))
            phone = str(act.get("phoneNumber", "Unknown")).lstrip("+")
            try:
                r = await client.set_status(aid, 6)
                if (isinstance(r, str) and ("ACTIVATION" in r or "ACCESS_OK" in r)) or (isinstance(r, dict) and r.get("status") == "success"):
                    await db.delete_activation(aid)
                    for k in list(processed_otps.keys()):
                        if k.startswith(f"{aid}:"):
                            del processed_otps[k]
                    finished_lines.append(f"• <b>+{phone}</b> - Finished ✅")
                else:
                    finished_lines.append(f"• <b>+{phone}</b> - ⚠️ Failed")
            except Exception as e:
                finished_lines.append(f"• <b>+{phone}</b> - Error: {e}")

        summary = f"<b>✅ Finished Activations ({len(finished_lines)}):</b>\n\n" + "\n".join(finished_lines)
        await message.answer(summary, parse_mode=ParseMode.HTML)

# --- Bulk Buy Engine (20s Checker Timeout + Cost Delta) ---
@router.message(F.text == "Bulk Buy Numbers")
async def text_bulk_buy(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): 
        return
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client: 
        return await message.answer("Please send your HeroSMS API Key first.")
    await message.answer("📦 <b>Bulk Purchase</b>\n\nHow many numbers do you want to buy? (1-50)", parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_bulk_amount)

@router.message(BotStates.waiting_for_bulk_amount)
async def process_bulk_amount(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Bulk buy cancelled.")

    try:
        amount = int(text)
        if not (1 <= amount <= 50): 
            raise ValueError
    except Exception:
        return await message.answer("Enter a valid number between 1 and 50.")

    await state.clear()
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client: 
        return

    # Low balance safety check
    init_bal = await client.get_balance()
    if init_bal is not None and init_bal < 0.50:
        await message.answer(f"⚠️ <b>Low Balance Alert:</b> Current balance is <code>${init_bal:.4f} USD</code>. Purchase might fail.", parse_mode=ParseMode.HTML)

    status_msg = await message.answer(f"Buying {amount} numbers... (0/{amount}) (0%)")
    
    purchased = []
    batch_db_records = []
    number_aid_map = {}
    number_cost_map = {}
    last_edit_time = time.time()
    
    current_exc = await get_excluded_prefixes_str()
    current_op = await get_preferred_operator_str()
    dynamic_max = await get_dynamic_max_price()

    api_exc = current_exc if current_exc else None
    api_op = current_op if current_op and current_op.lower() != "any" else None

    prev_balance = init_bal

    for i in range(amount):
        res = None
        for attempt in range(3):
            res = await client.get_number(
                service=TG_SERVICE, 
                country=COLOMBIA_ID, 
                max_price=dynamic_max,
                phone_exception=api_exc,
                operator=api_op
            )
            if isinstance(res, dict) and "activationId" in res:
                break
            await asyncio.sleep(0.8)

        if isinstance(res, dict) and "activationId" in res:
            aid = str(res["activationId"])
            phone = res.get("phoneNumber", "Unknown")
            cost_val = res.get("cost")
            
            clean_phone = str(phone).lstrip("+").strip()
            purchased.append(clean_phone)
            batch_db_records.append((aid, message.from_user.id, clean_phone))
            
            number_aid_map[clean_phone] = aid
            number_aid_map[f"+{clean_phone}"] = aid
            
            # Accurate Cost Delta Detection (No Hardcoded Fallback)
            if cost_val is not None:
                formatted_cost = f"${float(cost_val):.3f}"
            else:
                curr_balance = await client.get_balance()
                if prev_balance is not None and curr_balance is not None:
                    delta = prev_balance - curr_balance
                    formatted_cost = f"${delta:.3f}" if delta > 0 else f"${dynamic_max:.3f}"
                    prev_balance = curr_balance
                else:
                    formatted_cost = f"${dynamic_max:.3f}"
            
            number_cost_map[clean_phone] = formatted_cost
            number_cost_map[f"+{clean_phone}"] = formatted_cost
            
            # Flood-wait protected editing
            now = time.time()
            if (now - last_edit_time >= 3.5) or (i == amount - 1):
                try:
                    display_lines = purchased[-10:]
                    lines = "\n".join(
                        f"{n}. <b>+{p}</b> ({get_colombia_operator(p)})" 
                        for n, p in enumerate(display_lines, len(purchased)-len(display_lines)+1)
                    )
                    percent = int((len(purchased) / amount) * 100)
                    upd_text = f"Buying {amount} numbers... ({len(purchased)}/{amount}) ({percent}%)\n\n{lines}"
                    if len(purchased) > 10:
                        upd_text += f"\n...and {len(purchased)-10} earlier"
                    await status_msg.edit_text(upd_text, parse_mode=ParseMode.HTML)
                    last_edit_time = now
                except (TelegramRetryAfter, TelegramBadRequest, Exception):
                    pass
            
            await asyncio.sleep(0.3)
        else:
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message.answer(f"Stopped at #{i+1}: {clean_error_text(err)}")
            break

    if purchased:
        if hasattr(db, "save_activations_batch"):
            await db.save_activations_batch(batch_db_records)
        else:
            for aid, uid, p in batch_db_records:
                await db.save_activation(aid, uid, p)

        try:
            await status_msg.edit_text(f"✅ Purchased {len(purchased)} numbers! (100%)\n🔍 Checking Telegram registration status, please wait...")
        except Exception:
            pass

        # 20-Second Safe Timeout for Checker
        try:
            check_results = await asyncio.wait_for(check_telegram_numbers(purchased), timeout=20.0)
        except Exception as e:
            logging.warning(f"Telegram checker timeout or exception: {e}")
            check_results = {}

        parsed_items = []
        bad_numbers_to_cancel = []

        for p in purchased:
            clean_p = str(p).lstrip("+").strip()
            op = get_colombia_operator(clean_p)
            formatted_k = f"+{clean_p}"
            raw_st = check_results.get(formatted_k) or check_results.get(clean_p)
            info = format_tg_status(raw_st)
            
            cost_str = number_cost_map.get(clean_p) or f"${dynamic_max:.3f}"
            
            item_obj = {
                "phone": clean_p,
                "aid": number_aid_map.get(clean_p),
                "cost_str": cost_str,
                "operator": op,
                "badge": info["badge"],
                "priority": info["priority"],
                "is_fresh": info["is_fresh"],
                "is_error": info.get("is_error", False)
            }
            parsed_items.append(item_obj)

            if not info["is_fresh"] and not info.get("is_error") and info["priority"] in [3, 4]:
                bad_numbers_to_cancel.append(item_obj)

        fresh_list = [x for x in parsed_items if x["is_fresh"]]
        other_list = [x for x in parsed_items if not x["is_fresh"] and not x.get("is_error")]
        error_list = [x for x in parsed_items if x.get("is_error")]
        other_list.sort(key=lambda x: x["priority"])

        lines = [
            f"🎉 <b>Bulk Order Completed!</b>",
            f"Total: {len(purchased)} numbers (tap any number to copy)\n"
        ]

        if fresh_list:
            lines.append(f"🟢 <b>Fresh Numbers ({len(fresh_list)}):</b>")
            for idx, item in enumerate(fresh_list, 1):
                rate_text = f" <b>{item['cost_str']}</b>"
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — {item['badge']}{rate_text}\n")

        if other_list:
            lines.append(f"🔻 <b>Unavailable / Occupied ({len(other_list)}):</b>")
            for idx, item in enumerate(other_list, 1):
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — <b>{item['badge']}</b>")
            lines.append("<i>(Auto-cancelling for refund in 2 mins...)</i>\n")

        if error_list:
            lines.append(f"⚠️ <b>Check Unverified ({len(error_list)}):</b>")
            for idx, item in enumerate(error_list, 1):
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — <b>{item['badge']}</b>")
            lines.append("")

        lines.append("Waiting for OTPs...")

        if bad_numbers_to_cancel:
            asyncio.create_task(auto_cancel_bad_numbers(client, bad_numbers_to_cancel))

        final = "\n".join(lines)
        batch_id = str(uuid.uuid4())[:8]
        fresh_phones = [x["phone"] for x in fresh_list]
        fresh_batches_cache[batch_id] = {
            "summary": final,
            "fresh_phones": fresh_phones,
            "created_at": time.time()
        }
        cleanup_expired_cache()

        reply_markup = kb.bulk_result_menu(batch_id, len(fresh_list))

        if len(final) > 4000:
            for part in [final[j:j+4000] for j in range(0, len(final), 4000)]:
                await message.answer(part, parse_mode=ParseMode.HTML)
            try: 
                await status_msg.delete()
            except Exception: 
                pass
        else:
            await status_msg.edit_text(final, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await status_msg.edit_text("Could not purchase any numbers.")

# --- Fresh Batch Callbacks ---
@router.callback_query(F.data.startswith("show_fresh_"))
async def cb_show_fresh_numbers(callback: CallbackQuery):
    cleanup_expired_cache()
    batch_id = callback.data[len("show_fresh_"):]
    batch_data = fresh_batches_cache.get(batch_id)

    if not batch_data or not batch_data.get("fresh_phones"):
        return await callback.answer("No fresh numbers found or session expired.", show_alert=True)

    fresh_phones = batch_data["fresh_phones"]
    await callback.message.edit_text(
        f"🟢 <b>Fresh Numbers ({len(fresh_phones)})</b>\n<i>Tap any number to copy:</i>",
        reply_markup=kb.fresh_numbers_menu(fresh_phones, batch_id),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data.startswith("back_bulk_"))
async def cb_back_bulk(callback: CallbackQuery):
    cleanup_expired_cache()
    batch_id = callback.data[len("back_bulk_"):]
    batch_data = fresh_batches_cache.get(batch_id)

    if not batch_data:
        return await callback.answer("Session expired.", show_alert=True)

    summary = batch_data["summary"]
    fresh_count = len(batch_data.get("fresh_phones", []))
    await callback.message.edit_text(
        summary,
        reply_markup=kb.bulk_result_menu(batch_id, fresh_count),
        parse_mode=ParseMode.HTML
    )

# --- Active Numbers Navigation & Single Cancel ---
@router.callback_query(F.data.startswith("act_page_"))
async def cb_active_page(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client: 
        return

    res = await client.get_active_activations()
    if isinstance(res, dict) and res.get("status") == "success":
        activations = res.get("data", [])
        total = len(activations)
        if not activations:
            return await callback.message.edit_text("No active numbers left.", reply_markup=kb.back_button())
        await callback.message.edit_text(
            f"Active Numbers ({total}) - Page {page+1}/{(total+9)//10}:",
            reply_markup=kb.active_numbers_menu(activations, page=page)
        )

@router.callback_query(F.data == "cancel_all_active")
async def cb_cancel_all_active(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id): 
        return
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client: 
        return

    res = await client.get_active_activations()
    if not (isinstance(res, dict) and res.get("status") == "success"):
        return await callback.answer("Failed to fetch active numbers.", show_alert=True)
    activations = res.get("data", [])
    if not activations:
        return await callback.answer("No active numbers to cancel.", show_alert=True)

    await callback.message.edit_text(f"Cancelling {len(activations)} numbers... please wait.")
    sem = asyncio.Semaphore(4)

    async def cancel_one(act):
        async with sem:
            aid = str(act.get("activationId", ""))
            if not aid: 
                return False
            try:
                r = await client.set_status(aid, 8)
                if (isinstance(r, str) and ("CANCEL" in r)) or (isinstance(r, dict) and r.get("status") == "success"):
                    await db.delete_activation(aid)
                    for k in list(processed_otps.keys()):
                        if k.startswith(f"{aid}:"):
                            del processed_otps[k]
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.1)
            return False

    results = await asyncio.gather(*[cancel_one(a) for a in activations])
    ok = sum(1 for x in results if x)
    await callback.message.edit_text(f"Cancelled {ok}/{len(activations)} numbers. Balance refunded.", reply_markup=kb.back_button())

@router.callback_query(F.data.startswith("active_cancel_"))
async def cb_active_cancel(callback: CallbackQuery):
    parts = callback.data.split("_")
    aid = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0

    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client: 
        return

    r = await client.set_status(aid, 8)
    if isinstance(r, str) and ("CANCEL" in r):
        await db.delete_activation(aid)
        for k in list(processed_otps.keys()):
            if k.startswith(f"{aid}:"):
                del processed_otps[k]
        await callback.answer("Cancelled!", show_alert=True)
        res = await client.get_active_activations()
        if isinstance(res, dict) and res.get("status") == "success":
            acts = res.get("data", [])
            if not acts:
                await callback.message.edit_text("No active numbers left.", reply_markup=kb.back_button())
            else:
                total = len(acts)
                max_page = (total - 1) // 10
                current_page = min(page, max_page)
                await callback.message.edit_text(
                    f"Active Numbers ({total}) - Page {current_page+1}/{(total+9)//10}:",
                    reply_markup=kb.active_numbers_menu(acts, page=current_page)
                )
    elif isinstance(r, str) and "EARLY_CANCEL_DENIED" in r:
        await callback.answer("Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        await callback.answer("Failed to cancel.", show_alert=True)

# --- Broadcast & Maintenance Handlers ---
@router.callback_query(F.data == "admin_maintenance")
async def cb_admin_maintenance(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("Unauthorized", show_alert=True)
    cur = await get_cached_setting("maintenance", "0")
    new_val = "0" if cur == "1" else "1"
    await db.set_setting("maintenance", new_val)
    set_cached_setting("maintenance", new_val)
    await callback.answer(f"Maintenance Mode: {'ENABLED' if new_val == '1' else 'DISABLED'}", show_alert=True)
    return await cb_tools_main(callback, None)

@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("Unauthorized", show_alert=True)
    await callback.message.edit_text("Send the broadcast message:", reply_markup=kb.back_button())
    await state.set_state(BotStates.waiting_for_broadcast)

@router.message(BotStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: 
        return
    if message.text.strip() in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Broadcast cancelled.")

    users = await db.get_all_users()
    sent = 0
    for uid in users:
        try:
            await message.bot.send_message(uid, f"📢 <b>Announcement:</b>\n\n{message.text}", parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await message.answer(f"✅ Sent to {sent} active users.", reply_markup=kb.main_reply_menu())
    await state.clear()

@router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("Unauthorized", show_alert=True)
    await callback.message.edit_text("Send the User ID to ban/unban:", reply_markup=kb.back_button())
    await state.set_state(BotStates.waiting_for_ban_id)

@router.message(BotStates.waiting_for_ban_id)
async def process_ban_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: 
        return
    try: 
        target = int(message.text.strip())
    except Exception:
        return await message.answer("Invalid ID.")
    user = await db.get_user(target)
    if not user:
        return await message.answer("User not found.")
    new_status = not bool(user["is_banned"])
    await db.set_ban_status(target, new_status)
    label = "BANNED 🚫" if new_status else "UNBANNED ✅"
    await message.answer(f"User <code>{target}</code> is now <b>{label}</b>.", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    await state.clear()

# --- Single Check SMS & Cancel Handlers ---
@router.callback_query(F.data.startswith("check_"))
async def cb_check_sms(callback: CallbackQuery):
    aid = callback.data[len("check_"):]
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client:
        return await callback.answer("API Key not found.", show_alert=True)

    row = await db.get_activation_user(aid)
    phone = row[1] if row else "Unknown"

    res = await client.get_status(aid)
    if isinstance(res, str):
        if res.startswith("STATUS_OK:"):
            code = res.split(":", 1)[1]
            clean_p = str(phone).lstrip("+").strip()
            count = phone_otp_counter.get(clean_p, 0) + 1
            phone_otp_counter[clean_p] = count
            is_second = (count > 1)
            
            text = format_otp_text(phone, code, is_second=is_second)
            processed_otps[f"{aid}:{code}"] = time.time()
            cleanup_expired_cache()
            await callback.message.edit_text(text, reply_markup=kb.otp_copy_menu(code), parse_mode=ParseMode.HTML)
        elif res.startswith("STATUS_WAIT_CODE"):
            await callback.answer("Still waiting for SMS...", show_alert=True)
        elif res.startswith("STATUS_CANCEL"):
            await db.delete_activation(aid)
            await callback.message.edit_text("Activation cancelled.", reply_markup=kb.back_button())
        else:
            await callback.answer(f"Status: {clean_error_text(res)}", show_alert=True)
    else:
        await callback.answer("Error checking status.", show_alert=True)

@router.callback_query(F.data.startswith("single_cancel_"))
async def cb_cancel_single(callback: CallbackQuery):
    aid = callback.data[len("single_cancel_"):]
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client:
        return await callback.answer("API Key not found.", show_alert=True)

    res = await client.set_status(aid, 8)
    if isinstance(res, str) and ("ACCESS_CANCEL" in res or "STATUS_CANCEL" in res):
        await db.delete_activation(aid)
        for k in list(processed_otps.keys()):
            if k.startswith(f"{aid}:"):
                del processed_otps[k]
        await callback.message.edit_text("Cancelled. Balance refunded.", reply_markup=kb.back_button())
    elif isinstance(res, str) and "EARLY_CANCEL_DENIED" in res:
        await callback.answer("Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.answer(f"Error: {clean_error_text(err)}", show_alert=True)

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()
