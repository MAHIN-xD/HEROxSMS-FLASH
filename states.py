from aiogram.fsm.state import State, StatesGroup

class BotStates(StatesGroup):
    waiting_for_api_key = State()
    waiting_for_bulk_amount = State()
    waiting_for_broadcast = State()
    waiting_for_ban_id = State()
    waiting_for_add_id = State()
    waiting_for_revoke_id = State()
    waiting_for_max_price = State()
    waiting_for_operator = State()
    waiting_for_exclude = State()
    waiting_for_unexclude = State()
    waiting_for_retry = State()
    waiting_for_cancel = State()
    waiting_for_sniper_operator = State()
