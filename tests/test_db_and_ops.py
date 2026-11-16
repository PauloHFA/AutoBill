"""Tests for database repositories, Alembic migrations, backup, retention, and /healthz."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from autobill.config import settings
from autobill.db.migrations import run_migrations
from autobill.db.models import AlertType, BillOrigin, BillStatus
from autobill.db.repository import (
    AlertRepository,
    AuditRepository,
    BillRepository,
    Database,
    QueueRepository,
    RecurringRepository,
    SettingsRepository,
    SnoozeRepository,
)
from autobill.extract.pipeline import ExtractionResult
from autobill.ops.backup import create_backup
from autobill.ops.health import (
    get_health_payload,
    record_error,
    record_processed,
    start_health_server,
)
from autobill.ops.retention import cleanup_expired_files, purge_expired_soft_deletes, wipe_all_data


@pytest.fixture
async def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated SQLite DB + data directories per test."""
    from autobill.db.repository import db

    db_path = tmp_path / "test_autobill.db"
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "db_path", db_path)
    settings.ensure_directories()

    old_path = db.db_path
    await db.close()
    monkeypatch.setattr(db, "db_path", db_path)
    await db.connect()
    await db.create_tables()

    yield db
    await db.close()
    monkeypatch.setattr(db, "db_path", old_path)


async def test_bill_repository_full_lifecycle(isolated_db: Database) -> None:
    """CRUD, deduplication, mark_paid, undo, soft_delete, month summary."""
    async with isolated_db.session() as session:
        repo = BillRepository(session)
        res = ExtractionResult(
            valor=19990,
            vencimento="2026-11-20",
            beneficiario="Enel Distribuição",
            linha_digitavel="34191790010104351004791020150008291070026000",
            codigo_barras="341929107002600017900104351004791020150008",
            confidence=0.95,
            validation_passed=True,
            valor_confirmado=True,
            needs_review=False,
        )
        bill = await repo.create_bill_from_extraction(
            user_id=1001,
            extraction_result=res,
            source_file="/tmp/enel.pdf",
            source_hash="hash_enel_1",
            origin=BillOrigin.TELEGRAM,
            auto_approve=True,
        )
        assert bill.status == BillStatus.PENDENTE

        # Duplicate checks
        dup_hash = await repo.get_by_hash("hash_enel_1", user_id=1001)
        assert dup_hash is not None and dup_hash.id == bill.id
        assert res.linha_digitavel is not None
        dup_linha = await repo.get_by_linha_digitavel(res.linha_digitavel, user_id=1001)
        assert dup_linha is not None and dup_linha.id == bill.id
        dup_heur = await repo.find_heuristic_duplicate(
            user_id=1001,
            beneficiario="Enel Distribuição",
            valor_centavos=19990,
            vencimento=bill.vencimento,
        )
        assert dup_heur is not None and dup_heur.id == bill.id

        # Queries
        pending = await repo.list_pending_and_overdue(1001)
        assert len(pending) == 1
        upcoming = await repo.list_upcoming(1001, 30, date(2026, 11, 10))
        assert len(upcoming) == 1

        # Update field & mark paid
        await repo.update_bill_field(bill.id, "valor", 20500, user_id=1001)
        paid_bill, already = await repo.mark_paid(bill.id, user_id=1001, paid_by="@paulo")
        assert paid_bill is not None and not already
        assert paid_bill.status == BillStatus.PAGO
        assert paid_bill.paid_by == "@paulo"

        # Idempotent re-mark
        _, already2 = await repo.mark_paid(bill.id, user_id=1001, paid_by="@paulo")
        assert already2 is True

        # Month summary
        summary = await repo.get_month_summary(2026, 11, user_id=1001)
        assert summary["total_pago"] == 20500

        # Undo payment within 24h
        reverted = await repo.undo_last_payment(user_id=1001, max_age_hours=24)
        assert reverted is not None and reverted.status == BillStatus.PENDENTE

        # Manual bill + soft delete
        manual = await repo.create_manual_bill(
            user_id=1001,
            beneficiario="Condomínio",
            valor_centavos=50000,
            vencimento=datetime(2026, 11, 25, tzinfo=UTC),
            source_hash="manual_1",
        )
        assert len(await repo.list_all_for_export(1001)) == 2
        deleted = await repo.soft_delete_bill(manual.id, user_id=1001)
        assert deleted is not None and deleted.deleted_at is not None
        assert await repo.get_bill(manual.id, user_id=1001) is None


async def test_supporting_repositories(isolated_db: Database) -> None:
    """AlertRepository, SnoozeRepository, RecurringRepository, SettingsRepository, Queue, Audit."""
    async with isolated_db.session() as session:
        bill_repo = BillRepository(session)
        b1 = await bill_repo.create_manual_bill(
            user_id=200,
            beneficiario="Vivo Fibra",
            valor_centavos=9990,
            vencimento=datetime(2026, 10, 15, tzinfo=UTC),
            source_hash="v1",
        )
        rec_repo = RecurringRepository(session)
        assert await rec_repo.detect_and_upsert(b1) is None

        b2 = await bill_repo.create_manual_bill(
            user_id=200,
            beneficiario="Vivo Fibra",
            valor_centavos=9990,
            vencimento=datetime(2026, 11, 15, tzinfo=UTC),
            source_hash="v2",
        )

        # Alerts idempotency
        alert_repo = AlertRepository(session)
        today = date(2026, 11, 14)
        assert not await alert_repo.was_sent(b2.id, AlertType.D1, rule="D-1", sent_date=today)
        await alert_repo.mark_sent(b2.id, AlertType.D1, rule="D-1", sent_date=today)
        assert await alert_repo.was_sent(b2.id, AlertType.D1, rule="D-1", sent_date=today)

        # Snooze
        snooze_repo = SnoozeRepository(session)
        past = datetime.now(UTC) - timedelta(minutes=5)
        sn = await snooze_repo.create_snooze(b2.id, 200, past)
        due_sn = await snooze_repo.get_due_snoozes(datetime.now(UTC))
        assert len(due_sn) == 1
        await snooze_repo.mark_done(sn.id)
        assert await snooze_repo.get_due_snoozes(datetime.now(UTC)) == []

        # Recurring detection
        rec = await rec_repo.detect_and_upsert(b2)
        assert rec is not None and rec.dia_vencimento == 15

        # Settings
        cfg_repo = SettingsRepository(session)
        cfg = await cfg_repo.get_or_create(200)
        assert cfg.alert_time == "07:30"
        cfg2 = await cfg_repo.update_setting(200, "alert_time", "09:15")
        assert cfg2.alert_time == "09:15"

        # Queue & Audit
        q_repo = QueueRepository(session)
        q_item = await q_repo.enqueue(
            file_path="/tmp/x.pdf",
            source="pasta",
            chat_id=200,
            file_hash="qh1",
            error="timeout",
        )
        assert q_item.attempts == 0
        await q_repo.mark_failed(q_item.id, "still failing")
        await q_repo.mark_done(q_item.id)
        await AuditRepository(session).log_action(200, "test_action", bill_id=b2.id)


async def test_backup_retention_health_and_migrations(
    isolated_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test sqlite backup, retention purge, wipe_all_data, /healthz HTTP server, alembic."""
    import asyncio

    mig_db = tmp_path / "migrated.db"
    monkeypatch.setattr(settings, "db_path", mig_db)
    await asyncio.to_thread(run_migrations)
    assert mig_db.exists()
    monkeypatch.setattr(settings, "db_path", isolated_db.db_path)

    # Create sample bill + old soft-deleted bill
    async with isolated_db.session() as session:
        repo = BillRepository(session)
        old_bill = await repo.create_manual_bill(
            user_id=300,
            beneficiario="Antigo",
            valor_centavos=1000,
            vencimento=datetime(2026, 11, 1, tzinfo=UTC),
            source_hash="old1",
        )
        old_bill.deleted_at = datetime.now(UTC) - timedelta(days=45)
        await session.commit()

    backup_file = create_backup()
    assert backup_file is not None and backup_file.exists()

    purged = await purge_expired_soft_deletes(days=30)
    assert purged == 1

    # Expired file cleanup for PAGO bill older than retention_days
    old_file = settings.data_dir / "processados" / "old.pdf"
    old_file.write_bytes(b"pdf")
    async with isolated_db.session() as session:
        repo = BillRepository(session)
        paid_old = await repo.create_manual_bill(
            user_id=300,
            beneficiario="Pago Antigo",
            valor_centavos=2000,
            vencimento=datetime(2026, 10, 1, tzinfo=UTC),
            source_hash="paid_old_1",
        )
        paid_old.status = BillStatus.PAGO
        paid_old.paid_at = datetime.now(UTC) - timedelta(days=20)
        paid_old.arquivo_path = str(old_file)
        await session.commit()

    assert await cleanup_expired_files(retention_days=7) == 1
    assert not old_file.exists()

    # Additional repository coverage (status filters, delete, recurring due, queue errors)
    async with isolated_db.session() as session:
        repo = BillRepository(session)
        b_rev = await repo.create_manual_bill(
            user_id=301,
            beneficiario="Teste Rev",
            valor_centavos=4500,
            vencimento=datetime(2026, 11, 28, tzinfo=UTC),
            source_hash="rev_extra_1",
        )
        await repo.update_status(b_rev.id, BillStatus.EM_REVISAO)
        await repo.mark_valor_confirmado(b_rev.id, True)
        await repo.update_bill_status(b_rev.id, "approved", user_id=301)
        items, total = await repo.list_bills(user_id=301, status_filter=None, page=1, per_page=10)
        assert total == 1 and len(items) == 1
        await repo.update_bill_field(b_rev.id, "beneficiario", "Novo Nome", user_id=301)
        await repo.update_bill_field(b_rev.id, "vencimento", "30/11/2026", user_id=301)
        assert await repo.delete(b_rev.id) is True

        q_repo = QueueRepository(session)
        qi = await q_repo.enqueue(
            file_path="/tmp/fail.pdf", source="pasta", chat_id=301, file_hash="qf1", error="err"
        )
        await q_repo.mark_failed(qi.id, "err2")
        assert len(await q_repo.recent_errors()) >= 1
        assert isinstance(await q_repo.count_pending(), int)
        assert isinstance(await repo.get_bills_by_status(BillStatus.PENDENTE), list)
        assert isinstance(await repo.get_pending_bills(), list)
        assert isinstance(await repo.list_upcoming(301, 7, date(2026, 11, 16)), list)
        assert isinstance(await repo.list_pending_and_overdue(301), list)

    # Health check + HTTP server
    record_processed()
    record_error("test err")
    healthy, payload = await get_health_payload()
    assert healthy is True
    assert payload["db"] is True

    server = await start_health_server(host="127.0.0.1", port=0)
    port = server.sockets[0].getsockname()[1]
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{port}/healthz", timeout=3.0)
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
    finally:
        server.close()
        await server.wait_closed()

    # Full LGPD wipe
    wipe_summary = await wipe_all_data()
    assert wipe_summary["files_removed"] >= 0
