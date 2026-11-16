"""Command and message handlers for Telegram bot."""

import asyncio
import csv
import hashlib
import io
import logging
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any, NoReturn, cast
from zoneinfo import ZoneInfo

from telegram import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from autobill.bot.formatters import (
    format_bill_details,
    format_bill_list,
    format_bill_summary,
    format_config_status,
    format_error_message,
    format_help_message,
    format_welcome_message,
)
from autobill.bot.keyboards import (
    get_alert_config_keyboard,
    get_back_keyboard,
    get_bills_filter_keyboard,
    get_config_keyboard,
    get_confirm_keyboard,
    get_duplicate_confirm_keyboard,
    get_edit_field_keyboard,
    get_main_menu_keyboard,
    get_pages_grouping_keyboard,
    get_pagination_keyboard,
    get_review_keyboard,
    get_snooze_keyboard,
)
from autobill.bot.middleware import apply_middlewares
from autobill.bot.state import (
    UserState,
    UserStateData,
    state_manager,
)
from autobill.bot.utils import (
    download_file,
    get_actor_label,
    get_chat_id,
    get_user_id,
    is_authorized_user,
    is_image_file,
    is_pdf_file,
    parse_callback_data,
)
from autobill.config import settings
from autobill.db.models import Bill, BillOrigin, BillStatus
from autobill.db.repository import BillRepository
from autobill.extract.pipeline import ExtractionPipeline  # noqa: F401
from autobill.rules.boleto import (
    ARRECADACAO_LENGTH,
    BOLETO_LENGTH,
    CODIGO_BARRAS_LENGTH,
    codigo_barras_para_linha,
    linha_para_codigo_barras,
    parse_linha_digitavel,
    validar_linha_digitavel,
)
from autobill.rules.money import formatar_centavos, parse_valor_br

logger = logging.getLogger(__name__)

# Optional reference to active AlertScheduler for live rescheduling via /config
_active_scheduler: Any | None = None


def set_active_scheduler(scheduler: Any | None) -> None:
    """Register active AlertScheduler instance for live /config updates."""
    global _active_scheduler
    _active_scheduler = scheduler


# Pagination settings
BILLS_PER_PAGE = 10
MIN_CALLBACK_PARTS = 2
MAX_SNOOZE_DAYS = 365


class InvalidInputError(ValueError):
    """Invalid value supplied by a bot user."""


def _raise_invalid_input(message: str) -> NoReturn:
    raise InvalidInputError(message)


def _parse_edited_value(field_name: str, new_value: str) -> str | int:
    if field_name == "valor":
        if "," in new_value or "." in new_value or "r$" in new_value.lower():
            parsed_cents = parse_valor_br(new_value)
            if parsed_cents is None or parsed_cents < 0:
                _raise_invalid_input("Valor inválido (ex: 156,88 ou 15688)")
            return parsed_cents
        value = int(new_value)
        if value < 0:
            _raise_invalid_input("Valor deve ser positivo")
        return value

    if field_name == "vencimento":
        datetime.strptime(new_value, "%d/%m/%Y")
        return new_value

    if field_name in ("linha_digitavel", "codigo_barras"):
        digits = "".join(c for c in new_value if c.isdigit())
        if field_name == "linha_digitavel" and len(digits) not in (
            BOLETO_LENGTH,
            ARRECADACAO_LENGTH,
        ):
            _raise_invalid_input("Linha digitável deve ter 47 ou 48 dígitos")
        if field_name == "linha_digitavel" and not validar_linha_digitavel(digits):
            _raise_invalid_input("Dígito verificador da linha digitável inválido")
        if field_name == "codigo_barras" and len(digits) != CODIGO_BARRAS_LENGTH:
            _raise_invalid_input("Código de barras deve ter 44 dígitos")
        return digits

    return new_value


def _get_message(update: Update) -> Message:
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is None:
        _raise_invalid_input("Update does not contain a message")
    return cast("Message", message)


def _get_message_text(update: Update) -> str:
    text = _get_message(update).text
    if text is None:
        _raise_invalid_input("Message does not contain text")
    return text


async def start_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    if not user:
        return
    if update.message is None:
        return

    if not is_authorized_user(user.id):
        await update.message.reply_text(
            "🚫 <b>Acesso negado</b>\n\nEste bot é privado.",
            parse_mode="HTML",
        )
        return

    # Clear any existing state
    state_manager.clear_state(user.id)

    welcome_text = format_welcome_message(user.first_name or "Usuário")
    await update.message.reply_text(
        welcome_text,
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    # Check for category argument
    args = context.args
    category = args[0] if args else None

    help_text = format_help_message(category)
    await update.message.reply_text(
        help_text,
        parse_mode="HTML",
        reply_markup=get_main_menu_keyboard(),
    )


async def bills_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /bills command - list bills with filters."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    # Parse arguments: filter and page
    args = context.args
    filter_name = "all"
    page = 1

    if args:
        if args[0].isdigit():
            page = int(args[0])
        else:
            filter_name = args[0]
            if len(args) > 1 and args[1].isdigit():
                page = int(args[1])

    # Get bills from repository
    repo = await BillRepository.create_for_bot()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter=filter_name if filter_name != "all" else None,
            page=page,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        text = format_bill_list(bills, page, total_pages, filter_name)
        keyboard = InlineKeyboardMarkup(
            [
                *get_bills_filter_keyboard(filter_name).inline_keyboard,
                *get_pagination_keyboard(page, total_pages, "bills").inline_keyboard,
            ]
        )

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def pending_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pending command - show bills awaiting review."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter="pending",
            page=1,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        if not bills:
            await update.message.reply_text(
                "✅ <b>Nenhum boleto pendente de revisão!</b>\n\n"
                "Envie um PDF ou foto de boleto para começar.",
                parse_mode="HTML",
                reply_markup=get_main_menu_keyboard(),
            )
            return

        text = format_bill_list(bills, 1, total_pages, "pendentes")
        keyboard = InlineKeyboardMarkup(
            [
                *get_pagination_keyboard(1, total_pages, "pending").inline_keyboard,
                [InlineKeyboardButton("🔍 Revisar Primeiro", callback_data="review_next")],
            ]
        )

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def review_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /review command - start review flow for next pending bill."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        # Get first pending bill
        bills, _ = await repo.list_bills(
            user_id=user_id,
            status_filter="pending",
            page=1,
            per_page=1,
        )

        if not bills:
            await update.message.reply_text(
                "✅ <b>Nenhum boleto pendente de revisão!</b>",
                parse_mode="HTML",
                reply_markup=get_main_menu_keyboard(),
            )
            return

        bill = bills[0]
        await show_bill_for_review(update, bill)
    finally:
        await repo.close()


async def show_bill_for_review(
    update: Update | CallbackQuery, bill: Bill, edit: bool = False
) -> None:
    """Show a bill for review with inline keyboard."""
    user_id: int | None
    callback_query: CallbackQuery | None
    message: Message | None
    if isinstance(update, CallbackQuery):
        user_id = update.from_user.id
        callback_query = update
        message = None
    else:
        user_id = get_user_id(update)
        callback_query = update.callback_query
        message = update.message
    if not user_id:
        return

    # Set user state
    state_manager.set_state(
        user_id,
        UserState.AWAITING_BILL_REVIEW,
        bill_id=bill.id,
    )

    text = format_bill_details(bill)
    keyboard = get_review_keyboard(bill.id)

    if edit and callback_query:
        await callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        if message is None:
            return
        await message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def alerts_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /alerts command - manage alerts."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        # Get recent alert history
        alerts = await repo.get_recent_alerts(user_id=user_id, limit=10)

        if not alerts:
            text = "🔔 <b>Histórico de Alertas</b>\n\nNenhum alerta enviado recentemente."
        else:
            lines = ["🔔 <b>Últimos Alertas</b>", ""]
            for alert in alerts:
                bill = await repo.get_bill(alert.bill_id, user_id=user_id)
                if bill:
                    lines.append(
                        f"• {alert.sent_at.strftime('%d/%m %H:%M')} - "
                        f"{format_bill_summary(bill, show_id=False)} "
                        f"({alert.alert_type})"
                    )
            text = "\n".join(lines)

        keyboard = get_alert_config_keyboard()
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def config_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config command - show or update persisted configuration (RF21)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    from autobill.db.repository import SettingsRepository, db

    args = context.args or []
    if len(args) >= 2:
        key_alias = args[0].lower()
        raw_val = " ".join(args[1:]).strip()
        msg = await _apply_config_change(chat_id, key_alias, raw_val)
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_config_keyboard())
        return

    async with db.session() as session:
        row = await SettingsRepository(session).get_or_create(chat_id)
        config = {
            "alert_time": row.alert_time,
            "lead_days": row.lead_days,
            "timezone": row.timezone,
            "currency": row.currency,
            "auto_confirm": row.auto_confirm,
            "extraction_mode": row.extractor_mode,
            "llm_model": row.llm_model,
            "cloud_consent": row.cloud_consent,
        }

    text = format_config_status(config)
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=get_config_keyboard())


async def _apply_config_change(chat_id: int, key_alias: str, raw_val: str) -> str:
    """Validate and persist a configuration update in SQLite `settings` table."""
    from autobill.db.repository import SettingsRepository, db

    mapping = {
        "hora": "alert_time",
        "alert_time": "alert_time",
        "antecedencia": "lead_days",
        "lead_days": "lead_days",
        "timezone": "timezone",
        "fuso": "timezone",
        "moeda": "currency",
        "currency": "currency",
        "auto_confirm": "auto_confirm",
        "extraction_mode": "extractor_mode",
        "extractor_mode": "extractor_mode",
        "modo": "extractor_mode",
        "cloud_consent": "cloud_consent",
        "llm_model": "llm_model",
    }
    field = mapping.get(key_alias.lower())
    if not field:
        return f"❌ Campo de configuração desconhecido: <code>{escape(key_alias)}</code>"

    parsed_val: Any = raw_val
    try:
        if field == "alert_time":
            datetime.strptime(raw_val, "%H:%M")
        elif field == "lead_days":
            nums = [int(x.strip()) for x in raw_val.split(",") if x.strip()]
            if not nums or any(n < 0 for n in nums):
                return "❌ Informe inteiros >= 0 separados por vírgula (ex: 5,2,0)."
            parsed_val = ",".join(str(n) for n in sorted(set(nums), reverse=True))
        elif field == "timezone":
            ZoneInfo(raw_val)
        elif field in ("auto_confirm", "cloud_consent"):
            parsed_val = raw_val.lower() in ("1", "true", "sim", "yes", "on")
        elif field == "extractor_mode":
            if raw_val.lower() not in ("auto", "cloud", "local"):
                return "❌ Modo inválido. Use: auto, cloud ou local."
            parsed_val = raw_val.lower()
        elif field == "currency":
            parsed_val = raw_val.upper()[:8]
    except Exception as exc:
        return f"❌ Valor inválido para {escape(field)}: {escape(str(exc))}"

    async with db.session() as session:
        row = await SettingsRepository(session).update_setting(chat_id, field, parsed_val)

    if _active_scheduler is not None and field in ("alert_time", "timezone"):
        _active_scheduler.reschedule_daily_alerts(row.alert_time, row.timezone)

    return f"✅ Configuração <b>{escape(field)}</b> atualizada para <code>{escape(str(parsed_val))}</code>."


async def pendentes_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pendentes - lista faturas pendentes/vencidas ordenadas com total (RF14)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills = await repo.list_pending_and_overdue(chat_id)
        if not bills:
            await update.message.reply_text(
                "✅ <b>Nenhuma fatura pendente ou vencida!</b>", parse_mode="HTML"
            )
            return
        total_cents = sum(b.valor_centavos or 0 for b in bills)
        lines = ["<b>⏳ Faturas Pendentes</b>", ""]
        lines.extend(format_bill_summary(b) for b in bills[:25])
        lines.extend(
            [
                "",
                f"💰 <b>Total pendente:</b> {formatar_centavos(total_cents)} ({len(bills)} fatura(s))",
            ]
        )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    finally:
        await repo.close()


async def proximos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /proximos [dias] - próximos vencimentos (RF14)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    from autobill.alerts.policy import local_today

    days = 7
    if context.args and context.args[0].isdigit():
        days = max(1, min(365, int(context.args[0])))

    repo = await BillRepository.create_for_bot()
    try:
        bills = await repo.list_upcoming(chat_id, days, local_today(settings.default_timezone))
        if not bills:
            await update.message.reply_text(
                f"📭 Nenhuma fatura com vencimento nos próximos {days} dias."
            )
            return
        total_cents = sum(b.valor_centavos or 0 for b in bills)
        lines = [f"<b>📅 Próximos {days} dias</b>", ""]
        lines.extend(format_bill_summary(b) for b in bills[:25])
        lines.extend(["", f"💰 <b>Total:</b> {formatar_centavos(total_cents)}"])
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    finally:
        await repo.close()


async def mes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /mes [AAAA-MM] - resumo mensal previsto/pago/pendente (RF14)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    from autobill.alerts.formatter import format_monthly_summary
    from autobill.alerts.policy import local_today

    today = local_today(settings.default_timezone)
    year, month = today.year, today.month
    if context.args:
        try:
            dt = datetime.strptime(context.args[0], "%Y-%m")
            year, month = dt.year, dt.month
        except ValueError:
            await update.message.reply_text(
                "❌ Formato inválido. Use <code>/mes AAAA-MM</code>.", parse_mode="HTML"
            )
            return

    repo = await BillRepository.create_for_bot()
    try:
        summary = await repo.get_month_summary(year, month, user_id=chat_id)
        await update.message.reply_text(
            format_monthly_summary(year, month, summary), parse_mode="HTML"
        )
    finally:
        await repo.close()


async def fatura_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /fatura <id> - detalhes completos + reenvio do arquivo original (RF14)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use <code>/fatura &lt;id&gt;</code>.", parse_mode="HTML")
        return

    bill_id = int(context.args[0])
    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=chat_id)
        if bill is None:
            await update.message.reply_text("❌ Fatura não encontrada.")
            return
        from autobill.alerts.formatter import get_alert_action_keyboard

        await update.message.reply_text(
            format_bill_details(bill, show_confidence=True),
            parse_mode="HTML",
            reply_markup=get_alert_action_keyboard(bill),
        )
        if bill.arquivo_path:
            file_p = Path(bill.arquivo_path)
            if file_p.exists():
                with file_p.open("rb") as fh:
                    await update.message.reply_document(
                        document=InputFile(fh, filename=file_p.name)
                    )
    finally:
        await repo.close()


async def pagas_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pagas - lista últimas faturas pagas (RF14)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills = await repo.list_paid(chat_id, limit=20)
        if not bills:
            await update.message.reply_text("📭 Nenhuma fatura paga registrada.")
            return
        total_cents = sum(b.valor_centavos or 0 for b in bills)
        lines = ["<b>✅ Faturas Pagas</b>", ""]
        lines.extend(format_bill_summary(b) for b in bills)
        lines.extend(["", f"💰 <b>Total exibido:</b> {formatar_centavos(total_cents)}"])
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    finally:
        await repo.close()


async def pago_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pago <id> - marca fatura como paga (RF12)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use <code>/pago &lt;id&gt;</code>.", parse_mode="HTML")
        return

    bill_id = int(context.args[0])
    actor = get_actor_label(update)
    repo = await BillRepository.create_for_bot()
    try:
        bill, already_paid = await repo.mark_paid(bill_id, user_id=chat_id, paid_by=actor)
        if bill is None:
            await update.message.reply_text("❌ Fatura não encontrada.")
            return
        if already_paid:
            await update.message.reply_text(f"ℹ️ A fatura #{bill_id} já estava marcada como paga.")
            return

        from autobill.db.repository import AuditRepository

        await AuditRepository(repo.session).log_action(
            chat_id=chat_id, action="mark_paid", bill_id=bill.id, details=f"paid_by={actor}"
        )
        await repo.session.commit()
        paid_str = bill.paid_at.strftime("%d/%m") if bill.paid_at else "hoje"
        await update.message.reply_text(
            f"✅ Fatura #{bill.id} marcada como <b>PAGA</b> em {paid_str} por {escape(actor)}.",
            parse_mode="HTML",
        )
    finally:
        await repo.close()


async def desfazer_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /desfazer - reverte o último pagamento feito em até 24h (RF12)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.undo_last_payment(user_id=chat_id, max_age_hours=24)
        if bill is None:
            await update.message.reply_text(
                "ℹ️ Nenhum pagamento recente (últimas 24h) para desfazer."
            )
            return
        from autobill.db.repository import AuditRepository

        await AuditRepository(repo.session).log_action(
            chat_id=chat_id, action="undo_paid", bill_id=bill.id
        )
        await repo.session.commit()
        await update.message.reply_text(
            f"↩️ Pagamento da fatura #{bill.id} (<b>{escape(bill.beneficiario or 'Sem nome')}</b>) "
            f"revertido para <b>PENDENTE</b>.",
            parse_mode="HTML",
        )
    finally:
        await repo.close()


async def adicionar_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /adicionar - assistente manual passo a passo (RF04)."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id) or not update.message:
        return

    state_manager.set_state(user_id, UserState.AWAITING_ADD_BENEFICIARIO, temp_data={})
    await update.message.reply_text(
        "➕ <b>Adicionar Fatura Manualmente</b>\n\n"
        "Passo 1/4 — Envie o nome do <b>beneficiário</b> (ex: Enel, Condomínio):",
        parse_mode="HTML",
    )


async def editar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /editar <id> - abre menu de edição da fatura (RF15)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use <code>/editar &lt;id&gt;</code>.", parse_mode="HTML")
        return

    bill_id = int(context.args[0])
    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=chat_id)
        if not bill:
            await update.message.reply_text("❌ Fatura não encontrada.")
            return
        fields = ["valor", "vencimento", "beneficiario", "linha_digitavel", "codigo_barras"]
        state_manager.set_state(user_id, UserState.EDITING_FIELD, bill_id=bill_id)
        await update.message.reply_text(
            f"✏️ <b>Editar Fatura #{bill_id}</b>\n\nSelecione o campo:",
            parse_mode="HTML",
            reply_markup=get_edit_field_keyboard(bill_id, fields),
        )
    finally:
        await repo.close()


async def remover_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /remover <id> - pede confirmação e faz exclusão lógica (RF15)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Use <code>/remover &lt;id&gt;</code>.", parse_mode="HTML")
        return

    bill_id = int(context.args[0])
    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=chat_id)
        if not bill:
            await update.message.reply_text("❌ Fatura não encontrada.")
            return
        await update.message.reply_text(
            f"🗑️ <b>Confirmar remoção da fatura #{bill_id}?</b>\n"
            f"<i>({escape(bill.beneficiario or 'Sem nome')}) — recuperável por 30 dias.</i>",
            parse_mode="HTML",
            reply_markup=get_confirm_keyboard("remove", bill_id),
        )
    finally:
        await repo.close()


async def paginas_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /paginas - agrupa várias imagens em uma única fatura (RF02)."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id) or not update.message:
        return

    args = context.args or []
    if args and args[0].lower() in ("fim", "done", "processar"):
        await _finalize_grouped_pages(update, user_id)
        return

    state_manager.set_state(user_id, UserState.GROUPING_PAGES, temp_data={"pages": []})
    await update.message.reply_text(
        "📚 <b>Modo agrupamento de páginas ativado!</b>\n\n"
        "Envie as fotos da fatura em sequência e depois clique em <b>Processar páginas</b> "
        "ou envie <code>/paginas fim</code>.",
        parse_mode="HTML",
        reply_markup=get_pages_grouping_keyboard(),
    )


async def exportar_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /exportar - gera e envia CSV com todas as faturas (RF23)."""
    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id or not is_authorized_user(user_id) or not update.message:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills = await repo.list_all_for_export(chat_id)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "id",
                "beneficiario",
                "valor_reais",
                "vencimento",
                "status",
                "origem",
                "kind",
                "linha_digitavel",
                "paid_at",
                "paid_by",
                "created_at",
            ]
        )
        for b in bills:
            writer.writerow(
                [
                    b.id,
                    b.beneficiario or "",
                    f"{b.valor_centavos / 100:.2f}" if b.valor_centavos is not None else "",
                    b.vencimento.strftime("%Y-%m-%d") if b.vencimento else "",
                    b.status.value,
                    b.origem.value,
                    b.kind,
                    b.linha_digitavel or "",
                    b.paid_at.isoformat() if b.paid_at else "",
                    b.paid_by or "",
                    b.created_at.isoformat(),
                ]
            )
        csv_bytes = buf.getvalue().encode("utf-8-sig")
        await update.message.reply_document(
            document=InputFile(io.BytesIO(csv_bytes), filename="autobill_faturas.csv"),
            caption=f"📤 Exportação concluída: {len(bills)} fatura(s).",
        )
    finally:
        await repo.close()


async def apagar_tudo_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /apagar_tudo - exige confirmação dupla antes de limpar todos os dados (RF24)."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id) or not update.message:
        return

    state_manager.set_state(user_id, UserState.AWAITING_WIPE_CONFIRM_1)
    await update.message.reply_text(
        "⚠️ <b>ATENÇÃO: Exclusão Total (LGPD)</b>\n\n"
        "Esta ação removerá <b>permanentemente</b> o banco de dados, todas as faturas, "
        "arquivos originais e backups.\n\n"
        "Para continuar (passo 1/2), digite exatamente: <code>APAGAR</code>",
        parse_mode="HTML",
    )


async def status_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status - exibe saúde, fila, último processamento, erros e próximo alerta (RF25)."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id) or not update.message:
        return

    from autobill.extract.llm_vision import get_daily_llm_usage
    from autobill.ops.health import get_health_payload

    _healthy, payload = await get_health_payload()
    used_llm, limit_llm = get_daily_llm_usage()
    est_cost_usd = used_llm * 0.0002

    errs = payload.get("recent_errors") or []
    err_txt = "\n".join(f"• {escape(e['at'][:19])}: {escape(e['error'])}" for e in errs) or "Nenhum"

    msg = (
        f"<b>🩺 Status AutoBill v{escape(str(payload['version']))}</b>\n\n"
        f"• Banco: {'✅ OK' if payload['db'] else '❌ Erro'}\n"
        f"• Diretórios: {'✅ OK' if payload['directories'] else '❌ Erro'}\n"
        f"• Agendador: {'✅ Ativo' if payload['scheduler_running'] else '⏸️ Parado'}\n"
        f"• Itens na fila: {payload['queue_pending']}\n"
        f"• Último processamento: {escape(str(payload['last_processed_at'] or 'N/D'))}\n"
        f"• Próximo alerta: {escape(str(payload['next_alert_at'] or 'N/D'))}\n"
        f"• Uso LLM hoje: {used_llm}/{limit_llm} (~US$ {est_cost_usd:.4f})\n"
        f"• Erros recentes:\n{err_txt}"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


async def _process_telegram_upload(
    update: Update,
    file_bytes: bytes,
    filename: str,
    processing_msg: Message,
    pdf_password: str | None = None,
) -> None:
    """Process downloaded bytes through unified ingestor (`ingest_bytes`)."""
    from autobill.ingest.processor import ingest_bytes

    user_id = get_user_id(update)
    chat_id = get_chat_id(update) or user_id
    if not user_id or not chat_id:
        return

    outcome = await ingest_bytes(
        file_bytes=file_bytes,
        filename=filename,
        origin=BillOrigin.TELEGRAM,
        chat_id=chat_id,
        password=pdf_password,
    )

    if outcome.status == "password_required":
        state_manager.set_state(
            user_id,
            UserState.AWAITING_PDF_PASSWORD,
            temp_data={"file_bytes": file_bytes, "filename": filename},
        )
        await processing_msg.edit_text(
            "🔐 <b>PDF protegido por senha.</b>\n\nEnvie a senha para desbloquear (não será salva).",
            parse_mode="HTML",
        )
        return

    if outcome.status in ("duplicate_hash", "duplicate_linha") and outcome.duplicate_bill:
        await processing_msg.edit_text(
            f"ℹ️ <b>Boleto duplicado detectado!</b>\n\n"
            f"Mesmo conteúdo da fatura #{outcome.duplicate_bill.id} "
            f"({escape(outcome.duplicate_bill.beneficiario or 'Sem nome')}).",
            parse_mode="HTML",
        )
        return

    if outcome.bill is None:
        reason = outcome.reason or "Não foi possível extrair os dados do arquivo."
        await processing_msg.edit_text(
            "❌ <b>Não foi possível extrair dados deste arquivo.</b>\n\n"
            f"<i>{escape(reason)}</i>\n"
            "Use /adicionar para cadastrar manualmente.",
            parse_mode="HTML",
        )
        return

    if outcome.bill.status == BillStatus.PENDENTE:
        await processing_msg.edit_text(
            f"✅ <b>Boleto confirmado automaticamente!</b>\n\n{format_bill_details(outcome.bill)}",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
        if outcome.status == "duplicate_heuristic" and outcome.duplicate_bill and update.message:
            await update.message.reply_text(
                f"⚠️ <i>Atenção: existe uma fatura parecida (#{outcome.duplicate_bill.id}) "
                f"com mesmo valor/vencimento. Verifique com /fatura {outcome.bill.id}.</i>",
                parse_mode="HTML",
            )
        return

    await processing_msg.edit_text(
        "✅ <b>Boleto processado!</b>\n\nRevise os dados abaixo:",
        parse_mode="HTML",
    )
    await show_bill_for_review(update, outcome.bill)
    if outcome.status == "duplicate_heuristic" and outcome.duplicate_bill and update.message:
        await update.message.reply_text(
            f"⚠️ <b>Possível duplicata</b> de #{outcome.duplicate_bill.id} "
            "(mesmo valor/vencimento/beneficiário). Deseja manter?",
            parse_mode="HTML",
            reply_markup=get_duplicate_confirm_keyboard(outcome.bill.id),
        )


async def _finalize_grouped_pages(update: Update, user_id: int) -> None:
    """Combine pages collected during `/paginas` mode and process them."""
    from PIL import Image

    user_state = state_manager.get_state(user_id)
    pages: list[bytes] = user_state.temp_data.get("pages", [])
    state_manager.clear_state(user_id)

    if not update.message:
        return
    if not pages:
        await update.message.reply_text("ℹ️ Nenhuma página recebida; modo agrupamento encerrado.")
        return

    processing_msg = await update.message.reply_text(
        f"📥 Recebido ({len(pages)} página(s)), processando como PDF único...",
        parse_mode="HTML",
    )

    try:
        pil_images = []
        for raw in pages:
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            pil_images.append(img)
        pdf_buf = io.BytesIO()
        pil_images[0].save(pdf_buf, format="PDF", save_all=True, append_images=pil_images[1:])
        await _process_telegram_upload(
            update,
            pdf_buf.getvalue(),
            "paginas_agrupadas.pdf",
            processing_msg,
        )
    except Exception as exc:
        logger.exception("Error combining grouped pages")
        await processing_msg.edit_text(format_error_message(str(exc)), parse_mode="HTML")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document (PDF or image) uploads."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    document = update.message.document
    if not document:
        return

    filename = document.file_name or "boleto.pdf"
    if not (is_pdf_file(filename) or is_image_file(filename)):
        await update.message.reply_text(
            "📄 <b>Arquivo não suportado</b>\n\nEnvie PDF, PNG, JPG ou WEBP.",
            parse_mode="HTML",
        )
        return

    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "📄 <b>Arquivo muito grande</b>\n\nTamanho máximo: 20 MB",
            parse_mode="HTML",
        )
        return

    processing_msg = await update.message.reply_text(
        "📥 <b>Recebido, processando...</b>",
        parse_mode="HTML",
    )

    try:
        file_bytes = await download_file(context, document.file_id)
        if not file_bytes:
            await processing_msg.edit_text("❌ <b>Erro ao baixar arquivo</b>", parse_mode="HTML")
            return
        await _process_telegram_upload(update, file_bytes, filename, processing_msg)
    except Exception as e:
        logger.exception("Error processing document")
        await processing_msg.edit_text(format_error_message(str(e)), parse_mode="HTML")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads (including `/paginas` multi-page grouping)."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        return

    user_state = state_manager.get_state(user_id)
    if user_state.state == UserState.GROUPING_PAGES:
        file_bytes = await download_file(context, photo.file_id)
        if file_bytes:
            pages = user_state.temp_data.setdefault("pages", [])
            pages.append(file_bytes)
            await update.message.reply_text(
                f"📸 Página {len(pages)} adicionada. Envie a próxima ou finalize:",
                reply_markup=get_pages_grouping_keyboard(),
            )
        return

    processing_msg = await update.message.reply_text(
        "📥 <b>Recebido, processando...</b>",
        parse_mode="HTML",
    )

    try:
        file_bytes = await download_file(context, photo.file_id)
        if not file_bytes:
            await processing_msg.edit_text("❌ <b>Erro ao baixar imagem</b>", parse_mode="HTML")
            return
        await _process_telegram_upload(update, file_bytes, "boleto.jpg", processing_msg)
    except Exception as e:
        logger.exception("Error processing photo")
        await processing_msg.edit_text(format_error_message(str(e)), parse_mode="HTML")


async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all callback queries."""
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        await query.answer("🚫 Não autorizado", show_alert=True)
        return

    await query.answer()

    action, params = parse_callback_data(query.data)

    if action == "review_approve":
        await handle_review_approve(query, context, params)
    elif action == "review_reject":
        await handle_review_reject(query, context, params)
    elif action == "review_edit":
        await handle_review_edit(query, context, params)
    elif action == "review_details":
        await handle_review_details(query, context, params)
    elif action == "review_skip":
        await handle_review_skip(query, context, params)
    elif action == "review_next":
        await handle_review_next(query, context)
    elif action == "edit_field":
        await handle_edit_field(query, context, params)
    elif action == "edit_done":
        await handle_edit_done(query, context, params)
    elif action == "edit_cancel":
        await handle_edit_cancel(query, context, params)
    elif action == "confirm_approve":
        await handle_review_approve(query, context, params)
    elif action == "confirm_reject":
        await handle_confirm_reject(query, context, params)
    elif action == "confirm_remove":
        await handle_confirm_remove(query, context, params)
    elif action in ("cancel_approve", "cancel_reject", "cancel_remove"):
        await handle_review_cancel(query, context, params)
    elif action == "pay":
        await handle_pay_callback(query, context, params)
    elif action == "send_file":
        await handle_send_file_callback(query, context, params)
    elif action == "snooze_menu":
        await handle_snooze_menu(query, context, params)
    elif action == "snooze_hours":
        await handle_snooze_hours(query, context, params)
    elif action == "dup_force":
        await handle_dup_force(query, context, params)
    elif action == "pages":
        await handle_pages_callback(query, context, params)
    elif action == "filter":
        await handle_filter(query, context, params)
    elif action == "bills":
        await handle_bills_page(query, context, params)
    elif action == "pending":
        await handle_pending_page(query, context, params)
    elif action == "alert_config":
        await handle_alert_config(query, context, params)
    elif action == "snooze":
        await handle_snooze(query, context, params)
    elif action == "snooze_custom":
        await handle_snooze_custom(query, context, params)
    elif action == "snooze_cancel":
        await handle_snooze_cancel(query, context, params)
    elif action == "config":
        await handle_config(query, context, params)
    elif action == "back":
        await handle_back(query, context, params)
    elif action == "noop":
        pass  # Do nothing for pagination current page button


async def handle_pay_callback(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle `✅ Paguei` inline button on alerts (RF12)."""
    if not params or not params[0].isdigit():
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    chat_id = get_chat_id(query) or user_id
    if not user_id or not chat_id:
        return
    actor = get_actor_label(query)

    repo = await BillRepository.create_for_bot()
    try:
        bill, already_paid = await repo.mark_paid(bill_id, user_id=chat_id, paid_by=actor)
        if bill is None:
            await query.edit_message_text("❌ Fatura não encontrada.", parse_mode="HTML")
            return
        from autobill.db.repository import AuditRepository

        if not already_paid:
            await AuditRepository(repo.session).log_action(
                chat_id=chat_id, action="mark_paid", bill_id=bill.id, details=f"paid_by={actor}"
            )
            await repo.session.commit()

        paid_str = bill.paid_at.strftime("%d/%m") if bill.paid_at else "hoje"
        await query.edit_message_text(
            f"✅ <b>Pago em {paid_str} por {escape(actor)}</b>\n\n"
            f"Fatura #{bill.id} — {escape(bill.beneficiario or 'Sem nome')}\n"
            "<i>Use /desfazer em até 24h caso tenha clicado por engano.</i>",
            parse_mode="HTML",
        )
    finally:
        await repo.close()


async def handle_snooze_menu(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Show snooze duration options (`1h`, `3h`, `Amanhã`, `3 dias`, `7 dias`)."""
    if not params or not params[0].isdigit():
        return
    bill_id = int(params[0])
    await query.edit_message_text(
        f"⏰ <b>Adiar lembrete da fatura #{bill_id}</b>\n\nPor quanto tempo?",
        parse_mode="HTML",
        reply_markup=get_snooze_keyboard(bill_id),
    )


async def handle_snooze_hours(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Snooze a bill for N hours (RF13)."""
    if len(params) < MIN_CALLBACK_PARTS or not params[0].isdigit() or not params[1].isdigit():
        return
    bill_id = int(params[0])
    hours = int(params[1])
    user_id = get_user_id(query)
    chat_id = get_chat_id(query) or user_id
    if not user_id or not chat_id:
        return

    from autobill.db.repository import SnoozeRepository, db

    until = datetime.now(UTC) + timedelta(hours=hours)
    async with db.session() as session:
        await SnoozeRepository(session).create_snooze(bill_id, chat_id, until)
    await query.edit_message_text(
        f"😴 <b>Lembrete adiado por {hours} hora(s).</b>",
        parse_mode="HTML",
        reply_markup=get_back_keyboard("back:main"),
    )


async def handle_send_file_callback(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Resend original PDF/image attached to a bill."""
    if not params or not params[0].isdigit():
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    chat_id = get_chat_id(query) or user_id
    if not user_id or not chat_id:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=chat_id)
        if not bill or not bill.arquivo_path or not Path(bill.arquivo_path).exists():
            await query.answer("Arquivo original não disponível.", show_alert=True)
            return
        file_p = Path(bill.arquivo_path)
        if isinstance(query.message, Message):
            with file_p.open("rb") as fh:
                await query.message.reply_document(document=InputFile(fh, filename=file_p.name))
    finally:
        await repo.close()


async def handle_dup_force(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Confirm keeping or discarding a heuristic duplicate."""
    if not params:
        return
    if params[0] == "yes":
        await query.edit_message_text("✅ Fatura mantida normalmente.", parse_mode="HTML")
        return
    if params[0] == "no" and len(params) > 1 and params[1].isdigit():
        bill_id = int(params[1])
        user_id = get_user_id(query)
        chat_id = get_chat_id(query) or user_id
        if user_id and chat_id:
            repo = await BillRepository.create_for_bot()
            try:
                await repo.soft_delete_bill(bill_id, user_id=chat_id)
            finally:
                await repo.close()
        await query.edit_message_text("🗑️ Duplicata descartada.", parse_mode="HTML")


async def handle_pages_callback(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle `/paginas` inline actions."""
    user_id = get_user_id(query)
    if not user_id or not params:
        return
    if params[0] == "cancel":
        state_manager.clear_state(user_id)
        await query.edit_message_text("❌ Agrupamento de páginas cancelado.")
        return
    if params[0] == "process" and query.message:
        fake_update = Update(update_id=0, message=query.message)  # type: ignore[arg-type]
        await _finalize_grouped_pages(fake_update, user_id)


async def handle_confirm_remove(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Confirm soft-delete of a bill (RF15)."""
    if not params or not params[0].isdigit():
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    chat_id = get_chat_id(query) or user_id
    if not user_id or not chat_id:
        return

    repo = await BillRepository.create_for_bot()
    try:
        ok = await repo.soft_delete_bill(bill_id, user_id=chat_id)
        if ok:
            from autobill.db.repository import AuditRepository

            await AuditRepository(repo.session).log_action(
                chat_id=chat_id, action="soft_delete", bill_id=bill_id
            )
            await repo.session.commit()
            await query.edit_message_text(
                f"🗑️ <b>Fatura #{bill_id} removida</b> (recuperável no banco por 30 dias).",
                parse_mode="HTML",
            )
        else:
            await query.edit_message_text("❌ Fatura não encontrada.")
    finally:
        await repo.close()


# Review handlers
async def handle_review_approve(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle approve bill."""
    if not params:
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=user_id)
        if not bill:
            await query.edit_message_text("❌ Boleto não encontrado", parse_mode="HTML")
            return

        # Validate before approving
        errors = []
        if bill.linha_digitavel and not validar_linha_digitavel(bill.linha_digitavel):
            errors.append("Linha digitável inválida (módulo 10/11)")

        if errors:
            await query.edit_message_text(
                "⚠️ <b>Erros de validação:</b>\n"
                + "\n".join(f"• {e}" for e in errors)
                + "\n\nUse ✏️ Editar para corrigir ou ❌ Rejeitar para descartar.",
                parse_mode="HTML",
                reply_markup=get_review_keyboard(bill_id),
            )
            return

        # Approve
        await repo.update_bill_status(bill_id, "approved", user_id=user_id)
        bill.status = BillStatus.PENDENTE

        await query.edit_message_text(
            f"✅ <b>Boleto aprovado!</b>\n\nAlertas agendados para {bill.vencimento}.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )

        # Clear state
        if user_id:
            state_manager.clear_state(user_id)

        # Show next pending
        await handle_review_next(query, context)
    finally:
        await repo.close()


async def handle_review_reject(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle reject bill - show confirmation."""
    if not params:
        return
    bill_id = int(params[0])

    await query.edit_message_text(
        "❌ <b>Confirmar rejeição?</b>\n\nO boleto será descartado permanentemente.",
        parse_mode="HTML",
        reply_markup=get_confirm_keyboard("reject", bill_id),
    )


async def handle_confirm_reject(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Confirm bill rejection."""
    if not params:
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        await repo.update_bill_status(bill_id, "rejected", user_id=user_id)

        await query.edit_message_text(
            "❌ <b>Boleto rejeitado e descartado.</b>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )

        if user_id:
            state_manager.clear_state(user_id)

        await handle_review_next(query, context)
    finally:
        await repo.close()


async def handle_review_edit(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Start edit flow for a bill."""
    if not params:
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=user_id)
        if not bill:
            await query.edit_message_text("❌ Boleto não encontrado", parse_mode="HTML")
            return

        # Allow editing all core fields even when currently empty (RF15)
        fields = [
            "valor",
            "vencimento",
            "beneficiario",
            "linha_digitavel",
            "codigo_barras",
        ]

        if user_id:
            state_manager.set_state(
                user_id,
                UserState.EDITING_FIELD,
                bill_id=bill_id,
            )

        await query.edit_message_text(
            "✏️ <b>Editar Boleto</b>\n\nSelecione o campo para editar:",
            parse_mode="HTML",
            reply_markup=get_edit_field_keyboard(bill_id, fields),
        )
    finally:
        await repo.close()


async def handle_edit_field(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle field selection for editing."""
    if len(params) < MIN_CALLBACK_PARTS:
        return
    bill_id = int(params[0])
    field_name = params[1]

    user_id = get_user_id(query)
    if user_id:
        state_manager.set_state(
            user_id,
            UserState.EDITING_FIELD,
            bill_id=bill_id,
            field_name=field_name,
        )

    field_labels = {
        "valor": "Valor (em centavos, ex: 123456 = R$ 1.234,56)",
        "vencimento": "Vencimento (DD/MM/AAAA)",
        "beneficiario": "Beneficiário",
        "linha_digitavel": "Linha Digitável (47 ou 48 dígitos)",
        "codigo_barras": "Código de Barras (44 dígitos)",
    }

    await query.edit_message_text(
        f"✏️ <b>Editando: {field_name}</b>\n\n"
        f"Envie o novo valor:\n"
        f"<i>{field_labels.get(field_name, field_name)}</i>",
        parse_mode="HTML",
        reply_markup=get_back_keyboard(f"review_edit:{bill_id}"),
    )


async def handle_edit_done(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Finish editing and return to review."""
    if not params:
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=user_id)
        if bill:
            await show_bill_for_review(query, bill, edit=True)
    finally:
        await repo.close()

    if user_id:
        state_manager.clear_state(user_id)


async def handle_edit_cancel(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Cancel editing."""
    if not params:
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=user_id)
        if bill:
            await show_bill_for_review(query, bill, edit=True)
    finally:
        await repo.close()

    if user_id:
        state_manager.clear_state(user_id)


async def handle_review_details(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Show full bill details."""
    if not params:
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=user_id)
        if bill:
            text = format_bill_details(bill, show_confidence=True)
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=get_back_keyboard(f"review_edit:{bill_id}"),
            )
    finally:
        await repo.close()


async def handle_review_skip(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Skip current bill and show next."""
    if not params:
        return
    # Just show next without changing status
    await handle_review_next(query, _context)


async def handle_review_next(query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show next pending bill for review."""
    user_id = get_user_id(query)
    if not user_id:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills, _ = await repo.list_bills(
            user_id=user_id,
            status_filter="pending",
            page=1,
            per_page=1,
        )

        if not bills:
            await query.edit_message_text(
                "✅ <b>Todos os boletos revisados!</b>",
                parse_mode="HTML",
                reply_markup=get_back_keyboard("back:main"),
            )
            return

        bill = bills[0]
        await show_bill_for_review(query, bill, edit=True)
    finally:
        await repo.close()


async def handle_review_cancel(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, _params: list[str]
) -> None:
    """Cancel review flow."""
    user_id = get_user_id(query)
    if user_id:
        state_manager.clear_state(user_id)

    await query.edit_message_text(
        "❌ <b>Revisão cancelada.</b>",
        parse_mode="HTML",
        reply_markup=get_back_keyboard("back:main"),
    )


# Filter and pagination handlers
async def handle_filter(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle filter change."""
    if not params:
        return
    filter_name = params[0]

    user_id = get_user_id(query)
    if not user_id:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter=filter_name if filter_name != "all" else None,
            page=1,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        text = format_bill_list(bills, 1, total_pages, filter_name)
        keyboard = InlineKeyboardMarkup(
            [
                *get_bills_filter_keyboard(filter_name).inline_keyboard,
                *get_pagination_keyboard(1, total_pages, "bills").inline_keyboard,
            ]
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def handle_bills_page(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle bills pagination."""
    if not params:
        return
    page = int(params[0])

    # Get current filter from message or default
    filter_name = "all"  # Would need to parse from message

    user_id = get_user_id(query)
    if not user_id:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter=filter_name if filter_name != "all" else None,
            page=page,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        text = format_bill_list(bills, page, total_pages, filter_name)
        keyboard = InlineKeyboardMarkup(
            [
                *get_bills_filter_keyboard(filter_name).inline_keyboard,
                *get_pagination_keyboard(page, total_pages, "bills").inline_keyboard,
            ]
        )

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def handle_pending_page(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle pending bills pagination."""
    if not params:
        return
    page = int(params[0])

    user_id = get_user_id(query)
    if not user_id:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter="pending",
            page=page,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        text = format_bill_list(bills, page, total_pages, "pendentes")
        keyboard = get_pagination_keyboard(page, total_pages, "pending")

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


# Alert handlers
async def handle_alert_config(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle alert configuration."""
    if not params:
        return
    action = params[0]
    user_id = get_user_id(query)
    chat_id = get_chat_id(query) or user_id

    if action == "done":
        await query.edit_message_text(
            "✅ <b>Configuração de alertas salva.</b>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
    elif action == "time":
        if user_id:
            state_manager.set_state(user_id, UserState.AWAITING_ALERT_TIME)
        await query.edit_message_text(
            "🕐 <b>Novo horário de alertas</b>\n\nEnvie no formato HH:MM (ex: 07:30)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("alert_config:done"),
        )
    elif action == "lead_days":
        if user_id:
            state_manager.set_state(
                user_id, UserState.AWAITING_CONFIG_VALUE, field_name="lead_days"
            )
        await query.edit_message_text(
            "📅 <b>Dias de antecedência</b>\n\nEnvie números separados por vírgula (ex: 3,1,0)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("alert_config:done"),
        )
    elif action in ("mute", "unmute") and chat_id:
        from autobill.db.repository import SettingsRepository, db

        muted = action == "mute"
        async with db.session() as session:
            await SettingsRepository(session).update_setting(chat_id, "alerts_muted", muted)
        label = "🔕 <b>Alertas silenciados.</b>" if muted else "🔊 <b>Alertas ativados.</b>"
        await query.edit_message_text(
            label,
            parse_mode="HTML",
            reply_markup=get_alert_config_keyboard(),
        )


async def handle_snooze(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle snooze bill."""
    if len(params) < MIN_CALLBACK_PARTS:
        return
    bill_id = int(params[0])
    days = int(params[1])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        await repo.snooze_bill(bill_id, days, user_id=user_id)
        await query.edit_message_text(
            f"😴 <b>Boleto adiado por {days} dias.</b>\n\nNovo lembrete agendado.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
    finally:
        await repo.close()


async def handle_snooze_custom(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle custom snooze."""
    if not params:
        return
    bill_id = int(params[0])

    user_id = get_user_id(query)
    if user_id:
        state_manager.set_state(
            user_id,
            UserState.AWAITING_SNOOZE_DAYS,
            bill_id=bill_id,
        )

    await query.edit_message_text(
        "📅 <b>Adiar personalizado</b>\n\nEnvie o número de dias (ex: 5)",
        parse_mode="HTML",
        reply_markup=get_back_keyboard(f"snooze_cancel:{bill_id}"),
    )


async def handle_snooze_cancel(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Cancel snooze."""
    if not params:
        return
    bill_id = int(params[0])
    user_id = get_user_id(query)
    if user_id is None:
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=user_id)
        if bill:
            await show_bill_for_review(query, bill, edit=True)
    finally:
        await repo.close()


# Config handlers
async def handle_config(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle configuration changes."""
    if not params:
        return
    action = params[0]
    user_id = get_user_id(query)
    chat_id = get_chat_id(query) or user_id

    if action == "done":
        await query.edit_message_text(
            "✅ <b>Configurações salvas.</b>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
    elif action == "alert_time":
        if user_id:
            state_manager.set_state(
                user_id, UserState.AWAITING_CONFIG_VALUE, field_name="alert_time"
            )
        await query.edit_message_text(
            "🕐 <b>Horário dos alertas</b>\n\nEnvie no formato HH:MM (ex: 07:30)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "lead_days":
        if user_id:
            state_manager.set_state(
                user_id, UserState.AWAITING_CONFIG_VALUE, field_name="lead_days"
            )
        await query.edit_message_text(
            "📅 <b>Dias de antecedência</b>\n\nEnvie números separados por vírgula (ex: 3,1,0)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "timezone":
        if user_id:
            state_manager.set_state(user_id, UserState.AWAITING_CONFIG_VALUE, field_name="timezone")
        await query.edit_message_text(
            "🌍 <b>Fuso horário</b>\n\nEnvie o fuso (ex: <code>America/Sao_Paulo</code> ou <code>America/Fortaleza</code>):",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "extraction_mode":
        if user_id:
            state_manager.set_state(
                user_id, UserState.AWAITING_CONFIG_VALUE, field_name="extractor_mode"
            )
        await query.edit_message_text(
            "🔧 <b>Modo de extração</b>\n\nEnvie <code>auto</code>, <code>cloud</code> ou <code>local</code>:",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "llm_model":
        if user_id:
            state_manager.set_state(
                user_id, UserState.AWAITING_CONFIG_VALUE, field_name="llm_model"
            )
        await query.edit_message_text(
            "🤖 <b>Modelo LLM</b>\n\nEnvie o nome do modelo (ex: <code>qwen2.5-vl</code> ou <code>gemini-2.0-flash</code>):",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "cloud_consent" and chat_id:
        from autobill.db.repository import SettingsRepository, db

        async with db.session() as session:
            row = await SettingsRepository(session).get_or_create(chat_id)
            new_consent = not row.cloud_consent
            await SettingsRepository(session).update_setting(chat_id, "cloud_consent", new_consent)
        await query.edit_message_text(
            f"☁️ <b>Consentimento para nuvem:</b> {'✅ Ativado' if new_consent else '❌ Desativado'}",
            parse_mode="HTML",
            reply_markup=get_config_keyboard(),
        )


async def handle_back(
    query: CallbackQuery, _context: ContextTypes.DEFAULT_TYPE, params: list[str]
) -> None:
    """Handle back navigation."""
    if not params:
        return
    target = params[0]

    if target == "main":
        await query.edit_message_text(
            "🏠 <b>Menu Principal</b>\n\nUse os botões abaixo ou comandos:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([]),
        )
    elif target == "review_edit" and len(params) > 1:
        bill_id = int(params[1])
        user_id = get_user_id(query)
        if user_id is None:
            return
        repo = await BillRepository.create_for_bot()
        try:
            bill = await repo.get_bill(bill_id, user_id=user_id)
            if bill:
                await show_bill_for_review(query, bill, edit=True)
        finally:
            await repo.close()


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages for multi-step flows and ReplyKeyboard shortcuts."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return

    user_state = state_manager.get_state(user_id)

    if user_state.state == UserState.EDITING_FIELD:
        await handle_edit_value(update, context, user_state, user_id)
    elif user_state.state == UserState.AWAITING_CONFIG_VALUE:
        await handle_config_value(update, context, user_state, user_id)
    elif user_state.state == UserState.AWAITING_ALERT_TIME:
        await handle_alert_time_value(update, context, user_state, user_id)
    elif user_state.state == UserState.AWAITING_SNOOZE_DAYS:
        await handle_snooze_days_value(update, context, user_state, user_id)
    elif user_state.state in (
        UserState.AWAITING_ADD_BENEFICIARIO,
        UserState.AWAITING_ADD_VALOR,
        UserState.AWAITING_ADD_VENCIMENTO,
        UserState.AWAITING_ADD_LINHA,
    ):
        await _handle_add_wizard(update, user_state, user_id)
    elif user_state.state == UserState.AWAITING_PDF_PASSWORD:
        await _handle_pdf_password(update, user_state, user_id)
    elif user_state.state in (UserState.AWAITING_WIPE_CONFIRM_1, UserState.AWAITING_WIPE_CONFIRM_2):
        await _handle_wipe_confirm(update, user_state, user_id)
    else:
        await _handle_reply_keyboard_shortcut(update, context)


async def _handle_reply_keyboard_shortcut(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Route persistent ReplyKeyboard buttons to their commands."""
    if not update.message or not update.message.text:
        return
    txt = update.message.text.strip()
    if txt == "📋 Boletos":
        await bills_command(update, context)
    elif txt == "⏳ Pendentes":
        await pendentes_command(update, context)
    elif txt == "🔔 Alertas":
        await alerts_command(update, context)
    elif txt == "⚙️ Config":
        await config_command(update, context)
    elif txt == "❓ Ajuda":
        await help_command(update, context)


async def _handle_add_wizard(update: Update, user_state: UserStateData, user_id: int) -> None:
    """Step-by-step `/adicionar` wizard (RF04)."""
    if not update.message or not update.message.text:
        return
    txt = _get_message_text(update).strip()
    chat_id = get_chat_id(update) or user_id

    if txt.lower() == "cancelar":
        state_manager.clear_state(user_id)
        await update.message.reply_text("❌ Lançamento cancelado.")
        return

    if user_state.state == UserState.AWAITING_ADD_BENEFICIARIO:
        if len(txt) < 2:
            await update.message.reply_text("❌ Informe um nome válido (ou 'cancelar'):")
            return
        user_state.temp_data["beneficiario"] = txt[:255]
        state_manager.set_state(user_id, UserState.AWAITING_ADD_VALOR)
        await update.message.reply_text(
            "Passo 2/4 — Envie o <b>valor</b> em reais (ex: 156,88):", parse_mode="HTML"
        )
        return

    if user_state.state == UserState.AWAITING_ADD_VALOR:
        cents = parse_valor_br(txt)
        if cents is None or cents <= 0:
            await update.message.reply_text(
                "❌ Valor inválido. Envie ex: <code>156,88</code>:", parse_mode="HTML"
            )
            return
        user_state.temp_data["valor_centavos"] = cents
        state_manager.set_state(user_id, UserState.AWAITING_ADD_VENCIMENTO)
        await update.message.reply_text(
            "Passo 3/4 — Envie o <b>vencimento</b> (DD/MM/AAAA):", parse_mode="HTML"
        )
        return

    if user_state.state == UserState.AWAITING_ADD_VENCIMENTO:
        try:
            venc_dt = datetime.strptime(txt, "%d/%m/%Y").replace(tzinfo=UTC)
        except ValueError:
            await update.message.reply_text(
                "❌ Data inválida. Use <code>DD/MM/AAAA</code>:", parse_mode="HTML"
            )
            return
        user_state.temp_data["vencimento"] = venc_dt
        state_manager.set_state(user_id, UserState.AWAITING_ADD_LINHA)
        await update.message.reply_text(
            "Passo 4/4 — Envie a <b>linha digitável</b> (47/48 dígitos) ou digite <code>pular</code>:",
            parse_mode="HTML",
        )
        return

    if user_state.state == UserState.AWAITING_ADD_LINHA:
        linha: str | None = None
        codigo_barras: str | None = None
        if txt.lower() != "pular":
            digits = "".join(c for c in txt if c.isdigit())
            if len(digits) not in (
                BOLETO_LENGTH,
                ARRECADACAO_LENGTH,
            ) or not validar_linha_digitavel(digits):
                await update.message.reply_text(
                    "❌ Linha digitável inválida. Corrija ou digite <code>pular</code>:",
                    parse_mode="HTML",
                )
                return
            linha = digits
            parsed_manual = parse_linha_digitavel(digits)
            codigo_barras = linha_para_codigo_barras(parsed_manual) if parsed_manual else None

        from autobill.db.repository import AuditRepository, db

        manual_key = (
            f"manual:{chat_id}:{user_state.temp_data['beneficiario']}:"
            f"{user_state.temp_data['valor_centavos']}:{user_state.temp_data['vencimento'].isoformat()}:"
            f"{linha or ''}"
        )
        source_hash = hashlib.sha256(manual_key.encode("utf-8")).hexdigest()

        async with db.session() as session:
            repo = BillRepository(session)
            bill = await repo.create_manual_bill(
                user_id=chat_id,
                beneficiario=user_state.temp_data["beneficiario"],
                valor_centavos=user_state.temp_data["valor_centavos"],
                vencimento=user_state.temp_data["vencimento"],
                linha_digitavel=linha,
                codigo_barras=codigo_barras,
                source_hash=source_hash,
            )
            await AuditRepository(session).log_action(chat_id, "create_manual", bill_id=bill.id)

        state_manager.clear_state(user_id)
        await update.message.reply_text(
            f"✅ <b>Fatura #{bill.id} cadastrada!</b>\n\n{format_bill_details(bill, show_confidence=False)}",
            parse_mode="HTML",
        )


async def _handle_pdf_password(update: Update, user_state: UserStateData, user_id: int) -> None:
    """Decrypt protected PDF with provided password without storing password anywhere."""
    if not update.message or not update.message.text:
        return
    pwd = _get_message_text(update).strip()
    file_bytes = user_state.temp_data.get("file_bytes")
    filename = user_state.temp_data.get("filename", "boleto.pdf")
    state_manager.clear_state(user_id)

    if not file_bytes:
        await update.message.reply_text("❌ Arquivo expirado; envie o PDF novamente.")
        return

    processing_msg = await update.message.reply_text("🔓 Desbloqueando e processando PDF...")
    await _process_telegram_upload(update, file_bytes, filename, processing_msg, pdf_password=pwd)


async def _handle_wipe_confirm(update: Update, user_state: UserStateData, user_id: int) -> None:
    """Two-step confirmation for `/apagar_tudo` (RF24)."""
    if not update.message or not update.message.text:
        return
    txt = _get_message_text(update).strip()

    if user_state.state == UserState.AWAITING_WIPE_CONFIRM_1:
        if txt != "APAGAR":
            state_manager.clear_state(user_id)
            await update.message.reply_text("❌ Operação cancelada.")
            return
        state_manager.set_state(user_id, UserState.AWAITING_WIPE_CONFIRM_2)
        await update.message.reply_text(
            "⚠️ <b>Passo 2/2:</b> Confirma irreversivelmente a exclusão de TODOS os dados?\n"
            "Digite exatamente: <code>CONFIRMO APAGAR TUDO</code>",
            parse_mode="HTML",
        )
        return

    if user_state.state == UserState.AWAITING_WIPE_CONFIRM_2:
        state_manager.clear_state(user_id)
        if txt != "CONFIRMO APAGAR TUDO":
            await update.message.reply_text("❌ Operação cancelada.")
            return
        from autobill.ops.retention import wipe_all_data

        summary = await wipe_all_data()
        await update.message.reply_text(
            f"🗑️ <b>Todos os dados foram removidos</b> "
            f"({summary.get('files_removed', 0)} arquivo(s)/banco apagados).",
            parse_mode="HTML",
        )


async def handle_edit_value(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
    user_state: UserStateData,
    user_id: int,
) -> None:
    """Handle field value during editing."""
    bill_id = user_state.bill_id
    field_name = user_state.field_name
    if update.message is None or update.message.text is None:
        return
    new_value = _get_message_text(update).strip()

    if not bill_id or not field_name:
        await update.message.reply_text("❌ Estado inválido. Use /review para recomeçar.")
        state_manager.clear_state(user_id)
        return

    repo = await BillRepository.create_for_bot()
    try:
        bill = await repo.get_bill(bill_id, user_id=user_id)
        if not bill:
            await update.message.reply_text("❌ Boleto não encontrado.")
            state_manager.clear_state(user_id)
            return

        try:
            value = _parse_edited_value(field_name, new_value)
            await repo.update_bill_field(bill_id, field_name, value, user_id=user_id)
            if field_name == "linha_digitavel" and isinstance(value, str):
                parsed = parse_linha_digitavel(value)
                if parsed is not None:
                    cb = linha_para_codigo_barras(parsed)
                    if cb:
                        await repo.update_bill_field(bill_id, "codigo_barras", cb, user_id=user_id)
                        from autobill.rules.boleto import (
                            extrair_fator_vencimento,
                            extrair_valor_codigo_barras,
                        )
                        from autobill.rules.due_date import fator_para_data_auto

                        cb_val = extrair_valor_codigo_barras(cb)
                        if cb_val:
                            await repo.update_bill_field(bill_id, "valor", cb_val, user_id=user_id)
                        fator = extrair_fator_vencimento(cb)
                        if fator is not None:
                            cb_due = fator_para_data_auto(fator)
                            await repo.update_bill_field(
                                bill_id, "vencimento", cb_due.strftime("%d/%m/%Y"), user_id=user_id
                            )
            elif field_name == "codigo_barras" and isinstance(value, str):
                linha = codigo_barras_para_linha(value)
                if linha:
                    await repo.update_bill_field(bill_id, "linha_digitavel", linha, user_id=user_id)

            await update.message.reply_text(
                f"✅ <b>Campo {escape(field_name)} atualizado!</b>",
                parse_mode="HTML",
                reply_markup=get_back_keyboard(f"review_edit:{bill_id}"),
            )

            state_manager.set_state(
                user_id,
                UserState.EDITING_FIELD,
                bill_id=bill_id,
            )

        except ValueError as e:
            await update.message.reply_text(
                f"❌ <b>Valor inválido:</b> {escape(str(e))}\n\nTente novamente.",
                parse_mode="HTML",
            )
    finally:
        await repo.close()


async def handle_config_value(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
    user_state: UserStateData,
    user_id: int,
) -> None:
    """Handle config value input and persist in SQLite `settings`."""
    field_name = user_state.field_name or user_state.temp_data.get("field_name")
    if update.message is None or update.message.text is None or not field_name:
        state_manager.clear_state(user_id)
        return
    chat_id = get_chat_id(update) or user_id
    new_value = _get_message_text(update).strip()
    msg = await _apply_config_change(chat_id, field_name, new_value)
    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_markup=get_back_keyboard("config:done"),
    )
    state_manager.clear_state(user_id)


async def handle_alert_time_value(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
    _user_state: UserStateData,
    user_id: int,
) -> None:
    """Handle alert time input and persist + reschedule."""
    if update.message is None or update.message.text is None:
        return
    chat_id = get_chat_id(update) or user_id
    new_value = _get_message_text(update).strip()
    msg = await _apply_config_change(chat_id, "alert_time", new_value)
    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_markup=get_back_keyboard("alert_config:done"),
    )
    state_manager.clear_state(user_id)


async def handle_snooze_days_value(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
    user_state: UserStateData,
    user_id: int,
) -> None:
    """Handle snooze days input."""
    bill_id = user_state.bill_id
    if update.message is None or update.message.text is None:
        return
    new_value = _get_message_text(update).strip()

    try:
        days = int(new_value)
        if days < 1 or days > MAX_SNOOZE_DAYS:
            _raise_invalid_input("Dias deve ser entre 1 e 365")

        repo = await BillRepository.create_for_bot()
        try:
            await repo.snooze_bill(bill_id, days, user_id=user_id)
            await update.message.reply_text(
                f"😴 <b>Boleto adiado por {days} dias.</b>",
                parse_mode="HTML",
                reply_markup=get_back_keyboard("back:main"),
            )
        finally:
            await repo.close()
    except ValueError as e:
        await update.message.reply_text(
            f"❌ <b>Valor inválido:</b> {escape(str(e))}",
            parse_mode="HTML",
        )
    state_manager.clear_state(user_id)


def register_handlers(application: Any) -> None:
    """Register all handlers with the application."""
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("ajuda", help_command))
    application.add_handler(CommandHandler("bills", bills_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("pendentes", pendentes_command))
    application.add_handler(CommandHandler("proximos", proximos_command))
    application.add_handler(CommandHandler("mes", mes_command))
    application.add_handler(CommandHandler("fatura", fatura_command))
    application.add_handler(CommandHandler("pagas", pagas_command))
    application.add_handler(CommandHandler("pago", pago_command))
    application.add_handler(CommandHandler("desfazer", desfazer_command))
    application.add_handler(CommandHandler("adicionar", adicionar_command))
    application.add_handler(CommandHandler("editar", editar_command))
    application.add_handler(CommandHandler("remover", remover_command))
    application.add_handler(CommandHandler("paginas", paginas_command))
    application.add_handler(CommandHandler("exportar", exportar_command))
    application.add_handler(CommandHandler("apagar_tudo", apagar_tudo_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("config", config_command))

    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    apply_middlewares(application)
    logger.info("All bot handlers registered")


def build_application(token: str | None = None) -> Application:
    """Build configured `python-telegram-bot` Application."""
    bot_token = token or settings.telegram_bot_token
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN não configurado.")
    app = Application.builder().token(bot_token).build()
    register_handlers(app)
    return app


def run_bot() -> None:
    """Entrypoint `autobill-bot` para executar apenas o bot Telegram."""
    from autobill.db.repository import db
    from autobill.logging_config import setup_logging

    setup_logging(json_logs=settings.log_json, log_level=settings.log_level)
    settings.ensure_directories()

    async def _init_db() -> None:
        await db.connect()
        await db.create_tables()

    asyncio.run(_init_db())
    app = build_application()
    app.run_polling(drop_pending_updates=False)
