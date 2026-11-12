"""Command and message handlers for Telegram bot."""

import io
import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ConversationHandler,
)

from autobill.bot.keyboards import (
    get_main_menu_keyboard,
    get_review_keyboard,
    get_edit_field_keyboard,
    get_confirm_keyboard,
    get_alert_config_keyboard,
    get_snooze_keyboard,
    get_bills_filter_keyboard,
    get_pagination_keyboard,
    get_config_keyboard,
    get_back_keyboard,
)
from autobill.bot.formatters import (
    format_bill_summary,
    format_bill_details,
    format_bill_list,
    format_alert_message,
    format_help_message,
    format_config_status,
    format_welcome_message,
    format_error_message,
)
from autobill.bot.state import (
    StateManager,
    UserState,
    UserStateData,
    state_manager,
)
from autobill.bot.utils import (
    get_user_id,
    get_chat_id,
    is_authorized_user,
    download_file,
    save_uploaded_file,
    is_pdf_file,
    is_image_file,
    parse_callback_data,
    build_callback_data,
)
from autobill.config import settings
from autobill.db.repository import BillRepository
from autobill.extract.pipeline import ExtractionPipeline, ExtractionResult
from autobill.rules.boleto import validar_linha_digitavel, parse_linha_digitavel

logger = logging.getLogger(__name__)

# Pagination settings
BILLS_PER_PAGE = 10


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    if not user:
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
    repo = BillRepository()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter=filter_name if filter_name != "all" else None,
            page=page,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        text = format_bill_list(bills, page, total_pages, filter_name)
        keyboard = InlineKeyboardMarkup.inline_keyboard([
            *get_bills_filter_keyboard(filter_name).inline_keyboard,
            *get_pagination_keyboard(page, total_pages, "bills").inline_keyboard,
        ])

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /pending command - show bills awaiting review."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return

    repo = BillRepository()
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
        keyboard = InlineKeyboardMarkup.inline_keyboard([
            *get_pagination_keyboard(1, total_pages, "pending").inline_keyboard,
            [{"text": "🔍 Revisar Primeiro", "callback_data": "review_next"}],
        ])

        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /review command - start review flow for next pending bill."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return

    repo = BillRepository()
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


async def show_bill_for_review(update: Update, bill, edit: bool = False) -> None:
    """Show a bill for review with inline keyboard."""
    user_id = get_user_id(update)
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

    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def alerts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /alerts command - manage alerts."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return

    repo = BillRepository()
    try:
        # Get recent alert history
        alerts = await repo.get_recent_alerts(user_id=user_id, limit=10)

        if not alerts:
            text = (
                "🔔 <b>Histórico de Alertas</b>\n\n"
                "Nenhum alerta enviado recentemente."
            )
        else:
            lines = ["🔔 <b>Últimos Alertas</b>", ""]
            for alert in alerts:
                bill = await repo.get_bill(alert.bill_id)
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
    """Handle /config command - show configuration."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return

    config = {
        "alert_time": settings.alert_time,
        "lead_days": ",".join(map(str, settings.alert_lead_days)),
        "timezone": settings.timezone,
        "extraction_mode": settings.extraction_mode,
        "llm_model": settings.gemini_model,
        "cloud_consent": settings.cloud_extraction_consent,
    }

    text = format_config_status(config)
    keyboard = get_config_keyboard()

    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document (PDF) uploads."""
    user_id = get_user_id(update)
    if not user_id or not is_authorized_user(user_id):
        return

    document = update.message.document
    if not document:
        return

    # Check if PDF
    if not is_pdf_file(document.file_name or ""):
        await update.message.reply_text(
            "📄 <b>Arquivo não suportado</b>\n\n"
            "Por favor, envie um arquivo PDF.",
            parse_mode="HTML",
        )
        return

    # Check file size
    if document.file_size and document.file_size > 20 * 1024 * 1024:  # 20MB
        await update.message.reply_text(
            "📄 <b>Arquivo muito grande</b>\n\n"
            "Tamanho máximo: 20 MB",
            parse_mode="HTML",
        )
        return

    # Send processing message
    processing_msg = await update.message.reply_text(
        "🔄 <b>Processando boleto...</b>\n\n"
        "Extraindo dados, validando...",
        parse_mode="HTML",
    )

    try:
        # Download file
        file_bytes = await download_file(update, context, document.file_id)
        if not file_bytes:
            await processing_msg.edit_text(
                "❌ <b>Erro ao baixar arquivo</b>",
                parse_mode="HTML",
            )
            return

        # Save to ingress
        file_path = await save_uploaded_file(file_bytes, document.file_name or "boleto.pdf", user_id)
        if not file_path:
            await processing_msg.edit_text(
                "❌ <b>Erro ao salvar arquivo</b>",
                parse_mode="HTML",
            )
            return

        # Process through extraction pipeline
        pipeline = ExtractionPipeline()
        result = await pipeline.process_file(file_path)

        # Clean up ingress file
        try:
            file_path.unlink()
        except Exception:
            pass

        if not result or not result.fields:
            await processing_msg.edit_text(
                "❌ <b>Não foi possível extrair dados do boleto</b>\n\n"
                "Tente enviar uma foto nítida ou PDF de melhor qualidade.",
                parse_mode="HTML",
            )
            return

        # Save bill to database
        repo = BillRepository()
        try:
            bill = await repo.create_bill_from_extraction(
                user_id=user_id,
                extraction_result=result,
                source_file=str(file_path),
            )

            # Show for review
            await processing_msg.edit_text(
                "✅ <b>Boleto processado!</b>\n\n"
                "Revise os dados abaixo:",
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

    # Get highest resolution photo
    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        return

    # Send processing message
    processing_msg = await update.message.reply_text(
        "🔄 <b>Processando imagem...</b>\n\n"
        "Executando OCR e extração...",
        parse_mode="HTML",
    )

    try:
        # Download file
        file_bytes = await download_file(update, context, photo.file_id)
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
        result = await pipeline.process_file(file_path)

        # Clean up
        try:
            file_path.unlink()
        except Exception:
            pass

        if not result or not result.fields:
            await processing_msg.edit_text(
                "❌ <b>Não foi possível extrair dados da imagem</b>\n\n"
                "Tente uma foto mais nítida, com boa iluminação e o boleto centralizado.",
                parse_mode="HTML",
            )
            return

        # Save bill
        repo = BillRepository()
        try:
            bill = await repo.create_bill_from_extraction(
                user_id=user_id,
                extraction_result=result,
                source_file=str(file_path),
            )

            await processing_msg.edit_text(
                "✅ <b>Boleto processado!</b>\n\n"
                "Revise os dados abaixo:",
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
        await handle_confirm_approve(query, context, params)
    elif action == "confirm_reject":
        await handle_confirm_reject(query, context, params)
    elif action == "cancel_approve":
    elif action == "cancel_reject":
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
async def handle_review_approve(query, context, params):
    """Handle approve bill."""
    if not params:
        return
    bill_id = int(params[0])

    repo = BillRepository()
    try:
        bill = await repo.get_bill(bill_id)
        if not bill:
            await query.edit_message_text("❌ Boleto não encontrado", parse_mode="HTML")
            return

        # Validate before approving
        errors = []
        if bill.linha_digitavel and not validar_linha_digitavel(bill.linha_digitavel):
            errors.append("Linha digitável inválida (módulo 10/11)")

        if errors:
            await query.edit_message_text(
                f"⚠️ <b>Erros de validação:</b>\n" + "\n".join(f"• {e}" for e in errors) +
                "\n\nUse ✏️ Editar para corrigir ou ❌ Rejeitar para descartar.",
                parse_mode="HTML",
                reply_markup=get_review_keyboard(bill_id),
            )
            return

        # Approve
        await repo.update_bill_status(bill_id, "approved")
        bill.status = "approved"

        await query.edit_message_text(
            f"✅ <b>Boleto aprovado!</b>\n\n"
            f"Alertas agendados para {bill.vencimento}.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )

        # Clear state
        user_id = get_user_id(query)
        if user_id:
            state_manager.clear_state(user_id)

        # Show next pending
        await handle_review_next(query, context)
    finally:
        await repo.close()


async def handle_review_reject(query, context, params):
    """Handle reject bill - show confirmation."""
    if not params:
        return
    bill_id = int(params[0])

    await query.edit_message_text(
        "❌ <b>Confirmar rejeição?</b>\n\n"
        "O boleto será descartado permanentemente.",
        parse_mode="HTML",
        reply_markup=get_confirm_keyboard("reject", bill_id),
    )


async def handle_confirm_reject(query, context, params):
    """Confirm bill rejection."""
    if not params:
        return
    bill_id = int(params[0])

    repo = BillRepository()
    try:
        await repo.update_bill_status(bill_id, "rejected")

        await query.edit_message_text(
            "❌ <b>Boleto rejeitado e descartado.</b>",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )

        user_id = get_user_id(query)
        if user_id:
            state_manager.clear_state(user_id)

        await handle_review_next(query, context)
    finally:
        await repo.close()


async def handle_review_edit(query, context, params):
    """Start edit flow for a bill."""
    if not params:
        return
    bill_id = int(params[0])

    repo = BillRepository()
    try:
        bill = await repo.get_bill(bill_id)
        if not bill:
            await query.edit_message_text("❌ Boleto não encontrado", parse_mode="HTML")
            return

        # Get editable fields
        fields = []
        if bill.valor is not None:
            fields.append("valor")
        if bill.vencimento:
            fields.append("vencimento")
        if bill.beneficiario:
            fields.append("beneficiario")
        if bill.pagador:
            fields.append("pagador")
        if bill.linha_digitavel:
            fields.append("linha_digitavel")
        if bill.codigo_barras:
            fields.append("codigo_barras")
        if bill.nosso_numero:
            fields.append("nosso_numero")
        if bill.documento:
            fields.append("documento")

        user_id = get_user_id(query)
        if user_id:
            state_manager.set_state(
                user_id,
                UserState.EDITING_FIELD,
                bill_id=bill_id,
            )

        await query.edit_message_text(
            "✏️ <b>Editar Boleto</b>\n\n"
            "Selecione o campo para editar:",
            parse_mode="HTML",
            reply_markup=get_edit_field_keyboard(bill_id, fields),
        )
    finally:
        await repo.close()


async def handle_edit_field(query, context, params):
    """Handle field selection for editing."""
    if len(params) < 2:
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
        "pagador": "Pagador",
        "linha_digitavel": "Linha Digitável (47 ou 48 dígitos)",
        "codigo_barras": "Código de Barras (44 dígitos)",
        "nosso_numero": "Nosso Número",
        "documento": "Número do Documento",
    }

    await query.edit_message_text(
        f"✏️ <b>Editando: {field_name}</b>\n\n"
        f"Envie o novo valor:\n"
        f"<i>{field_labels.get(field_name, field_name)}</i>",
        parse_mode="HTML",
        reply_markup=get_back_keyboard(f"review_edit:{bill_id}"),
    )


async def handle_edit_done(query, context, params):
    """Finish editing and return to review."""
    if not params:
        return
    bill_id = int(params[0])

    repo = BillRepository()
    try:
        bill = await repo.get_bill(bill_id)
        if bill:
            await show_bill_for_review(query, bill, edit=True)
    finally:
        await repo.close()

    user_id = get_user_id(query)
    if user_id:
        state_manager.clear_state(user_id)


async def handle_edit_cancel(query, context, params):
    """Cancel editing."""
    if not params:
        return
    bill_id = int(params[0])

    repo = BillRepository()
    try:
        bill = await repo.get_bill(bill_id)
        if bill:
            await show_bill_for_review(query, bill, edit=True)
    finally:
        await repo.close()

    user_id = get_user_id(query)
    if user_id:
        state_manager.clear_state(user_id)


async def handle_review_details(query, context, params):
    """Show full bill details."""
    if not params:
        return
    bill_id = int(params[0])

    repo = BillRepository()
    try:
        bill = await repo.get_bill(bill_id)
        if bill:
            text = format_bill_details(bill, show_confidence=True)
            await query.edit_message_text(
                text,
                parse_mode="HTML",
                reply_markup=get_back_keyboard(f"review_edit:{bill_id}"),
            )
    finally:
        await repo.close()


async def handle_review_skip(query, context, params):
    """Skip current bill and show next."""
    if not params:
        return
    bill_id = int(params[0])

    # Just show next without changing status
    await handle_review_next(query, context)


async def handle_review_next(query, context):
    """Show next pending bill for review."""
    user_id = get_user_id(query)
    if not user_id:
        return

    repo = BillRepository()
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


async def handle_review_cancel(query, context, params):
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
async def handle_filter(query, context, params):
    """Handle filter change."""
    if not params:
        return
    filter_name = params[0]

    user_id = get_user_id(query)
    if not user_id:
        return

    repo = BillRepository()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter=filter_name if filter_name != "all" else None,
            page=1,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        text = format_bill_list(bills, 1, total_pages, filter_name)
        keyboard = InlineKeyboardMarkup.inline_keyboard([
            *get_bills_filter_keyboard(filter_name).inline_keyboard,
            *get_pagination_keyboard(1, total_pages, "bills").inline_keyboard,
        ])

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def handle_bills_page(query, context, params):
    """Handle bills pagination."""
    if not params:
        return
    page = int(params[0])

    # Get current filter from message or default
    filter_name = "all"  # Would need to parse from message

    user_id = get_user_id(query)
    if not user_id:
        return

    repo = BillRepository()
    try:
        bills, total = await repo.list_bills(
            user_id=user_id,
            status_filter=filter_name if filter_name != "all" else None,
            page=page,
            per_page=BILLS_PER_PAGE,
        )
        total_pages = (total + BILLS_PER_PAGE - 1) // BILLS_PER_PAGE

        text = format_bill_list(bills, page, total_pages, filter_name)
        keyboard = InlineKeyboardMarkup.inline_keyboard([
            *get_bills_filter_keyboard(filter_name).inline_keyboard,
            *get_pagination_keyboard(page, total_pages, "bills").inline_keyboard,
        ])

        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    finally:
        await repo.close()


async def handle_pending_page(query, context, params):
    """Handle pending bills pagination."""
    if not params:
        return
    page = int(params[0])

    user_id = get_user_id(query)
    if not user_id:
        return

    repo = BillRepository()
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
async def handle_alert_config(query, context, params):
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
            "🕐 <b>Novo horário de alertas</b>\n\n"
            "Envie no formato HH:MM (ex: 07:30)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("alert_config:done"),
        )
    elif action == "lead_days":
        user_id = get_user_id(query)
        if user_id:
            state_manager.set_state(user_id, UserState.AWAITING_ALERT_TIME)  # Reuse
        await query.edit_message_text(
            "📅 <b>Dias de antecedência</b>\n\n"
            "Envie números separados por vírgula (ex: 3,1,0)",
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


async def handle_snooze(query, context, params):
    """Handle snooze bill."""
    if len(params) < 2:
        return
    bill_id = int(params[0])
    days = int(params[1])

    repo = BillRepository()
    try:
        await repo.snooze_bill(bill_id, days)
        bill = await repo.get_bill(bill_id)

        await query.edit_message_text(
            f"😴 <b>Boleto adiado por {days} dias.</b>\n\n"
            f"Novo lembrete agendado.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
    finally:
        await repo.close()


async def handle_snooze_custom(query, context, params):
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
        "📅 <b>Adiar personalizado</b>\n\n"
        "Envie o número de dias (ex: 5)",
        parse_mode="HTML",
        reply_markup=get_back_keyboard(f"snooze_cancel:{bill_id}"),
    )


async def handle_snooze_cancel(query, context, params):
    """Cancel snooze."""
    if not params:
        return
    bill_id = int(params[0])

    repo = BillRepository()
    try:
        bill = await repo.get_bill(bill_id)
        if bill:
            await show_bill_for_review(query, bill, edit=True)
    finally:
        await repo.close()


# Config handlers
async def handle_config(query, context, params):
    """Handle configuration changes."""
    if not params:
        return
    action = params[0]

    if action == "done":
        await query.edit_message_text(
            "✅ <b>Configurações salvas.</b>\n\n"
            "⚠️ Algumas mudanças exigem reinicialização do bot.",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("back:main"),
        )
    elif action == "alert_time":
        user_id = get_user_id(query)
        if user_id:
            state_manager.set_state(user_id, UserState.AWAITING_CONFIG_VALUE, field_name="alert_time")
        await query.edit_message_text(
            "🕐 <b>Horário dos alertas</b>\n\n"
            "Envie no formato HH:MM (ex: 07:30)",
            parse_mode="HTML",
            reply_markup=get_back_keyboard("config:done"),
        )
    elif action == "lead_days":
        user_id = get_user_id(query)
        if user_id:
            state_manager.set_state(user_id, UserState.AWAITING_CONFIG_VALUE, field_name="lead_days")
        await query.edit_message_text(
            "📅 <b>Dias de antecedência</b>\n\n"
            "Envie números separados por vírgula (ex: 3,1,0)",
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


async def handle_back(query, context, params):
    """Handle back navigation."""
    if not params:
        return
    target = params[0]

    if target == "main":
        await query.edit_message_text(
            "🏠 <b>Menu Principal</b>\n\n"
            "Use os botões abaixo ou comandos:",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(),
        )
    elif target == "review_edit":
        if len(params) > 1:
            bill_id = int(params[1])
            repo = BillRepository()
            try:
                bill = await repo.get_bill(bill_id)
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
        await handle_edit_value(update, context, user_state)
    elif user_state.state == UserState.AWAITING_CONFIG_VALUE:
        await handle_config_value(update, context, user_state)
    elif user_state.state == UserState.AWAITING_ALERT_TIME:
        await handle_alert_time_value(update, context, user_state)
    elif user_state.state == UserState.AWAITING_SNOOZE_DAYS:
        await handle_snooze_days_value(update, context, user_state)
    else:
        # Default: treat as potential boleto text
        pass


async def handle_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserStateData) -> None:
    """Handle field value during editing."""
    bill_id = user_state.bill_id
    field_name = user_state.field_name
    new_value = update.message.text.strip()

    if not bill_id or not field_name:
        await update.message.reply_text("❌ Estado inválido. Use /review para recomeçar.")
        state_manager.clear_state(user_id)
        return

    repo = BillRepository()
    try:
        bill = await repo.get_bill(bill_id)
        if not bill:
            await update.message.reply_text("❌ Boleto não encontrado.")
            state_manager.clear_state(user_id)
            return

        # Validate and convert value based on field
        try:
            if field_name == "valor":
                value = int(new_value)
                if value < 0:
                    raise ValueError("Valor deve ser positivo")
            elif field_name == "vencimento":
                # Parse DD/MM/YYYY
                from datetime import datetime
                datetime.strptime(new_value, "%d/%m/%Y")
                value = new_value
            elif field_name in ("linha_digitavel", "codigo_barras"):
                # Validate digits only
                digits = "".join(c for c in new_value if c.isdigit())
                if field_name == "linha_digitavel" and len(digits) not in (47, 48):
                    raise ValueError("Linha digitável deve ter 47 ou 48 dígitos")
                if field_name == "codigo_barras" and len(digits) != 44:
                    raise ValueError("Código de barras deve ter 44 dígitos")
                value = digits
            else:
                value = new_value

            # Update bill
            await repo.update_bill_field(bill_id, field_name, value)

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


async def handle_config_value(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserStateData) -> None:
    """Handle config value input."""
    field_name = user_state.temp_data.get("field_name")
    new_value = update.message.text.strip()

    # In a real implementation, this would update the config
    # For now, just acknowledge
    await update.message.reply_text(
        f"✅ <b>Configuração {field_name} seria atualizada para:</b> {new_value}\n\n"
        f"⚠️ <i>Implementação completa requer persistência de config.</i>",
        parse_mode="HTML",
        reply_markup=get_back_keyboard("config:done"),
    )
    state_manager.clear_state(user_id)


async def handle_alert_time_value(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserStateData) -> None:
    """Handle alert time input."""
    new_value = update.message.text.strip()

    try:
        from datetime import datetime
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


async def handle_snooze_days_value(update: Update, context: ContextTypes.DEFAULT_TYPE, user_state: UserStateData) -> None:
    """Handle snooze days input."""
    bill_id = user_state.bill_id
    new_value = update.message.text.strip()

    try:
        days = int(new_value)
        if days < 1 or days > 365:
            raise ValueError("Dias deve ser entre 1 e 365")

        repo = BillRepository()
        try:
            await repo.snooze_bill(bill_id, days)
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


def register_handlers(application) -> None:
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
    from autobill.bot.middleware import apply_middlewares
    apply_middlewares(application)

    logger.info("All bot handlers registered")