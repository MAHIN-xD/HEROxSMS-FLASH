from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

def main_reply_menu() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="Bulk Buy Numbers")
    b.button(text="Finish")
    b.button(text="🛠 Tools")
    b.adjust(2, 1)
    return b.as_markup(resize_keyboard=True)

def back_button(callback_data: str = "tools_main") -> InlineKeyboardMarkup:
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
    b.row(InlineKeyboardButton(text="Back to Tools", callback_data="tools_main"))
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

def tools_menu(maintenance: bool, restock: bool) -> InlineKeyboardMarkup:
    maint_status = "ON" if maintenance else "OFF"
    restock_status = "ON" if restock else "OFF"
    b = InlineKeyboardBuilder()
    
    # Row 1: Core Number Ops
    b.button(text="📱 Active Numbers", callback_data="tool_active_numbers")
    b.button(text="📦 Live Stock & Rates", callback_data="tool_live_stock")
    
    # Row 2: Finance & Statistics
    b.button(text="💰 Check Balance", callback_data="tool_balance")
    b.button(text="📊 HeroSMS Stats", callback_data="tool_stats")
    
    # Row 3: Live Settings
    b.button(text="💵 Set Max Price", callback_data="tool_set_max_price")
    b.button(text=f"🔔 Restock Alert: {restock_status}", callback_data="tool_toggle_restock")
    
    # Row 4: Operator Settings
    b.button(text="📡 Set Operator", callback_data="tool_set_operator")
    b.button(text="📋 Operator List", callback_data="tool_operator_list")
    b.button(text="🔄 Reset Operator", callback_data="tool_reset_operator")
    
    # Row 5: Prefix Exclude Settings
    b.button(text="➕ Exclude Prefix", callback_data="tool_exclude")
    b.button(text="➖ Unexclude", callback_data="tool_unexclude")
    b.button(text="📜 Exclude List", callback_data="tool_exclude_list")
    b.button(text="🔄 Reset Exclude", callback_data="tool_reset_exclude")
    
    # Row 6: Single Activations Ops
    b.button(text="🔁 Retry Number", callback_data="tool_retry")
    b.button(text="❌ Cancel Specific", callback_data="tool_cancel_number")
    
    # Row 7: Stealth Access Controls
    b.button(text="👥 Add User", callback_data="tool_add_user")
    b.button(text="❌ Revoke User", callback_data="tool_revoke_user")
    b.button(text="🚫 Ban / Unban", callback_data="admin_ban")
    
    # Row 8: System Controls
    b.button(text="📢 Broadcast", callback_data="admin_broadcast")
    b.button(text=f"⚙️ Maintenance: {maint_status}", callback_data="admin_maintenance")
    b.button(text="🔙 Back to Main Menu", callback_data="menu_main")
    
    b.adjust(2, 2, 2, 3, 4, 2, 3, 2, 1)
    return b.as_markup()
