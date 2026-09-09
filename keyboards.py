from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

def main_reply_menu() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="Buy Telegram Number")
    b.button(text="Bulk Buy Numbers")
    b.button(text="Active Numbers")
    b.button(text="Balance")
    b.button(text="Profile")
    b.adjust(2, 2, 1)
    return b.as_markup(resize_keyboard=True)

def profile_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Change API Key", callback_data="profile_change_key")
    b.button(text="Back", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()

def back_button(callback_data: str = "menu_main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Back", callback_data=callback_data)
    return b.as_markup()

def confirm_number_menu(country_id, service_code) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Confirm Purchase", callback_data=f"buy_{country_id}_{service_code}")
    b.button(text="Cancel", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()

def number_action_menu(activation_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Check SMS", callback_data=f"check_{activation_id}")
    b.button(text="Cancel", callback_data=f"single_cancel_{activation_id}")
    b.adjust(1)
    return b.as_markup()

def active_numbers_menu(activations: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for act in activations:
        aid = str(act.get("activationId", ""))
        phone = str(act.get("phoneNumber", "Unknown"))
        if aid:
            b.button(text=f"Cancel +{phone}", callback_data=f"active_cancel_{aid}")
    
    b.button(text="Cancel All", callback_data="cancel_all_active")
    b.button(text="Back", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()

def otp_copy_menu(otp_code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    try:
        from aiogram.types import CopyTextButton
        b.row(InlineKeyboardButton(text="Copy Code", copy_text=CopyTextButton(text=otp_code)))
    except ImportError:
        b.button(text="Copy Code", callback_data="noop")
    return b.as_markup()

def admin_menu(maintenance: bool) -> InlineKeyboardMarkup:
    status = "ON" if maintenance else "OFF"
    b = InlineKeyboardBuilder()
    b.button(text="Broadcast", callback_data="admin_broadcast")
    b.button(text="Ban / Unban User", callback_data="admin_ban")
    b.button(text=f"Maintenance: {status}", callback_data="admin_maintenance")
    b.button(text="Exit", callback_data="menu_main")
    b.adjust(1)
    return b.as_markup()
