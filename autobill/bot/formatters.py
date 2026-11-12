"""Message formatters for Telegram bot."""

from datetime import datetime

from autobill.db.models import Bill, BillStatus
from autobill.rules.money import formatar_centavos


def format_bill_summary(bill: Bill, show_id: bool = True) -> str:
    """Format a bill as a summary line."""
    parts = []

    if show_id:
        parts.append(f"#{bill.id}")

    if bill.valor:
        parts.append(f"💰 {formatar_centavos(bill.valor)}")

    if bill.vencimento:
        try:
            venc = datetime.fromisoformat(bill.vencimento)
            parts.append(f"📅 {venc.strftime('%d/%m/%Y')}")
        except ValueError:
            parts.append(f"📅 {bill.vencimento}")

    if bill.beneficiario:
        parts.append(f"🏢 {bill.beneficiario[:30]}")

    if bill.pagador:
        parts.append(f"👤 {bill.pagador[:30]}")

    status_emoji = {
        BillStatus.PENDING: "⏳",
        BillStatus.APPROVED: "✅",
        BillStatus.REJECTED: "❌",
        BillStatus.PAID: "💰",
        BillStatus.OVERDUE: "⚠️",
        BillStatus.SNOOZED: "😴",
    }
    parts.append(f"{status_emoji.get(bill.status, '❓')} {bill.status.value}")

    return " | ".join(parts)


def format_bill_details(bill: Bill, show_confidence: bool = True) -> str:
    """Format full bill details for review."""
    lines = [
        f"<b>📄 Boleto #{bill.id}</b>",
        f"<b>Status:</b> {bill.status.value}",
        "",
    ]

    if bill.valor:
        lines.append(f"💰 <b>Valor:</b> {formatar_centavos(bill.valor)}")

    if bill.vencimento:
        try:
            venc = datetime.fromisoformat(bill.vencimento)
            today = datetime.now().date()
            venc_date = venc.date()
            days_diff = (venc_date - today).days

            if days_diff < 0:
                status = f"⚠️ VENCIDO há {abs(days_diff)} dias"
            elif days_diff == 0:
                status = "⚠️ VENCE HOJE"
            elif days_diff <= 3:
                status = f"⚠️ Vence em {days_diff} dias"
            else:
                status = f"📅 Em {days_diff} dias"

            lines.append(f"📅 <b>Vencimento:</b> {venc.strftime('%d/%m/%Y')} ({status})")
        except ValueError:
            lines.append(f"📅 <b>Vencimento:</b> {bill.vencimento}")

    if bill.beneficiario:
        lines.append(f"🏢 <b>Beneficiário:</b> {bill.beneficiario}")

    if bill.pagador:
        lines.append(f"👤 <b>Pagador:</b> {bill.pagador}")

    if bill.linha_digitavel:
        masked = bill.linha_digitavel[:-4].replace(bill.linha_digitavel[:-4], "•" * (len(bill.linha_digitavel) - 4))
        lines.append(f"🔢 <b>Linha Digitável:</b> <code>{masked}{bill.linha_digitavel[-4:]}</code>")

    if bill.codigo_barras:
        masked = bill.codigo_barras[:-4].replace(bill.codigo_barras[:-4], "•" * (len(bill.codigo_barras) - 4))
        lines.append(f"📊 <b>Código de Barras:</b> <code>{masked}{bill.codigo_barras[-4:]}</code>")

    if bill.nosso_numero:
        lines.append(f"🔖 <b>Nosso Número:</b> {bill.nosso_numero}")

    if bill.documento:
        lines.append(f"📋 <b>Documento:</b> {bill.documento}")

    if bill.data_documento:
        lines.append(f"📝 <b>Data Documento:</b> {bill.data_documento}")

    if bill.especie:
        lines.append(f"📄 <b>Espécie:</b> {bill.especie}")

    if bill.aceite:
        lines.append(f"✍️ <b>Aceite:</b> {bill.aceite}")

    if bill.data_processamento:
        lines.append(f"⚙️ <b>Data Processamento:</b> {bill.data_processamento}")

    if bill.instrucoes:
        lines.append(f"📝 <b>Instruções:</b> {bill.instrucoes[:200]}")

    if bill.local_pagamento:
        lines.append(f"🏦 <b>Local Pagamento:</b> {bill.local_pagamento}")

    if bill.source:
        lines.append(f"📥 <b>Origem:</b> {bill.source}")

    if show_confidence and bill.confidence is not None:
        conf_pct = int(bill.confidence * 100)
        conf_emoji = "🟢" if conf_pct >= 80 else "🟡" if conf_pct >= 50 else "🔴"
        lines.append(f"{conf_emoji} <b>Confiança:</b> {conf_pct}%")

    if bill.extraction_method:
        lines.append(f"🔧 <b>Método:</b> {bill.extraction_method}")

    if bill.validation_errors:
        lines.append("")
        lines.append("⚠️ <b>Erros de Validação:</b>")
        for error in bill.validation_errors:
            lines.append(f"  • {error}")

    return "\n".join(lines)


def format_bill_list(bills: list[Bill], page: int, total_pages: int, filter_name: str) -> str:
    """Format a list of bills."""
    if not bills:
        return f"📭 Nenhum boleto encontrado (filtro: {filter_name})"

    lines = [
        f"<b>📋 Boletos - {filter_name.title()} (Página {page}/{total_pages})</b>",
        "",
    ]

    for bill in bills:
        lines.append(format_bill_summary(bill))

    return "\n".join(lines)


def format_alert_message(bill: Bill, alert_type: str = "upcoming") -> str:
    """Format alert message for a bill."""
    if alert_type == "upcoming":
        try:
            venc = datetime.fromisoformat(bill.vencimento)
            days = (venc.date() - datetime.now().date()).days
            if days == 0:
                urgency = "🚨 <b>VENCE HOJE!</b>"
            elif days == 1:
                urgency = "⚠️ <b>Vence amanhã!</b>"
            else:
                urgency = f"📅 <b>Vence em {days} dias</b>"
        except ValueError:
            urgency = "📅 <b>Vencimento próximo</b>"

        return (
            f"{urgency}\n\n"
            f"{format_bill_details(bill, show_confidence=False)}"
        )

    if alert_type == "overdue":
        try:
            venc = datetime.fromisoformat(bill.vencimento)
            days = (datetime.now().date() - venc.date()).days
            return (
                f"🚨 <b>BOLETO VENCIDO HÁ {days} DIAS!</b>\n\n"
                f"{format_bill_details(bill, show_confidence=False)}"
            )
        except ValueError:
            return f"🚨 <b>BOLETO VENCIDO!</b>\n\n{format_bill_details(bill, show_confidence=False)}"

    elif alert_type == "snooze_end":
        return (
            f"⏰ <b>Lembrete de boleto adiado</b>\n\n"
            f"{format_bill_details(bill, show_confidence=False)}"
        )

    return format_bill_details(bill, show_confidence=False)


def format_help_message(category: str | None = None) -> str:
    """Format help message."""
    if category == "commands":
        return (
            "<b>📋 Comandos Disponíveis</b>\n\n"
            "/start - Iniciar bot e configurar\n"
            "/help - Mostrar esta ajuda\n"
            "/bills - Listar boletos\n"
            "/pending - Boletos pendentes de revisão\n"
            "/review - Revisar próximo boleto\n"
            "/alerts - Gerenciar alertas\n"
            "/config - Configurações\n\n"
            "💡 <b>Dica:</b> Envie PDFs ou fotos de boletos diretamente no chat!"
        )

    if category == "review":
        return (
            "<b>✏️ Como Revisar Boletos</b>\n\n"
            "1. Use /pending para ver boletos aguardando revisão\n"
            "2. Use /review para revisar o próximo\n"
            "3. Escolha:\n"
            "   ✅ <b>Aprovar</b> - Confirma e agenda alertas\n"
            "   ❌ <b>Rejeitar</b> - Descarta o boleto\n"
            "   ✏️ <b>Editar</b> - Corrige campos individuais\n"
            "   🔍 <b>Ver Detalhes</b> - Mostra info completa\n\n"
            "Ao editar, selecione o campo e envie o novo valor."
        )

    if category == "alerts":
        return (
            "<b>🔔 Sistema de Alertas</b>\n\n"
            "Alertas automáticos são enviados:\n"
            "• 3 dias antes do vencimento\n"
            "• 1 dia antes do vencimento\n"
            "• No dia do vencimento\n"
            "• Se vencido e não pago\n\n"
            "Use /alerts para:\n"
            "• Ver histórico de alertas\n"
            "• Configurar horário de envio\n"
            "• Silenciar/ativar alertas\n"
            "• Adiar (snooze) alertas específicos"
        )

    if category == "config":
        return (
            "<b>⚙️ Configurações</b>\n\n"
            "Use /config para alterar:\n"
            "• 🕐 Horário dos alertas (padrão: 07:30)\n"
            "• 📅 Dias de antecedência (padrão: 3,1,0)\n"
            "• 🌍 Fuso horário (padrão: America/Fortaleza)\n"
            "• 🔧 Modo de extração (auto/cloud/local)\n"
            "• 🤖 Modelo LLM (Gemini Flash)\n"
            "• ☁️ Consentimento para nuvem\n\n"
            "⚠️ Algumas mudanças exigem reinicialização."
        )

    # General help
    return (
        "<b>🤖 AutoBill - Monitor Inteligente de Boletos</b>\n\n"
        "Extrai dados de boletos (PDF/foto), valida matematicamente,\n"
        "armazena localmente e envia alertas proativos no Telegram.\n\n"
        "<b>🚀 Início Rápido:</b>\n"
        "1. Envie um PDF ou foto de boleto\n"
        "2. Revise os dados extraídos\n"
        "3. Aprove para ativar alertas\n\n"
        "<b>📋 Comandos Principais:</b>\n"
        "/bills - Ver todos os boletos\n"
        "/pending - Boletos para revisar\n"
        "/review - Revisar próximo\n"
        "/alerts - Gerenciar alertas\n"
        "/config - Configurações\n"
        "/help - Ajuda detalhada\n\n"
        "<b>🔒 Privacidade:</b> Dados ficam no seu servidor.\n"
        "Extração em nuvem requer consentimento (/config)."
    )


def format_config_status(config: dict) -> str:
    """Format current configuration status."""
    lines = [
        "<b>⚙️ Configurações Atuais</b>",
        "",
        f"🕐 <b>Horário Alertas:</b> {config.get('alert_time', '07:30')}",
        f"📅 <b>Dias Antecedência:</b> {config.get('lead_days', '3,1,0')}",
        f"🌍 <b>Fuso Horário:</b> {config.get('timezone', 'America/Fortaleza')}",
        f"🔧 <b>Modo Extração:</b> {config.get('extraction_mode', 'auto')}",
        f"🤖 <b>Modelo LLM:</b> {config.get('llm_model', 'gemini-2.0-flash-exp')}",
        f"☁️ <b>Consentimento Nuvem:</b> {'✅ Sim' if config.get('cloud_consent') else '❌ Não'}",
        "",
        "Use os botões abaixo para alterar.",
    ]
    return "\n".join(lines)


def format_welcome_message(user_name: str) -> str:
    """Format welcome message for new users."""
    return (
        f"👋 Olá, {user_name}!\n\n"
        f"Bem-vindo ao <b>AutoBill</b> - seu monitor inteligente de boletos.\n\n"
        f"📋 <b>O que eu faço:</b>\n"
        f"• Extraio dados de boletos (PDF ou foto)\n"
        f"• Valido matematicamente (módulo 10/11, fator vencimento)\n"
        f"• Armazeno localmente com segurança\n"
        f"• Envio alertas proativos antes do vencimento\n\n"
        f"🚀 <b>Para começar:</b>\n"
        f"1. Envie um PDF ou foto de um boleto\n"
        f"2. Revise os dados extraídos\n"
        f"3. Aprove para ativar os alertas\n\n"
        f"Use /help para ver todos os comandos."
    )


def format_error_message(error: str, user_friendly: bool = True) -> str:
    """Format error message."""
    if user_friendly:
        return (
            "❌ <b>Ops, algo deu errado!</b>\n\n"
            f"<i>{error}</i>\n\n"
            "Tente novamente ou use /help para ajuda."
        )
    return f"❌ <b>Erro:</b> <code>{error}</code>"
