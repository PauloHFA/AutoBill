"""Inline and reply keyboard builders for Telegram bot."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


def get_main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Main menu reply keyboard."""
    keyboard = [
        [KeyboardButton("📋 Boletos"), KeyboardButton("⏳ Pendentes")],
        [KeyboardButton("🔔 Alertas"), KeyboardButton("⚙️ Config")],
        [KeyboardButton("❓ Ajuda")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)


def get_review_keyboard(bill_id: int) -> InlineKeyboardMarkup:
    """Inline keyboard for bill review."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Aprovar", callback_data=f"review_approve:{bill_id}"),
            InlineKeyboardButton("❌ Rejeitar", callback_data=f"review_reject:{bill_id}"),
        ],
        [
            InlineKeyboardButton("✏️ Editar", callback_data=f"review_edit:{bill_id}"),
            InlineKeyboardButton("🔍 Ver Detalhes", callback_data=f"review_details:{bill_id}"),
        ],
        [
            InlineKeyboardButton("⏭ Pular", callback_data=f"review_skip:{bill_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_edit_field_keyboard(bill_id: int, fields: list[str]) -> InlineKeyboardMarkup:
    """Inline keyboard for selecting field to edit."""
    keyboard = []
    row = []
    for i, field in enumerate(fields):
        row.append(InlineKeyboardButton(field, callback_data=f"edit_field:{bill_id}:{field}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("✅ Concluído", callback_data=f"edit_done:{bill_id}")])
    keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data=f"edit_cancel:{bill_id}")])

    return InlineKeyboardMarkup(keyboard)


def get_confirm_keyboard(action: str, bill_id: int) -> InlineKeyboardMarkup:
    """Confirmation keyboard for destructive actions."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Sim", callback_data=f"confirm_{action}:{bill_id}"),
            InlineKeyboardButton("❌ Não", callback_data=f"cancel_{action}:{bill_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_alert_config_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for alert configuration."""
    keyboard = [
        [
            InlineKeyboardButton("🕐 Horário", callback_data="alert_config:time"),
            InlineKeyboardButton("📅 Antecedência", callback_data="alert_config:lead_days"),
        ],
        [
            InlineKeyboardButton("🔕 Silenciar", callback_data="alert_config:mute"),
            InlineKeyboardButton("🔊 Ativar", callback_data="alert_config:unmute"),
        ],
        [
            InlineKeyboardButton("✅ Concluído", callback_data="alert_config:done"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_snooze_keyboard(bill_id: int) -> InlineKeyboardMarkup:
    """Keyboard for snooze options."""
    keyboard = [
        [
            InlineKeyboardButton("1 dia", callback_data=f"snooze:{bill_id}:1"),
            InlineKeyboardButton("3 dias", callback_data=f"snooze:{bill_id}:3"),
            InlineKeyboardButton("7 dias", callback_data=f"snooze:{bill_id}:7"),
        ],
        [
            InlineKeyboardButton("📅 Personalizado", callback_data=f"snooze_custom:{bill_id}"),
            InlineKeyboardButton("❌ Cancelar", callback_data=f"snooze_cancel:{bill_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_bills_filter_keyboard(current_filter: str = "all") -> InlineKeyboardMarkup:
    """Keyboard for filtering bills list."""
    filters = [
        ("all", "📋 Todos"),
        ("pending", "⏳ Pendentes"),
        ("approved", "✅ Aprovados"),
        ("rejected", "❌ Rejeitados"),
        ("paid", "💰 Pagos"),
        ("overdue", "⚠️ Vencidos"),
    ]

    keyboard = []
    row = []
    for filter_key, label in filters:
        prefix = "✓ " if filter_key == current_filter else ""
        row.append(InlineKeyboardButton(f"{prefix}{label}", callback_data=f"filter:{filter_key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    return InlineKeyboardMarkup(keyboard)


def get_pagination_keyboard(
    current_page: int,
    total_pages: int,
    base_callback: str,
) -> InlineKeyboardMarkup:
    """Pagination keyboard for lists."""
    if total_pages <= 1:
        return InlineKeyboardMarkup([])

    keyboard = []
    row = []

    if current_page > 1:
        row.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"{base_callback}:{current_page - 1}"))

    row.append(InlineKeyboardButton(f"{current_page}/{total_pages}", callback_data="noop"))

    if current_page < total_pages:
        row.append(InlineKeyboardButton("Próximo ➡️", callback_data=f"{base_callback}:{current_page + 1}"))

    keyboard.append(row)
    return InlineKeyboardMarkup(keyboard)


def get_config_keyboard() -> InlineKeyboardMarkup:
    """Keyboard for configuration options."""
    keyboard = [
        [
            InlineKeyboardButton("🕐 Horário Alerta", callback_data="config:alert_time"),
            InlineKeyboardButton("📅 Dias Antecedência", callback_data="config:lead_days"),
        ],
        [
            InlineKeyboardButton("🌍 Fuso Horário", callback_data="config:timezone"),
            InlineKeyboardButton("🔧 Modo Extração", callback_data="config:extraction_mode"),
        ],
        [
            InlineKeyboardButton("🤖 Modelo LLM", callback_data="config:llm_model"),
            InlineKeyboardButton("☁️ Consentimento Nuvem", callback_data="config:cloud_consent"),
        ],
        [
            InlineKeyboardButton("✅ Concluído", callback_data="config:done"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_back_keyboard(callback_data: str = "back:main") -> InlineKeyboardMarkup:
    """Simple back button keyboard."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Voltar", callback_data=callback_data)],
    ])
