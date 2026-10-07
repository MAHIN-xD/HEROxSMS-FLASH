import asyncio
import html
import logging
import re
from datetime import datetime, timezone
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from aiogram.utils.chat_action import ChatActionSender
from aiohttp import web
from api_client import HeroSMSClient, check_telegram_numbers
import database as db
import keyboards as kb
from states import BotStates

# Fallback in case states.py is missing any state definitions
if not hasattr(BotStates, "waiting_for_revoke_id"):
    BotStates.waiting_for_revoke_id = getattr(BotStates, "waiting_for_unapprove_id", State())
if not hasattr(BotStates, "waiting_for_extend_id"):
    BotStates.waiting_for_extend_id = State()
if not hasattr(BotStates, "waiting_for_extend_days"):
    BotStates.waiting_for_extend_days = State()

router = Router()
global_bot = None


def set_bot_instance(bot):
    global global_bot
    global_bot = bot


def get_bot_instance():
    return global_bot


ADMIN_ID = 7266067201
COLOMBIA_ID = 33
TG_SERVICE = "tg"
MAX_PRICE = 0.145

MENU_BUTTONS = [
    "Bulk Buy Numbers",
    "Active Numbers",
    "Balance",
    "Profile",
    "📦 Check Live Stock",
    "📊 Users & OTP Monitor",
    "👥 Live Active Users",
    "📢 Broadcast Message",
    "🚫 Ban / Unban User",
    "❌ Unapprove / Revoke Access",
    "⏳ Extend User Subscription",
    "🔔 Restock Alert: ON",
    "🔕 Restock Alert: OFF",
    "⚙️ Toggle Maintenance",
    "⚙ Toggle Maintenance",
    "⬅️ Back to User Menu"
]

# --- Verified Custom Premium Emojis ---
EMOJI_FLAG = '<tg-emoji emoji-id="5294010206974397371">🇨🇴</tg-emoji>'
EMOJI_CARD = '<tg-emoji emoji-id="5206330150433595241">💳</tg-emoji>'
EMOJI_TICK = '<tg-emoji emoji-id="6087154735125630953">✅</tg-emoji>'
EMOJI_CROSS = '<tg-emoji emoji-id="5321012601939838274">❌</tg-emoji>'
EMOJI_WARN = '<tg-emoji emoji-id="5420323339723881652">⚠️</tg-emoji>'
EMOJI_SIREN = '<tg-emoji emoji-id="5395695537687123235">🚨</tg-emoji>'
EMOJI_LOCK = '<tg-emoji emoji-id="6334379984760604198">🔒</tg-emoji>'
EMOJI_BAN = '<tg-emoji emoji-id="5280803324273115630">🚫</tg-emoji>'
EMOJI_PLANE = '<tg-emoji emoji-id="5411563083908797492">🛫</tg-emoji>'
EMOJI_BOX = '<tg-emoji emoji-id="5298809897352193254">📦</tg-emoji>'
EMOJI_KEY = '<tg-emoji emoji-id="5193070340850327783">🔑</tg-emoji>'
EMOJI_USER = '<tg-emoji emoji-id="5249053508681883137">👤</tg-emoji>'

# --- In-Memory Speed Cache (0ms Latency) ---
USER_CACHE = {}
SETTINGS_CACHE = {}
last_known_stocks = {}
LATEST_STOCK_REPORT = ""


async def get_cached_user(user_id: int):
    if user_id in USER_CACHE:
        return USER_CACHE[user_id]
    user = await db.get_user(user_id)
    if user:
        USER_CACHE[user_id] = user
    return user


def invalidate_user_cache(user_id: int):
    USER_CACHE.pop(user_id, None)


async def get_cached_setting(key: str):
    if key in SETTINGS_CACHE:
        return SETTINGS_CACHE[key]
    val = await db.get_setting(key)
    SETTINGS_CACHE[key] = val
    return val


def update_cached_setting(key: str, val: str):
    SETTINGS_CACHE[key] = val


# --- Background Tasks ---
async def auto_cancel_bad_number_worker(client: HeroSMSClient, aid: str, phone: str, user_id: int):
    await asyncio.sleep(125)
    try:
        r = await client.set_status(aid, 8)
        if (isinstance(r, str) and ("CANCEL" in r)) or (isinstance(r, dict) and r.get("status") == "success"):
            await db.delete_activation(aid)
            bot = get_bot_instance()
            if bot:
                await bot.send_message(
                    user_id,
                    f'{EMOJI_WARN} <b>Refund Alert:</b> Number <code>+{phone}</code> was cancelled. Balance refunded.',
                    parse_mode="HTML"
                )
    except Exception as e:
        logging.warning(f"Auto-cancel worker error for {aid}: {e}")


async def start_restock_monitor():
    await asyncio.sleep(15)
    first_run = True
    while True:
        try:
            is_enabled = await get_cached_setting("restock_monitor")
            if is_enabled == "1":
                admin_user = await get_cached_user(ADMIN_ID)
                if admin_user and admin_user.get("api_key"):
                    client = HeroSMSClient(admin_user["api_key"])
                    prices_res = await client.get_prices(country=COLOMBIA_ID, service=TG_SERVICE)
                    
                    if isinstance(prices_res, dict):
                        c_dict = {}
                        if str(COLOMBIA_ID) in prices_res:
                            c_dict = prices_res[str(COLOMBIA_ID)].get(TG_SERVICE, {})
                        elif TG_SERVICE in prices_res:
                            c_dict = prices_res[TG_SERVICE]
                        else:
                            c_dict = prices_res

                        if isinstance(c_dict, dict):
                            for op, d in c_dict.items():
                                if isinstance(d, dict):
                                    cnt = int(d.get("count", d.get("amount", 0)))
                                    prc = float(d.get("cost", d.get("price", 0.0)))
                                    prev_count = last_known_stocks.get(op, 0)

                                    is_initial_stock = first_run and cnt >= 5
                                    is_stock_increased = (cnt - prev_count) >= 5

                                    if (is_initial_stock or is_stock_increased) and prc <= 0.145:
                                        approved = await db.get_approved_users()
                                        bot = get_bot_instance()
                                        if bot:
                                            msg_text = (
                                                f'{EMOJI_SIREN} <b>RESTOCK ALERT!</b>\n\n'
                                                f'{EMOJI_FLAG} Operator: <b>{op.upper()}</b>\n'
                                                f'{EMOJI_CARD} Price: <b>${prc:.3f}</b>\n'
                                                f'{EMOJI_BOX} Available Stock: <b>{cnt} numbers</b>\n\n'
                                                f'{EMOJI_TICK} Grab now from <b>Bulk Buy Numbers</b>!'
                                            )
                                            for u in approved:
                                                try:
                                                    await bot.send_message(u["user_id"], msg_text, parse_mode="HTML")
                                                    await asyncio.sleep(0.04)
                                                except Exception:
                                                    pass
                                    last_known_stocks[op] = cnt
                            first_run = False
        except Exception as e:
            logging.error(f"Restock monitor error: {e}")
        await asyncio.sleep(60)


def format_tg_status(raw_status: any) -> tuple:
    if raw_status is None:
        return f'{EMOJI_WARN} Check Failed', False

    st = str(raw_status.get("status") if isinstance(raw_status, dict) else raw_status).strip().lower()
    if any(w in st for w in ["unoccupied", "unregistered", "not_registered", "free", "fresh", "available", "false", "0"]):
        if "banned" in st and not any(neg in st for neg in ["not", "un", "no", "non", "false"]):
            return f'{EMOJI_BAN} Banned', False
        return f'{EMOJI_TICK} Fresh', True

    if any(w in st for w in ["flood", "locked", "lock", "wait", "restricted", "2fa"]):
        return f'{EMOJI_LOCK} Locked', False
    if any(w in st for w in ["occupied", "registered", "taken", "used", "true", "1"]):
        return f'{EMOJI_CROSS} Registered', False
    if "banned" in st or "ban" in st:
        return f'{EMOJI_BAN} Banned', False

    clean = re.sub(r'phone_number_', '', st, flags=re.IGNORECASE).replace('_', ' ').strip().title()
    return f'{EMOJI_WARN} {clean}', False


async def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    
    user = await get_cached_user(user_id)
    if not user or user.get("is_banned") or not user.get("is_approved"):
        return False

    exp = user.get("expiry_date")
    if exp and exp != "LIFETIME":
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_date:
                await db.set_approval_status(user_id, False)
                invalidate_user_cache(user_id)
                bot = get_bot_instance()
                if bot:
                    await bot.send_message(
                        user_id,
                        f'{EMOJI_WARN} <b>Your Subscription Has Expired!</b>\n\nPlease contact Admin to renew your access.',
                        parse_mode="HTML"
                    )
                return False
        except Exception:
            pass

    maintenance = await get_cached_setting("maintenance")
    if maintenance == "1":
        return False
    return True


# --- Webhook Handler ---
async def handle_herosms_webhook(request):
    action = request.query.get("action")
    aid = request.query.get("activationId") or request.query.get("id")
    code = request.query.get("code")

    if not action or not aid:
        return web.Response(text="Missing parameters", status=400)

    if action == "STATUS_OK" and code:
        row = await db.get_activation(aid)
        if row:
            user_id = row["user_id"]
            phone = row["phone"]
            msg_id = row["message_id"]
            text = (
                f'{EMOJI_FLAG} Number: <code>+{phone}</code>\n'
                f'{EMOJI_CARD} OTP: <code>{code}</code>'
            )
            bot = get_bot_instance()
            if bot:
                try:
                    if msg_id:
                        await bot.edit_message_text(
                            text=text, chat_id=user_id, message_id=msg_id,
                            reply_markup=kb.otp_copy_menu(code),
                            parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(
                            user_id, text,
                            reply_markup=kb.otp_copy_menu(code),
                            parse_mode="HTML"
                        )

                    await db.increment_user_stats(user_id, otps=1)
                    user = await get_cached_user(user_id)
                    if user and user.get("api_key"):
                        client = HeroSMSClient(user["api_key"])
                        await client.set_status(aid, 6)
                    await db.delete_activation(aid)
                except Exception as e:
                    logging.error(f"Failed to process webhook OTP for {user_id}: {e}")
        return web.Response(text="OK")
    return web.Response(text="Ignored")


async def poll_sms(bot, chat_id: int, activation_id: str, phone: str, client: HeroSMSClient):
    for _ in range(200):
        await asyncio.sleep(3)
        row = await db.get_activation(activation_id)
        if not row:
            return

        try:
            res = await client.get_status(activation_id)
            if isinstance(res, str):
                if res.startswith("STATUS_OK:"):
                    code = res.split(":", 1)[1]
                    text = (
                        f'{EMOJI_FLAG} Number: <code>+{phone}</code>\n'
                        f'{EMOJI_CARD} OTP: <code>{code}</code>'
                    )
                    msg_id = row["message_id"]
                    if msg_id:
                        await bot.edit_message_text(
                            text=text, chat_id=chat_id, message_id=msg_id,
                            reply_markup=kb.otp_copy_menu(code),
                            parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(
                            chat_id, text,
                            reply_markup=kb.otp_copy_menu(code),
                            parse_mode="HTML"
                        )

                    await db.increment_user_stats(chat_id, otps=1)
                    await client.set_status(activation_id, 6)
                    await db.delete_activation(activation_id)
                    return
                elif res.startswith("STATUS_CANCEL"):
                    await db.delete_activation(activation_id)
                    return
        except Exception:
            pass


# --- Start & Security ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        await state.clear()
        uid = message.from_user.id
        uname = message.from_user.username
        fname = message.from_user.full_name

        is_appr = 1 if uid == ADMIN_ID else 0
        await db.add_user(uid, uname, fname, is_approved=is_appr)
        if uid == ADMIN_ID:
            await db.set_user_subscription(ADMIN_ID, days=None)
        invalidate_user_cache(uid)

        user = await get_cached_user(uid)
        if user and user.get("is_banned"):
            await message.answer(f'{EMOJI_BAN} You are banned from using this bot.')
            return

        if not user or not user.get("is_approved"):
            await message.answer(
                f'{EMOJI_LOCK} <b>Access Restricted!</b>\n\n'
                'This is a private paid bot. Your approval request has been forwarded to the Admin.',
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
            bot = get_bot_instance()
            if bot:
                username_str = f"@{uname}" if uname else "No Username"
                alert_text = (
                    f'{EMOJI_SIREN} <b>New Approval Request!</b>\n\n'
                    f'{EMOJI_USER} <b>Name:</b> {html.escape(fname)}\n'
                    f'{EMOJI_PLANE} <b>Username:</b> {username_str}\n'
                    f'{EMOJI_CARD} <b>User ID:</b> <code>{uid}</code>\n\n'
                    'Select duration to approve:'
                )
                await bot.send_message(
                    ADMIN_ID, alert_text, reply_markup=kb.approval_duration_menu(uid), parse_mode="HTML"
                )
            return

        if not await is_allowed(uid):
            return

        maintenance = await get_cached_setting("maintenance")
        if maintenance == "1" and uid != ADMIN_ID:
            return await message.answer(f'{EMOJI_WARN} Bot is under maintenance. Contact Admin.')

        if not user.get("api_key"):
            await message.answer(
                f'{EMOJI_TICK} <b>Welcome to HeroSMS Premium Bot!</b>\n\nPlease send your HeroSMS API Key to get started:',
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML"
            )
            await state.set_state(BotStates.waiting_for_api_key)
        else:
            await message.answer("Welcome back! Select an option:", reply_markup=kb.main_reply_menu())


# --- Admin Approval Callbacks ---
@router.callback_query(F.data.startswith("appr_"))
async def cb_approval_action(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Unauthorized", show_alert=True)

    parts = callback.data.split("_")
    target_id = int(parts[1])
    action = parts[2]

    if action == "reject":
        await db.set_approval_status(target_id, False)
        invalidate_user_cache(target_id)
        await callback.message.edit_text(callback.message.html_text + f'\n\n<b>STATUS: REJECTED {EMOJI_CROSS}</b>', parse_mode="HTML")
        return await callback.answer("User Rejected")

    days_map = {"1": 1, "3": 3, "7": 7, "30": 30, "life": None}
    days = days_map.get(action)
    await db.set_user_subscription(target_id, days=days)
    invalidate_user_cache(target_id)

    exp_label = f"{days} Day{'s' if days > 1 else ''}" if days else "Lifetime"
    await callback.message.edit_text(
        callback.message.html_text + f'\n\n<b>STATUS: APPROVED ({exp_label}) {EMOJI_TICK}</b>', parse_mode="HTML"
    )
    await callback.answer(f"Approved for {exp_label}!")

    bot = get_bot_instance()
    if bot:
        try:
            await bot.send_message(
                target_id,
                f'{EMOJI_TICK} <b>Congratulations!</b>\n\n'
                f'Your account has been approved by Admin for <b>{exp_label}</b>!\n'
                f'Please send your HeroSMS API Key to start using the bot:',
                reply_markup=ReplyKeyboardRemove(),
                parse_mode="HTML"
            )
        except Exception:
            pass


@router.message(BotStates.waiting_for_api_key)
async def process_api_key(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id):
        await state.clear()
        return await message.answer(f'{EMOJI_LOCK} You are not approved yet.')

    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Action cancelled.")

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        api_key = text.strip("\"'").strip()
        client = HeroSMSClient(api_key)
        balance = await client.get_balance()

        if balance is not None:
            await db.update_api_key(message.from_user.id, api_key)
            invalidate_user_cache(message.from_user.id)
            await state.clear()
            await message.answer(
                f'{EMOJI_TICK} API Key saved successfully!\nBalance: <code>{balance:.4f} USD</code>',
                reply_markup=kb.main_reply_menu(),
                parse_mode="HTML"
            )
        else:
            await message.answer(f'{EMOJI_CROSS} Invalid API Key. Please check and try again.')


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


@router.message(F.text == "Profile")
async def text_profile(message: Message):
    if not await is_allowed(message.from_user.id):
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user = await get_cached_user(message.from_user.id)
        if not user or not user.get("api_key"):
            return await message.answer("Set your API Key first with /start")
        client = HeroSMSClient(user["api_key"])
        balance = await client.get_balance()
        bal_str = f"<code>{balance:.4f} USD</code>" if balance is not None else "Error"
        exp_str = user.get("expiry_date") or "Lifetime"
        text = (
            f'{EMOJI_USER} <b>Account Profile</b>\n\n'
            f'{EMOJI_CARD} <b>HeroSMS Balance:</b> {bal_str}\n'
            f'{EMOJI_PLANE} <b>Subscription Expiry:</b> <code>{exp_str}</code>\n'
            f'{EMOJI_KEY} <b>API Key:</b> <code>{user["api_key"][:12]}...</code>\n'
            f'{EMOJI_BOX} <b>Total Purchased:</b> {user.get("total_purchased", 0)}\n'
            f'{EMOJI_CARD} <b>Total OTPs:</b> {user.get("total_otps", 0)}'
        )
        await message.answer(text, reply_markup=kb.profile_menu(), parse_mode="HTML")


@router.callback_query(F.data == "profile_change_key")
async def cb_change_key(callback: CallbackQuery, state: FSMContext):
    if not await is_allowed(callback.from_user.id):
        return
    await callback.message.edit_text("Please send your new HeroSMS API Key:", reply_markup=kb.back_button())
    await state.set_state(BotStates.waiting_for_api_key)


@router.message(F.text == "Balance")
async def text_balance(message: Message):
    if not await is_allowed(message.from_user.id):
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user = await get_cached_user(message.from_user.id)
        if not user or not user.get("api_key"):
            return await message.answer("Set your API Key first.")
        client = HeroSMSClient(user["api_key"])
        balance = await client.get_balance()
        if balance is not None:
            alert = f'\n{EMOJI_WARN} <b>Warning:</b> Balance is below $0.50! Please recharge.' if balance < 0.50 else ""
            await message.answer(f'{EMOJI_CARD} Balance: <code>{balance:.4f} USD</code>{alert}', parse_mode="HTML")
        else:
            await message.answer(f'{EMOJI_CROSS} Error fetching balance.')


# --- Number Purchase Engine ---
async def buy_single_number_process(bot, user_id: int, chat_id: int, service: str, country_id: int, client: HeroSMSClient):
    try:
        res = await asyncio.wait_for(
            client.get_number(service=service, country=country_id, max_price=MAX_PRICE),
            timeout=9.0
        )
    except asyncio.TimeoutError:
        await bot.send_message(chat_id, f'{EMOJI_WARN} HeroSMS API response timed out. Skipping...')
        return False
    except Exception as e:
        await bot.send_message(chat_id, f'{EMOJI_WARN} Error contacting HeroSMS: {e}')
        return False

    if not isinstance(res, dict) or "activationId" not in res:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await bot.send_message(chat_id, f'Failed to buy number: {html.escape(str(err))}')
        return False

    aid = str(res["activationId"])
    phone = res.get("phoneNumber", "Unknown")

    await db.increment_user_stats(user_id, purchased=1)

    status_emoji = EMOJI_TICK
    is_fresh = True
    badge = "Fresh"

    try:
        check_res = await asyncio.wait_for(check_telegram_numbers([phone]), timeout=5.0)
        if isinstance(check_res, dict):
            badge, is_fresh = format_tg_status(check_res.get(f"+{phone}") or check_res.get(phone))
            if is_fresh:
                status_emoji = EMOJI_TICK
            elif any(x in badge for x in ["Banned", "Ban"]):
                status_emoji = EMOJI_BAN
            elif any(x in badge for x in ["Registered", "Occupied"]):
                status_emoji = EMOJI_CROSS
            elif any(x in badge for x in ["Locked", "Flood"]):
                status_emoji = EMOJI_LOCK
            else:
                status_emoji = EMOJI_WARN
    except Exception as e:
        logging.warning(f"Checker skipped for {phone}: {e}")
        status_emoji = EMOJI_WARN

    msg = await bot.send_message(
        chat_id,
        f'{EMOJI_FLAG} Number: <code>+{phone}</code> {status_emoji}\n'
        f'{EMOJI_CARD} OTP: Waiting for SMS...',
        reply_markup=kb.number_action_menu(aid),
        parse_mode="HTML"
    )

    await db.save_activation(aid, user_id, phone, msg.message_id)

    if not is_fresh and any(x in badge for x in ["Registered", "Banned", "Locked"]):
        asyncio.create_task(auto_cancel_bad_number_worker(client, aid, phone, user_id))
    else:
        asyncio.create_task(poll_sms(bot, chat_id, aid, phone, client))

    return True


@router.callback_query(F.data.startswith("refresh_"))
async def cb_refresh_sms(callback: CallbackQuery):
    aid = callback.data[len("refresh_"):]
    user = await get_cached_user(callback.from_user.id)
    if not user or not user.get("api_key"):
        return
    async with ChatActionSender.typing(bot=callback.bot, chat_id=callback.message.chat.id):
        client = HeroSMSClient(user["api_key"])
        res = await client.get_status(aid)

        if isinstance(res, str):
            if res.startswith("STATUS_OK:"):
                code = res.split(":", 1)[1]
                row = await db.get_activation(aid)
                phone = row["phone"] if row else "Unknown"
                await callback.message.edit_text(
                    f'{EMOJI_FLAG} Number: <code>+{phone}</code>\n'
                    f'{EMOJI_CARD} OTP: <code>{code}</code>',
                    reply_markup=kb.otp_copy_menu(code),
                    parse_mode="HTML"
                )
                await db.increment_user_stats(callback.from_user.id, otps=1)
                await client.set_status(aid, 6)
                await db.delete_activation(aid)
                await callback.answer("OTP Received!")
            elif res.startswith("STATUS_WAIT_CODE"):
                await callback.answer("Still waiting for OTP...", show_alert=True)
            elif res.startswith("STATUS_CANCEL"):
                await db.delete_activation(aid)
                await callback.message.edit_text("Activation cancelled.")
            else:
                await callback.answer(f"Status: {res}", show_alert=True)
        else:
            await callback.answer("Error checking status.", show_alert=True)


@router.callback_query(F.data.startswith("single_cancel_"))
async def cb_cancel_single(callback: CallbackQuery):
    aid = callback.data[len("single_cancel_"):]
    user = await get_cached_user(callback.from_user.id)
    if not user or not user.get("api_key"):
        return
    client = HeroSMSClient(user["api_key"])
    res = await client.set_status(aid, 8)
    if isinstance(res, str) and (res.startswith("ACCESS_CANCEL") or res.startswith("STATUS_CANCEL")):
        await db.delete_activation(aid)
        await callback.message.edit_text("Cancelled. Balance refunded.")
    elif isinstance(res, str) and "EARLY_CANCEL_DENIED" in res:
        await callback.answer("Cannot cancel within first 2 minutes. Auto-refund worker active.", show_alert=True)
    else:
        err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
        await callback.answer(f"Error: {err}", show_alert=True)


@router.message(F.text == "Bulk Buy Numbers")
async def text_bulk_buy(message: Message, state: FSMContext):
    if not await is_allowed(message.from_user.id):
        return
    user = await get_cached_user(message.from_user.id)
    if not user or not user.get("api_key"):
        return await message.answer("Please send your HeroSMS API Key first.")
    await message.answer(f'{EMOJI_BOX} <b>Bulk Purchase</b>\n\nHow many numbers do you want to buy? (1-50):', parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_bulk_amount)


@router.message(BotStates.waiting_for_bulk_amount)
async def process_bulk_amount(message: Message, state: FSMContext):
    text = message.text.strip()
    if text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Bulk buy cancelled.")

    try:
        amount = int(text)
        if amount < 1 or amount > 50:
            raise ValueError
    except Exception:
        await message.answer("Enter a valid number between 1 and 50.")
        return

    await state.clear()
    user = await get_cached_user(message.from_user.id)
    if not user or not user.get("api_key"):
        return await message.answer("Please set your API key first.")

    client = HeroSMSClient(user["api_key"])
    await message.answer(f'{EMOJI_PLANE} Starting bulk purchase of {amount} numbers...', parse_mode="HTML")

    for i in range(amount):
        success = await buy_single_number_process(
            message.bot, message.from_user.id, message.chat.id,
            TG_SERVICE, COLOMBIA_ID, client
        )
        if not success:
            await message.answer(f"Stopped bulk purchase at item #{i+1}.")
            break
        await asyncio.sleep(0.3)


@router.message(F.text == "Active Numbers")
async def text_active_numbers(message: Message):
    if not await is_allowed(message.from_user.id):
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        user = await get_cached_user(message.from_user.id)
        if not user or not user.get("api_key"):
            return await message.answer("Set your API Key first.")
        client = HeroSMSClient(user["api_key"])
        res = await client.get_active_activations()

        if not (isinstance(res, dict) and res.get("status") == "success"):
            err = res.get("title", str(res)) if isinstance(res, dict) else str(res)
            return await message.answer(f"Error: {html.escape(err)}")

        activations = res.get("data", [])
        if not activations:
            return await message.answer(f'{EMOJI_WARN} No active numbers.', parse_mode="HTML")

        await message.answer(f'Active Numbers ({len(activations)}):', reply_markup=kb.active_numbers_menu(activations))


@router.callback_query(F.data == "cancel_all_active")
async def cb_cancel_all_active(callback: CallbackQuery):
    if not await is_allowed(callback.from_user.id):
        return
    user = await get_cached_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    res = await client.get_active_activations()
    if not (isinstance(res, dict) and res.get("status") == "success"):
        return await callback.answer("Failed to fetch active numbers.", show_alert=True)
    activations = res.get("data", [])
    if not activations:
        return await callback.answer("No active numbers to cancel.", show_alert=True)

    await callback.message.edit_text(f"Cancelling {len(activations)} numbers... please wait.")

    async def cancel_one(act):
        aid = str(act.get("activationId", ""))
        if not aid:
            return False
        try:
            r = await client.set_status(aid, 8)
            if (isinstance(r, str) and ("CANCEL" in r)) or (isinstance(r, dict) and r.get("status") == "success"):
                await db.delete_activation(aid)
                return True
        except Exception:
            pass
        return False

    results = await asyncio.gather(*[cancel_one(a) for a in activations])
    ok = sum(1 for x in results if x)
    await callback.message.edit_text(f"Cancelled {ok}/{len(activations)} numbers. Balance refunded.")


@router.callback_query(F.data.startswith("active_cancel_"))
async def cb_active_cancel(callback: CallbackQuery):
    aid = callback.data[len("active_cancel_"):]
    user = await get_cached_user(callback.from_user.id)
    client = HeroSMSClient(user["api_key"])
    r = await client.set_status(aid, 8)
    if isinstance(r, str) and ("CANCEL" in r):
        await db.delete_activation(aid)
        await callback.answer("Cancelled!", show_alert=True)
        res = await client.get_active_activations()
        if isinstance(res, dict) and res.get("status") == "success":
            acts = res.get("data", [])
            if not acts:
                await callback.message.edit_text("No active numbers left.")
            else:
                await callback.message.edit_reply_markup(reply_markup=kb.active_numbers_menu(acts))
    elif isinstance(r, str) and "EARLY_CANCEL_DENIED" in r:
        await callback.answer("Cannot cancel within first 2 minutes.", show_alert=True)
    else:
        await callback.answer("Failed to cancel.", show_alert=True)


# --- Admin Panel & Live Stock Controls ---
@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    is_restock = (await get_cached_setting("restock_monitor")) == "1"
    await message.answer(
        f'{EMOJI_KEY} <b>Admin Control Panel Activated!</b>\n\nKeyboard switched to Admin tools:',
        reply_markup=kb.admin_reply_menu(restock_on=is_restock),
        parse_mode="HTML"
    )


@router.message(F.text == "⬅️ Back to User Menu")
async def back_to_user_menu(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(f'{EMOJI_PLANE} Switched back to User Menu:', reply_markup=kb.main_reply_menu(), parse_mode="HTML")


# --- Live Stock Check & Manual Broadcast ---
@router.message(F.text == "📦 Check Live Stock")
async def admin_check_live_stock(message: Message):
    global LATEST_STOCK_REPORT
    if message.from_user.id != ADMIN_ID:
        return

    admin_user = await get_cached_user(ADMIN_ID)
    if not admin_user or not admin_user.get("api_key"):
        return await message.answer(f'{EMOJI_WARN} Admin API Key is not set! Set your key using /start or Profile first.', parse_mode="HTML")

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        client = HeroSMSClient(admin_user["api_key"])
        prices_res = await client.get_prices(country=COLOMBIA_ID, service=TG_SERVICE)

        if not isinstance(prices_res, dict):
            return await message.answer(f'{EMOJI_CROSS} Failed to fetch prices from HeroSMS API.', parse_mode="HTML")

        c_dict = {}
        if str(COLOMBIA_ID) in prices_res:
            c_dict = prices_res[str(COLOMBIA_ID)].get(TG_SERVICE, {})
        elif TG_SERVICE in prices_res:
            c_dict = prices_res[TG_SERVICE]
        else:
            c_dict = prices_res

        common_ops = ["claro", "movistar", "tigo", "wom", "virgin", "exito", "flash"]
        found_ops = {}

        if isinstance(c_dict, dict):
            for op_name, op_val in c_dict.items():
                if isinstance(op_val, dict):
                    cnt = int(op_val.get("count", op_val.get("amount", 0)))
                    prc = float(op_val.get("cost", op_val.get("price", 0.0)))
                    found_ops[op_name.lower()] = {"count": cnt, "cost": prc}

        lines = [f'{EMOJI_FLAG} <b>Live Colombia Telegram Stock:</b>\n']
        total_available = 0

        for op, data in found_ops.items():
            cnt = data["count"]
            cost = data["cost"]
            total_available += cnt
            badge = EMOJI_TICK if cnt > 0 else EMOJI_CROSS
            cost_str = f"${cost:.3f}" if cost > 0 else "N/A"
            lines.append(f"{badge} <b>{op.upper()}</b>: <b>{cnt} pcs</b> | Rate: <code>{cost_str}</code>")

        for op in common_ops:
            if op not in found_ops:
                lines.append(f'{EMOJI_CROSS} <b>{op.upper()}</b>: <b>0 pcs</b> | Rate: <code>Out of stock</code>')

        lines.append(f'\n{EMOJI_BOX} <b>Total Stock:</b> <code>{total_available} numbers</code>')
        report_text = "\n".join(lines)
        LATEST_STOCK_REPORT = report_text

        await message.answer(report_text, reply_markup=kb.stock_broadcast_menu(), parse_mode="HTML")


@router.callback_query(F.data == "broadcast_live_stock")
async def cb_broadcast_live_stock(callback: CallbackQuery):
    global LATEST_STOCK_REPORT
    if callback.from_user.id != ADMIN_ID:
        return await callback.answer("Unauthorized", show_alert=True)

    if not LATEST_STOCK_REPORT:
        return await callback.answer("No stock report available to broadcast.", show_alert=True)

    users = await db.get_approved_users()
    if not users:
        return await callback.answer("No approved users found.", show_alert=True)

    await callback.answer("Broadcasting stock alert to users...", show_alert=False)

    msg_to_send = (
        f'{EMOJI_SIREN} <b>LIVE STOCK UPDATE!</b>\n\n'
        f'{LATEST_STOCK_REPORT}\n\n'
        f'{EMOJI_TICK} <i>Go to "Bulk Buy Numbers" to purchase now!</i>'
    )

    sent = 0
    for u in users:
        try:
            await callback.bot.send_message(u["user_id"], msg_to_send, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass

    await callback.message.reply(f'{EMOJI_TICK} Stock alert broadcasted to <b>{sent}</b> active users!', parse_mode="HTML")


@router.message(F.text.startswith("🔔 Restock Alert") | F.text.startswith("🔕 Restock Alert"))
async def toggle_restock_alert(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    current = await get_cached_setting("restock_monitor")
    new_val = "0" if current == "1" else "1"
    await db.set_setting("restock_monitor", new_val)
    update_cached_setting("restock_monitor", new_val)
    status_str = "ENABLED (Users will get alerts)" if new_val == "1" else "DISABLED"
    await message.answer(
        f"Restock Monitor is now: <b>{status_str}</b>",
        reply_markup=kb.admin_reply_menu(restock_on=(new_val == "1")),
        parse_mode="HTML"
    )


@router.message(F.text == "⚙️ Toggle Maintenance")
@router.message(F.text == "⚙ Toggle Maintenance")
async def admin_toggle_maint(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    current = await get_cached_setting("maintenance")
    new_val = "0" if current == "1" else "1"
    await db.set_setting("maintenance", new_val)
    update_cached_setting("maintenance", new_val)
    status_label = f'ENABLED (Users Locked {EMOJI_CROSS})' if new_val == "1" else f'DISABLED (Normal Mode {EMOJI_TICK})'
    await message.answer(f'{EMOJI_WARN} Maintenance Mode is now: <b>{status_label}</b>', parse_mode="HTML")


@router.message(F.text == "📊 Users & OTP Monitor")
async def admin_monitor_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        users = await db.get_approved_users()
        if not users:
            return await message.answer(f'{EMOJI_WARN} No approved users found.', parse_mode="HTML")

        lines = [f'{EMOJI_USER} <b>Approved Users Monitoring ({len(users)}):</b>\n']
        for idx, u in enumerate(users, 1):
            uname = f"@{u['username']}" if u['username'] else "No Username"
            api_preview = f"<code>{u['api_key'][:8]}...</code>" if u.get('api_key') else "<i>Not Set</i>"
            exp = u.get("expiry_date") or "Lifetime"
            lines.append(
                f"<b>{idx}. {html.escape(str(u.get('full_name') or 'User'))}</b> ({uname})\n"
                f"• ID: <code>{u['user_id']}</code> | Exp: <code>{exp}</code>\n"
                f"• Key: {api_preview}\n"
                f"• Bought: <b>{u.get('total_purchased', 0)}</b> | OTPs: <b>{u.get('total_otps', 0)}</b>\n"
            )
        text = "\n".join(lines)
        if len(text) > 4000:
            for chunk in [text[j:j+4000] for j in range(0, len(text), 4000)]:
                await message.answer(chunk, parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")


@router.message(F.text == "👥 Live Active Users")
async def admin_live_active_users(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        users = await db.get_approved_users()
        if not users:
            return await message.answer(f'{EMOJI_WARN} No active users found.', parse_mode="HTML")

        active_list = []
        now = datetime.now(timezone.utc)
        for u in users:
            exp = u.get("expiry_date")
            if exp == "LIFETIME":
                active_list.append((u, "Lifetime Access"))
            elif exp:
                try:
                    exp_date = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if exp_date > now:
                        delta = exp_date - now
                        days_left = delta.days
                        hours_left = delta.seconds // 3600
                        active_list.append((u, f"{days_left}d {hours_left}h remaining"))
                except Exception:
                    pass

        if not active_list:
            return await message.answer(f'{EMOJI_WARN} Currently no users have active valid subscriptions.', parse_mode="HTML")

        lines = [f'{EMOJI_USER} <b>Live Active Users ({len(active_list)}):</b>\n']
        for idx, (u, time_left) in enumerate(active_list, 1):
            uname = f"@{u['username']}" if u['username'] else "No Username"
            lines.append(
                f"<b>{idx}. {html.escape(str(u.get('full_name') or 'User'))}</b> ({uname})\n"
                f"• ID: <code>{u['user_id']}</code>\n"
                f'• Status: {EMOJI_TICK} <b>Active</b> ({time_left})\n'
            )
        await message.answer("\n".join(lines), parse_mode="HTML")


# --- Step-by-Step Ban/Unban ---
@router.message(F.text == "🚫 Ban / Unban User")
async def admin_req_ban(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Send the <b>User ID</b> to Ban / Unban (or send cancel):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_ban_id)


@router.message(BotStates.waiting_for_ban_id)
async def process_ban_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    if text.lower() in ["cancel", "back"] or text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Ban/Unban action cancelled.")

    try:
        target = int(text)
    except Exception:
        return await message.answer("Invalid ID. Send a numeric User ID:")

    user = await get_cached_user(target)
    if not user:
        return await message.answer("User not found in database. Try another ID:")

    new_status = not bool(user.get("is_banned"))
    await db.set_ban_status(target, new_status)
    invalidate_user_cache(target)
    label = f'BANNED {EMOJI_BAN}' if new_status else f'UNBANNED {EMOJI_TICK}'
    await message.answer(f"User <code>{target}</code> is now <b>{label}</b>.", parse_mode="HTML")
    await state.clear()


# --- Step-by-Step Revoke Access ---
@router.message(F.text == "❌ Unapprove / Revoke Access")
async def admin_revoke_access_req(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Send the <b>User ID</b> to revoke access (or send cancel):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_revoke_id)


@router.message(BotStates.waiting_for_revoke_id)
async def process_unapprove_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    if text.lower() in ["cancel", "back"] or text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Unapprove action cancelled.")

    try:
        target = int(text)
    except Exception:
        return await message.answer("Invalid ID. Send a numeric User ID:")

    await db.set_approval_status(target, False)
    invalidate_user_cache(target)
    await message.answer(f'{EMOJI_LOCK} Access revoked for user <code>{target}</code>.', parse_mode="HTML")
    bot = get_bot_instance()
    if bot:
        try:
            await bot.send_message(target, f'{EMOJI_WARN} Your bot access has been revoked by the Admin.', reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
        except Exception:
            pass
    await state.clear()


# --- Step-by-Step Extend Subscription ---
@router.message(F.text == "⏳ Extend User Subscription")
async def admin_extend_user_req(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Send the <b>User ID</b> to extend subscription (or send cancel):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_extend_id)


@router.message(BotStates.waiting_for_extend_id)
async def process_extend_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    if text.lower() in ["cancel", "back"] or text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Extend action cancelled.")

    try:
        target = int(text)
    except Exception:
        return await message.answer("Invalid ID. Send a numeric User ID:")

    user = await get_cached_user(target)
    if not user:
        return await message.answer("User not found. Try another ID:")

    await state.update_data(extend_target_id=target)
    await message.answer(f"User <code>{target}</code> selected.\nNow send the number of <b>days</b> to add (e.g. 1, 3, 7, 30):", parse_mode="HTML")
    await state.set_state(BotStates.waiting_for_extend_days)


@router.message(BotStates.waiting_for_extend_days)
async def process_extend_days(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    text = message.text.strip()
    if text.lower() in ["cancel", "back"] or text in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Extend action cancelled.")

    try:
        days = int(text)
        if days <= 0:
            raise ValueError
    except Exception:
        return await message.answer("Enter a positive number of days:")

    data = await state.get_data()
    target = data.get("extend_target_id")
    await state.clear()

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        new_exp = await db.extend_user_subscription(target, days)
        invalidate_user_cache(target)
        if new_exp:
            await message.answer(f'{EMOJI_TICK} Subscription extended by {days} days for <code>{target}</code>!\nNew Expiry: <code>{new_exp}</code>', parse_mode="HTML")
            bot = get_bot_instance()
            if bot:
                try:
                    await bot.send_message(
                        target,
                        f'{EMOJI_TICK} <b>Subscription Extended!</b>\n\nYour access has been extended by {days} days.\nValid until: <code>{new_exp}</code>',
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
        else:
            await message.answer("Failed to extend subscription.")


# --- Broadcast ---
@router.message(F.text == "📢 Broadcast Message")
async def admin_req_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("Send the message to broadcast (or type cancel):")
    await state.set_state(BotStates.waiting_for_broadcast)


@router.message(BotStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text.strip().lower() in ["cancel", "back"] or message.text.strip() in MENU_BUTTONS:
        await state.clear()
        return await message.answer("Broadcast cancelled.")

    async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
        users = await db.get_all_users()
        sent = 0
        for uid in users:
            try:
                await message.bot.send_message(uid, f'{EMOJI_PLANE} <b>Announcement:</b>\n\n{message.text}', parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        await message.answer(f'{EMOJI_TICK} Broadcast sent to {sent} users.', parse_mode="HTML")
        await state.clear()


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()
