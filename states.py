from aiogram.fsm.state import State, StatesGroup

class BotStates(StatesGroup):
    waiting_for_api_key = State()
    waiting_for_bulk_amount = State()
    waiting_for_broadcast = State()
    waiting_for_ban_id = State()
