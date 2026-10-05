import asyncio
import logging
import time
from typing import Dict, Any, Optional

import database as db
from api_client import HeroSMSClient, check_telegram_numbers
import keyboards as kb

logger = logging.getLogger("SniperEngine")

# ==========================================================
# SUB-50MS DIRECT IN-MEMORY CACHE
# ==========================================================
class MemoryCacheEngine:
    def __init__(self):
        self._activations: Dict[str, Dict[str, Any]] = {}
        self._phone_map: Dict[str, str] = {}
        self._user_active_snipers: Dict[int, bool] = {}

    def set_activation(self, aid: str, user_id: int, phone: str, cost: str = "", operator: str = ""):
        clean_p = str(phone).lstrip("+").strip()
        self._activations[str(aid)] = {
            "aid": str(aid),
            "user_id": user_id,
            "phone": clean_p,
            "cost": cost,
            "operator": operator,
            "created_at": time.time()
        }
        self._phone_map[clean_p] = str(aid)

    def get_activation_by_aid(self, aid: str) -> Optional[Dict[str, Any]]:
        return self._activations.get(str(aid))

    def remove_activation(self, aid: str):
        aid_str = str(aid)
        if aid_str in self._activations:
            phone = self._activations[aid_str].get("phone")
            if phone and phone in self._phone_map:
                del self._phone_map[phone]
            del self._activations[aid_str]

    def set_user_sniper_state(self, user_id: int, active: bool):
        self._user_active_snipers[user_id] = active

    def is_user_sniper_active(self, user_id: int) -> bool:
        return self._user_active_snipers.get(user_id, False)

    def prune_expired(self, max_age_seconds: int = 1800):
        now = time.time()
        expired = [aid for aid, item in self._activations.items() if now - item.get("created_at", 0) > max_age_seconds]
        for aid in expired:
            self.remove_activation(aid)

memory_cache = MemoryCacheEngine()

# ==========================================================
# SAFE & CALM ASYNC SNIPER PIPELINE
# ==========================================================
class SafeSniperPipeline:
    def __init__(self):
        self.hunt_queue: asyncio.Queue = asyncio.Queue()
        self.verify_queue: asyncio.Queue = asyncio.Queue()
        self.dispatch_queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._tasks = []

    def start_pipeline(self):
        if not self._running:
            self._running = True
            self._tasks = [
                asyncio.create_task(self._safe_hunt_worker()),
                asyncio.create_task(self._verify_worker()),
                asyncio.create_task(self._dispatch_worker())
            ]
            logger.info("Safe Sniper Pipeline running.")

    async def submit_hunt_job(self, user_id: int, client: HeroSMSClient, target_op: str, max_price: float, exc_prefixes: str, status_msg: Any):
        memory_cache.set_user_sniper_state(user_id, True)
        job = {
            "user_id": user_id,
            "client": client,
            "target_op": target_op.strip().lower(),
            "max_price": max_price,
            "exc_prefixes": exc_prefixes,
            "status_msg": status_msg,
            "attempt": 0
        }
        await self.hunt_queue.put(job)

    async def _safe_hunt_worker(self):
        while self._running:
            try:
                job = await self.hunt_queue.get()
                user_id = job["user_id"]

                if not memory_cache.is_user_sniper_active(user_id):
                    self.hunt_queue.task_done()
                    continue

                job["attempt"] += 1
                client = job["client"]

                res = await client.get_number(
                    service="tg",
                    country=33,
                    max_price=job["max_price"],
                    phone_exception=job["exc_prefixes"] if job["exc_prefixes"] else None,
                    operator=job["target_op"]
                )

                if isinstance(res, dict) and "activationId" in res:
                    aid = str(res["activationId"])
                    phone = str(res.get("phoneNumber", "")).lstrip("+").strip()
                    cost = res.get("cost")
                    rate_str = f"${float(cost):.3f}" if cost is not None else f"${job['max_price']:.3f}"

                    from handlers import get_colombia_operator
                    op_detected = get_colombia_operator(phone)

                    # Strict check: Claro na hole silent cancel & retry
                    if job["target_op"] != "any" and op_detected.lower() != job["target_op"]:
                        try:
                            await client.set_status(aid, 8)
                        except Exception:
                            pass
                        await asyncio.sleep(0.8)
                        await self.hunt_queue.put(job)
                        self.hunt_queue.task_done()
                        continue

                    # Exact match! Auto stop loop
                    memory_cache.set_user_sniper_state(user_id, False)

                    job.update({
                        "aid": aid,
                        "phone": phone,
                        "rate_str": rate_str,
                        "operator": op_detected
                    })
                    await self.verify_queue.put(job)

                else:
                    if isinstance(res, str) and "429" in res:
                        await asyncio.sleep(8.0)

                    if job["attempt"] % 2 == 0:
                        try:
                            await job["status_msg"].edit_text(
                                f"🎯 <b>Sniper Active: {job['target_op'].upper()}</b> (Max: ${job['max_price']:.3f})\n"
                                f"🔍 Searching & attempting to grab... (Attempt #{job['attempt']})",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                    # Gentle safe pacing (3.2 seconds)
                    await asyncio.sleep(3.2)
                    await self.hunt_queue.put(job)

                self.hunt_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in hunt worker: {e}")
                await asyncio.sleep(2.0)

    async def _verify_worker(self):
        while self._running:
            try:
                item = await self.verify_queue.get()
                phone = item["phone"]

                try:
                    await item["status_msg"].edit_text(
                        f"🎯 <b>Number Grabbed!</b>\n"
                        f"🇨🇴 <code>+{phone}</code> ({item['operator'].upper()})\n\n"
                        f"🔍 Checking Telegram status, please wait...",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

                try:
                    check_results = await asyncio.wait_for(check_telegram_numbers([phone]), timeout=20.0)
                except Exception:
                    check_results = {}

                from handlers import format_tg_status
                formatted_k = f"+{phone}"
                raw_st = check_results.get(formatted_k) or check_results.get(phone)
                item["tg_info"] = format_tg_status(raw_st)

                await self.dispatch_queue.put(item)
                self.verify_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in verify worker: {e}")
                await asyncio.sleep(1.0)

    async def _dispatch_worker(self):
        while self._running:
            try:
                item = await self.dispatch_queue.get()
                aid = item["aid"]
                phone = item["phone"]
                user_id = item["user_id"]
                rate_str = item["rate_str"]
                op_name = item["operator"]
                tg_info = item["tg_info"]
                status_msg = item["status_msg"]
                client = item["client"]

                # In-memory instant cache
                memory_cache.set_activation(aid, user_id, phone, rate_str, op_name)

                # Persist DB
                await db.save_activation(aid, user_id, phone)
                await db.set_setting("sniper_active", "0")

                if tg_info["is_fresh"]:
                    verdict = "🟢 <b>FRESH NUMBER!</b> Waiting for OTP..."
                else:
                    verdict = f"🔻 <b>{tg_info['badge']}</b> (<i>Auto-cancelling for refund in 2 mins...</i>)"
                    from handlers import auto_cancel_bad_numbers
                    bad_list = [{"aid": aid, "phone": phone}]
                    asyncio.create_task(auto_cancel_bad_numbers(client, bad_list))

                final_text = (
                    f"🎉 <b>SNIPER RESULT</b>\n\n"
                    f"📡 Operator: <b>{op_name.upper()}</b>\n"
                    f"🇨🇴 Telegram: <code>+{phone}</code>\n"
                    f"💵 Rate: <b>{rate_str}</b>\n"
                    f"🔍 Status: <b>{tg_info['badge']}</b>\n\n"
                    f"{verdict}"
                )

                try:
                    await status_msg.edit_text(
                        final_text,
                        reply_markup=kb.number_action_menu(aid),
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

                # Triple Loud Audible Notification
                try:
                    bot = status_msg.bot
                    # SMS 1
                    await bot.send_message(
                        user_id,
                        f"🚨 <b>SNIPER ALERT (1/3)</b>\n\n"
                        f"📡 Operator: <b>{op_name.upper()}</b>\n"
                        f"🇨🇴 Number: <code>+{phone}</code>\n"
                        f"💵 Rate: <b>{rate_str}</b>\n"
                        f"🔍 Status: <b>{tg_info['badge']}</b>",
                        parse_mode="HTML",
                        disable_notification=False
                    )
                    await asyncio.sleep(0.3)

                    # SMS 2
                    await bot.send_message(
                        user_id,
                        f"⚡ <b>TAP TO COPY (2/3):</b>\n\n<code>+{phone}</code>",
                        parse_mode="HTML",
                        disable_notification=False
                    )
                    await asyncio.sleep(0.3)

                    # SMS 3
                    await bot.send_message(
                        user_id,
                        "⏳ <b>OTP READY (3/3)</b>\n\n"
                        "Telegram-e number boshiye code pathan. OTP asha matroi button shoho show korbe!",
                        parse_mode="HTML",
                        disable_notification=False
                    )
                except Exception as e:
                    logger.error(f"Alert dispatch error: {e}")

                self.dispatch_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in dispatch worker: {e}")
                await asyncio.sleep(1.0)

pipeline_engine = SafeSniperPipeline()
