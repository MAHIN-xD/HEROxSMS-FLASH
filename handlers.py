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
MAX_PRICE   = 0.1428

DEFAULT_EXCLUDE_LIST = ["57350"]
DEFAULT_OPERATOR = "any"

processed_otps = {}
fresh_batches_cache = {}

MENU_BUTTONS = ["Bulk Buy Numbers", "Active Numbers"]

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
    elif prefix in ["350", "351"]:
        return "WOM"
    elif prefix in ["319"]:
        return "Virgin"
    return "Unknown"

def format_otp_text(phone: str, code: str) -> str:
    clean_phone = str(phone).lstrip("+").strip()
    safe_phone = html.escape(clean_phone)
    return f"🇨🇴 <b>Telegram</b> <code>{safe_phone}</code>"

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
        return {"badge": "✅ Fresh", "priority": 1, "is_fresh": True, "is_error": False}

    locked_signals = ["flood", "locked", "lock", "wait", "restricted", "2fa", "password", "has_password"]
    if any(w in st for w in locked_signals):
        return {"badge": "🔒 Locked", "priority": 2, "is_fresh": False, "is_error": False}

    occupied_signals = ["occupied", "registered", "taken", "used", "true", "1"]
    if any(w in st for w in occupied_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):
        return {"badge": "❌", "priority": 3, "is_fresh": False, "is_error": False}

    banned_signals = ["banned", "ban", "blocked"]
    if any(w in st for w in banned_signals) and not any(neg in st for neg in ["not", "un", "no", "non", "without", "false"]):
        return {"badge": "🚫", "priority": 4, "is_fresh": False, "is_error": False}

    clean = re.sub(r'phone_number_', '', st, flags=re.IGNORECASE).replace('_', ' ').strip().title()
    return {"badge": f"⚠️ {clean}", "priority": 5, "is_fresh": False, "is_error": False}

async def get_excluded_prefixes_str() -> str:
    saved = await db.get_setting("excluded_prefixes")
    if not saved:
        return ",".join(DEFAULT_EXCLUDE_LIST)
    return str(saved)

async def get_preferred_operator_str() -> str:
    saved = await db.get_setting("preferred_operator")
    if not saved:
        return DEFAULT_OPERATOR
    return str(saved)

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
    if user_id == ADMIN_ID: return True
    user = await db.get_user(user_id)
    try:
        if user and user["is_banned"]: return False
    except Exception:
        pass
    maintenance = await db.get_setting("maintenance")
    if maintenance == "1": return False
    return True

# --- Webhook Handler ---
async def handle_herosms_webhook(request: web.Request):
    try:
        if request.can_read_body:
            try:
                data = await request.json()
            except Exception:
                data = dict(await request.post())
        else:
            data = dict(request.query)
    except Exception as e:
        logging.error(f"Error reading webhook request: {e}")
        return web.Response(text="Bad Request", status=400)

    aid = str(data.get("activationId") or data.get("id") or "").strip()
    code = str(data.get("code") or "").strip()
    text_body = data.get("text")

    if not code and text_body:
        match = re.search(r'\b\d{4,6}\b', str(text_body))
        if match:
            code = match.group(0)

    if not aid or not code:
        return web.Response(text="OK", status=200)

    cache_key = f"{aid}:{code}"
    if cache_key in processed_otps:
        return web.Response(text="OK", status=200)

    processed_otps[cache_key] = time.time()

    now = time.time()
    for k in list(processed_otps.keys()):
        if now - processed_otps[k] > 1200:
            del processed_otps[k]

    try:
        row = await db.get_activation_user(aid)
        if row:
            user_id = row[0]
            phone = row[1]
            msg_text = format_otp_text(phone, code)
            bot = get_bot_instance()
            if bot:
                try:
                    await bot.send_message(
                        user_id,
                        msg_text,
                        reply_markup=kb.otp_copy_menu(code),
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    logging.error(f"Failed to send webhook OTP to {user_id}: {e}")
    except Exception as e:
        logging.error(f"Error processing webhook for {aid}: {e}")

    return web.Response(text="OK", status=200)

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id)
    user = await db.get_user(message.from_user.id)

    try:
        if user and user["is_banned"]:
            await message.answer("You are banned from using this bot.")
            return
    except Exception:
        pass

    maintenance = await db.get_setting("maintenance")
    if maintenance == "1" and message.from_user.id != ADMIN_ID:
        await message.answer("Bot is under maintenance. Contact Admin.")
        return

    has_api_key = False
    try:
        if user and user["api_key"]:
            has_api_key = True
    except Exception:
        has_api_key = False

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
                f"✅ API Key saved successfully!\n💰 Balance: {balance:.4f} USD",
                reply_markup=kb.main_reply_menu()
            )
        else:
            res = await client._get("getBalance")
            err_msg = ""
            if isinstance(res, dict):
                err_msg = res.get("title") or res.get("details") or str(res)
            elif isinstance(res, str):
                err_msg = res
            
            err_msg = html.escape(str(err_msg))
            if err_msg and err_msg != "None":
                await message.answer(f"❌ Invalid API Key ({err_msg}). Please check and try again.")
            else:
                await message.answer("❌ Invalid API Key. Please check and try again.")

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_allowed(callback.from_user.id): return
    try: await callback.message.delete()
    except: pass
    await callback.message.answer("Main Menu:", reply_markup=kb.main_reply_menu())

# --- /balance কমান্ড ---
@router.message(Command("balance"))
@router.message(F.text == "Balance")
async def cmd_balance(message: Message):
    if not await is_allowed(message.from_user.id): return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please set your HeroSMS API Key first with /api")
            return
        balance = await client.get_balance()
        if balance is not None:
            await message.answer(f"💰 Your Current Balance: <b>{balance:.4f} USD</b>", parse_mode=ParseMode.HTML)
        else:
            await message.answer("❌ Error fetching balance. Check your API key.")

# --- /api কমান্ড ---
@router.message(Command("api"))
async def cmd_api(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): return
    await message.answer(
        "🔑 <b>Update API Key</b>\n\nPlease send your new HeroSMS API Key:",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.HTML
    )
    await state.set_state(BotStates.waiting_for_api_key)

# --- /check_operators কমান্ড ---
@router.message(Command("check_operators", "operators"))
async def cmd_check_live_operators(message: Message):
    if not await is_allowed(message.from_user.id): return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please set your API key first with /api")
            return

        res = await client.get_operators(country=COLOMBIA_ID)
        if isinstance(res, dict) and res.get("status") == "success":
            ops_dict = res.get("countryOperators", {})
            colombia_ops = ops_dict.get(str(COLOMBIA_ID), [])
            if colombia_ops:
                op_list = ", ".join(colombia_ops)
                await message.answer(
                    f"📡 <b>HeroSMS Live Operators (Colombia):</b>\n\n"
                    f"<code>{op_list}</code>\n\n"
                    f"💡 <i>অপারেটর সেট করতে লিখুন:</i>\n<code>/operator {colombia_ops[0]}</code>",
                    parse_mode=ParseMode.HTML
                )
            else:
                await message.answer("বর্তমানে কলম্বিয়ার জন্য কোনো নির্দিষ্ট অপারেটর তালিকাভুক্ত নেই। শুধুমাত্র <code>/operator any</code> কাজ করবে।", parse_mode=ParseMode.HTML)
        else:
            await message.answer("❌ অপারেটর তালিকা আনা সম্ভব হয়নি।")

# --- /ok কমান্ড ---
@router.message(Command("ok"))
async def cmd_ok_finish(message: Message):
    if not await is_allowed(message.from_user.id): return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please set your API key first.")
            return

        args = message.text.split()
        target = args[1].replace("+", "").strip() if len(args) > 1 else None

        res = await client.get_active_activations()
        if not (isinstance(res, dict) and res.get("status") == "success"):
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message.answer(f"❌ Failed to fetch active numbers: {html.escape(str(err))}")
            return

        activations = res.get("data", [])
        if not activations:
            await message.answer("ℹ️ No active numbers found.")
            return

        to_finish = []
        if target:
            for act in activations:
                if str(act.get("phoneNumber")) == target or str(act.get("activationId")) == target:
                    to_finish.append(act)
                    break
            if not to_finish:
                await message.answer(f"❌ Number or Activation ID {target} not found in active list.")
                return
        else:
            for act in activations:
                has_otp = bool(act.get("smsCode"))
                status = str(act.get("activationStatus", ""))
                if has_otp or status in ["4", "6"]:
                    to_finish.append(act)

        if not to_finish:
            await message.answer(
                "ℹ️ No completed activations found to finish.\n"
                "💡 <i>Tip: যেসব নম্বরে ওটিপি চলে এসেছে সেগুলো অটো সিলেক্ট হবে। নির্দিষ্ট নম্বর ম্যানুয়ালি ফিনিশ করতে লিখুন:</i> <code>/ok +573...</code>",
                parse_mode=ParseMode.HTML
            )
            return

        finished_lines = []
        for act in to_finish:
            aid = str(act.get("activationId"))
            phone = str(act.get("phoneNumber", "Unknown"))
            clean_phone = phone.lstrip("+").strip()
            try:
                r = await client.set_status(aid, 6)
                if isinstance(r, str) and (r.startswith("ACCESS_ACTIVATION") or r.startswith("ACCESS_OK") or "ACTIVATION" in r):
                    await db.delete_activation(aid)
                    for k in list(processed_otps.keys()):
                        if k.startswith(f"{aid}:"):
                            del processed_otps[k]
                    op = get_colombia_operator(clean_phone)
                    finished_lines.append(f"• <b>+{clean_phone}</b> ({op}) - Finished ✅")
                elif isinstance(r, dict) and r.get("status") == "success":
                    await db.delete_activation(aid)
                    for k in list(processed_otps.keys()):
                        if k.startswith(f"{aid}:"):
                            del processed_otps[k]
                    op = get_colombia_operator(clean_phone)
                    finished_lines.append(f"• <b>+{clean_phone}</b> ({op}) - Finished ✅")
                else:
                    err = r.get("title", str(r)) if isinstance(r, dict) else str(r)
                    finished_lines.append(f"• <b>+{clean_phone}</b> - ⚠️ {html.escape(str(err))}")
            except Exception as e:
                finished_lines.append(f"• <b>+{clean_phone}</b> - Error: {e}")

        summary_text = f"<b>✅ Finished Activations ({len(finished_lines)}):</b>\n\n" + "\n".join(finished_lines)
        await message.answer(summary_text, parse_mode=ParseMode.HTML)

# --- /retry কমান্ড ---
@router.message(Command("retry"))
async def cmd_retry_number(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /retry +573... or /retry 12345678")
        return

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        target = args[1].replace("+", "").strip()
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please set your API key first.")
            return

        aid_to_retry = target
        phone_num = target

        res = await client.get_active_activations()
        if isinstance(res, dict) and res.get("status") == "success":
            for act in res.get("data", []):
                if str(act.get("phoneNumber")) == target or str(act.get("activationId")) == target:
                    aid_to_retry = str(act.get("activationId"))
                    phone_num = str(act.get("phoneNumber"))
                    break

        row = await db.get_activation_user(aid_to_retry)
        if row:
            phone_num = row[1]
        else:
            await db.save_activation(aid_to_retry, message.from_user.id, phone_num)

        for k in list(processed_otps.keys()):
            if k.startswith(f"{aid_to_retry}:"):
                del processed_otps[k]

        retry_res = await client.set_status(aid_to_retry, 3)

        if isinstance(retry_res, str) and (
            retry_res.startswith("ACCESS_RETRY_GET") or
            retry_res.startswith("STATUS_WAIT_RETRY") or
            retry_res.startswith("STATUS_WAIT_CODE") or
            retry_res.startswith("ACCESS_ACTIVATION")
        ):
            clean_p = str(phone_num).lstrip("+")
            await message.answer(
                f"Retry mode activated.\n\n"
                f"<b>Number:</b> 🇨🇴 <code>+{clean_p}</code>\n"
                f"ID: <code>{aid_to_retry}</code>\n\n"
                f"Please click 'Resend SMS' in Telegram. New code will be delivered automatically.",
                parse_mode=ParseMode.HTML
            )
        else:
            err = retry_res.get("title", str(retry_res)) if isinstance(retry_res, dict) else str(retry_res)
            await message.answer(f"Failed to retry: {html.escape(str(err))}\n(Number might be cancelled or expired)")

# --- /getallsms কমান্ড ---
@router.message(Command("getallsms", "allsms"))
async def cmd_get_all_sms(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /getallsms <Activation_ID or Phone>\nExample: /getallsms 12345678")
        return

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please send your HeroSMS API Key first.")
            return

        target = args[1].replace("+", "").strip()
        aid = target

        res_active = await client.get_active_activations()
        if isinstance(res_active, dict) and res_active.get("status") == "success":
            for act in res_active.get("data", []):
                if str(act.get("phoneNumber")) == target or str(act.get("activationId")) == target:
                    aid = str(act.get("activationId"))
                    break

        res = await client.get_all_sms(aid)

        if not res or not isinstance(res, dict):
            err = str(res) if res else "No response"
            await message.answer(f"Could not load SMS: {html.escape(err)}")
            return

        sms_list = res.get("data", [])
        meta = res.get("meta", {})
        total = meta.get("total", len(sms_list))

        if not sms_list:
            await message.answer(f"No SMS found for ID: {aid}")
            return

        lines = [f"All SMS List (Total: {total}):\n"]
        for idx, item in enumerate(sms_list, 1):
            sender = html.escape(str(item.get("phoneFrom") or "System"))
            code = html.escape(str(item.get("code") or "N/A"))
            text_body = html.escape(str(item.get("text") or ""))
            time_str = html.escape(str(item.get("date") or ""))
            v_type = html.escape(str(item.get("type") or "sms"))

            lines.append(
                f"#{idx} [{v_type.upper()}] From: {sender}\n"
                f"Code: <code>{code}</code>\n"
                f"Message: {text_body}\n"
                f"Date: {time_str}\n"
            )

        final_text = "\n".join(lines)
        if len(final_text) > 4000:
            final_text = final_text[:3990] + "..."
        await message.answer(final_text, parse_mode=ParseMode.HTML)

# --- /stats কমান্ড ---
@router.message(Command("stats", "statistics"))
async def cmd_stats(message: Message):
    if not await is_allowed(message.from_user.id): return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please send your HeroSMS API Key first.")
            return

        args = message.text.split()
        date_arg = args[1].strip() if len(args) >= 2 else None

        res = await client.get_stats(date_arg)

        if not res or not isinstance(res, dict) or "data" not in res:
            err = res.get("details") or res.get("title") or str(res) if isinstance(res, dict) else str(res)
            await message.answer(f"Stats not found: {html.escape(str(err))}")
            return

        data = res.get("data", {})
        if not data or not isinstance(data, dict):
            await message.answer("No statistics found for the selected date.")
            return

        display_date = date_arg or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [f"HeroSMS Live Stats ({display_date}):\n"]

        total_purchased = 0
        total_success = 0
        has_warning = False

        for country_key, services in data.items():
            if isinstance(services, dict):
                for service_name, s_data in services.items():
                    if isinstance(s_data, dict):
                        c_count = s_data.get("count", 0)
                        c_success = s_data.get("success", 0)
                        c_percent = float(s_data.get("percent", 0.0))

                        total_purchased += c_count
                        total_success += c_success

                        if c_percent < 6.0 and c_count >= 10:
                            has_warning = True

                        lines.append(
                            f"Country ID: {country_key} | Service: {service_name.upper()}\n"
                            f"- Total: {c_count}\n"
                            f"- Success: {c_success}\n"
                            f"- Rate: {c_percent:.1f}%\n"
                        )

        overall_rate = (total_success / total_purchased * 100) if total_purchased > 0 else 0.0
        lines.append(
            f"-------------------\n"
            f"Summary:\n"
            f"- Total Numbers: {total_purchased}\n"
            f"- Total Success: {total_success}\n"
            f"- Average Success Rate: {overall_rate:.1f}%\n"
        )

        if has_warning or (total_purchased >= 10 and overall_rate < 6.0):
            lines.append("Warning: Success rate is below 6%! Use /exclude or change country to prevent account ban.")
        else:
            lines.append("Success rate is within safe range.")

        lines.append("\n(Stats reset daily at 21:00 UTC)")
        await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# --- /history কমান্ড ---
@router.message(Command("history"))
async def cmd_history(message: Message):
    if not await is_allowed(message.from_user.id): return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please send your HeroSMS API Key first.")
            return

        args = message.text.split()
        limit = 10
        if len(args) >= 2 and args[1].isdigit():
            limit = min(int(args[1]), 30)

        res = await client.get_history(size=limit)

        if not res or not isinstance(res, list):
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message.answer(f"History not found: {html.escape(str(err))}")
            return

        if not res:
            await message.answer("No activation history found.")
            return

        lines = [f"Latest {len(res)} Activations:\n"]
        for idx, item in enumerate(res, 1):
            phone = html.escape(str(item.get("phone", "Unknown"))).lstrip("+")
            cost = item.get("cost", 0)
            status_code = str(item.get("status", ""))
            date_str = html.escape(str(item.get("date", "")))
            sms_code = html.escape(str(item.get("sms", "None")))

            status_label = "Success" if status_code in ["4", "6"] else ("Cancelled" if status_code == "8" else f"Status {status_code}")

            lines.append(
                f"#{idx} <b>+{phone}</b>\n"
                f"- OTP: <code>{sms_code}</code>\n"
                f"- Cost: {cost} USD | Status: {status_label}\n"
                f"- Date: {date_str}\n"
            )

        final_text = "\n".join(lines)
        if len(final_text) > 4000:
            final_text = final_text[:3990] + "..."
        await message.answer(final_text, parse_mode=ParseMode.HTML)

# --- /act_history কমান্ড ---
@router.message(Command("activations_history", "act_history"))
async def cmd_activations_history(message: Message):
    if not await is_allowed(message.from_user.id): return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user:
            await message.answer("Please send your HeroSMS API Key first with /api")
            return

        now = datetime.now(timezone.utc)
        to_date = now.strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=7)).strftime("%Y-%m-%d")

        res = await client.get_activations_history(from_date=from_date, to_date=to_date, size=15)

        if not res or not isinstance(res, dict) or "data" not in res:
            res_legacy = await client.get_history(size=10)
            if isinstance(res_legacy, list) and res_legacy:
                return await cmd_history(message)
            err = res.get("details") or res.get("title") or str(res) if isinstance(res, dict) else str(res)
            await message.answer(f"Could not load report: {html.escape(str(err))}")
            return

        totals = res.get("totals", [])
        total_sum = 0.0
        total_success_count = 0
        if totals and isinstance(totals, list) and isinstance(totals[0], dict):
            total_sum = float(totals[0].get("sum", 0.0))
            total_success_count = int(totals[0].get("successCount", 0))

        items = res.get("data", [])
        lines = [
            f"📊 <b>Activation Report ({from_date} to {to_date}):</b>\n",
            f"💰 Total Cost: <b>{total_sum:.4f} USD</b>",
            f"✅ Total Success: <b>{total_success_count}</b>\n",
            "-------------------\n"
        ]

        for idx, item in enumerate(items[:10], 1):
            phone = html.escape(str(item.get("phone", "Unknown"))).lstrip("+")
            cost = item.get("cost", 0)
            date_str = html.escape(str(item.get("createDate", "")))
            codes = html.escape(str(item.get("moreCodes", "None")))

            lines.append(
                f"#{idx} <b>+{phone}</b> | {cost} USD\n"
                f"- Code: <code>{codes}</code>\n"
                f"- Date: {date_str}\n"
            )

        final_text = "\n".join(lines)
        if len(final_text) > 4000:
            final_text = final_text[:3990] + "..."
        await message.answer(final_text, parse_mode=ParseMode.HTML)

# --- /cancel কমান্ড ---
@router.message(Command("cancel"))
async def cmd_cancel_number(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Please specify the number or activation ID.\nUsage: /cancel +573... or /cancel 12345")
        return
    
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        target = args[1].replace("+", "").strip()
        user, client = await get_valid_user_client(message.from_user.id)
        if not user: return
        
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
            for k in list(processed_otps.keys()):
                if k.startswith(f"{aid_to_cancel}:"):
                    del processed_otps[k]
            await message.answer(f"Successfully cancelled <b>+{target.lstrip('+')}</b>. Balance refunded.", parse_mode=ParseMode.HTML)
        elif isinstance(cancel_res, str) and "EARLY_CANCEL_DENIED" in cancel_res:
            await message.answer("Cannot cancel within first 2 minutes.")
        else:
            err = cancel_res.get("title", str(cancel_res)) if isinstance(cancel_res, dict) else str(cancel_res)
            await message.answer(f"Failed to cancel: {html.escape(str(err))}")

@router.callback_query(F.data.startswith("single_cancel_"))
async def cb_cancel_single(callback: CallbackQuery):
    aid = callback.data[len("single_cancel_"):]
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user: return

    res = await client.set_status(aid, 8)
    if isinstance(res, str) and res.startswith("ACCESS_CANCEL"):
        await db.delete_activation(aid)
        for k in list(processed_otps.keys()):
            if k.startswith(f"{aid}:"):
                del processed_otps[k]
        await callback.message.edit_text("Cancelled. Balance refunded.", reply_markup=kb.back_button())
    elif isinstance(res, str) and "EARLY_CANCEL_DENIED" in res:
        await callback.answer("Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.answer(f"Error: {err}", show_alert=True)

@router.callback_query(F.data.startswith("check_"))
async def cb_check_sms(callback: CallbackQuery):
    aid = callback.data[len("check_"):]
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user: return

    row = await db.get_activation_user(aid)
    phone = row[1] if row else "Unknown"

    res = await client.get_status(aid)
    if isinstance(res, str):
        if res.startswith("STATUS_OK:"):
            code = res.split(":", 1)[1]
            text = format_otp_text(phone, code)
            processed_otps[f"{aid}:{code}"] = time.time()
            await callback.message.edit_text(text, reply_markup=kb.otp_copy_menu(code), parse_mode=ParseMode.HTML)
        elif res.startswith("STATUS_WAIT_CODE"):
            await callback.answer("Still waiting for SMS...", show_alert=True)
        elif res.startswith("STATUS_CANCEL"):
            await db.delete_activation(aid)
            for k in list(processed_otps.keys()):
                if k.startswith(f"{aid}:"):
                    del processed_otps[k]
            await callback.message.edit_text("Activation cancelled.", reply_markup=kb.back_button())
        else:
            await callback.answer(f"Status: {res}", show_alert=True)
    else:
        await callback.answer("Error checking status.", show_alert=True)

# --- বাল্ক বাই ---
@router.message(F.text == "Bulk Buy Numbers")
async def text_bulk_buy(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): return
    user, client = await get_valid_user_client(message.from_user.id)
    if not user: 
        await message.answer("Please send your HeroSMS API Key first with /api")
        return
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
        if not (1 <= amount <= 50): raise ValueError
    except:
        await message.answer("Enter a valid number between 1 and 50.")
        return

    await state.clear()
    user, client = await get_valid_user_client(message.from_user.id)
    if not user: return

    status_msg = await message.answer(f"Buying {amount} numbers...")
    
    purchased = []
    last_edit_time = time.time()
    current_exc = await get_excluded_prefixes_str()
    current_op = await get_preferred_operator_str()

    for i in range(amount):
        res = await client.get_number(
            service=TG_SERVICE, 
            country=COLOMBIA_ID, 
            max_price=MAX_PRICE,
            phone_exception=current_exc,
            operator=current_op
        )
        if isinstance(res, dict) and "activationId" in res:
            aid = str(res["activationId"])
            phone = res.get("phoneNumber", "Unknown")
            clean_phone = str(phone).lstrip("+").strip()
            purchased.append(clean_phone)
            await db.save_activation(aid, message.from_user.id, clean_phone)
            
            now = time.time()
            if (now - last_edit_time >= 3.0) or (i == amount - 1):
                try:
                    display_lines = purchased[-10:]
                    lines = "\n".join(
                        f"{n}. <b>+{p}</b> ({get_colombia_operator(p)})" 
                        for n, p in enumerate(display_lines, len(purchased)-len(display_lines)+1)
                    )
                    upd_text = f"Buying {amount} numbers... ({len(purchased)}/{amount})\n\n{lines}"
                    if len(purchased) > 10:
                        upd_text += f"\n...and {len(purchased)-10} earlier"
                    await status_msg.edit_text(upd_text, parse_mode=ParseMode.HTML)
                    last_edit_time = now
                except Exception:
                    pass
            
            await asyncio.sleep(0.3)
        else:
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message.answer(f"Stopped at #{i+1}: {html.escape(str(err))}")
            break

    if purchased:
        try:
            await status_msg.edit_text(f"✅ Purchased {len(purchased)} numbers!\n🔍 Checking Telegram registration status, please wait...")
        except Exception:
            pass

        check_results = await check_telegram_numbers(purchased)

        parsed_items = []
        for p in purchased:
            op = get_colombia_operator(p)
            formatted_k = f"+{p}"
            raw_st = check_results.get(formatted_k) or check_results.get(p)
            info = format_tg_status(raw_st)
            parsed_items.append({
                "phone": p,
                "operator": op,
                "badge": info["badge"],
                "priority": info["priority"],
                "is_fresh": info["is_fresh"],
                "is_error": info.get("is_error", False)
            })

        fresh_list = [x for x in parsed_items if x["is_fresh"]]
        other_list = [x for x in parsed_items if not x["is_fresh"] and not x.get("is_error")]
        error_list = [x for x in parsed_items if x.get("is_error")]
        other_list.sort(key=lambda x: x["priority"])

        lines = [
            f"🎉 <b>Bulk Order Completed!</b>",
            f"Total: {len(purchased)} numbers (tap any number to copy)\n"
        ]

        # ফ্রেশ নম্বরের লিস্ট (৩নং ছবির মতো একটার পর এক লাইন ফাঁকা থাকবে)
        if fresh_list:
            lines.append(f"🟢 <b>Fresh Numbers ({len(fresh_list)}):</b>")
            for idx, item in enumerate(fresh_list, 1):
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — <b>{item['badge']}</b>\n")

        if other_list:
            lines.append(f"🔻 <b>Unavailable / Occupied ({len(other_list)}):</b>")
            for idx, item in enumerate(other_list, 1):
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — <b>{item['badge']}</b>")
            lines.append("")

        if error_list:
            lines.append(f"⚠️ <b>Check Unverified ({len(error_list)}):</b>")
            for idx, item in enumerate(error_list, 1):
                lines.append(f"{idx}. <code>+{item['phone']}</code> ({item['operator']}) — <b>{item['badge']}</b>")
            lines.append("")

        lines.append("Waiting for OTPs...")

        final = "\n".join(lines)
        batch_id = str(uuid.uuid4())[:8]
        fresh_phones = [x["phone"] for x in fresh_list]
        fresh_batches_cache[batch_id] = {
            "summary": final,
            "fresh_phones": fresh_phones
        }

        reply_markup = kb.bulk_result_menu(batch_id, len(fresh_list))

        if len(final) > 4000:
            for part in [final[j:j+4000] for j in range(0, len(final), 4000)]:
                await message.answer(part, parse_mode=ParseMode.HTML)
            try: await status_msg.delete()
            except: pass
        else:
            await status_msg.edit_text(final, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else:
        await status_msg.edit_text("Could not purchase any numbers.")

# --- ফ্রেশ নাম্বার বাটন হ্যান্ডলার ---
@router.callback_query(F.data.startswith("show_fresh_"))
async def cb_show_fresh_numbers(callback: CallbackQuery):
    batch_id = callback.data[len("show_fresh_"):]
    batch_data = fresh_batches_cache.get(batch_id)

    if not batch_data or not batch_data.get("fresh_phones"):
        await callback.answer("No fresh numbers found or session expired.", show_alert=True)
        return

    fresh_phones = batch_data["fresh_phones"]
    await callback.message.edit_text(
        f"🟢 <b>Fresh Numbers ({len(fresh_phones)})</b>\n<i>যেকোনো নম্বরে ট্যাপ করলেই সরাসরি কপি হয়ে যাবে:</i>",
        reply_markup=kb.fresh_numbers_menu(fresh_phones, batch_id),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data.startswith("back_bulk_"))
async def cb_back_bulk(callback: CallbackQuery):
    batch_id = callback.data[len("back_bulk_"):]
    batch_data = fresh_batches_cache.get(batch_id)

    if not batch_data:
        await callback.answer("Session expired.", show_alert=True)
        return

    summary = batch_data["summary"]
    fresh_count = len(batch_data.get("fresh_phones", []))
    await callback.message.edit_text(
        summary,
        reply_markup=kb.bulk_result_menu(batch_id, fresh_count),
        parse_mode=ParseMode.HTML
    )

# --- অ্যাক্টিভ নাম্বার লিস্ট ---
@router.message(F.text == "Active Numbers")
async def text_active_numbers(message: Message):
    if not await is_allowed(message.from_user.id): return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user, client = await get_valid_user_client(message.from_user.id)
        if not user: 
            await message.answer("Please send your HeroSMS API Key first with /api")
            return

        res = await client.get_active_activations()

        if not (isinstance(res, dict) and res.get("status") == "success"):
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message.answer(f"Error: {html.escape(str(err))}")
            return

        activations = res.get("data", [])
        if not activations:
            await message.answer("No active numbers.")
            return

        total = len(activations)
        await message.answer(
            f"Active Numbers ({total}) - Page 1/{(total+9)//10}:",
            reply_markup=kb.active_numbers_menu(activations, page=0)
        )

@router.callback_query(F.data.startswith("act_page_"))
async def cb_active_page(callback: CallbackQuery):
    page = int(callback.data.split("_")[2])
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user: return

    res = await client.get_active_activations()
    if isinstance(res, dict) and res.get("status") == "success":
        activations = res.get("data", [])
        total = len(activations)
        if not activations:
            await callback.message.edit_text("No active numbers left.")
            return
        await callback.message.edit_text(
            f"Active Numbers ({total}) - Page {page+1}/{(total+9)//10}:",
            reply_markup=kb.active_numbers_menu(activations, page=page)
        )

@router.callback_query(F.data == "cancel_all_active")
async def cb_cancel_all_active(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id): return
    user, client = await get_valid_user_client(callback.from_user.id)
    if not user: return

    res = await client.get_active_activations()
    if not (isinstance(res, dict) and res.get("status") == "success"):
        await callback.answer("Failed to fetch active numbers.", show_alert=True)
        return
    activations = res.get("data", [])
    if not activations:
        await callback.answer("No active numbers to cancel.", show_alert=True)
        return

    await callback.message.edit_text(f"Cancelling {len(activations)} numbers... please wait.")

    sem = asyncio.Semaphore(4)

    async def cancel_one(act):
        async with sem:
            aid = str(act.get("activationId", ""))
            if not aid: return False
            try:
                r = await client.set_status(aid, 8)
                if isinstance(r, str) and (r.startswith("ACCESS_CANCEL") or r.startswith("STATUS_CANCEL")):
                    await db.delete_activation(aid)
                    for k in list(processed_otps.keys()):
                        if k.startswith(f"{aid}:"):
                            del processed_otps[k]
                    return True
                if isinstance(r, dict) and r.get("status") == "success":
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
    await callback.message.edit_text(f"Cancelled {ok}/{len(activations)} numbers. Balance refunded.")

@router.callback_query(F.data.startswith("active_cancel_"))
async def cb_active_cancel(callback: CallbackQuery):
    parts = callback.data.split("_")
    aid = parts[2]
    page = int(parts[3]) if len(parts) > 3 else 0

    user, client = await get_valid_user_client(callback.from_user.id)
    if not user: return

    r = await client.set_status(aid, 8)
    if isinstance(r, str) and (r.startswith("ACCESS_CANCEL") or r.startswith("STATUS_CANCEL")):
        await db.delete_activation(aid)
        for k in list(processed_otps.keys()):
            if k.startswith(f"{aid}:"):
                del processed_otps[k]
        await callback.answer("Cancelled!", show_alert=True)
        res = await client.get_active_activations()
        if isinstance(res, dict) and res.get("status") == "success":
            acts = res.get("data", [])
            if not acts:
                await callback.message.edit_text("No active numbers left.")
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

# --- প্রিফিক্স ও অপারেটর কমান্ডস ---
@router.message(Command("exclude", "blacklist"))
async def cmd_add_exclude(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /exclude 57300 or /exclude 57300,57301")
        return
    
    new_items = [p.replace("+", "").strip() for p in args[1].split(",") if p.strip()]
    current_str = await db.get_setting("excluded_prefixes")
    current_list = current_str.split(",") if current_str else list(DEFAULT_EXCLUDE_LIST)
    
    added = []
    for item in new_items:
        if item not in current_list:
            current_list.append(item)
            added.append(item)
            
    if len(current_list) > 20:
        await message.answer("HeroSMS allows a maximum of 20 prefixes in the blacklist.")
        return

    await db.set_setting("excluded_prefixes", ",".join(current_list))
    await message.answer(f"Added: {', '.join(added)}\nCurrent Blacklist: {', '.join(current_list)}")

@router.message(Command("unexclude", "whitelist"))
async def cmd_remove_exclude(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        await message.answer("Usage: /unexclude 57350")
        return
    
    remove_item = args[1].replace("+", "").strip()
    current_str = await db.get_setting("excluded_prefixes")
    current_list = current_str.split(",") if current_str else list(DEFAULT_EXCLUDE_LIST)
    
    if remove_item in current_list:
        current_list.remove(remove_item)
        await db.set_setting("excluded_prefixes", ",".join(current_list))
        await message.answer(f"{remove_item} removed from blacklist.\nCurrent Blacklist: {', '.join(current_list) if current_list else 'Empty'}")
    else:
        await message.answer(f"{remove_item} not found in blacklist.")

@router.message(Command("exclude_list"))
async def cmd_view_exclude(message: Message):
    if not await is_allowed(message.from_user.id): return
    current_str = await get_excluded_prefixes_str()
    prefixes = current_str.split(",") if current_str else []
    
    if not prefixes:
        await message.answer("Currently no prefixes are blacklisted.")
    else:
        formatted = "\n".join(f"- <code>+{p}</code>" for p in prefixes)
        await message.answer(f"Currently blacklisted prefixes:\n\n{formatted}", parse_mode=ParseMode.HTML)

# --- /reset_exclude কমান্ড ---
@router.message(Command("reset_exclude"))
async def cmd_reset_exclude(message: Message):
    if not await is_allowed(message.from_user.id): return
    await db.set_setting("excluded_prefixes", ",".join(DEFAULT_EXCLUDE_LIST))
    await message.answer(f"✅ Blacklist reset to default: <code>+{DEFAULT_EXCLUDE_LIST[0]}</code>", parse_mode=ParseMode.HTML)

@router.message(Command("operator"))
async def cmd_set_operator(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        current_op = await get_preferred_operator_str()
        await message.answer(
            f"Current Operator: {current_op}\n\n"
            f"Usage:\n"
            f"- Set operator: /operator claro\n"
            f"- Check live operators: /check_operators\n"
            f"- Reset: /operator any"
        )
        return

    new_op = args[1].strip().lower()
    await db.set_setting("preferred_operator", new_op)
    await message.answer(f"Preferred operator set to: {new_op}")

# --- /operator_list কমান্ড ---
@router.message(Command("operator_list"))
async def cmd_view_operator(message: Message):
    if not await is_allowed(message.from_user.id): return
    current_op = await get_preferred_operator_str()
    await message.answer(f"📡 Current operator setting: <b>{current_op}</b>", parse_mode=ParseMode.HTML)

# --- /reset_operator কমান্ড ---
@router.message(Command("reset_operator"))
async def cmd_reset_operator(message: Message):
    if not await is_allowed(message.from_user.id): return
    await db.set_setting("preferred_operator", DEFAULT_OPERATOR)
    await message.answer(f"✅ Operator setting reset to default: <b>{DEFAULT_OPERATOR}</b>", parse_mode=ParseMode.HTML)

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
    if message.text.strip() in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Broadcast cancelled.")

    users = await db.get_all_users()
    sent = 0
    for uid in users:
        try:
            await message.bot.send_message(uid, f"Broadcast:\n\n{message.text}")
            sent += 1
        except Exception:
            pass
    await message.answer(f"Sent to {sent} users.")
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
    try:
        new_status = not bool(user["is_banned"])
    except Exception:
        new_status = True
    await db.set_ban_status(target, new_status)
    label = "Banned" if new_status else "Unbanned"
    await message.answer(f"User {target} {label}.")
    await state.clear()

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()
