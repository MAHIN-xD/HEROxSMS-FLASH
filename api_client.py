import aiohttp
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

BASE_URL = "https://hero-sms.com/stubs/handler_api.php"
V1_BASE_URL = "https://hero-sms.com/api/v1"

# Telegram Checker API Credentials
CHECKER_URL = "http://api.agbots.site:8080/check/"
CHECKER_AUTH = "user8354"
CHECKER_API_KEY = "SIGUzg7Xf7euGs8B"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

_session_pool = None

async def get_session() -> aiohttp.ClientSession:
    global _session_pool
    if _session_pool is None or _session_pool.closed:
        connector = aiohttp.TCPConnector(limit=100, keepalive_timeout=60, enable_cleanup_closed=True)
        timeout = aiohttp.ClientTimeout(total=25)
        _session_pool = aiohttp.ClientSession(connector=connector, headers=HEADERS, timeout=timeout)
    return _session_pool

async def _check_single_chunk(session: aiohttp.ClientSession, chunk: list) -> dict:
    payload = {
        "auth": CHECKER_AUTH,
        "api_key": CHECKER_API_KEY,
        "phone_numbers": chunk
    }

    # মেথড ১: aiohttp GET (স্যাম্পল কোডের মতো সরাসরি কল)
    try:
        async with session.get(CHECKER_URL, json=payload, timeout=aiohttp.ClientTimeout(total=35)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if str(data.get("status")) == "200" and "result_obj" in data:
                    return data.get("result_obj") or {}
                elif str(data.get("status")) != "200":
                    err_msg = data.get("msg") or data.get("message") or "API Logic Error"
                    logging.warning(f"Checker API error: {err_msg}")
                    return {num: f"API_ERROR: {err_msg}" for num in chunk}
    except Exception as e:
        logging.warning(f"Checker GET failed: {e}, attempting POST fallback...")

    # মেথড ২: aiohttp POST ফলব্যাক (যদি GET ড্রপ হয়)
    try:
        async with session.post(CHECKER_URL, json=payload, timeout=aiohttp.ClientTimeout(total=35)) as resp:
            if resp.status == 200:
                data = await resp.json(content_type=None)
                if str(data.get("status")) == "200" and "result_obj" in data:
                    return data.get("result_obj") or {}
                elif str(data.get("status")) != "200":
                    err_msg = data.get("msg") or data.get("message") or "API Logic Error"
                    return {num: f"API_ERROR: {err_msg}" for num in chunk}
    except Exception as e:
        logging.error(f"Checker POST failed: {e}")

    # কোনো কারণে নেটওয়ার্ক ফেইল হলে যাতে ব্যান না দেখায়
    return {num: "CHECK_FAILED" for num in chunk}

async def check_telegram_numbers(phone_numbers: list) -> dict:
    if not phone_numbers:
        return {}

    unique_numbers = []
    seen = set()
    for p in phone_numbers:
        clean = re.sub(r'[^\d]', '', str(p))
        if 7 <= len(clean) <= 16:
            formatted = f"+{clean}"
            if formatted not in seen:
                seen.add(formatted)
                unique_numbers.append(formatted)

    if not unique_numbers:
        return {}

    all_results = {}
    chunk_size = 15
    session = await get_session()

    for i in range(0, len(unique_numbers), chunk_size):
        chunk = unique_numbers[i:i + chunk_size]
        res_obj = await _check_single_chunk(session, chunk)
        for k, v in res_obj.items():
            clean_k = re.sub(r'[^\d]', '', str(k))
            all_results[str(k)] = v
            all_results[f"+{clean_k}"] = v
            all_results[clean_k] = v

    return all_results

class HeroSMSClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip().strip("\"'").strip() if api_key else ""

    async def _get(self, action: str, **kwargs):
        params = {"api_key": self.api_key, "action": action}
        params.update(kwargs)
        try:
            session = await get_session()
            async with session.get(BASE_URL, params=params) as response:
                text = await response.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text.strip()
        except Exception as e:
            logging.error(f"API Error ({action}): {e}")
            return None

    async def _get_v1(self, endpoint: str, **kwargs):
        url = f"{V1_BASE_URL}/{endpoint.lstrip('/')}"
        headers = dict(HEADERS)
        headers["Authorization"] = f"ApiKey {self.api_key}"
        try:
            session = await get_session()
            async with session.get(url, params=kwargs, headers=headers) as response:
                text = await response.text()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text.strip()
        except Exception as e:
            logging.error(f"V1 API Error ({endpoint}): {e}")
            return None

    async def get_balance(self):
        res = await self._get("getBalance")
        if isinstance(res, str):
            res_clean = res.strip()
            if res_clean.startswith("ACCESS_BALANCE:"):
                try:
                    return float(res_clean.split(":", 1)[1].strip())
                except:
                    return None
            try:
                return float(res_clean)
            except:
                pass
        elif isinstance(res, dict):
            if "balance" in res:
                try:
                    return float(res["balance"])
                except:
                    pass
            if "data" in res and isinstance(res["data"], dict) and "balance" in res["data"]:
                try:
                    return float(res["data"]["balance"])
                except:
                    pass
        return None

    async def get_prices(self, country: int = None, service: str = None):
        params = {}
        if country: params["country"] = country
        if service: params["service"] = service
        return await self._get("getPrices", **params)

    async def get_operators(self, country: int = None):
        params = {}
        if country: params["country"] = country
        return await self._get("getOperators", **params)

    async def get_number(self, service: str, country: int, max_price: float = None, phone_exception: str = None, operator: str = None):
        params = {"service": service, "country": country}
        if max_price: 
            params["maxPrice"] = max_price
        
        if phone_exception:
            if isinstance(phone_exception, (list, tuple)):
                params["phoneException"] = ",".join(str(p).strip().lstrip("+") for p in phone_exception)
            else:
                params["phoneException"] = str(phone_exception).strip().lstrip("+")

        if operator and str(operator).lower() != "any":
            params["operator"] = str(operator).strip().lower()

        res = await self._get("getNumberV2", **params)

        if isinstance(res, str) and res.startswith("ACCESS_NUMBER"):
            parts = res.split(":")
            if len(parts) >= 3:
                return {
                    "status": "SUCCESS",
                    "activationId": parts[1],
                    "phoneNumber": parts[2]
                }
        
        if isinstance(res, dict) and "activationId" in res:
            res["status"] = "SUCCESS"
            return res

        return res

    async def get_status(self, activation_id: str):
        return await self._get("getStatus", id=str(activation_id))

    async def set_status(self, activation_id: str, status: int):
        return await self._get("setStatus", id=str(activation_id), status=status)

    async def finish_activation(self, activation_id: str):
        return await self._get("setStatus", id=str(activation_id), status=6)

    async def get_active_activations(self):
        return await self._get("getActiveActivations")

    async def get_all_sms(self, activation_id: str):
        return await self._get("getAllSms", id=str(activation_id))

    async def get_history(self, size: int = 10, offset: int = 0, start: int = None, end: int = None):
        params = {"size": size, "offset": offset}
        if start: params["start"] = start
        if end: params["end"] = end
        return await self._get("getHistory", **params)

    async def get_stats(self, date_str: str = None):
        if not date_str:
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return await self._get_v1("activations/stats", date=date_str)

    async def get_activations_history(self, from_date: str, to_date: str, size: int = 15):
        return await self._get_v1("activations/history", **{"from": from_date, "to": to_date, "size": size})
