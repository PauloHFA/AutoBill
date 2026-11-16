"""Tests for alert policy, scheduler idempotency, middlewares, and Telegram handlers."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from autobill.alerts.formatter import (
    format_monthly_summary,
    format_proactive_alert,
    format_weekly_summary,
    get_alert_action_keyboard,
)
from autobill.alerts.policy import (
    AlertType,
    check_anti_golpe_bank_mismatch,
    decide_bill_alert,
    should_alert_missing_recurring,
)
from autobill.alerts.scheduler import AlertScheduler
from autobill.bot import handlers
from autobill.bot.formatters import format_bill_details
from autobill.bot.keyboards import get_config_keyboard, get_snooze_keyboard
from autobill.bot.middleware import RateLimiter, auth_middleware
from autobill.bot.state import UserState, state_manager
from autobill.config import settings
from autobill.db.models import Bill, BillOrigin, BillStatus
from autobill.db.repository import BillRepository
from autobill.main import lifespan


def test_alert_policy_and_anti_golpe() -> None:
    """D-3, D-1, D0, overdue daily/weekly, snooze, and anti-golpe mismatch."""
    today = date(2026, 11, 16)
    bill = Bill(
        id=1,
        user_id=99,
        beneficiario="Bradesco Cartões",
        valor_centavos=35000,
        vencimento=datetime(2026, 11, 19, tzinfo=UTC),
        status=BillStatus.PENDENTE,
        origem=BillOrigin.TELEGRAM,
        hash_sha256="h1",
        linha_digitavel="34191790010104351004791020150008291070026000",
    )

    # D-3 & D-1 & outside window
    d3 = decide_bill_alert(bill_due=bill.vencimento.date(), today=today, lead_days=[3, 1, 0])
    assert d3 is not None and d3.alert_type == AlertType.D3
    d1 = decide_bill_alert(bill_due=date(2026, 11, 17), today=today, lead_days=[3, 1, 0])
    assert d1 is not None and d1.alert_type == AlertType.D1
    assert decide_bill_alert(bill_due=date(2026, 12, 1), today=today, lead_days=[3, 1, 0]) is None
    assert format_proactive_alert(bill, d3) is not None

    from autobill.alerts.policy import parse_lead_days

    assert parse_lead_days("5,2,0") == [5, 2, 0]
    assert parse_lead_days("bad") == []

    # D0
    d0 = decide_bill_alert(bill_due=today, today=today, lead_days=[3, 1, 0])
    assert d0 is not None and d0.alert_type == AlertType.D0

    # Overdue 2 days -> daily
    ov = decide_bill_alert(bill_due=date(2026, 11, 14), today=today, lead_days=[3, 1, 0])
    assert ov is not None and ov.alert_type == AlertType.ATRASO

    # Overdue 10 days with recent alert 2 days ago -> suppressed until 7d
    assert (
        decide_bill_alert(
            bill_due=date(2026, 11, 6),
            today=today,
            lead_days=[3, 1, 0],
            last_overdue_sent=date(2026, 11, 14),
        )
        is None
    )

    # Anti-golpe mismatch: code is Itaú (341) but beneficiary says Bradesco
    mismatch = check_anti_golpe_bank_mismatch(bill.beneficiario, bill.linha_digitavel)
    assert mismatch is not None and "Itaú" in mismatch

    # Format limits <= 12 lines (RNF15)
    assert d0 is not None
    txt = format_proactive_alert(bill, d0)
    assert len(txt.splitlines()) <= 12
    assert "34191" in txt
    assert get_alert_action_keyboard(bill) is not None
    assert format_weekly_summary([bill], [], []) is not None
    assert format_monthly_summary(
        2026,
        11,
        {
            "total_previsto": 1000,
            "total_pago": 500,
            "total_pendente": 500,
            "total_vencida": 0,
            "vencida": 0,
        },
    )

    # Recurring reminder
    ok_rec, next_d = should_alert_missing_recurring(
        last_seen=date(2026, 10, 20),
        typical_day=20,
        last_alerted_at=None,
        today=date(2026, 11, 16),
    )
    assert ok_rec is True and next_d == date(2026, 11, 20)
    assert ov is not None and format_proactive_alert(bill, ov) is not None
    ok_rec_early, _ = should_alert_missing_recurring(
        last_seen=date(2026, 10, 20),
        typical_day=20,
        last_alerted_at=date(2026, 11, 15),
        today=date(2026, 11, 16),
    )
    assert ok_rec_early is False


async def test_scheduler_idempotency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AlertScheduler envia apenas 1 alerta por (fatura, regra, dia)."""
    from autobill.db.repository import db

    db_path = tmp_path / "sched.db"
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "allowed_chat_ids", [777])
    settings.ensure_directories()

    old_path = db.db_path
    await db.close()
    monkeypatch.setattr(db, "db_path", db_path)
    await db.connect()
    await db.create_tables()

    try:
        from autobill.alerts.policy import local_today

        today = local_today(settings.default_timezone)
        async with db.session() as session:
            repo = BillRepository(session)
            await repo.create_manual_bill(
                user_id=777,
                beneficiario="Luz",
                valor_centavos=12000,
                vencimento=datetime(today.year, today.month, today.day, tzinfo=UTC),
                source_hash="luz_d0",
            )

        fake_bot = SimpleNamespace(send_message=AsyncMock())
        sched = AlertScheduler(bot=fake_bot)
        sent1 = await sched.run_daily_alert_cycle()
        sent2 = await sched.run_daily_alert_cycle()
        assert sent1 == 1
        assert sent2 == 0
        assert fake_bot.send_message.await_count == 1

        # Snooze due + weekly + monthly + maintenance sweeps
        async with db.session() as session:
            from autobill.db.repository import SnoozeRepository

            sn_repo = SnoozeRepository(session)
            await sn_repo.create_snooze(1, 777, datetime.now(UTC) - timedelta(minutes=1))

        await sched.run_periodic_sweeps()
        await sched.run_weekly_summary()
        await sched.run_monthly_summary()
        await sched.run_daily_maintenance()
        assert fake_bot.send_message.await_count >= 3

        sched.start()
        sched.reschedule_daily_alerts("09:00", "America/Sao_Paulo")
        sched.stop()
    finally:
        await db.close()
        monkeypatch.setattr(db, "db_path", old_path)


async def test_middleware_keyboards_and_bot_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Middleware auth, rate limiter, formatters, and new bot commands."""
    monkeypatch.setattr(settings, "allowed_chat_ids", [12345])
    unauthorized = SimpleNamespace(
        effective_chat=SimpleNamespace(id=99999),
        effective_user=SimpleNamespace(id=99999),
    )
    with pytest.raises(ApplicationHandlerStop):
        await auth_middleware(unauthorized, None)  # type: ignore[arg-type]

    rl = RateLimiter(max_requests=2, window_seconds=60)
    assert rl.is_allowed(1) is True
    assert rl.is_allowed(1) is True
    assert rl.is_allowed(1) is False

    assert get_snooze_keyboard(1) is not None
    assert get_config_keyboard() is not None

    # HTML escape in bill details
    bill = Bill(
        id=5,
        user_id=12345,
        beneficiario="Empresa <A&B>",
        valor_centavos=1000,
        vencimento=datetime.now(UTC) + timedelta(days=2),
        status=BillStatus.PENDENTE,
        origem=BillOrigin.MANUAL,
        hash_sha256="esc",
    )
    html_details = format_bill_details(bill)
    assert "Empresa &lt;A&amp;B&gt;" in html_details

    # Test real handlers against isolated DB
    from autobill.db.repository import db

    db_path = tmp_path / "bot.db"
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    settings.ensure_directories()

    old_path = db.db_path
    await db.close()
    monkeypatch.setattr(db, "db_path", db_path)
    await db.connect()
    await db.create_tables()

    try:
        replies: list[str] = []
        docs: list[str] = []

        class FakeMsg:
            def __init__(self, text: str = ""):
                self.text = text

            async def reply_text(self, text: str, **_kwargs):
                replies.append(text)

            async def reply_document(self, document, **_kwargs):
                docs.append(getattr(document, "filename", "doc"))

        def make_update(text: str = "") -> SimpleNamespace:
            return SimpleNamespace(
                effective_user=SimpleNamespace(id=12345, username="paulo", first_name="Paulo"),
                effective_chat=SimpleNamespace(id=12345, type="private"),
                message=FakeMsg(text),
            )

        ctx_empty = SimpleNamespace(args=[])
        await handlers.start_command(make_update("/start"), ctx_empty)  # type: ignore[arg-type]
        await handlers.adicionar_command(make_update("/adicionar"), ctx_empty)  # type: ignore[arg-type]
        assert state_manager.is_in_state(12345, UserState.AWAITING_ADD_BENEFICIARIO)

        await handlers.handle_text_message(make_update("Água Compesa"), ctx_empty)  # type: ignore[arg-type]
        await handlers.handle_text_message(make_update("95,40"), ctx_empty)  # type: ignore[arg-type]
        await handlers.handle_text_message(make_update("25/11/2026"), ctx_empty)  # type: ignore[arg-type]
        await handlers.handle_text_message(make_update("pular"), ctx_empty)  # type: ignore[arg-type]
        assert any("cadastrada" in r for r in replies)

        await handlers.pendentes_command(make_update("/pendentes"), ctx_empty)  # type: ignore[arg-type]
        await handlers.proximos_command(make_update("/proximos"), SimpleNamespace(args=["30"]))  # type: ignore[arg-type]
        await handlers.mes_command(make_update("/mes"), SimpleNamespace(args=["2026-11"]))  # type: ignore[arg-type]
        await handlers.pago_command(make_update("/pago"), SimpleNamespace(args=["1"]))  # type: ignore[arg-type]
        await handlers.pagas_command(make_update("/pagas"), ctx_empty)  # type: ignore[arg-type]
        await handlers.desfazer_command(make_update("/desfazer"), ctx_empty)  # type: ignore[arg-type]
        await handlers.fatura_command(make_update("/fatura"), SimpleNamespace(args=["1"]))  # type: ignore[arg-type]
        await handlers.exportar_command(make_update("/exportar"), ctx_empty)  # type: ignore[arg-type]
        assert len(docs) == 1

        await handlers.config_command(
            make_update("/config"), SimpleNamespace(args=["hora", "08:00"])
        )  # type: ignore[arg-type]
        await handlers.status_command(make_update("/status"), ctx_empty)  # type: ignore[arg-type]
        assert any("Status AutoBill" in r for r in replies)
        state_manager.clear_state(12345)
    finally:
        await db.close()
        monkeypatch.setattr(db, "db_path", old_path)


async def test_main_lifespan_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """main.lifespan inicializa DB, /healthz, scheduler e encerra sem erros."""
    from autobill.db.repository import db

    db_path = tmp_path / "life.db"
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "imap_enabled", False)
    monkeypatch.setattr(settings, "health_port", 0)

    old_path = db.db_path
    await db.close()
    monkeypatch.setattr(db, "db_path", db_path)
    try:
        async with lifespan():
            assert db.engine is not None
    finally:
        monkeypatch.setattr(db, "db_path", old_path)
