import asyncio
import logging
import time
from typing import Dict, Any, Optional

import database as db
from api_client import HeroSMSClient, check_telegram_numbers
import keyboards as kb

logger = logging.getLogger("SniperEngine")


# ==========================================================
# 1. SUB-50MS DIRECT IN-MEMORY CACHING ENGINE
# ==========================================================
class MemoryCacheEngine:
    """
    Sub-50ms RAM Hashmap Cache:
    Database disk I/O bypass kore memory theke instant lookup kore.
    """
    def __init__(self):
        self._activations: Dict[str, Dict[str, Any]] = {}
        self._phone_map: Dict[str, str] = {}
        self._user_active_snipers: Dict[int, bool] = {}

    def set_activation(self, aid: str, user_id: int, phone: str, cost: str = "", operator: str = ""):
        clean_p = str(phone).lstrip("+").strip()
        data = {
            "aid": str(aid),
            "user_id": user_id,
            "phone": clean_p,
            "cost": cost,
            "operator": operator,
            "created_at": time.time()
        }
        self._activations[str(aid)] = data
        self._phone_map[clean_p] = str(aid)

    def get_activation_by_aid(self, aid: str) -> Optional[Dict[str, Any]]:
        return self._activations.get(str(aid))

    def get_activation_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        clean_p = str(phone).lstrip("+").strip()
        aid = self._phone_map.get(clean_p)
        if aid:
            return self._activations.get(aid)
        return None

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
        expired_aids = [
            aid for aid, item in self._activations.items()
            if now - item.get("created_at", 0) > max_age_seconds
        ]
        for aid in expired_aids:
            self.remove_activation(aid)


memory_cache = MemoryCacheEngine()


# ==========================================================
# 2. ASYNC PIPELINE QUEUE ARCHITECTURE (WORKER POOL)
# ==========================================================
class AsyncSniperPipeline:
    """
    3-Stage Decoupled Pipeline:
    Stage 1: Hunt Worker -> Fast direct buy attempts
    Stage 2: Verify Worker -> Telegram freshness checking (independent)
    Stage 3: Dispatch Worker -> Loud 3x alerts & memory registration
    """
    def __init__(self):
        self.hunt_queue: asyncio.Queue = asyncio.Queue()
        self.verify_queue: asyncio.Queue = asyncio.Queue()
        self.dispatch_queue: asyncio.Queue = asyncio.Queue()

        self._workers_running = False
        self._tasks = []

    def start_pipeline(self):
        if not self._workers_running:
            self._workers_running = True
            self._tasks = [
                asyncio.create_task(self._hunt_worker(), name="pipeline_hunter"),
                asyncio.create_task(self._verify_worker(), name="pipeline_verifier"),
                asyncio.create_task(self._dispatch_worker(), name="pipeline_dispatcher"),
            ]
            logger.info("Sniper Pipeline Worker Pool started.")

    async def stop_pipeline(self):
        self._workers_running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Sniper Pipeline Worker Pool stopped.")

    # --- Task Submission ---
    async def submit_hunt_job(self, user_id: int, client: HeroSMSClient, target_op: str, max_price: float, exc_prefixes: str, status_msg: Any):
        memory_cache.set_user_sniper_state(user_id, True)
        job = {
            "user_id": user_id,
            "client": client,
            "target_op": target_op.strip().lower(),
            "max_price": max_price,
            "exc_prefixes": exc_prefixes,
            "status_msg": status_msg,
            "start_time": time.time(),
            "attempt": 0
        }
        await self.hunt_queue.put(job)

    # ------------------------------------------------------
    # STAGE 1: HUNT WORKER (Continuous Non-Blocking Buy)
    # ------------------------------------------------------
    async def _hunt_worker(self):
        while self._workers_running:
            try:
                job = await self.hunt_queue.get()
                user_id = job["user_id"]

                # User cancelled check
                if not memory_cache.is_user_sniper_active(user_id):
                    self.hunt_queue.task_done()
                    continue

                job["attempt"] += 1
                client = job["client"]

                # Direct API Hit
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

                    # Prefix operator verify
                    from handlers import get_colombia_operator
                    op_detected = get_colombia_operator(phone)

                    # Strict Operator Enforce
                    if job["target_op"] != "any" and op_detected.lower() != job["target_op"]:
                        try:
                            await client.set_status(aid, 8)
                        except Exception:
                            pass
                        await self.hunt_queue.put(job)
                        self.hunt_queue.task_done()
                        await asyncio.sleep(0.4)
                        continue

                    # Sniper job completed for this user
                    memory_cache.set_user_sniper_state(user_id, False)

                    # Stage 2: Handover to Verification Queue
                    job.update({
                        "aid": aid,
                        "phone": phone,
                        "rate_str": rate_str,
                        "operator": op_detected
                    })
                    await self.verify_queue.put(job)

                else:
                    # 429 backoff
                    if isinstance(res, str) and "429" in res:
                        await asyncio.sleep(6.0)

                    # Update UI Attempt counter every 2 attempts
                    if job["attempt"] % 2 == 0:
                        try:
                            await job["status_msg"].edit_text(
                                f"🎯 <b>Sniper Active: {job['target_op'].upper()}</b> (Max: ${job['max_price']:.3f})\n"
                                f"🔍 Searching & attempting to grab... (Attempt #{job['attempt']})",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass

                    # Re-queue job for next attempt
                    await asyncio.sleep(2.2)
                    await self.hunt_queue.put(job)

                self.hunt_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in hunt_worker: {e}")
                await asyncio.sleep(2.0)

    # ------------------------------------------------------
    # STAGE 2: VERIFICATION WORKER (Async Checker)
    # ------------------------------------------------------
    async def _verify_worker(self):
        while self._workers_running:
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

                # Check Telegram freshness asynchronously
                try:
                    check_results = await asyncio.wait_for(check_telegram_numbers([phone]), timeout=20.0)
                except Exception:
                    check_results = {}

                from handlers import format_tg_status
                formatted_k = f"+{phone}"
                raw_st = check_results.get(formatted_k) or check_results.get(phone)
                tg_info = format_tg_status(raw_st)

                item["tg_info"] = tg_info

                # Stage 3: Handover to Dispatcher Queue
                await self.dispatch_queue.put(item)
                self.verify_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in verify_worker: {e}")
                await asyncio.sleep(1.0)

    # ------------------------------------------------------
    # STAGE 3: DISPATCHER WORKER (Loud 3x Alerts & DB Sync)
    # ------------------------------------------------------
    async def _dispatch_worker(self):
        while self._workers_running:
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

                # 1. Direct RAM Cache Registration (Sub-50ms)
                memory_cache.set_activation(aid, user_id, phone, rate_str, op_name)

                # 2. Disk Persistence (Non-blocking)
                await db.save_activation(aid, user_id, phone)
                await db.set_setting("sniper_active", "0")

                # 3. Handle Bad vs Fresh numbers
                if tg_info["is_fresh"]:
                    verdict = "🟢 <b>FRESH NUMBER!</b> Waiting for OTP..."
                else:
                    verdict = f"🔻 <b>{tg_info['badge']}</b> (<i>Auto-cancelling for refund in 2 mins...</i>)"
                    from handlers import auto_cancel_bad_numbers
                    bad_list = [{"aid": aid, "phone": phone}]
                    asyncio.create_task(auto_cancel_bad_numbers(client, bad_list))

                # 4. Final Display Box
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

                # 5. Loud 3x Sound Alerts (Screen Lock Wake-up)
                try:
                    bot = status_msg.bot
                    # Alert 1
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

                    # Alert 2
                    await bot.send_message(
                        user_id,
                        f"⚡ <b>TAP TO COPY (2/3):</b>\n\n<code>+{phone}</code>",
                        parse_mode="HTML",
                        disable_notification=False
                    )
                    await asyncio.sleep(0.3)

                    # Alert 3
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
                logger.error(f"Error in dispatch_worker: {e}")
                await asyncio.sleep(1.0)


pipeline_engine = AsyncSniperPipeline()
