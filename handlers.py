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

MENU_BUTTONS = ["Bulk Buy Numbers", "Finish"]

SETTINGS_CACHE = {}

# --- Verified Custom Premium Emojis ---
EMOJI_FLAG  = '<tg-emoji emoji-id="5294010206974397371">🇨🇴</tg-emoji>'
EMOJI_CARD  = '<tg-emoji emoji-id="5206330150433595241">💳</tg-emoji>'
EMOJI_TICK  = '<tg-emoji emoji-id="6087154735125630953">✅</tg-emoji>'
EMOJI_CROSS = '<tg-emoji emoji-id="5321012601939838274">❌</tg-emoji>'
EMOJI_WARN  = '<tg-emoji emoji-id="5420323339723881652">⚠️</tg-emoji>'
EMOJI_SIREN = '<tg-emoji emoji-id="5395695537687123235">🚨</tg-emoji>'
EMOJI_LOCK  = '<tg-emoji emoji-id="6334379984760604198">🔒</tg-emoji>'
EMOJI_BAN   = '<tg-emoji emoji-id="5280803324273115630">🚫</tg-emoji>'
EMOJI_PLANE = '<tg-emoji emoji-id="5411563083908797492">🛫</tg-emoji>'
EMOJI_BOX   = '<tg-emoji emoji-id="5298809897352193254">📦</tg-emoji>'
EMOJI_KEY   = '<tg-emoji emoji-id="5193070340850327783">🔑</tg-emoji>'
EMOJI_USER  = '<tg-emoji emoji-id="5249053508681883137">👤</tg-emoji>'

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

# --- Real Live Sniper Grabber with Telegram Freshness Verification & 3x Alerts ---
async def start_live_sniper_process(message: Message, target_op: str):
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client:
        return await message.answer("API Key not found.")

    dynamic_max = await get_dynamic_max_price()
    current_exc = await get_excluded_prefixes_str()
    api_exc = current_exc if current_exc else None
    clean_op = target_op.strip().lower()

    status_msg = await message.answer(
        f"🎯 <b>Sniper Active: {clean_op.upper()}</b> (Max: ${dynamic_max:.3f})\n"
        f"🔍 Searching & attempting to grab... (Attempt #1)",
        parse_mode=ParseMode.HTML
    )

    attempt = 0
    init_bal = await client.get_balance()
    last_ui_edit = time.time()

    while True:
        is_active = (await get_cached_setting("sniper_active", "0")) == "1"
        if not is_active:
            try:
                await status_msg.edit_text("🛑 <b>Sniper Stopped by User.</b>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
            break

        attempt += 1

        res = await client.get_number(
            service=TG_SERVICE,
            country=COLOMBIA_ID,
            max_price=dynamic_max,
            phone_exception=api_exc,
            operator=clean_op
        )

        if isinstance(res, dict) and "activationId" in res:
            aid = str(res["activationId"])
            phone = res.get("phoneNumber", "Unknown")
            cost_val = res.get("cost")
            clean_phone = str(phone).lstrip("+").strip()

            op_detected = get_colombia_operator(clean_phone)

            if clean_op != "any" and op_detected.lower() != clean_op:
                try:
                    await client.set_status(aid, 8)
                except Exception:
                    pass
                await asyncio.sleep(0.5)
                continue

            await db.set_setting("sniper_active", "0")
            set_cached_setting("sniper_active", "0")

            await db.save_activation(aid, message.from_user.id, clean_phone)

            if cost_val is not None:
                rate_str = f"${float(cost_val):.3f}"
            else:
                curr_bal = await client.get_balance()
                if init_bal is not None and curr_bal is not None and (init_bal - curr_bal) > 0:
                    rate_str = f"${(init_bal - curr_bal):.3f}"
                else:
                    rate_str = f"${dynamic_max:.3f}"

            try:
                await status_msg.edit_text(
                    f"🎯 <b>Number Grabbed!</b>\n"
                    f"{EMOJI_FLAG} <code>+{clean_phone}</code> ({op_detected.upper()})\n\n"
                    f"🔍 Checking Telegram status, please wait...",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

            try:
                check_results = await asyncio.wait_for(check_telegram_numbers([clean_phone]), timeout=20.0)
            except Exception:
                check_results = {}

            formatted_k = f"+{clean_phone}"
            raw_st = check_results.get(formatted_k) or check_results.get(clean_phone)
            tg_info = format_tg_status(raw_st)

            if tg_info["is_fresh"]:
                verdict_text = f"{EMOJI_TICK} <b>FRESH NUMBER!</b> Waiting for OTP..."
            else:
                verdict_text = f"🔻 <b>{tg_info['badge']}</b> (<i>Auto-cancelling for refund in 2 mins...</i>)"
                bad_item = [{"aid": aid, "phone": clean_phone}]
                asyncio.create_task(auto_cancel_bad_numbers(client, bad_item))

            final_text = (
                f"🎉 <b>SNIPER RESULT</b>\n\n"
                f"📡 Operator: <b>{op_detected.upper()}</b>\n"
                f"{EMOJI_FLAG} Telegram: <code>+{clean_phone}</code>\n"
                f"{EMOJI_CARD} Rate: <b>{rate_str}</b>\n"
                f"🔍 Status: <b>{tg_info['badge']}</b>\n\n"
                f"{verdict_text}"
            )

            try:
                await status_msg.edit_text(
                    final_text,
                    reply_markup=kb.number_action_menu(aid),
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

            try:
                await message.answer(
                    f"{EMOJI_SIREN} <b>SNIPER ALERT (1/3)</b>\n\n"
                    f"📡 Operator: <b>{op_detected.upper()}</b>\n"
                    f"{EMOJI_FLAG} Number: <code>+{clean_phone}</code>\n"
                    f"{EMOJI_CARD} Rate: <b>{rate_str}</b>\n"
                    f"🔍 Status: <b>{tg_info['badge']}</b>",
                    parse_mode=ParseMode.HTML,
                    disable_notification=False
                )
                await asyncio.sleep(0.3)

                await message.answer(
                    f"⚡ <b>TAP TO COPY (2/3):</b>\n\n<code>+{clean_phone}</code>",
                    parse_mode=ParseMode.HTML,
                    disable_notification=False
                )
                await asyncio.sleep(0.3)

                await message.answer(
                    "⏳ <b>OTP READY (3/3)</b>\n\n"
                    "Telegram-e number boshiye code pathan. OTP asha matroi button shoho show korbe!",
                    parse_mode=ParseMode.HTML,
                    disable_notification=False
                )
            except Exception as e:
                logging.error(f"Failed to send 3x loud notifications: {e}")

            break

        elif isinstance(res, str) and "429" in res:
            await asyncio.sleep(6)

        now = time.time()
        if now - last_ui_edit >= 2.0:
            try:
                await status_msg.edit_text(
                    f"🎯 <b>Sniper Active: {clean_op.upper()}</b> (Max: ${dynamic_max:.3f})\n"
                    f"🔍 Searching & attempting to grab... (Attempt #{attempt})",
                    parse_mode=ParseMode.HTML
                )
                last_ui_edit = now
            except (TelegramRetryAfter, TelegramBadRequest, Exception):
                pass

        await asyncio.sleep(2.5)

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
    return f"{EMOJI_FLAG} <b>Telegram</b> <code>{safe_phone}</code>{suffix}"

def format_tg_status(raw_status: any) -> dict:
    if raw_status is None:
        return {"badge": f"{EMOJI_WARN} Check Failed", "priority": 5, "is_fresh": False, "is_error": True}

    if isinstance(raw_status, dict):
        st = str(raw_status.get("status") or raw_status.get("result") or raw_status.get("msg") or raw_status).strip().lower()
    elif isinstance(raw_status, bool):
        st = "occupied" if raw_status else "unoccupied"
    else:
        st = str(raw_status).strip().lower()

    if "api_error" in st or "check_failed" in st or not st:
        return {"badge": f"{EMOJI_WARN} Check Failed", "priority": 5, "is_fresh": False, "is_error": True}

    fresh_signals = [
        "unoccupied", "phone_number_unoccupied",
        "unregistered", "not_registered", "not registered", "non_registered",
        "not_occupied", "not occupied", "free", "fresh", "available",
        "ready", "allow", "ok", "valid", "clean", "false", "0",
        "no_account", "no account", "does_not_exist", "not_exists"
    ]
    if any(w in st for w in fresh_signals):
        if "banned" in st and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):
            return {"badge": f"{EMOJI_BAN} Banned", "priority": 4, "is_fresh": False, "is_error": False}
        return {"badge": f"{EMOJI_TICK}", "priority": 1, "is_fresh": True, "is_error": False}

    locked_signals = ["flood", "locked", "lock", "wait", "restricted", "2fa", "password", "has_password"]
    if any(w in st for w in locked_signals):
        return {"badge": f"{EMOJI_LOCK}", "priority": 2, "is_fresh": False, "is_error": False}

    occupied_signals = ["occupied", "registered", "taken", "used", "true", "1"]
    if any(w in st for w in occupied_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):
        return {"badge": f"{EMOJI_CROSS}", "priority": 3, "is_fresh": False, "is_error": False}

    banned_signals = ["banned", "ban", "blocked"]
    if any(w in st for w in banned_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "without", "false"]):
        return {"badge": f"{EMOJI_BAN}", "priority": 4, "is_fresh": False, "is_error": False}

    clean = clean_error_text(st)
    return {"badge": f"{EMOJI_WARN} {clean}", "priority": 5, "is_fresh": False, "is_error": False}

async def get_excluded_prefixes_str() -> str:
    return await get_cached_setting("excluded_prefixes", ",".join(DEFAULT_EXCLUDE_LIST))

async def get_preferred_operator_str() -> str:
    return await get_cached_setting("preferred_operator", DEFAULT_OPERATOR)

async def get_valid_user_client(user_id: int):
    user = await db.get_user(user_id)
    if not user:
        return None, None
    try:
        user_dict = dict(user) if not isinstance(user, dict) else user
        api_key = user_dict.get("api_key")
        if not api_key:
            return None, None
        return user_dict, HeroSMSClient(api_key)
    except Exception:
        return None, None

async def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID: 
        return True
    
    user = await db.get_user(user_id)
    if not user:
        return False
    user_dict = dict(user) if not isinstance(user, dict) else user
    if user_dict.get("is_banned") or not user_dict.get("is_approved"):
        return False
        
    maintenance = await get_cached_setting("maintenance", "0")
    return maintenance != "1"

# --- Webhook Handler (Universal GET/POST & Direct SQLite Lookup) ---
async def handle_herosms_webhook(request: web.Request):
    data = {}
    try:
        if request.query:
            data.update(dict(request.query))
        if request.method == "POST":
            try:
                body_json = await request.json()
                if isinstance(body_json, dict):
                    data.update(body_json)
            except Exception:
                try:
                    body_post = await request.post()
                    data.update(dict(body_post))
                except Exception:
                    pass
    except Exception as e:
        logging.error(f"Error parsing webhook incoming data: {e}")
        return web.Response(text="Bad Request", status=400)

    aid = str(
        data.get("activationId") or 
        data.get("id") or 
        data.get("activation_id") or 
        data.get("activationId".lower()) or 
        ""
    ).strip()

    raw_code = (
        data.get("code") or 
        data.get("smsCode") or 
        data.get("text") or 
        data.get("sms") or 
        data.get("action") or 
        ""
    )

    code = ""
    if raw_code:
        str_val = str(raw_code).strip()
        if str_val.startswith("STATUS_OK:"):
            code = str_val.split(":", 1)[1].strip()
        else:
            match = re.search(r'\d{4,8}', str_val)
            code = match.group(0) if match else str_val

    direct_phone = str(data.get("phoneNumber") or data.get("phone") or "").lstrip("+").strip()

    logging.info(f"⚡ [Webhook Received] AID: {aid} | Code: {code} | Phone: {direct_phone}")

    if not aid or not code or code in ["STATUS_WAIT_CODE", "STATUS_CANCEL"]:
        return web.Response(text="OK", status=200)

    cache_key = f"{aid}:{code}"
    if cache_key in processed_otps:
        return web.Response(text="OK", status=200)

    processed_otps[cache_key] = time.time()
    cleanup_expired_cache()

    try:
        user_id = None
        phone = direct_phone

        # Safe database inspection (dict, sqlite3.Row, tuple handling)
        row = await db.get_activation_user(aid)
        if not row and hasattr(db, "get_activation"):
            row = await db.get_activation(aid)

        if row:
            if isinstance(row, dict):
                user_id = row.get("user_id")
                phone = phone or row.get("phone")
            elif hasattr(row, "keys"):
                r_dict = dict(row)
                user_id = r_dict.get("user_id")
                phone = phone or r_dict.get("phone")
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                user_id = row[0]
                phone = phone or row[1]

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
                    logging.info(f"✅ OTP successfully delivered to User {user_id} for number {phone}")
                except Exception as e:
                    logging.error(f"Failed to send webhook OTP to user {user_id}: {e}")
        else:
            logging.warning(f"⚠️ Webhook matching failed: AID {aid} not found in database or user_id missing.")
    except Exception as e:
        logging.error(f"Error processing webhook database lookup for {aid}: {e}")

    return web.Response(text="OK", status=200)

# --- Start Command ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    try:
        await db.add_user(uid, message.from_user.username, message.from_user.full_name)
    except TypeError:
        try:
            await db.add_user(uid)
        except Exception:
            pass
    except Exception:
        pass

    if uid == ADMIN_ID:
        try:
            await db.set_approval_status(uid, True)
        except Exception:
            pass

        user = await db.get_user(uid)
        user_dict = dict(user) if user and hasattr(user, "keys") else (user if isinstance(user, dict) else {})
        has_api_key = bool(user_dict.get("api_key"))
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

    user = await db.get_user(uid)
    if not user:
        return
    user_dict = dict(user) if hasattr(user, "keys") else (user if isinstance(user, dict) else {})
    if not user_dict.get("is_approved") or user_dict.get("is_banned"):
        return

    maintenance = await get_cached_setting("maintenance", "0")
    if maintenance == "1":
        return

    has_api_key = bool(user_dict.get("api_key"))
    if not has_api_key:
        await message.answer(
            "Welcome to HeroSMS Bot!\n\nPlease send your HeroSMS API Key to get started.",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(BotStates.waiting_for_api_key)
    else:
        await message.answer("Welcome back!", reply_markup=kb.main_reply_menu())

# --- Direct /api Command & Input Handler ---
@router.message(Command("api"))
async def cmd_direct_api(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id):
        return
    await state.clear()
    args = message.text.split()
    if len(args) >= 2:
        api_key = args[1].strip("\"'").strip()
        client = HeroSMSClient(api_key)
        balance = await client.get_balance()
        if balance is not None:
            await db.update_api_key(message.from_user.id, api_key)
            return await message.answer(
                f"{EMOJI_TICK} <b>API Key Updated!</b>\n{EMOJI_CARD} Balance: <code>{balance:.4f} USD</code>",
                reply_markup=kb.main_reply_menu(),
                parse_mode=ParseMode.HTML
            )
        else:
            return await message.answer(f"{EMOJI_CROSS} Invalid API Key. Please verify your key.")

    await message.answer(
        f"{EMOJI_KEY} <b>Send New API Key:</b>\n\nNiche apnar new HeroSMS API Key paste kore pathan:",
        reply_markup=kb.api_key_cancel_menu(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_api_key)

@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS or text.lower() == "t":
        await state.clear()
        return await message.answer("Action cancelled.")

    api_key = text.strip("\"'").strip()
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        client = HeroSMSClient(api_key)
        balance = await client.get_balance()
        
        if balance is not None:
            await db.update_api_key(message.from_user.id, api_key)
            await state.clear()
            await message.answer(
                f"{EMOJI_TICK} <b>API Key Saved Successfully!</b>\n{EMOJI_CARD} Balance: <code>{balance:.4f} USD</code>",
                reply_markup=kb.main_reply_menu(),
                parse_mode=ParseMode.HTML
            )
        else:
            await message.answer(f"{EMOJI_CROSS} Invalid API Key. Please send a valid key (or send 't' to cancel):")

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

# --- Quick Tools Dashboard (/t, /tools, /admin) ---
@router.message(Command("t", "tools", "admin"))
@router.message(F.text.casefold() == "t")
async def open_tools_menu(message: Message, state: FSMContext):
    await state.clear()
    if not await is_allowed(message.from_user.id): 
        return
    
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        max_p = await get_dynamic_max_price()
        op = await get_preferred_operator_str()
        sniper_on = (await get_cached_setting("sniper_active", "0")) == "1"
        sniper_op = await get_cached_setting("sniper_op", "claro")
        
        snapshot = (
            f"🛠️ <b>Control Center & Tools (Page 1/2)</b>\n\n"
            f"📊 <b>Quick Settings Snapshot:</b>\n"
            f"• Max Price Limit: <code>${max_p:.3f}</code>\n"
            f"• Active Operator: <code>{op.upper()}</code>\n"
            f"• Auto Sniper: <code>{'ACTIVE (' + sniper_op.upper() + ')' if sniper_on else 'OFF'}</code>\n\n"
            f"Select any tool or action from the buttons below:"
        )
        await message.answer(snapshot, reply_markup=kb.tools_menu_page_1(sniper_on, sniper_op), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tools_page_1")
async def cb_tools_page_1(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id): 
        return
    max_p = await get_dynamic_max_price()
    op = await get_preferred_operator_str()
    sniper_on = (await get_cached_setting("sniper_active", "0")) == "1"
    sniper_op = await get_cached_setting("sniper_op", "claro")
    
    snapshot = (
        f"🛠️ <b>Control Center & Tools (Page 1/2)</b>\n\n"
        f"📊 <b>Quick Settings Snapshot:</b>\n"
        f"• Max Price Limit: <code>${max_p:.3f}</code>\n"
        f"• Active Operator: <code>{op.upper()}</code>\n"
        f"• Auto Sniper: <code>{'ACTIVE (' + sniper_op.upper() + ')' if sniper_on else 'OFF'}</code>\n\n"
        f"Select any tool or action from the buttons below:"
    )
    try:
        await callback.message.edit_text(snapshot, reply_markup=kb.tools_menu_page_1(sniper_on, sniper_op), parse_mode=ParseMode.HTML)
    except (TelegramBadRequest, Exception):
        pass

@router.callback_query(F.data == "tools_page_2")
async def cb_tools_page_2(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id): 
        return
    maintenance = (await get_cached_setting("maintenance", "0")) == "1"
    
    snapshot = (
        f"🛠️ <b>Control Center & Tools (Page 2/2)</b>\n\n"
        f"⚙️ <b>Advanced & Administrative Controls:</b>\n"
        f"• HeroSMS API Key Management\n"
        f"• Order Statistics & User Management\n"
        f"• System Maintenance & Broadcast\n\n"
        f"Select an action from below:"
    )
    try:
        await callback.message.edit_text(snapshot, reply_markup=kb.tools_menu_page_2(maintenance), parse_mode=ParseMode.HTML)
    except (TelegramBadRequest, Exception):
        pass

# --- Sniper Toggle & Start Flow ---
@router.callback_query(F.data == "tool_toggle_sniper")
async def cb_tool_toggle_sniper(callback: CallbackQuery, state: FSMContext):
    current_status = await get_cached_setting("sniper_active", "0")
    
    if current_status == "1":
        await db.set_setting("sniper_active", "0")
        set_cached_setting("sniper_active", "0")
        await callback.answer("🛑 Auto Sniper Stopped!", show_alert=True)
        return await cb_tools_page_1(callback)
    else:
        await callback.answer()
        await callback.message.edit_text(
            "🎯 <b>Auto Sniper Direct Grabber</b>\n\n"
            "Kon operator-er number continuous try korte chan? Operator name likhun (e.g. <code>claro</code>, <code>tigo</code>, <code>movistar</code>):",
            reply_markup=kb.back_button("tools_page_1"),
            parse_mode=ParseMode.HTML
        )
        await state.set_state(BotStates.waiting_for_sniper_operator)

@router.message(BotStates.waiting_for_sniper_operator)
async def process_sniper_operator(message: Message, state: FSMContext):
    target_op = message.text.strip().lower()
    if target_op in MENU_BUTTONS or target_op.lower() == "t":
        await state.clear()
        return await message.answer("Sniper configuration cancelled.")

    await state.clear()
    await db.set_setting("sniper_active", "1")
    set_cached_setting("sniper_active", "1")
    await db.set_setting("sniper_op", target_op)
    set_cached_setting("sniper_op", target_op)

    asyncio.create_task(start_live_sniper_process(message, target_op))

# --- Button: Check Balance ---
@router.callback_query(F.data == "tool_balance")
async def cb_tool_balance(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user or not client:
        return await callback.answer("API Key not found.", show_alert=True)
    bal = await client.get_balance()
    if bal is not None:
        alert = f"\n\n{EMOJI_WARN} <b>Warning:</b> Balance is below $0.50! Please recharge soon." if bal < 0.50 else ""
        text = f"{EMOJI_CARD} <b>Your Current Balance:</b> <code>{bal:.4f} USD</code>{alert}"
        await callback.message.edit_text(text, reply_markup=kb.back_button("tools_page_1"), parse_mode=ParseMode.HTML)
    else:
        await callback.answer("Error fetching balance.", show_alert=True)

# --- View / Change API Key ---
@router.callback_query(F.data == "tool_view_api_key")
async def cb_tool_view_api_key(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user = await db.get_user(callback.from_user.id)
    user_dict = dict(user) if user and hasattr(user, "keys") else (user if isinstance(user, dict) else {})
    api_k = user_dict.get("api_key")
    
    if api_k:
        masked_preview = f"{api_k[:6]}...{api_k[-4:]}"
        text = (
            f"{EMOJI_KEY} <b>Your HeroSMS API Key:</b>\n\n"
            f"<code>{api_k}</code>\n\n"
            f"• Preview: <code>{masked_preview}</code>\n"
            f"• Status: {EMOJI_TICK} <b>Configured</b>\n\n"
            f"<i>Tap above to copy your key or click Change API Key below to update it.</i>"
        )
    else:
        text = (
            f"{EMOJI_KEY} <b>API Key Not Set!</b>\n\n"
            f"You have not configured your HeroSMS API key yet."
        )
    
    await callback.message.edit_text(text, reply_markup=kb.api_key_view_menu(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_change_api_key")
async def cb_tool_change_api_key(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(BotStates.waiting_for_api_key)
    await callback.message.edit_text(
        f"{EMOJI_KEY} <b>Update HeroSMS API Key</b>\n\n"
        "Please send your new API Key directly in this chat:\n\n"
        "<i>(Or click cancel below to abort)</i>",
        reply_markup=kb.api_key_cancel_menu(),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data == "tool_cancel_api_change")
async def cb_tool_cancel_api_change(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Cancelled", show_alert=False)
    await cb_tool_view_api_key(callback, state)

# --- Button: Set Max Price ---
@router.callback_query(F.data == "tool_set_max_price")
async def cb_tool_set_max_price(callback: CallbackQuery, state: FSMContext):
    cur_p = await get_dynamic_max_price()
    await callback.message.edit_text(
        f"{EMOJI_CARD} <b>Current Max Price:</b> <code>${cur_p:.3f}</code>\n\n"
        f"Please send the new max price (e.g. <code>0.18</code> or <code>0.20</code>):",
        reply_markup=kb.back_button("tools_page_1"),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_max_price)

@router.message(BotStates.waiting_for_max_price)
async def process_new_max_price(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): 
        return
    text = message.text.strip()
    if text in MENU_BUTTONS or text.lower() == "t":
        await state.clear()
        return await message.answer("Action cancelled.")
    try:
        new_val = float(text)
        if new_val <= 0:
            raise ValueError
        await db.set_setting("max_price", str(new_val))
        set_cached_setting("max_price", str(new_val))
        await state.clear()
        await message.answer(f"{EMOJI_TICK} Max Purchase Price updated to <b>${new_val:.3f}</b>!", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
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
        await message.answer(f"{EMOJI_TICK} Max Purchase Price updated to <b>${new_val:.3f}</b>!", parse_mode=ParseMode.HTML)
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
        return await callback.message.edit_text("HeroSMS is compiling statistics. Please check back shortly.", reply_markup=kb.back_button("tools_page_2"))

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
    await callback.message.edit_text(final_text, reply_markup=kb.back_button("tools_page_2"), parse_mode=ParseMode.HTML)

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
        return await callback.message.edit_text(f"Error: {clean_error_text(err)}", reply_markup=kb.back_button("tools_page_1"))

    activations = res.get("data", [])
    if not activations:
        return await callback.message.edit_text(f"{EMOJI_WARN} No active numbers found.", reply_markup=kb.back_button("tools_page_1"))

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
        reply_markup=kb.back_button("tools_page_1"),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_operator)

@router.message(BotStates.waiting_for_operator)
async def process_operator_input(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    if text in MENU_BUTTONS or text == "t":
        await state.clear()
        return await message.answer("Action cancelled.")
    await db.set_setting("preferred_operator", text)
    set_cached_setting("preferred_operator", text)
    await state.clear()
    await message.answer(f"{EMOJI_TICK} Preferred operator set to: <b>{text.upper()}</b>", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

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
        await callback.message.edit_text(text, reply_markup=kb.back_button("tools_page_1"), parse_mode=ParseMode.HTML)
    else:
        await callback.answer("Could not load operators.", show_alert=True)

@router.callback_query(F.data == "tool_reset_operator")
async def cb_tool_reset_op(callback: CallbackQuery):
    await db.set_setting("preferred_operator", DEFAULT_OPERATOR)
    set_cached_setting("preferred_operator", DEFAULT_OPERATOR)
    await callback.answer(f"Operator reset to {DEFAULT_OPERATOR.upper()}!", show_alert=True)
    try:
        return await cb_tools_page_1(callback)
    except (TelegramBadRequest, Exception):
        pass

# --- Exclude / Blacklist Management ---
@router.callback_query(F.data == "tool_exclude")
async def cb_tool_exclude(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➕ <b>Exclude Prefix:</b>\n\nSend prefix to blacklist (e.g. <code>57350</code> or <code>57300,57301</code>):",
        reply_markup=kb.back_button("tools_page_1"),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_exclude)

@router.message(BotStates.waiting_for_exclude)
async def process_exclude_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS or text.lower() == "t":
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
    await message.answer(f"{EMOJI_TICK} Added to blacklist!\nCurrent: <code>{saved_str}</code>", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_unexclude")
async def cb_tool_unexclude(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "➖ <b>Unexclude Prefix:</b>\n\nSend prefix to remove from blacklist (e.g. <code>57350</code>):",
        reply_markup=kb.back_button("tools_page_1"),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_unexclude)

@router.message(BotStates.waiting_for_unexclude)
async def process_unexclude_input(message: Message, state: FSMContext):
    text = message.text.strip().replace("+", "")
    if text in MENU_BUTTONS or text.lower() == "t":
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
        await message.answer(f"{EMOJI_TICK} Removed <code>{text}</code>!\nCurrent: <code>{saved_str}</code>", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    else:
        await message.answer(f"Prefix <code>{text}</code> was not found in blacklist.", parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_exclude_list")
async def cb_tool_exclude_list(callback: CallbackQuery):
    cur_str = await get_excluded_prefixes_str()
    prefixes = cur_str.split(",") if cur_str else []
    formatted = "\n".join(f"- <code>+{p}</code>" for p in prefixes) if prefixes else "Empty"
    await callback.message.edit_text(f"📜 <b>Currently Blacklisted Prefixes:</b>\n\n{formatted}", reply_markup=kb.back_button("tools_page_1"), parse_mode=ParseMode.HTML)

@router.callback_query(F.data == "tool_reset_exclude")
async def cb_tool_reset_exclude(callback: CallbackQuery):
    saved_str = ",".join(DEFAULT_EXCLUDE_LIST)
    await db.set_setting("excluded_prefixes", saved_str)
    set_cached_setting("excluded_prefixes", saved_str)
    await callback.answer("Blacklist reset to default (57350, 57351)!", show_alert=True)
    try:
        return await cb_tools_page_1(callback)
    except (TelegramBadRequest, Exception):
        pass

# --- Retry & Cancel Specific Number ---
@router.callback_query(F.data == "tool_retry")
async def cb_tool_retry(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔁 <b>Retry Number:</b>\n\nSend the Number or Activation ID to resend SMS:",
        reply_markup=kb.back_button("tools_page_1"),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_retry)

@router.message(BotStates.waiting_for_retry)
async def process_retry_input(message: Message, state: FSMContext):
    target = message.text.strip().replace("+", "")
    if target in MENU_BUTTONS or target.lower() == "t":
        await state.clear()
        return await message.answer("Action cancelled.")
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client: 
        return
    res = await client.set_status(target, 3)
    await state.clear()
    if isinstance(res, str) and ("ACCESS_RETRY" in res or "STATUS_WAIT" in res):
        await message.answer(f"{EMOJI_TICK} Retry mode activated for <code>+{target}</code>!", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await message.answer(f"Failed to retry: {clean_error_text(err)}", reply_markup=kb.main_reply_menu())

@router.callback_query(F.data == "tool_cancel_number")
async def cb_tool_cancel_number(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        f"{EMOJI_CROSS} <b>Cancel Specific Number:</b>\n\nSend the Number or Activation ID to cancel and refund:",
        reply_markup=kb.back_button("tools_page_2"),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_cancel)

@router.message(BotStates.waiting_for_cancel)
async def process_cancel_input(message: Message, state: FSMContext):
    target = message.text.strip().replace("+", "")
    if target in MENU_BUTTONS or target.lower() == "t":
        await state.clear()
        return await message.answer("Action cancelled.")
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client: 
        return
    res = await client.set_status(target, 8)
    await state.clear()
    if isinstance(res, str) and ("ACCESS_CANCEL" in res or "STATUS_CANCEL" in res):
        await db.delete_activation(target)
        await message.answer(f"{EMOJI_TICK} Successfully cancelled <code>+{target}</code> and refunded.", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await message.answer(f"Failed to cancel: {clean_error_text(err)}", reply_markup=kb.main_reply_menu())

# --- Stealth Access Controls (/add & /d) ---
@router.callback_query(F.data == "tool_add_user")
async def cb_tool_add_user(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Unauthorized", show_alert=True)
    await callback.message.edit_text(f"{EMOJI_USER} Send the <b>User ID</b> to approve access:", reply_markup=kb.back_button("tools_page_2"), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_add_id)

@router.message(BotStates.waiting_for_add_id)
async def process_add_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: 
        return
    text = message.text.strip()
    if text in MENU_BUTTONS or text.lower() == "t":
        await state.clear()
        return await message.answer("Action cancelled.")
    try:
        target = int(text)
    except Exception:
        return await message.answer("Enter a valid numeric User ID:")
    
    await db.set_approval_status(target, True)
    await state.clear()
    await message.answer(f"{EMOJI_TICK} User <code>{target}</code> approved with full access!", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)
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
    await message.answer(f"{EMOJI_TICK} User <code>{target}</code> authorized!", parse_mode=ParseMode.HTML)
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
    await callback.message.edit_text(f"{EMOJI_CROSS} Send the <b>User ID</b> to secretly revoke access:", reply_markup=kb.back_button("tools_page_2"), parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_revoke_id)

@router.message(BotStates.waiting_for_revoke_id)
async def process_revoke_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: 
        return
    text = message.text.strip()
    if text in MENU_BUTTONS or text.lower() == "t":
        await state.clear()
        return await message.answer("Action cancelled.")
    try:
        target = int(text)
    except Exception:
        return await message.answer("Enter a valid numeric User ID:")
    await db.set_approval_status(target, False)
    await state.clear()
    await message.answer(f"{EMOJI_LOCK} Access revoked for user <code>{target}</code> silently.", reply_markup=kb.main_reply_menu(), parse_mode=ParseMode.HTML)

@router.message(Command("d"))
async def cmd_quick_revoke_user(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("Usage: <code>/d 123456789</code>", parse_mode=ParseMode.HTML)
    target = int(args[1])
    await db.set_approval_status(target, False)
    await message.answer(f"{EMOJI_LOCK} Access revoked for user <code>{target}</code> silently.", parse_mode=ParseMode.HTML)

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
            return await message.answer(f"{EMOJI_CROSS} Failed to fetch active numbers.")

        activations = res.get("data", [])
        to_finish = [act for act in activations if bool(act.get("smsCode")) or str(act.get("activationStatus")) in ["4", "6"]]

        if not to_finish:
            return await message.answer(f"{EMOJI_WARN} No completed activations found with SMS to finish.")

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
                    finished_lines.append(f"• <b>+{phone}</b> - Finished {EMOJI_TICK}")
                else:
                    finished_lines.append(f"• <b>+{phone}</b> - {EMOJI_WARN} Failed")
            except Exception as e:
                finished_lines.append(f"• <b>+{phone}</b> - Error: {e}")

        summary = f"<b>{EMOJI_TICK} Finished Activations ({len(finished_lines)}):</b>\n\n" + "\n".join(finished_lines)
        await message.answer(summary, parse_mode=ParseMode.HTML)

# --- Bulk Buy Numbers ---
@router.message(F.text == "Bulk Buy Numbers")
async def text_bulk_buy(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): 
        return
    user, client = await get_valid_user_client(message.from_user.id)
    if not user or not client: 
        return await message.answer("Please send your HeroSMS API Key first.")
    await message.answer(f"{EMOJI_BOX} <b>Bulk Purchase</b>\n\nHow many numbers do you want to buy? (1-50)", parse_mode=ParseMode.HTML)
    await state.set_state(BotStates.waiting_for_bulk_amount)

@router.message(BotStates.waiting_for_bulk_amount)
async def process_bulk_amount(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS or text.lower() == "t":
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

    init_bal = await client.get_balance()
    if init_bal is not None and init_bal < 0.50:
        await message.answer(f"{EMOJI_WARN} <b>Low Balance Alert:</b> Current balance is <code>${init_bal:.4f} USD</code>. Purchase might fail.", parse_mode=ParseMode.HTML)

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
                except Exception:
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
            await status_msg.edit_text(f"{EMOJI_TICK} Purchased {len(purchased)} numbers! (100%)\n🔍 Checking Telegram registration status, please wait...")
        except Exception:
            pass

        try:
            check_results = await asyncio.wait_for(check_telegram_numbers(purchased), timeout=20.0)
        except Exception:
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
            lines.append(f"{EMOJI_TICK} <b>Fresh Numbers ({len(fresh_list)}):</b>")
            for idx, item in enumerate(fresh_list, 1):
                rate_text = f" <b>{item['cost_str']}</b>"
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — {item['badge']}{rate_text}\n")

        if other_list:
            lines.append(f"🔻 <b>Unavailable / Occupied ({len(other_list)}):</b>")
            for idx, item in enumerate(other_list, 1):
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — <b>{item['badge']}</b>")
            lines.append("<i>(Auto-cancelling for refund in 2 mins...)</i>\n")

        if error_list:
            lines.append(f"{EMOJI_WARN} <b>Check Unverified ({len(error_list)}):</b>")
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
        f"{EMOJI_TICK} <b>Fresh Numbers ({len(fresh_phones)})</b>\n<i>Tap any number to copy:</i>",
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

# --- Active Numbers Pagination ---
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
            return await callback.message.edit_text("No active numbers left.", reply_markup=kb.back_button("tools_page_1"))
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
    await callback.message.edit_text(f"Cancelled {ok}/{len(activations)} numbers. Balance refunded.", reply_markup=kb.back_button("tools_page_1"))

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
                await callback.message.edit_text("No active numbers left.", reply_markup=kb.back_button("tools_page_1"))
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
    return await cb_tools_page_2(callback)

@router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("Unauthorized", show_alert=True)
    await callback.message.edit_text("Send the broadcast message:", reply_markup=kb.back_button("tools_page_2"))
    await state.set_state(BotStates.waiting_for_broadcast)

@router.message(BotStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: 
        return
    if message.text.strip() in MENU_BUTTONS or message.text.strip().lower() == "t":
        await state.clear()
        return await message.answer("Broadcast cancelled.")

    users = await db.get_all_users()
    sent = 0
    for uid in users:
        try:
            await message.bot.send_message(uid, f"{EMOJI_PLANE} <b>Announcement:</b>\n\n{message.text}", parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await message.answer(f"{EMOJI_TICK} Sent to {sent} active users.", reply_markup=kb.main_reply_menu())
    await state.clear()

@router.callback_query(F.data == "admin_ban")
async def cb_admin_ban(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: 
        return await callback.answer("Unauthorized", show_alert=True)
    await callback.message.edit_text(f"{EMOJI_BAN} Send the User ID to ban/unban:", reply_markup=kb.back_button("tools_page_2"))
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
    user_dict = dict(user) if hasattr(user, "keys") else (user if isinstance(user, dict) else {})
    new_status = not bool(user_dict.get("is_banned"))
    await db.set_ban_status(target, new_status)
    label = f"BANNED {EMOJI_BAN}" if new_status else f"UNBANNED {EMOJI_TICK}"
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
    if not row and hasattr(db, "get_activation"):
        row = await db.get_activation(aid)

    phone = "Unknown"
    if row:
        if isinstance(row, dict):
            phone = row.get("phone", "Unknown")
        elif hasattr(row, "keys"):
            phone = dict(row).get("phone", "Unknown")
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            phone = row[1]

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
            await callback.message.edit_text("Activation cancelled.", reply_markup=kb.back_button("tools_page_1"))
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
        await callback.message.edit_text("Cancelled. Balance refunded.", reply_markup=kb.back_button("tools_page_1"))
    elif isinstance(res, str) and "EARLY_CANCEL_DENIED" in res:
        await callback.answer("Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.answer(f"Error: {clean_error_text(err)}", show_alert=True)

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()
