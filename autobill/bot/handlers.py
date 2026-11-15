"""Command and message handlers for Telegram bot."""

import asyncio
import hashlib
import logging
from contextlib import suppress
from datetime import datetime
from typing import Any, NoReturn

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
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
    get_edit_field_keyboard,
    get_main_menu_keyboard,
    get_pagination_keyboard,
    get_review_keyboard,
)
from autobill.bot.middleware import apply_middlewares
from autobill.bot.state import (
    UserState,
    UserStateData,
    state_manager,
)
from autobill.bot.utils import (
    download_file,
    get_user_id,
    is_authorized_user,
    is_pdf_file,
    parse_callback_data,
    save_uploaded_file,
)
from autobill.config import settings
from autobill.db.models import Bill, BillStatus
from autobill.db.repository import BillRepository
from autobill.extract.pipeline import ExtractionPipeline
from autobill.rules.boleto import (
    ARRECADACAO_LENGTH,
    BOLETO_LENGTH,
    CODIGO_BARRAS_LENGTH,
    validar_linha_digitavel,
)

logger = logging.getLogger(__name__)

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
        if field_name == "codigo_barras" and len(digits) != CODIGO_BARRAS_LENGTH:
            _raise_invalid_input("Código de barras deve ter 44 dígitos")
        return digits

    return new_value


def _get_message(update: Update) -> Message:
    message = update.effective_message
    if message is None:
        _raise_invalid_input("Update does not contain a message")
    return message


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


async def config_command(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /config command - show configuration."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    config = {
        "alert_time": settings.alert_time,
        "lead_days": ",".join(map(str, settings.lead_days)),
        "timezone": settings.default_timezone,
        "extraction_mode": settings.extractor,
        "llm_model": settings.llm_vision_model,
        "cloud_consent": settings.cloud_extraction_consent,
    }

    text = format_config_status(config)
    keyboard = get_config_keyboard()

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def handle_document(  # noqa: PLR0911
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle document (PDF) uploads."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    document = update.message.document
    if not document:
        return

    # Check if PDF
    if not is_pdf_file(document.file_name or ""):
        await update.message.reply_text(
            "📄 <b>Arquivo não suportado</b>\n\nPor favor, envie um arquivo PDF.",
            parse_mode="HTML",
        )
        return

    # Check file size
    if document.file_size and document.file_size > 20 * 1024 * 1024:  # 20MB
        await update.message.reply_text(
            "📄 <b>Arquivo muito grande</b>\n\nTamanho máximo: 20 MB",
            parse_mode="HTML",
        )
        return

    # Send processing message
    processing_msg = await update.message.reply_text(
        "🔄 <b>Processando boleto...</b>\n\nExtraindo dados, validando...",
        parse_mode="HTML",
    )

    try:
        # Download file
        file_bytes = await download_file(context, document.file_id)
        if not file_bytes:
            await processing_msg.edit_text(
                "❌ <b>Erro ao baixar arquivo</b>",
                parse_mode="HTML",
            )
            return

        # Save to ingress
        file_path = await save_uploaded_file(
            file_bytes, document.file_name or "boleto.pdf", user_id
        )
        if not file_path:
            await processing_msg.edit_text(
                "❌ <b>Erro ao salvar arquivo</b>",
                parse_mode="HTML",
            )
            return

        # Process through extraction pipeline
        pipeline = ExtractionPipeline()
        result = await asyncio.to_thread(pipeline.extract, file_path)

        # Clean up ingress file
        with suppress(Exception):
            file_path.unlink()

        if not result.is_valid():
            await processing_msg.edit_text(
                "❌ <b>Não foi possível extrair dados do boleto</b>\n\n"
                "Tente enviar uma foto nítida ou PDF de melhor qualidade.",
                parse_mode="HTML",
            )
            return

        # Save bill to database
        repo = await BillRepository.create_for_bot()
        try:
            bill = await repo.create_bill_from_extraction(
                user_id=user_id,
                extraction_result=result,
                source_file=str(file_path),
                source_hash=hashlib.sha256(file_bytes).hexdigest(),
            )

            # Show for review
            await processing_msg.edit_text(
                "✅ <b>Boleto processado!</b>\n\nRevise os dados abaixo:",
                parse_mode="HTML",
            )
            await show_bill_for_review(update, bill)
        finally:
            await repo.close()

    except Exception as e:
        logger.exception("Error processing document")
        await processing_msg.edit_text(
            format_error_message(str(e)),
            parse_mode="HTML",
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo uploads."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return
    if update.message is None:
        return

    # Get highest resolution photo
    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        return

    # Send processing message
    processing_msg = await update.message.reply_text(
        "🔄 <b>Processando imagem...</b>\n\nExecutando OCR e extração...",
        parse_mode="HTML",
    )

    try:
        # Download file
        file_bytes = await download_file(context, photo.file_id)
        if not file_bytes:
            await processing_msg.edit_text(
                "❌ <b>Erro ao baixar imagem</b>",
                parse_mode="HTML",
            )
            return

        # Save to ingress
        file_path = await save_uploaded_file(file_bytes, "boleto.jpg", user_id)
        if not file_path:
            await processing_msg.edit_text(
                "❌ <b>Erro ao salvar imagem</b>",
                parse_mode="HTML",
            )
            return

        # Process through extraction pipeline
        pipeline = ExtractionPipeline()
        result = await asyncio.to_thread(pipeline.extract, file_path)

        # Clean up
        with suppress(Exception):
            file_path.unlink()

        if not result.is_valid():
            await processing_msg.edit_text(
                "❌ <b>Não foi possível extrair dados da imagem</b>\n\n"
                "Tente uma foto mais nítida, com boa iluminação e o boleto centralizado.",
                parse_mode="HTML",
            )
            return

        # Save bill
        repo = await BillRepository.create_for_bot()
        try:
            bill = await repo.create_bill_from_extraction(
                user_id=user_id,
                extraction_result=result,
                source_file=str(file_path),
                source_hash=hashlib.sha256(file_bytes).hexdigest(),
            )

            await processing_msg.edit_text(
                "✅ <b>Boleto processado!</b>\n\nRevise os dados abaixo:",
                parse_mode="HTML",
            )
            await show_bill_for_review(update, bill)
        finally:
            await repo.close()

    except Exception as e:
        logger.exception("Error processing photo")
        await processing_msg.edit_text(
            format_error_message(str(e)),
            parse_mode="HTML",
        )


async def handle_callback_query(  # noqa: C901, PLR0912, PLR0915
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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

    # Route to appropriate handler
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
    elif action in ("cancel_approve", "cancel_reject"):
        await handle_review_cancel(query, context, params)
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

        # Get editable fields
        fields = []
        if bill.valor_centavos is not None:
            fields.append("valor")
        if bill.vencimento:
            fields.append("vencimento")
        if bill.beneficiario:
            fields.append("beneficiario")
        if bill.linha_digitavel:
            fields.append("linha_digitavel")
        if bill.codigo_barras:
            fields.append("codigo_barras")

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

    if action == "done":
        await query.edit_message_text(
            "✅ <b>Configuração de alertas salva.</b>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
    elif action == "time":
        user_id = get_user_id(query)
        if user_id:
            state_manager.set_state(user_id, UserState.AWAITING_ALERT_TIME)
        await query.edit_message_text(
            "🕐 <b>Novo horário de alertas</b>\n\nEnvie no formato HH:MM (ex: 07:30)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("alert_config:done"),
        )
    elif action == "lead_days":
        user_id = get_user_id(query)
        if user_id:
            state_manager.set_state(user_id, UserState.AWAITING_ALERT_TIME)  # Reuse
        await query.edit_message_text(
            "📅 <b>Dias de antecedência</b>\n\nEnvie números separados por vírgula (ex: 3,1,0)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("alert_config:done"),
        )
    elif action == "mute":
        # Would update user preferences
        await query.edit_message_text(
            "🔕 <b>Alertas silenciados.</b>",
            parse_mode="HTML",
            reply_markup=get_alert_config_keyboard(),
        )
    elif action == "unmute":
        await query.edit_message_text(
            "🔊 <b>Alertas ativados.</b>",
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

    if action == "done":
        await query.edit_message_text(
            "✅ <b>Configurações salvas.</b>\n\n⚠️ Algumas mudanças exigem reinicialização do bot.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
    elif action == "alert_time":
        user_id = get_user_id(query)
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
        user_id = get_user_id(query)
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
        await query.edit_message_text(
            "🌍 <b>Fuso horário</b>\n\n"
            "Use /config timezone <zona> (ex: America/Sao_Paulo)\n\n"
            "Fusos comuns:\n"
            "• America/Fortaleza\n"
            "• America/Sao_Paulo\n"
            "• America/Recife\n"
            "• UTC",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "extraction_mode":
        await query.edit_message_text(
            "🔧 <b>Modo de extração</b>\n\n"
            "Opções:\n"
            "• auto - Tenta PDF → Código barras → LLM → OCR\n"
            "• cloud - Usa apenas LLM (Gemini)\n"
            "• local - Usa apenas OCR local\n\n"
            "Use /config extraction_mode <modo>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "cloud_consent":
        # Toggle consent
        await query.edit_message_text(
            "☁️ <b>Consentimento para nuvem</b>\n\n"
            "Use /config cloud_consent true/false\n\n"
            "⚠️ Envia imagens para Google Gemini.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
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
    """Handle text messages for multi-step flows."""
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
    else:
        # Default: treat as potential boleto text
        pass


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

            # Update bill
            await repo.update_bill_field(bill_id, field_name, value, user_id=user_id)

            await update.message.reply_text(
                f"✅ <b>Campo {field_name} atualizado!</b>",
                parse_mode="HTML",
                reply_markup=get_back_keyboard(f"review_edit:{bill_id}"),
            )

            # Return to edit menu
            state_manager.set_state(
                user_id,
                UserState.EDITING_FIELD,
                bill_id=bill_id,
            )

        except ValueError as e:
            await update.message.reply_text(
                f"❌ <b>Valor inválido:</b> {e}\n\nTente novamente.",
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
    """Handle config value input."""
    field_name = user_state.temp_data.get("field_name")
    if update.message is None or update.message.text is None:
        return
    new_value = _get_message_text(update).strip()

    # In a real implementation, this would update the config
    # For now, just acknowledge
    await update.message.reply_text(
        f"✅ <b>Configuração {field_name} seria atualizada para:</b> {new_value}\n\n"
        f"⚠️ <i>Implementação completa requer persistência de config.</i>",
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
    """Handle alert time input."""
    if update.message is None or update.message.text is None:
        return
    new_value = _get_message_text(update).strip()

    try:
        datetime.strptime(new_value, "%H:%M")

        await update.message.reply_text(
            f"✅ <b>Horário de alertas atualizado para:</b> {new_value}",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("alert_config:done"),
        )
    except ValueError:
        await update.message.reply_text(
            "❌ <b>Formato inválido.</b> Use HH:MM (ex: 07:30)",
            parse_mode="HTML",
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
            f"❌ <b>Valor inválido:</b> {e}",
            parse_mode="HTML",
        )
    state_manager.clear_state(user_id)


def register_handlers(application: Any) -> None:
    """Register all handlers with the application."""
    # Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("bills", bills_command))
    application.add_handler(CommandHandler("pending", pending_command))
    application.add_handler(CommandHandler("review", review_command))
    application.add_handler(CommandHandler("alerts", alerts_command))
    application.add_handler(CommandHandler("config", config_command))

    # Messages
    application.add_handler(MessageHandler(filters.Document.PDF, handle_document))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Callbacks
    application.add_handler(CallbackQueryHandler(handle_callback_query))

    # Apply middlewares
    apply_middlewares(application)

    logger.info("All bot handlers registered")
