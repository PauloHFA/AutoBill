import sys
from datetime import datetime
from types import ModuleType

from autobill.db.models import Bill, BillOrigin, BillStatus


def stub_bot_dependencies(monkeypatch) -> None:
    repository_module = ModuleType("autobill.db.repository")
    repository_module.BillRepository = type("BillRepository", (), {})
    monkeypatch.setitem(sys.modules, "autobill.db.repository", repository_module)
    pipeline_module = ModuleType("autobill.extract.pipeline")
    pipeline_module.ExtractionPipeline = type("ExtractionPipeline", (), {})
    monkeypatch.setitem(sys.modules, "autobill.extract.pipeline", pipeline_module)


def test_format_bill_summary_uses_persisted_bill_fields(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    stub_bot_dependencies(monkeypatch)

    from autobill.bot.formatters import format_bill_summary

    bill = Bill(
        id=1,
        hash_sha256="a" * 64,
        valor_centavos=12345,
        vencimento=datetime(2026, 12, 1),
        beneficiario="Utility",
        status=BillStatus.PENDENTE,
        origem=BillOrigin.MANUAL,
    )

    summary = format_bill_summary(bill)

    assert "#1" in summary
    assert "R$ 123,45" in summary
    assert "01/12/2026" in summary
    assert "⏳ PENDENTE" in summary


def test_format_bill_details_and_alert_accept_missing_due_date(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    stub_bot_dependencies(monkeypatch)

    from autobill.bot.formatters import (
        format_alert_message,
        format_bill_details,
    )

    bill = Bill(
        id=1,
        hash_sha256="a" * 64,
        status=BillStatus.EM_REVISAO,
        origem=BillOrigin.TELEGRAM,
    )

    details = format_bill_details(bill)
    alert = format_alert_message(bill, alert_type="upcoming")

    assert "Origem:</b> telegram" in details
    assert "Vencimento próximo" in alert
