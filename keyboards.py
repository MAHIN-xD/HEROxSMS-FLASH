from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

def main_reply_menu() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.button(text="Bulk Buy Numbers", style="success")
    b.button(text="Finish", style="danger")
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)

def back_button(callback_data: str = "tools_page_1") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Back", callback_data=callback_data, style="primary")
    return b.as_markup()

def number_action_menu(activation_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Check SMS", callback_data=f"check_{activation_id}", style="success")
    b.button(text="Cancel", callback_data=f"single_cancel_{activation_id}", style="danger")
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
            b.button(text=f"Cancel +{phone}", callback_data=f"active_cancel_{aid}_{page}", style="danger")

    b.adjust(1)

    nav_buttons = []
    total_pages = (len(activations) + per_page - 1) // per_page
    
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"act_page_{page-1}", style="primary"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"act_page_{page+1}", style="primary"))
    
    if nav_buttons:
        b.row(*nav_buttons)

    b.row(InlineKeyboardButton(text="Cancel All", callback_data="cancel_all_active", style="danger"))
    b.row(InlineKeyboardButton(text="Back to Tools", callback_data="tools_page_1", style="primary"))
    return b.as_markup()

def otp_copy_menu(otp_code: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    code_str = str(otp_code).strip()
    try:
        from aiogram.types import CopyTextButton
        b.row(InlineKeyboardButton(text=code_str, copy_text=CopyTextButton(text=code_str), style="success"))
    except ImportError:
        b.button(text=code_str, callback_data="noop", style="success")
    return b.as_markup()

def bulk_result_menu(batch_id: str, fresh_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if fresh_count > 0:
        b.button(text=f"🟢 View Fresh Numbers ({fresh_count})", callback_data=f"show_fresh_{batch_id}", style="success")
    b.adjust(1)
    return b.as_markup()

def fresh_numbers_menu(fresh_phones: list, batch_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for idx, phone in enumerate(fresh_phones, 1):
        clean = str(phone).lstrip("+").strip()
        btn_text = f"{idx}. {clean} 🟢"
        try:
            from aiogram.types import CopyTextButton
            b.row(InlineKeyboardButton(text=btn_text, copy_text=CopyTextButton(text=f"+{clean}"), style="success"))
        except ImportError:
            b.button(text=btn_text, callback_data="noop", style="success")

    b.row(InlineKeyboardButton(text="🔙 Back", callback_data=f"back_bulk_{batch_id}", style="primary"))
    return b.as_markup()

def api_key_view_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Change API Key", callback_data="tool_change_api_key", style="primary")
    b.button(text="Back", callback_data="tools_page_2", style="primary")
    b.adjust(1)
    return b.as_markup()

def api_key_cancel_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Cancel", callback_data="tool_cancel_api_change", style="danger")
    b.adjust(1)
    return b.as_markup()

def sniper_alert_menu(operator: str) -> InlineKeyboardMarkup:
    """Claro ba specific operator paowa gele instant bulk buy buttons"""
    b = InlineKeyboardBuilder()
    b.button(text="Buy 5 Pcs", callback_data=f"snipe_buy_{operator}_5", style="success")
    b.button(text="Buy 10 Pcs", callback_data=f"snipe_buy_{operator}_10", style="success")
    b.button(text="Buy 20 Pcs", callback_data=f"snipe_buy_{operator}_20", style="success")
    b.button(text="Buy 50 Pcs", callback_data=f"snipe_buy_{operator}_50", style="success")
    b.button(text="🛑 Stop Sniper", callback_data="tool_toggle_sniper", style="danger")
    b.adjust(2, 2, 1)
    return b.as_markup()

def tools_menu_page_1(sniper_active: bool, sniper_op: str = "") -> InlineKeyboardMarkup:
    """Page 1: Sniper Shobar Prothome, Tar Niche Balance, Erpor Operators & Excludes"""
    b = InlineKeyboardBuilder()
    
    sniper_text = f"🎯 Sniper: ON ({sniper_op.upper()})" if sniper_active else "🎯 Auto Sniper Monitor: OFF"
    
    # Row 1: Sniper Shobar Age
    b.button(text=sniper_text, callback_data="tool_toggle_sniper", style="success" if sniper_active else "primary")
    
    # Row 2: Sniper er nichei Balance & Active Numbers
    b.button(text="💰 Check Balance", callback_data="tool_balance", style="success")
    b.button(text="📱 Active Numbers", callback_data="tool_active_numbers", style="primary")
    
    # Row 3: Max Price & Set Operator
    b.button(text="💵 Set Max Price", callback_data="tool_set_max_price", style="primary")
    b.button(text="📡 Set Operator", callback_data="tool_set_operator", style="primary")
    
    # Row 4: Operator List & Reset
    b.button(text="📋 Operator List", callback_data="tool_operator_list", style="primary")
    b.button(text="🔄 Reset Operator", callback_data="tool_reset_operator", style="danger")
    
    # Row 5: Exclude & Unexclude
    b.button(text="➕ Exclude Prefix", callback_data="tool_exclude", style="primary")
    b.button(text="➖ Unexclude Prefix", callback_data="tool_unexclude", style="danger")
    
    # Row 6: Exclude List & Reset Exclude
    b.button(text="📜 Exclude List", callback_data="tool_exclude_list", style="primary")
    b.button(text="🔄 Reset Exclude", callback_data="tool_reset_exclude", style="danger")
    
    # Row 7: Retry Number
    b.button(text="🔁 Retry Number", callback_data="tool_retry", style="primary")
    
    # Row 8: Navigation
    b.button(text="➡️ Next Page", callback_data="tools_page_2", style="primary")
    b.button(text="🔙 Back to Main Menu", callback_data="menu_main", style="primary")
    
    b.adjust(1, 2, 2, 2, 2, 2, 1, 2)
    return b.as_markup()

def tools_menu_page_2(maintenance: bool) -> InlineKeyboardMarkup:
    """Page 2: API Key, Stats, Single Cancel & Admin Management"""
    maint_status = "ON" if maintenance else "OFF"
    b = InlineKeyboardBuilder()
    
    b.button(text="🔑 View / Change API Key", callback_data="tool_view_api_key", style="primary")
    b.button(text="📊 HeroSMS Stats", callback_data="tool_stats", style="primary")
    
    b.button(text="❌ Cancel Number", callback_data="tool_cancel_number", style="danger")
    b.button(text="👥 Add User", callback_data="tool_add_user", style="success")
    
    b.button(text="❌ Revoke User", callback_data="tool_revoke_user", style="danger")
    b.button(text="🚫 Ban / Unban User", callback_data="admin_ban", style="danger")
    
    b.button(text="📢 Broadcast Message", callback_data="admin_broadcast", style="primary")
    b.button(text=f"⚙️ Maintenance: {maint_status}", callback_data="admin_maintenance", style="danger" if maintenance else "success")
    
    b.button(text="⬅️ Previous Page", callback_data="tools_page_1", style="primary")
    
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()
