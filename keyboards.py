from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

def main_reply_menu() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="Bulk Buy Numbers")
    b.button(text="Active Numbers")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)

def back_button(callback_data: str = "menu_main") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Back", callback_data=callback_data)
    return b.as_markup()

def number_action_menu(activation_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Check SMS", callback_data=f"check_{activation_id}")
    b.button(text="Cancel", callback_data=f"single_cancel_{activation_id}")
    b.adjust(1)
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
            b.button(text=f"Cancel +{phone}", callback_data=f"active_cancel_{aid}_{page}")

    b.adjust(1)

    nav_buttons = []
    total_pages = (len(activations) + per_page - 1) // per_page
    
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"act_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"act_page_{page+1}"))
    
    if nav_buttons:
        b.row(*nav_buttons)

    b.row(InlineKeyboardButton(text="Cancel All", callback_data="cancel_all_active"))
    b.row(InlineKeyboardButton(text="Back", callback_data="menu_main"))
    return b.as_markup()

def otp_copy_menu(otp_code: str) -> InlineKeyboardMarkup:
    """ওটিপি কোড বাটনে সরাসরি প্রিন্ট থাকবে এবং ক্লিক করলেই ইনস্ট্যান্ট কপি হবে"""
    b = InlineKeyboardBuilder()
    try:
        from aiogram.types import CopyTextButton
        b.row(InlineKeyboardButton(text=f"• Copy {otp_code} •", copy_text=CopyTextButton(text=str(otp_code))))
    except ImportError:
        b.button(text=f"• Copy {otp_code} •", callback_data="noop")
    return b.as_markup()

def bulk_result_menu(batch_id: str, fresh_count: int) -> InlineKeyboardMarkup:
    """বাল্ক বাই শেষে ফ্রেশ নম্বর বাটন আকারে দেখার মেনু"""
    b = InlineKeyboardBuilder()
    if fresh_count > 0:
        b.button(text=f"🟢 View Fresh Numbers ({fresh_count})", callback_data=f"show_fresh_{batch_id}")
    b.adjust(1)
    return b.as_markup()

def fresh_numbers_menu(fresh_phones: list, batch_id: str) -> InlineKeyboardMarkup:
    """৩য় ছবির মতো প্রতিটি ফ্রেশ নম্বর আলাদা বাটন আকারে থাকবে এবং ট্যাপ করলেই কপি হবে"""
    b = InlineKeyboardBuilder()
    for idx, phone in enumerate(fresh_phones, 1):
        clean = str(phone).lstrip("+").strip()
        btn_text = f"{idx}. {clean} 🟢"
        try:
            from aiogram.types import CopyTextButton
            b.row(InlineKeyboardButton(text=btn_text, copy_text=CopyTextButton(text=f"+{clean}")))
        except ImportError:
            b.button(text=btn_text, callback_data="noop")

    b.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"back_bulk_{batch_id}"))
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
