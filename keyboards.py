from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

def make_green_button(text: str, callback_data: str = None, copy_text = None, url: str = None) -> InlineKeyboardButton:
    """Telegram inline button-e style='success' bosiye green color ensure kore"""
    kwargs = {"text": text, "style": "success"}
    if callback_data:
        kwargs["callback_data"] = callback_data
    if copy_text:
        kwargs["copy_text"] = copy_text
    if url:
        kwargs["url"] = url
    try:
        return InlineKeyboardButton(**kwargs)
    except TypeError:
        # Fallback jodi platform runtime pydantic model strictly filter kore
        kwargs.pop("style", None)
        btn = InlineKeyboardButton(**kwargs)
        setattr(btn, "style", "success")
        return btn

def main_reply_menu() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="Bulk Buy Numbers")
    b.button(text="Active Numbers")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)

def back_button(callback_data: str = "menu_main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(make_green_button(text="Back", callback_data=callback_data))
    return b.as_markup()

def number_action_menu(activation_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(make_green_button(text="Check SMS", callback_data=f"check_{activation_id}"))
    b.row(make_green_button(text="Cancel", callback_data=f"single_cancel_{activation_id}"))
    return b.as_markup()

def active_numbers_menu(activations: list, page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    start_idx = page * per_page
    end_idx = start_idx + per_page
    current_page_items = activations[start_idx:end_idx]

    for act in current_page_items:
        aid = str(act.get("activationId", ""))
        phone = str(act.get("phoneNumber", "Unknown"))
        if aid:
            b.row(make_green_button(text=f"Cancel +{phone}", callback_data=f"active_cancel_{aid}_{page}"))

    nav_buttons = []
    total_pages = (len(activations) + per_page - 1) // per_page
    
    if page > 0:
        nav_buttons.append(make_green_button(text="⬅️ Prev", callback_data=f"act_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(make_green_button(text="Next ➡️", callback_data=f"act_page_{page+1}"))
    
    if nav_buttons:
        b.row(*nav_buttons)

    b.row(make_green_button(text="Cancel All", callback_data="cancel_all_active"))
    b.row(make_green_button(text="Back", callback_data="menu_main"))
    return b.as_markup()

def otp_copy_menu(otp_code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    code_str = str(otp_code).strip()
    try:
        from aiogram.types import CopyTextButton
        b.row(make_green_button(text=code_str, copy_text=CopyTextButton(text=code_str)))
    except ImportError:
        b.row(make_green_button(text=code_str, callback_data="noop"))
    return b.as_markup()

def bulk_result_menu(batch_id: str, fresh_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if fresh_count > 0:
        b.row(make_green_button(text=f"🟢 View Fresh Numbers ({fresh_count})", callback_data=f"show_fresh_{batch_id}"))
    return b.as_markup()

def fresh_numbers_menu(fresh_phones: list, batch_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, phone in enumerate(fresh_phones, 1):
        clean = str(phone).lstrip("+").strip()
        btn_text = f"{idx}. {clean} 🟢"
        try:
            from aiogram.types import CopyTextButton
            b.row(make_green_button(text=btn_text, copy_text=CopyTextButton(text=f"+{clean}")))
        except ImportError:
            b.row(make_green_button(text=btn_text, callback_data="noop"))

    b.row(make_green_button(text="🔙 Back", callback_data=f"back_bulk_{batch_id}"))
    return b.as_markup()

def admin_menu(maintenance: bool) -> InlineKeyboardMarkup:
    status = "ON" if maintenance else "OFF"
    b = InlineKeyboardBuilder()
    b.row(make_green_button(text="Broadcast", callback_data="admin_broadcast"))
    b.row(make_green_button(text="Ban / Unban User", callback_data="admin_ban"))
    b.row(make_green_button(text=f"Maintenance: {status}", callback_data="admin_maintenance"))
    b.row(make_green_button(text="Exit", callback_data="menu_main"))
    return b.as_markup()
