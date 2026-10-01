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
MAX_PRICE   = 0.150

DEFAULT_EXCLUDE_LIST = ["57350", "57351"]
DEFAULT_OPERATOR = "any"

processed_otps = {}
fresh_batches_cache = {}
MENU_BUTTONS = ["Bulk Buy Numbers", "Active Numbers"]

def clean_error_text(raw_err: any) -> str:
    if not raw_err: return "Unknown Error"
    text = str(raw_err).replace("phone_number_", "").replace("error_", "").replace("_", " ")
    return html.escape(text.strip().title())

def cleanup_cache():
    now = time.time()
    for k in list(processed_otps.keys()):
        if now - processed_otps[k] > 1200:
            del processed_otps[k]
    for b_id in list(fresh_batches_cache.keys()):
        if now - fresh_batches_cache[b_id].get("created_at", 0) > 1800:
            del fresh_batches_cache[b_id]

async def start_periodic_janitor():
    while True:
        try:
            await asyncio.sleep(600)
            cleanup_cache()
        except asyncio.CancelledError:
            break
        except Exception:
            pass

async def auto_cancel_bad_numbers(client: HeroSMSClient, bad_items: list):
    if not bad_items: return
    await asyncio.sleep(125)  # 2 minute HeroSMS lock bypass
    for item in bad_items:
        aid = item.get("aid")
        if not aid: continue
        try:
            r = await client.set_status(aid, 8)
            if (isinstance(r, str) and "CANCEL" in r) or (isinstance(r, dict) and r.get("status") == "success"):
                await db.delete_activation(aid)
                for k in list(processed_otps.keys()):
                    if k.startswith(f"{aid}:"): del processed_otps[k]
        except Exception:
            pass

def get_colombia_operator(phone: str) -> str:
    clean = str(phone).lstrip("+").strip()
    if clean.startswith("57"): clean = clean[2:]
    prefix = clean[:3]
    if prefix in ["310", "311", "312", "313", "314", "320", "321", "322", "323"]: return "Claro"
    if prefix in ["300", "301", "302", "304", "305", "324"]: return "Tigo"
    if prefix in ["315", "316", "317", "318"]: return "Movistar"
    if prefix in ["350", "351", "333"]: return "WOM"
    if prefix in ["319"]: return "Virgin"
    return "Unknown"

def format_otp_text(phone: str, code: str) -> str:
    return f"🇨🇴 <b>Telegram</b> <code>{html.escape(str(phone).lstrip('+').strip())}</code>"

def format_tg_status(raw_status: any) -> dict:
    if raw_status is None: return {"badge": "⚠️ Check Failed", "priority": 5, "is_fresh": False, "is_error": True}
    st = str(raw_status.get("status") if isinstance(raw_status, dict) else raw_status).strip().lower()
    if any(x in st for x in ["api_error", "check_failed", "error"]) or not st:
        return {"badge": "⚠️ Check Failed", "priority": 5, "is_fresh": False, "is_error": True}
    if any(x in st for x in ["unoccupied", "unregistered", "not_registered", "free", "fresh", "available", "valid", "false", "0"]):
        if "banned" in st and not any(n in st for n in ["not", "un", "no", "non", "false"]):
            return {"badge": "🚫 Banned", "priority": 4, "is_fresh": False, "is_error": False}
        return {"badge": "✅", "priority": 1, "is_fresh": True, "is_error": False}
    if any(x in st for x in ["occupied", "registered", "taken", "used", "true", "1"]):
        return {"badge": "❌ Registered", "priority": 3, "is_fresh": False, "is_error": False}
    if "banned" in st or "ban" in st:
        return {"badge": "🚫 Banned", "priority": 4, "is_fresh": False, "is_error": False}
    return {"badge": f"⚠ {clean_error_text(st)}", "priority": 5, "is_fresh": False, "is_error": False}

async def get_valid_user_client(user_id: int):
    user = await db.get_user(user_id)
    if not user or not user.get("api_key"): return None, None
    return user, HeroSMSClient(user["api_key"])

async def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    user = await db.get_user(user_id)
    if user and user.get("is_banned"): return False
    maintenance = await db.get_setting("maintenance")
    return maintenance != "1"

# --- Webhook Handler ---
async def handle_herosms_webhook(request: web.Request):
    data = {}
    try:
        data = await request.json() if request.method == "POST" and request.can_read_body else dict(await request.post() if request.method == "POST" else request.query)
    except Exception:
        data = dict(request.query)

    aid = str(data.get("activationId") or data.get("id") or "").strip()
    raw_code = data.get("code") or data.get("smsCode") or data.get("text") or ""
    match = re.search(r'\b\d{4,8}\b', str(raw_code))
    code = match.group(0) if match else str(raw_code).strip()

    if not aid or not code: return web.Response(text="OK", status=200)
    cache_key = f"{aid}:{code}"
    if cache_key in processed_otps: return web.Response(text="OK", status=200)
    processed_otps[cache_key] = time.time()
    cleanup_cache()

    try:
        row = await db.get_activation_user(aid)
        if row and row[0]:
            phone = data.get("phoneNumber") or data.get("phone") or row[1]
            bot = get_bot_instance()
            if bot and phone:
                await bot.send_message(row[0], format_otp_text(phone, code), reply_markup=kb.otp_copy_menu(code), parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Webhook forward error: {e}")
    return web.Response(text="OK", status=200)

# --- Start & Main ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.add_user(message.from_user.id)
    user = await db.get_user(message.from_user.id)
    if user and user.get("is_banned"): return await message.answer("You are banned.")
    if await db.get_setting("maintenance") == "1" and message.from_user.id != ADMIN_ID:
        return await message.answer("Bot is under maintenance.")
    if not user or not user.get("api_key"):
        await message.answer("Welcome! Please send your HeroSMS API Key:", reply_markup=ReplyKeyboardRemove())
        await state.set_state(BotStates.waiting_for_api_key)
    else:
        await message.answer("Welcome back!", reply_markup=kb.main_reply_menu())

@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    api_key = message.text.strip().strip("\"'")
    if api_key in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Cancelled.")
    client = HeroSMSClient(api_key)
    bal = await client.get_balance()
    if bal is not None:
        await db.update_api_key(message.from_user.id, api_key)
        await state.clear()
        await message.answer(f"✅ API Key saved!\n💰 Balance: {bal:.4f} USD", reply_markup=kb.main_reply_menu())
    else:
        await message.answer("❌ Invalid API Key. Please verify and try again.")

@router.callback_query(F.data == "menu_main")
async def cb_menu_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await callback.message.delete()
    except Exception: pass
    await callback.message.answer("Main Menu:", reply_markup=kb.main_reply_menu())

@router.message(Command("balance"))
@router.message(F.text == "Balance")
async def cmd_balance(message: Message):
    if not await is_allowed(message.from_user.id): return
    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return await message.answer("Please set your API key first with /api")
    bal = await client.get_balance()
    await message.answer(f"💰 Balance: <b>{bal:.4f} USD</b>" if bal is not None else "❌ Error loading balance.")

@router.message(Command("api"))
async def cmd_api(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): return
    await message.answer("🔑 Send your new HeroSMS API Key:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(BotStates.waiting_for_api_key)

# --- Robust /live parser ---
def extract_operator_data(raw_data):
    """HeroSMS prices response theke stock ebong price ber kore"""
    extracted = {}
    if not isinstance(raw_data, dict): return extracted
    
    # Check if format is { "33": { "tg": { ... } } }
    curr = raw_data
    if str(COLOMBIA_ID) in curr: curr = curr[str(COLOMBIA_ID)]
    if TG_SERVICE in curr: curr = curr[TG_SERVICE]
    
    if isinstance(curr, dict):
        for op, val in curr.items():
            if isinstance(val, dict):
                cost = float(val.get("cost") or val.get("price") or 0.0)
                count = int(val.get("count") or val.get("total") or 0)
                if cost > 0 or count > 0:
                    extracted[str(op).lower()] = {"cost": cost, "count": count}
            elif isinstance(val, (int, float)):
                extracted[str(op).lower()] = {"cost": float(val), "count": 1}
    return extracted

@router.message(Command("live", "prices"))
async def cmd_live_prices(message: Message):
    if not await is_allowed(message.from_user.id): return
    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return await message.answer("Set your API Key first with /api")

    res = await client.get_prices(country=COLOMBIA_ID, service=TG_SERVICE)
    ops = extract_operator_data(res)
    
    lines = ["📡 <b>HeroSMS Live Stock & Prices (Colombia - TG):</b>\n"]
    valid_count = 0
    for op, data in ops.items():
        if data["count"] > 0:
            valid_count += 1
            lines.append(f"• <b>{op.upper()}</b>: <code>${data['cost']:.3f}</code> (Stock: <b>{data['count']}</b> ta)")

    if valid_count == 0:
        if ops:
            lines.append("<i>Operators available but current stock is 0:</i>")
            for op, data in ops.items():
                lines.append(f"• {op.upper()}: ${data['cost']:.3f}")
        else:
            lines.append("ℹ️ No operator data returned from HeroSMS right now.")
    else:
        lines.append("\n💡 <b>Direct Buy:</b> <code>/buy &lt;operator&gt; &lt;amount&gt;</code>\n<i>Example:</i> <code>/buy claro 5</code>")

    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# --- Universal Purchase Core ---
async def execute_bulk_order(message_or_cb, client, user_id: int, amount: int, operator_name: str = None, max_price: float = MAX_PRICE):
    target_op = operator_name.lower() if operator_name and operator_name.lower() != "any" else None
    exc_saved = await db.get_setting("excluded_prefixes")
    api_exc = str(exc_saved) if exc_saved else ",".join(DEFAULT_EXCLUDE_LIST)

    label = f" ({target_op.upper()})" if target_op else ""
    status_msg = await message_or_cb.answer(f"⏳ Buying {amount} numbers{label}... (0/{amount}) (0%)")

    purchased = []
    batch_records = []
    number_aid_map = {}
    number_cost_map = {}
    last_edit = time.time()

    for i in range(amount):
        res = None
        for _ in range(3):
            res = await client.get_number(
                service=TG_SERVICE, country=COLOMBIA_ID,
                max_price=max_price, phone_exception=api_exc, operator=target_op
            )
            if isinstance(res, dict) and "activationId" in res: break
            await asyncio.sleep(0.7)

        if isinstance(res, dict) and "activationId" in res:
            aid = str(res["activationId"])
            phone = str(res.get("phoneNumber") or "").lstrip("+").strip()
            cost_val = float(res.get("cost") or max_price)

            purchased.append(phone)
            batch_records.append((aid, user_id, phone))
            number_aid_map[phone] = aid
            number_aid_map[f"+{phone}"] = aid
            number_cost_map[phone] = f"${cost_val:.3f}"
            number_cost_map[f"+{phone}"] = f"${cost_val:.3f}"

            now = time.time()
            if (now - last_edit >= 3.0) or (i == amount - 1):
                try:
                    display_lines = purchased[-10:]
                    lines = "\n".join(f"{n}. <b>+{p}</b> ({get_colombia_operator(p)})" for n, p in enumerate(display_lines, len(purchased)-len(display_lines)+1))
                    pct = int((len(purchased) / amount) * 100)
                    upd = f"Buying {amount} numbers{label}... ({len(purchased)}/{amount}) ({pct}%)\n\n{lines}"
                    await status_msg.edit_text(upd, parse_mode=ParseMode.HTML)
                    last_edit = now
                except Exception: pass
            await asyncio.sleep(0.3)
        else:
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            await message_or_cb.answer(f"⚠️ Stopped at #{i+1}: {clean_error_text(err)}")
            break

    if not purchased:
        return await status_msg.edit_text("❌ Could not purchase any numbers.")

    # Save to database
    if hasattr(db, "save_activations_batch"):
        await db.save_activations_batch(batch_records)
    else:
        for aid, uid, p in batch_records: await db.save_activation(aid, uid, p)

    try: await status_msg.edit_text(f"✅ Purchased {len(purchased)} numbers!\n🔍 Checking Telegram status, wait a moment...")
    except Exception: pass

    check_results = await check_telegram_numbers(purchased)
    parsed_items = []
    bad_numbers = []

    for p in purchased:
        op = get_colombia_operator(p)
        info = format_tg_status(check_results.get(f"+{p}") or check_results.get(p))
        c_str = number_cost_map.get(p, f"${max_price:.3f}")
        item = {
            "phone": p, "aid": number_aid_map.get(p), "cost_str": c_str,
            "operator": op, "badge": info["badge"], "priority": info["priority"],
            "is_fresh": info["is_fresh"], "is_error": info.get("is_error")
        }
        parsed_items.append(item)
        if not info["is_fresh"] and not info.get("is_error") and info["priority"] in [3, 4]:
            bad_numbers.append(item)

    fresh_list = [x for x in parsed_items if x["is_fresh"]]
    other_list = sorted([x for x in parsed_items if not x["is_fresh"] and not x.get("is_error")], key=lambda x: x["priority"])
    error_list = [x for x in parsed_items if x.get("is_error")]

    out = [f"🎉 <b>Order Completed!{label}</b>\nTotal: {len(purchased)} numbers (tap to copy)\n"]
    if fresh_list:
        out.append(f"🟢 <b>Fresh Numbers ({len(fresh_list)}):</b>")
        for idx, it in enumerate(fresh_list, 1):
            out.append(f"{idx}. <code>+{it['phone']}</code> ({it['operator']}) — {it['badge']} <b>{it['cost_str']}</b>")
        out.append("")
    if other_list:
        out.append(f"🔻 <b>Unavailable / Occupied ({len(other_list)}):</b>")
        for idx, it in enumerate(other_list, 1):
            out.append(f"{idx}. <code>+{it['phone']}</code> ({it['operator']}) — <b>{it['badge']}</b>")
        out.append("<i>(Auto-cancelling for refund in 2 mins...)</i>\n")
    if error_list:
        out.append(f"⚠️ <b>Check Unverified ({len(error_list)}):</b>")
        for idx, it in enumerate(error_list, 1):
            out.append(f"{idx}. <code>+{it['phone']}</code> ({it['operator']}) — <b>{it['badge']}</b>\n")

    out.append("Waiting for OTPs...")
    if bad_numbers: asyncio.create_task(auto_cancel_bad_numbers(client, bad_numbers))

    final = "\n".join(out)
    batch_id = str(uuid.uuid4())[:8]
    fresh_batches_cache[batch_id] = {"summary": final, "fresh_phones": [x["phone"] for x in fresh_list], "created_at": time.time()}
    cleanup_cache()

    markup = kb.bulk_result_menu(batch_id, len(fresh_list))
    if len(final) > 4000:
        for chunk in [final[j:j+4000] for j in range(0, len(final), 4000)]:
            await message_or_cb.answer(chunk, parse_mode=ParseMode.HTML)
        try: await status_msg.delete()
        except Exception: pass
    else:
        await status_msg.edit_text(final, reply_markup=markup, parse_mode=ParseMode.HTML)

# --- Direct Buy Command ---
@router.message(Command("buy"))
async def cmd_direct_buy(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 3:
        return await message.answer("⚠️️ <b>Usage:</b> <code>/buy &lt;operator&gt; &lt;amount&gt;</code>\nExample: <code>/buy claro 5</code>", parse_mode=ParseMode.HTML)

    target_op = args[1].strip().lower()
    try:
        amount = int(args[2].strip())
        if not (1 <= amount <= 50): raise ValueError
    except Exception:
        return await message.answer("Please enter amount between 1 and 50.")

    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return await message.answer("Set your API key first with /api")

    # Detect live price
    res = await client.get_prices(country=COLOMBIA_ID, service=TG_SERVICE)
    ops = extract_operator_data(res)
    detected_price = MAX_PRICE
    if target_op in ops and ops[target_op]["cost"] > 0:
        detected_price = ops[target_op]["cost"] + 0.005

    await execute_bulk_order(message, client, message.from_user.id, amount, target_op, detected_price)

# --- Bulk Buy Menu ---
@router.message(F.text == "Bulk Buy Numbers")
async def text_bulk_buy(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id): return
    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return await message.answer("Set your API key first with /api")
    await message.answer("📦 How many numbers do you want to buy? (1-50):")
    await state.set_state(BotStates.waiting_for_bulk_amount)

@router.message(BotStates.waiting_for_bulk_amount)
async def process_bulk_amount(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Cancelled.")
    try:
        amount = int(text)
        if not (1 <= amount <= 50): raise ValueError
    except Exception:
        return await message.answer("Enter a valid number between 1 and 50.")

    await state.clear()
    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return
    saved_op = await db.get_setting("preferred_operator")
    await execute_bulk_order(message, client, message.from_user.id, amount, saved_op or DEFAULT_OPERATOR, MAX_PRICE)

# --- Fresh Numbers & Callbacks ---
@router.callback_query(F.data.startswith("show_fresh_"))
async def cb_show_fresh_numbers(callback: CallbackQuery):
    cleanup_cache()
    batch = fresh_batches_cache.get(callback.data[len("show_fresh_"):])
    if not batch or not batch.get("fresh_phones"):
        return await callback.answer("Session expired.", show_alert=True)
    await callback.message.edit_text(
        f"🟢 <b>Fresh Numbers ({len(batch['fresh_phones'])})</b>\n<i>Tap to copy:</i>",
        reply_markup=kb.fresh_numbers_menu(batch["fresh_phones"], callback.data[len("show_fresh_"):]),
        parse_mode=ParseMode.HTML
    )

@router.callback_query(F.data.startswith("back_bulk_"))
async def cb_back_bulk(callback: CallbackQuery):
    cleanup_cache()
    batch = fresh_batches_cache.get(callback.data[len("back_bulk_"):])
    if not batch: return await callback.answer("Session expired.", show_alert=True)
    await callback.message.edit_text(batch["summary"], reply_markup=kb.bulk_result_menu(callback.data[len("back_bulk_"):], len(batch.get("fresh_phones", []))), parse_mode=ParseMode.HTML)

# --- Active Numbers & Finish ---
@router.message(F.text == "Active Numbers")
async def text_active_numbers(message: Message):
    if not await is_allowed(message.from_user.id): return
    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return await message.answer("Set your API Key first.")
    res = await client.get_active_activations()
    acts = res.get("data", []) if isinstance(res, dict) else []
    if not acts: return await message.answer("No active numbers.")
    await message.answer(f"Active Numbers ({len(acts)}):", reply_markup=kb.active_numbers_menu(acts, page=0))

@router.message(Command("ok"))
async def cmd_ok_finish(message: Message):
    if not await is_allowed(message.from_user.id): return
    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return await message.answer("Set your API Key first.")
    args = message.text.split()
    target = args[1].replace("+", "").strip() if len(args) > 1 else None
    res = await client.get_active_activations()
    acts = res.get("data", []) if isinstance(res, dict) else []
    to_finish = [a for a in acts if (target and (str(a.get("phoneNumber")) == target or str(a.get("activationId")) == target)) or (not target and (bool(a.get("smsCode")) or str(a.get("activationStatus")) in ["4", "6"]))]
    if not to_finish: return await message.answer("ℹ️ No finished numbers found.")

    finished = []
    for a in to_finish:
        aid = str(a.get("activationId"))
        p = str(a.get("phoneNumber", "Unknown")).lstrip("+")
        try:
            r = await client.set_status(aid, 6)
            if (isinstance(r, str) and "ACTIVATION" in r) or (isinstance(r, dict) and r.get("status") == "success"):
                await db.delete_activation(aid)
                for k in list(processed_otps.keys()):
                    if k.startswith(f"{aid}:"): del processed_otps[k]
                finished.append(f"• +{p} - Finished ✅")
        except Exception: pass
    await message.answer(f"<b>Finished Activations ({len(finished)}):</b>\n\n" + "\n".join(finished), parse_mode=ParseMode.HTML)

# --- Stats Handler ---
@router.message(Command("stats", "statistics"))
async def cmd_stats(message: Message):
    if not await is_allowed(message.from_user.id): return
    user, client = await get_valid_user_client(message.from_user.id)
    if not client: return await message.answer("Set your API Key first.")
    args = message.text.split()
    date_arg = args[1].strip() if len(args) >= 2 else None
    res = await client.get_stats(date_arg)
    if not res or not isinstance(res, dict) or "data" not in res:
        return await message.answer("Stats not available right now.")
    data = res.get("data", {})
    disp_date = date_arg or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    tot_p, tot_s = 0, 0
    lines = [f"📊 <b>HeroSMS Stats ({disp_date}):</b>\n"]
    for c_key, svs in data.items():
        if isinstance(svs, dict):
            for s_name, s_data in svs.items():
                if isinstance(s_data, dict):
                    cnt = s_data.get("count", 0)
                    suc = s_data.get("success", 0)
                    pct = float(s_data.get("percent", 0.0))
                    tot_p += cnt
                    tot_s += suc
                    lines.append(f"Country {c_key} | {s_name.upper()}: Total {cnt} | Success {suc} ({pct:.1f}%)")

    overall = (tot_s / tot_p * 100) if tot_p > 0 else 0.0
    lines.append(f"\nSummary:\nTotal: {tot_p} | Success: {tot_s} | Avg Rate: {overall:.1f}%")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

# --- Exclude, Operator & Admin Controls ---
@router.message(Command("exclude"))
async def cmd_exclude(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2: return await message.answer("Usage: /exclude 57300,57301")
    curr = (await db.get_setting("excluded_prefixes") or ",".join(DEFAULT_EXCLUDE_LIST)).split(",")
    new_p = [x.replace("+", "").strip() for x in args[1].split(",") if x.strip()]
    curr = list(set(curr + new_p))[:20]
    await db.set_setting("excluded_prefixes", ",".join(curr))
    await message.answer(f"✅ Blacklist: {', '.join(curr)}")

@router.message(Command("operator"))
async def cmd_operator(message: Message):
    if not await is_allowed(message.from_user.id): return
    args = message.text.split()
    if len(args) < 2:
        curr = await db.get_setting("preferred_operator") or DEFAULT_OPERATOR
        return await message.answer(f"Current operator: <b>{curr}</b>\nSet: /operator claro\nReset: /operator any", parse_mode=ParseMode.HTML)
    new_op = args[1].strip().lower()
    await db.set_setting("preferred_operator", new_op)
    await message.answer(f"Preferred operator set to: <b>{new_op}</b>", parse_mode=ParseMode.HTML)

@router.callback_query(F.data.startswith("active_cancel_"))
async def cb_active_cancel(callback: CallbackQuery):
    aid = callback.data.split("_")[2]
    user, client = await get_valid_user_client(callback.from_user.id)
    if not client: return
    r = await client.set_status(aid, 8)
    if isinstance(r, str) and "EARLY_CANCEL_DENIED" in r:
        return await callback.answer("Cannot cancel within first 2 minutes.", show_alert=True)
    await db.delete_activation(aid)
    await callback.answer("Cancelled!")
    try: await callback.message.delete()
    except Exception: pass

@router.callback_query(F.data == "cancel_all_active")
async def cb_cancel_all(callback: CallbackQuery):
    user, client = await get_valid_user_client(callback.from_user.id)
    if not client: return
    res = await client.get_active_activations()
    acts = res.get("data", []) if isinstance(res, dict) else []
    for a in acts:
        try: await client.set_status(str(a.get("activationId")), 8)
        except Exception: pass
    await callback.message.edit_text(f"Cancelled {len(acts)} active numbers.")

@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()
