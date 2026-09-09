import aiohttp
import json
import logging

BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

class HeroSMSClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip().strip("\"'").strip() if api_key else ""

    async def _get(self, action: str, **kwargs):
        params = {"api_key": self.api_key, "action": action}
        params.update(kwargs)
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(headers=HEADERS, timeout=timeout) as session:
                async with session.get(BASE_URL, params=params) as response:
                    text = await response.text()
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text.strip()
        except Exception as e:
            logging.error(f"API Error ({action}): {e}")
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

    async def get_number(self, service: str, country: int, max_price: float = None):
        params = {"service": service, "country": country}
        if max_price: params["maxPrice"] = max_price
        res = await self._get("getNumberV2", **params)

        # যদি API টেক্সট রেসপন্স পাঠায় (ACCESS_NUMBER:id:number)
        if isinstance(res, str) and res.startswith("ACCESS_NUMBER"):
            parts = res.split(":")
            if len(parts) >= 3:
                return {
                    "status": "SUCCESS",
                    "activationId": parts[1],
                    "phoneNumber": parts[2]
                }
        
        # যদি ডিকশনারি রেসপন্স পাঠায়
        if isinstance(res, dict) and "activationId" in res:
            res["status"] = "SUCCESS"
            return res

        return res

    async def buy_colombia_telegram_number(self, max_price: float = 0.135):
        """handlers.py এর সাথে সামঞ্জস্য রাখার জন্য শর্টকাট মেথড"""
        return await self.get_number(service="tg", country=33, max_price=max_price)

    async def get_status(self, activation_id: str):
        return await self._get("getStatus", id=str(activation_id))

    async def set_status(self, activation_id: str, status: int):
        return await self._get("setStatus", id=str(activation_id), status=status)

    async def get_active_activations(self):
        return await self._get("getActiveActivations")
