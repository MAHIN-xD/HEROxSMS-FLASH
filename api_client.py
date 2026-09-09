import aiohttp
import json
import logging

BASE_URL = "https://hero-sms.com/stubs/handler_api.php"

HEADERS = {
    "User-Agent": "HeroSMSBot/2.0",
    "Accept": "application/json, text/plain, */*",
}

class HeroSMSClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip().strip("\"'").strip() if api_key else ""

    async def _get(self, action: str, **kwargs):
        params = {"api_key": self.api_key, "action": action}
        params.update(kwargs)
        try:
            async with aiohttp.ClientSession(headers=HEADERS) as session:
                async with session.get(BASE_URL, params=params, timeout=15) as response:
                    text = await response.text()
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return text
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
                except ValueError:
                    return None
            try:
                return float(res_clean)
            except ValueError:
                pass
        elif isinstance(res, dict) and "balance" in res:
            try:
                return float(res["balance"])
            except (ValueError, TypeError):
                pass
        return None

    async def buy_colombia_telegram_number(self, max_price: float = 0.135):
        """কোলম্বিয়ার Telegram নম্বরের জন্য সরাসরি API কল"""
        params = {
            "service": "tg",
            "country": 33, # Colombia ID
            "maxPrice": max_price
        }
        return await self._get("getNumberV2", **params)

    async def set_status(self, activation_id: str, status: int):
        # status 6 = Complete, status 8 = Cancel
        return await self._get("setStatus", id=str(activation_id), status=status)
