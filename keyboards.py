from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

def main_reply_menu() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="Bulk Buy Numbers")
    b.button(text="Finish")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)

def back_button(callback_data: str = "tools_page_1") -> InlineKeyboardMarkup:
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
    b.row(InlineKeyboardButton(text="Back to Tools", callback_data="tools_page_1"))
    return b.as_markup()

def otp_copy_menu(otp_code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    code_str = str(otp_code).strip()
    try:
        from aiogram.types import CopyTextButton
        b.row(InlineKeyboardButton(text=code_str, copy_text=CopyTextButton(text=code_str)))
    except ImportError:
        b.button(text=code_str, callback_data="noop")
    return b.as_markup()

def bulk_result_menu(batch_id: str, fresh_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if fresh_count > 0:
        b.button(text=f"🟢 View Fresh Numbers ({fresh_count})", callback_data=f"show_fresh_{batch_id}")
    b.adjust(1)
    return b.as_markup()

def fresh_numbers_menu(fresh_phones: list, batch_id: str) -> InlineKeyboardMarkup:
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

def api_key_view_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Change API Key", callback_data="tool_change_api_key")
    b.button(text="Back", callback_data="tools_page_2")
    b.adjust(1)
    return b.as_markup()

def api_key_cancel_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data="tool_cancel_api_change")
    b.adjust(1)
    return b.as_markup()

def tools_menu_page_1() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📱 Active Numbers", callback_data="tool_active_numbers")
    b.button(text="💵 Set Max Price", callback_data="tool_set_max_price")
    b.button(text="📡 Set Operator", callback_data="tool_set_operator")
    b.button(text="📋 Operator List", callback_data="tool_operator_list")
    b.button(text="➕ Exclude Prefix", callback_data="tool_exclude")
    b.button(text="➖ Unexclude Prefix", callback_data="tool_unexclude")
    b.button(text="📜 Exclude List", callback_data="tool_exclude_list")
    b.button(text="🔄 Reset Exclude", callback_data="tool_reset_exclude")
    b.button(text="🔄 Reset Operator", callback_data="tool_reset_operator")
    b.button(text="🔁 Retry Number", callback_data="tool_retry")
    b.button(text="➡️ Next Page", callback_data="tools_page_2")
    b.button(text="🔙 Back to Main Menu", callback_data="menu_main")
    b.adjust(2, 2, 2, 2, 2, 2)
    return b.as_markup()

def tools_menu_page_2(maintenance: bool) -> InlineKeyboardMarkup:
    maint_status = "ON" if maintenance else "OFF"
    b = InlineKeyboardBuilder()
    b.button(text="🔑 View / Change API Key", callback_data="tool_view_api_key")
    b.button(text="💰 Check Balance", callback_data="tool_balance")
    b.button(text="📊 HeroSMS Stats", callback_data="tool_stats")
    b.button(text="❌ Cancel Number", callback_data="tool_cancel_number")
    b.button(text="👥 Add User", callback_data="tool_add_user")
    b.button(text="❌ Revoke User", callback_data="tool_revoke_user")
    b.button(text="🚫 Ban / Unban User", callback_data="admin_ban")
    b.button(text="📢 Broadcast Message", callback_data="admin_broadcast")
    b.button(text=f"⚙️️ Maintenance: {maint_status}", callback_data="admin_maintenance")
    b.button(text="⬅️ Previous Page", callback_data="tools_page_1")
    b.adjust(2, 2, 2, 2, 2)
    return b.as_markup()
