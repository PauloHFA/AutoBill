"""Database repository layer for Sistema-Monitoramento-Boletos."""

import re
import unicodedata
from calendar import monthrange
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import selectinload

from autobill.config import settings
from autobill.db.models import (
    AlertSent,
    AlertType,
    AuditLog,
    Base,
    Bill,
    BillOrigin,
    BillStatus,
    ProcessingQueueItem,
    QueueStatus,
    RecurringBill,
    Snooze,
    UserSettings,
)

if TYPE_CHECKING:
    from autobill.extract.pipeline import ExtractionResult

MAX_QUEUE_ATTEMPTS = 3


def _raise_repository_error(message: str) -> NoReturn:
    raise ValueError(message)


def normalize_issuer(name: str | None) -> str | None:
    """Normalize beneficiário name for recurring and duplicate matching."""
    if not name:
        return None
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9]+", " ", ascii_only.lower()).strip()
    return cleaned or None


class Database:
    """Database connection manager."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(settings.db_path)
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        """Initialize database connection."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}",
            echo=False,
            connect_args={
                "check_same_thread": False,
                "timeout": 30,
            },
        )

        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            await conn.exec_driver_sql("PRAGMA busy_timeout=30000;")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")
            await conn.exec_driver_sql("PRAGMA foreign_keys=ON;")

        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def create_tables(self) -> None:
        """Create all tables and safely add new columns on legacy SQLite databases."""
        engine = self.engine
        if engine is None:
            await self.connect()
            engine = self.engine
        assert engine is not None
        async with engine.begin() as conn:
            table = await conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bills'"
            )
            if table.first() is not None:
                columns = await conn.exec_driver_sql("PRAGMA table_info(bills)")
                existing_columns = {row._mapping["name"] for row in columns}
                new_columns = {
                    "user_id": "BIGINT",
                    "snoozed_until": "DATETIME",
                    "issuer_normalized": "VARCHAR(255)",
                    "kind": "VARCHAR(32) NOT NULL DEFAULT 'outro'",
                    "review_notes": "TEXT",
                    "review_reminded_at": "DATETIME",
                    "paid_by": "VARCHAR(128)",
                    "recurring_id": "INTEGER",
                    "deleted_at": "DATETIME",
                }
                for name, column_type in new_columns.items():
                    if name not in existing_columns:
                        await conn.exec_driver_sql(
                            f"ALTER TABLE bills ADD COLUMN {name} {column_type}"
                        )
                await conn.exec_driver_sql("DROP INDEX IF EXISTS ix_bills_hash_sha256")
                await conn.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_bills_user_hash "
                    "ON bills (user_id, hash_sha256)"
                )

            alerts_tbl = await conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alerts_sent'"
            )
            if alerts_tbl.first() is not None:
                acol = await conn.exec_driver_sql("PRAGMA table_info(alerts_sent)")
                existing_acols = {row._mapping["name"] for row in acol}
                for name, column_type in {
                    "rule": "VARCHAR(32) NOT NULL DEFAULT 'D0'",
                    "sent_date": "DATE NOT NULL DEFAULT '1970-01-01'",
                    "telegram_message_id": "BIGINT",
                }.items():
                    if name not in existing_acols:
                        await conn.exec_driver_sql(
                            f"ALTER TABLE alerts_sent ADD COLUMN {name} {column_type}"
                        )

            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        """Close database connection."""
        if self.engine:
            await self.engine.dispose()
            self.engine = None
            self.session_factory = None

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Get a database session."""
        session_factory = self.session_factory
        if session_factory is None:
            await self.connect()
            session_factory = self.session_factory
        assert session_factory is not None
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


class BillRepository:
    """Repository for Bill operations."""

    def __init__(self, session: AsyncSession, *, owns_session: bool = False):
        self.session = session
        self._owns_session = owns_session

    @classmethod
    async def create_for_bot(cls) -> "BillRepository":
        """Create a repository backed by the application's database."""
        if db.session_factory is None:
            await db.connect()
        await db.create_tables()
        session_factory = db.session_factory
        assert session_factory is not None
        return cls(session_factory(), owns_session=True)

    async def close(self) -> None:
        """Close a repository-owned session."""
        if self._owns_session:
            await self.session.close()

    async def _commit_if_owned(self) -> None:
        if self._owns_session:
            await self.session.commit()

    async def get_bill(
        self, bill_id: int, *, user_id: int, include_deleted: bool = False
    ) -> Bill | None:
        """Get a bill owned by the requesting user/chat."""
        stmt = select(Bill).where(Bill.id == bill_id, Bill.user_id == user_id)
        if not include_deleted:
            stmt = stmt.where(Bill.deleted_at.is_(None))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_bills(
        self,
        user_id: int,
        status_filter: str | None,
        page: int,
        per_page: int,
    ) -> tuple[list[Bill], int]:
        """List bills using the bot's status filters and pagination."""
        status_by_filter = {
            "pending": BillStatus.EM_REVISAO,
            "review": BillStatus.EM_REVISAO,
            "approved": BillStatus.PENDENTE,
            "paid": BillStatus.PAGO,
            "overdue": BillStatus.VENCIDA,
            "rejected": BillStatus.DESCARTADA,
        }
        query = select(Bill).where(Bill.user_id == user_id, Bill.deleted_at.is_(None))
        if status_filter:
            status = status_by_filter.get(status_filter.lower())
            if status is None:
                return [], 0
            query = query.where(Bill.status == status)

        total = await self.session.scalar(select(func.count()).select_from(query.subquery()))
        result = await self.session.execute(
            query.order_by(Bill.vencimento.asc().nulls_last(), Bill.created_at.desc())
            .offset(max(0, (page - 1) * per_page))
            .limit(per_page)
        )
        return list(result.scalars().all()), total or 0

    async def list_pending_and_overdue(self, user_id: int) -> list[Bill]:
        """List active PENDENTE and VENCIDA bills ordered by due date."""
        result = await self.session.execute(
            select(Bill)
            .where(
                Bill.user_id == user_id,
                Bill.deleted_at.is_(None),
                Bill.status.in_([BillStatus.PENDENTE, BillStatus.VENCIDA]),
            )
            .order_by(Bill.vencimento.asc().nulls_last(), Bill.id.asc())
        )
        return list(result.scalars().all())

    async def list_upcoming(self, user_id: int, days: int, today_local: date) -> list[Bill]:
        """List PENDENTE/VENCIDA bills due within the next `days` days (including overdue)."""
        limit_date = today_local + timedelta(days=days)
        result = await self.session.execute(
            select(Bill)
            .where(
                Bill.user_id == user_id,
                Bill.deleted_at.is_(None),
                Bill.status.in_([BillStatus.PENDENTE, BillStatus.VENCIDA]),
                Bill.vencimento.is_not(None),
                func.date(Bill.vencimento) <= limit_date,
            )
            .order_by(Bill.vencimento.asc())
        )
        return list(result.scalars().all())

    async def list_paid(self, user_id: int, limit: int = 20) -> list[Bill]:
        """List paid bills ordered by payment date descending."""
        result = await self.session.execute(
            select(Bill)
            .where(
                Bill.user_id == user_id,
                Bill.deleted_at.is_(None),
                Bill.status == BillStatus.PAGO,
            )
            .order_by(Bill.paid_at.desc().nulls_last(), Bill.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_all_for_export(self, user_id: int) -> list[Bill]:
        """List all non-deleted bills for CSV export."""
        result = await self.session.execute(
            select(Bill)
            .where(Bill.user_id == user_id, Bill.deleted_at.is_(None))
            .order_by(Bill.vencimento.asc().nulls_last(), Bill.id.asc())
        )
        return list(result.scalars().all())

    async def get_recent_alerts(self, user_id: int, limit: int) -> Sequence[AlertSent]:
        """Get the most recent sent alerts."""
        result = await self.session.execute(
            select(AlertSent)
            .join(Bill, AlertSent.bill_id == Bill.id)
            .where(Bill.user_id == user_id)
            .order_by(AlertSent.sent_at.desc())
            .limit(limit)
        )
        return result.scalars().all()

    async def create_bill_from_extraction(
        self,
        user_id: int,
        extraction_result: "ExtractionResult",
        source_file: str,
        source_hash: str,
        *,
        origin: BillOrigin = BillOrigin.TELEGRAM,
        auto_approve: bool = False,
    ) -> Bill:
        """Create a reviewable or auto-approved bill from extracted fields."""
        existing_bill = await self.get_by_hash(source_hash, user_id=user_id)
        if existing_bill is not None:
            return existing_bill
        vencimento = (
            datetime.fromisoformat(extraction_result.vencimento)
            if extraction_result.vencimento
            else None
        )
        kind = getattr(extraction_result, "kind", "outro") or "outro"
        valor_confirmado = bool(getattr(extraction_result, "valor_confirmado", False))
        review_notes_list = getattr(extraction_result, "discrepancies", [])
        review_notes = "; ".join(review_notes_list) if review_notes_list else None
        can_confirm_fn = getattr(extraction_result, "can_auto_confirm", None)
        should_approve = bool(auto_approve and can_confirm_fn and can_confirm_fn())
        status = BillStatus.PENDENTE if should_approve else BillStatus.EM_REVISAO

        bill = Bill(
            hash_sha256=source_hash,
            linha_digitavel=extraction_result.linha_digitavel,
            codigo_barras=extraction_result.codigo_barras,
            beneficiario=extraction_result.beneficiario,
            issuer_normalized=normalize_issuer(extraction_result.beneficiario),
            valor_centavos=extraction_result.valor,
            vencimento=vencimento,
            kind=kind,
            status=status,
            user_id=user_id,
            origem=origin,
            arquivo_path=source_file,
            confidence=extraction_result.confidence,
            valor_confirmado=valor_confirmado,
            review_notes=review_notes,
        )
        self.session.add(bill)
        await self.session.flush()
        await self.session.refresh(bill)
        await self._commit_if_owned()
        return bill

    async def create_manual_bill(
        self,
        *,
        user_id: int,
        beneficiario: str,
        valor_centavos: int,
        vencimento: datetime,
        linha_digitavel: str | None = None,
        codigo_barras: str | None = None,
        kind: str = "outro",
        source_hash: str,
    ) -> Bill:
        """Create a manually entered bill directly in PENDENTE status."""
        bill = Bill(
            hash_sha256=source_hash,
            linha_digitavel=linha_digitavel,
            codigo_barras=codigo_barras,
            beneficiario=beneficiario,
            issuer_normalized=normalize_issuer(beneficiario),
            valor_centavos=valor_centavos,
            vencimento=vencimento,
            kind=kind,
            status=BillStatus.PENDENTE,
            user_id=user_id,
            origem=BillOrigin.MANUAL,
            confidence=1.0,
            valor_confirmado=bool(linha_digitavel),
        )
        self.session.add(bill)
        await self.session.flush()
        await self.session.refresh(bill)
        await self._commit_if_owned()
        return bill

    async def update_bill_status(self, bill_id: int, status: str, *, user_id: int) -> Bill | None:
        """Update a bill using the bot's status names."""
        status_by_name = {
            "approved": BillStatus.PENDENTE,
            "pending": BillStatus.EM_REVISAO,
            "paid": BillStatus.PAGO,
            "overdue": BillStatus.VENCIDA,
            "rejected": BillStatus.DESCARTADA,
        }
        try:
            bill_status = status_by_name[status.lower()]
        except KeyError:
            _raise_repository_error(f"Unknown bill status: {status}")
        bill = await self.get_bill(bill_id, user_id=user_id)
        if bill is None:
            return None
        bill.status = bill_status
        if bill_status == BillStatus.PAGO:
            bill.paid_at = datetime.now(UTC)
        await self.session.flush()
        await self._commit_if_owned()
        return bill

    async def mark_paid(
        self,
        bill_id: int,
        *,
        user_id: int,
        paid_by: str | None = None,
        paid_at: datetime | None = None,
    ) -> tuple[Bill | None, bool]:
        """
        Mark a bill as PAGO.

        Returns:
            (bill, already_paid)
        """
        bill = await self.get_bill(bill_id, user_id=user_id)
        if bill is None:
            return (None, False)
        if bill.status == BillStatus.PAGO:
            return (bill, True)
        bill.status = BillStatus.PAGO
        bill.paid_at = paid_at or datetime.now(UTC)
        if paid_by:
            bill.paid_by = paid_by
        await self.session.execute(
            delete(Snooze).where(Snooze.bill_id == bill_id, Snooze.done.is_(False))
        )
        await self.session.flush()
        await self._commit_if_owned()
        return (bill, False)

    async def undo_last_payment(self, *, user_id: int, max_age_hours: int = 24) -> Bill | None:
        """Revert the most recent payment within `max_age_hours` back to PENDENTE."""
        cutoff = datetime.now(UTC) - timedelta(hours=max_age_hours)
        result = await self.session.execute(
            select(Bill)
            .where(
                Bill.user_id == user_id,
                Bill.deleted_at.is_(None),
                Bill.status == BillStatus.PAGO,
                Bill.paid_at.is_not(None),
                Bill.paid_at >= cutoff,
            )
            .order_by(Bill.paid_at.desc())
            .limit(1)
        )
        bill = result.scalar_one_or_none()
        if bill is None:
            return None
        bill.status = BillStatus.PENDENTE
        bill.paid_at = None
        bill.paid_by = None
        await self.session.flush()
        await self._commit_if_owned()
        return bill

    async def soft_delete_bill(self, bill_id: int, *, user_id: int) -> Bill | None:
        """Soft delete a bill (recoverable for 30 days)."""
        bill = await self.get_bill(bill_id, user_id=user_id)
        if bill is None:
            return None
        bill.deleted_at = datetime.now(UTC)
        await self.session.flush()
        await self._commit_if_owned()
        return bill

    async def purge_soft_deleted(self, *, days: int = 30) -> int:
        """Permanently remove bills soft-deleted more than `days` ago."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        result = await self.session.execute(
            select(Bill).where(Bill.deleted_at.is_not(None), Bill.deleted_at <= cutoff)
        )
        old = list(result.scalars().all())
        for bill in old:
            await self.session.delete(bill)
        await self.session.flush()
        await self._commit_if_owned()
        return len(old)

    async def update_bill_field(
        self, bill_id: int, field_name: str, value: str | int, *, user_id: int
    ) -> Bill | None:
        """Update an editable bill field."""
        bill = await self.get_bill(bill_id, user_id=user_id)
        if bill is None:
            return None
        if field_name == "valor":
            bill.valor_centavos = int(value)
        elif field_name == "vencimento":
            bill.vencimento = datetime.strptime(str(value), "%d/%m/%Y")
        elif field_name == "beneficiario":
            bill.beneficiario = str(value)
            bill.issuer_normalized = normalize_issuer(str(value))
        elif field_name in ("linha_digitavel", "codigo_barras"):
            setattr(bill, field_name, str(value))
        else:
            _raise_repository_error(f"Unsupported bill field: {field_name}")
        await self.session.flush()
        await self._commit_if_owned()
        return bill

    async def snooze_bill(self, bill_id: int | None, days: int, *, user_id: int) -> Bill:
        """Delay a bill's upcoming alerts for the requested number of days."""
        if bill_id is None:
            _raise_repository_error("A bill must be selected before snoozing")
        bill = await self.get_bill(bill_id, user_id=user_id)
        if bill is None:
            _raise_repository_error("Bill not found")
        remind_at = datetime.now(UTC) + timedelta(days=days)
        bill.snoozed_until = remind_at
        self.session.add(Snooze(bill_id=bill.id, chat_id=user_id, remind_at=remind_at, done=False))
        await self.session.flush()
        await self._commit_if_owned()
        return bill

    async def create(self, bill: Bill) -> Bill:
        """Create a new bill."""
        if bill.beneficiario and not bill.issuer_normalized:
            bill.issuer_normalized = normalize_issuer(bill.beneficiario)
        self.session.add(bill)
        await self.session.flush()
        await self.session.refresh(bill)
        await self._commit_if_owned()
        return bill

    async def get_by_id(self, bill_id: int) -> Bill | None:
        """Get bill by ID."""
        result = await self.session.execute(
            select(Bill)
            .where(Bill.id == bill_id, Bill.deleted_at.is_(None))
            .options(selectinload(Bill.alerts_sent))
        )
        return result.scalar_one_or_none()

    async def get_by_hash(self, hash_sha256: str, *, user_id: int | None = None) -> Bill | None:
        """Get a bill by SHA-256 hash (optionally scoped to user/chat)."""
        stmt = select(Bill).where(Bill.hash_sha256 == hash_sha256, Bill.deleted_at.is_(None))
        if user_id is not None:
            stmt = stmt.where(Bill.user_id == user_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_linha_digitavel(
        self, linha_digitavel: str, *, user_id: int | None = None
    ) -> Bill | None:
        """Get non-discarded, non-deleted bill by linha digitável."""
        stmt = select(Bill).where(
            Bill.linha_digitavel == linha_digitavel,
            Bill.deleted_at.is_(None),
            Bill.status != BillStatus.DESCARTADA,
        )
        if user_id is not None:
            stmt = stmt.where(Bill.user_id == user_id)
        result = await self.session.execute(stmt.limit(1))
        return result.scalar_one_or_none()

    async def find_heuristic_duplicate(
        self,
        *,
        user_id: int,
        beneficiario: str | None,
        valor_centavos: int | None,
        vencimento: date | datetime | None,
    ) -> Bill | None:
        """Detect potential duplicate when linha_digitavel is absent."""
        issuer_norm = normalize_issuer(beneficiario)
        if not issuer_norm or valor_centavos is None or vencimento is None:
            return None
        due_d = vencimento.date() if isinstance(vencimento, datetime) else vencimento
        result = await self.session.execute(
            select(Bill)
            .where(
                Bill.user_id == user_id,
                Bill.deleted_at.is_(None),
                Bill.status != BillStatus.DESCARTADA,
                Bill.issuer_normalized == issuer_norm,
                Bill.valor_centavos == valor_centavos,
                Bill.vencimento.is_not(None),
                func.date(Bill.vencimento) == due_d,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_pending_bills(self) -> Sequence[Bill]:
        """Get all pending bills (PENDENTE or VENCIDA)."""
        result = await self.session.execute(
            select(Bill)
            .where(
                Bill.deleted_at.is_(None),
                Bill.status.in_([BillStatus.PENDENTE, BillStatus.VENCIDA]),
            )
            .order_by(Bill.vencimento.asc().nulls_last())
        )
        return result.scalars().all()

    async def mark_overdue_bills(self, today_local: date) -> int:
        """Transition PENDENTE bills past due_date into VENCIDA."""
        result = await self.session.execute(
            select(Bill).where(
                Bill.deleted_at.is_(None),
                Bill.status == BillStatus.PENDENTE,
                Bill.vencimento.is_not(None),
                func.date(Bill.vencimento) < today_local,
            )
        )
        bills = list(result.scalars().all())
        for bill in bills:
            bill.status = BillStatus.VENCIDA
        await self.session.flush()
        await self._commit_if_owned()
        return len(bills)

    async def get_pending_review_for_reminder(self, older_than: datetime) -> list[Bill]:
        """Return EM_REVISAO bills older than `older_than` that haven't been reminded recently."""
        result = await self.session.execute(
            select(Bill).where(
                Bill.deleted_at.is_(None),
                Bill.status == BillStatus.EM_REVISAO,
                Bill.created_at <= older_than,
                or_(
                    Bill.review_reminded_at.is_(None),
                    Bill.review_reminded_at <= older_than,
                ),
            )
        )
        return list(result.scalars().all())

    async def get_bills_for_alert(self, alert_type: AlertType) -> Sequence[Bill]:
        """Get bills that need a specific alert type."""
        today = date.today()
        lead_days = settings.lead_days

        if alert_type == AlertType.D3:
            target_date = today + timedelta(days=lead_days[0] if lead_days else 3)
        elif alert_type == AlertType.D1:
            target_date = today + timedelta(days=lead_days[1] if len(lead_days) > 1 else 1)
        elif alert_type == AlertType.D0:
            target_date = today
        elif alert_type == AlertType.ATRASO:
            target_date = today - timedelta(days=1)
        else:
            return []

        subquery = select(AlertSent.bill_id).where(
            AlertSent.alert_type == alert_type,
            AlertSent.sent_date == today,
        )

        if alert_type == AlertType.ATRASO:
            query = (
                select(Bill)
                .where(
                    and_(
                        Bill.deleted_at.is_(None),
                        Bill.status.in_([BillStatus.PENDENTE, BillStatus.VENCIDA]),
                        or_(
                            Bill.snoozed_until.is_(None),
                            Bill.snoozed_until <= datetime.now(UTC),
                        ),
                        Bill.vencimento.is_not(None),
                        func.date(Bill.vencimento) <= target_date,
                        Bill.id.not_in(subquery),
                    )
                )
                .order_by(Bill.vencimento.asc())
            )
        else:
            query = (
                select(Bill)
                .where(
                    and_(
                        Bill.deleted_at.is_(None),
                        Bill.status == BillStatus.PENDENTE,
                        or_(
                            Bill.snoozed_until.is_(None),
                            Bill.snoozed_until <= datetime.now(UTC),
                        ),
                        Bill.vencimento.is_not(None),
                        func.date(Bill.vencimento) == target_date,
                        Bill.id.not_in(subquery),
                    )
                )
                .order_by(Bill.vencimento.asc())
            )

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_bills_by_status(self, status: BillStatus) -> Sequence[Bill]:
        """Get bills by status."""
        result = await self.session.execute(
            select(Bill)
            .where(Bill.status == status, Bill.deleted_at.is_(None))
            .order_by(Bill.vencimento.asc().nulls_last())
        )
        return result.scalars().all()

    async def get_month_summary(self, year: int, month: int, *, user_id: int | None = None) -> dict:
        """Get summary for a specific month."""
        start_date = date(year, month, 1)
        end_date = date(year, month, monthrange(year, month)[1])

        conditions = [
            Bill.deleted_at.is_(None),
            Bill.status != BillStatus.DESCARTADA,
            Bill.vencimento.is_not(None),
            func.date(Bill.vencimento) >= start_date,
            func.date(Bill.vencimento) <= end_date,
        ]
        if user_id is not None:
            conditions.append(Bill.user_id == user_id)

        result = await self.session.execute(
            select(
                Bill.status,
                func.count(Bill.id).label("bill_count"),
                func.coalesce(func.sum(Bill.valor_centavos), 0).label("total"),
            )
            .where(and_(*conditions))
            .group_by(Bill.status)
        )

        summary: dict[str, int | float] = {
            "previsto": 0,
            "pago": 0,
            "pendente": 0,
            "vencida": 0,
            "total_previsto": 0,
            "total_pago": 0,
            "total_pendente": 0,
            "total_vencida": 0,
        }

        for row in result:
            status = row.status.value.lower()
            if status in summary:
                summary[status] = row._mapping["bill_count"]
                summary[f"total_{status}"] = row._mapping["total"]

        summary["previsto"] = sum(summary.get(s, 0) for s in ["pendente", "pago", "vencida"])
        summary["total_previsto"] = sum(
            summary.get(f"total_{s}", 0) for s in ["pendente", "pago", "vencida"]
        )

        return summary

    async def update_status(
        self, bill_id: int, status: BillStatus, paid_at: datetime | None = None
    ) -> Bill | None:
        """Update bill status."""
        bill = await self.get_by_id(bill_id)
        if bill:
            bill.status = status
            if status == BillStatus.PAGO and paid_at:
                bill.paid_at = paid_at
            await self.session.flush()
            await self.session.refresh(bill)
        return bill

    async def mark_valor_confirmado(self, bill_id: int, confirmed: bool) -> Bill | None:
        """Mark bill valor as confirmed."""
        bill = await self.get_by_id(bill_id)
        if bill:
            bill.valor_confirmado = confirmed
            await self.session.flush()
            await self.session.refresh(bill)
        return bill

    async def delete(self, bill_id: int) -> bool:
        """Delete a bill."""
        bill = await self.get_by_id(bill_id)
        if bill:
            await self.session.delete(bill)
            await self.session.flush()
            return True
        return False

    async def get_paid_bills_for_file_retention(self, cutoff: datetime) -> list[Bill]:
        """Get PAGO bills whose original files should be purged by retention policy."""
        result = await self.session.execute(
            select(Bill).where(
                Bill.status == BillStatus.PAGO,
                Bill.paid_at.is_not(None),
                Bill.paid_at <= cutoff,
                Bill.arquivo_path.is_not(None),
            )
        )
        return list(result.scalars().all())


class AlertRepository:
    """Repository for AlertSent operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def mark_sent(
        self,
        bill_id: int,
        alert_type: AlertType,
        *,
        rule: str | None = None,
        sent_date: date | None = None,
        telegram_message_id: int | None = None,
    ) -> AlertSent:
        """Mark an alert as sent."""
        alert = AlertSent(
            bill_id=bill_id,
            alert_type=alert_type,
            rule=rule or alert_type.value,
            sent_date=sent_date or date.today(),
            telegram_message_id=telegram_message_id,
        )
        self.session.add(alert)
        await self.session.flush()
        return alert

    async def was_sent(
        self,
        bill_id: int,
        alert_type: AlertType,
        *,
        rule: str | None = None,
        sent_date: date | None = None,
    ) -> bool:
        """Check if alert was already sent (for rule+date when provided)."""
        conditions = [AlertSent.bill_id == bill_id]
        if rule is not None:
            conditions.append(AlertSent.rule == rule)
        else:
            conditions.append(AlertSent.alert_type == alert_type)
        if sent_date is not None:
            conditions.append(AlertSent.sent_date == sent_date)

        result = await self.session.execute(select(AlertSent).where(and_(*conditions)).limit(1))
        return result.scalar_one_or_none() is not None

    async def get_last_overdue_sent_date(self, bill_id: int) -> date | None:
        """Get the most recent date an ATRASO alert was sent for `bill_id`."""
        result = await self.session.execute(
            select(AlertSent.sent_date)
            .where(AlertSent.bill_id == bill_id, AlertSent.alert_type == AlertType.ATRASO)
            .order_by(AlertSent.sent_date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class SnoozeRepository:
    """Repository for Snooze operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_snooze(self, bill_id: int, chat_id: int, remind_at: datetime) -> Snooze:
        snooze = Snooze(bill_id=bill_id, chat_id=chat_id, remind_at=remind_at, done=False)
        self.session.add(snooze)
        bill = await self.session.get(Bill, bill_id)
        if bill:
            bill.snoozed_until = remind_at
        await self.session.flush()
        return snooze

    async def get_due_snoozes(self, now: datetime) -> list[Snooze]:
        result = await self.session.execute(
            select(Snooze)
            .where(Snooze.done.is_(False), Snooze.remind_at <= now)
            .order_by(Snooze.remind_at.asc())
        )
        return list(result.scalars().all())

    async def mark_done(self, snooze_id: int) -> None:
        snooze = await self.session.get(Snooze, snooze_id)
        if snooze:
            snooze.done = True
            await self.session.flush()


class SettingsRepository:
    """Repository for per-chat persisted user preferences."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create(self, chat_id: int) -> UserSettings:
        row = await self.session.get(UserSettings, chat_id)
        if row is None:
            row = UserSettings(
                chat_id=chat_id,
                alert_time=settings.alert_time,
                lead_days=",".join(str(d) for d in settings.lead_days),
                timezone=settings.default_timezone,
                auto_confirm=settings.auto_confirm,
                currency=settings.currency,
                extractor_mode=settings.extractor,
                cloud_consent=settings.cloud_extraction_consent,
                llm_model=settings.llm_vision_model,
            )
            self.session.add(row)
            await self.session.flush()
        return row

    async def update_setting(self, chat_id: int, field_name: str, value: Any) -> UserSettings:
        row = await self.get_or_create(chat_id)
        allowed = {
            "alert_time",
            "lead_days",
            "timezone",
            "auto_confirm",
            "currency",
            "extractor_mode",
            "cloud_consent",
            "llm_model",
            "muted",
            "locale",
        }
        if field_name not in allowed:
            _raise_repository_error(f"Unsupported setting field: {field_name}")
        setattr(row, field_name, value)
        await self.session.flush()
        return row


class QueueRepository:
    """Repository for persistent retry queue."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def enqueue(
        self,
        *,
        file_hash: str,
        file_path: str,
        source: str,
        chat_id: int | None = None,
        error: str | None = None,
    ) -> ProcessingQueueItem:
        item = ProcessingQueueItem(
            file_hash=file_hash,
            file_path=file_path,
            source=source,
            chat_id=chat_id,
            status=QueueStatus.PENDING,
            attempts=0,
            last_error=error,
            next_retry_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def get_due_items(self, now: datetime) -> list[ProcessingQueueItem]:
        result = await self.session.execute(
            select(ProcessingQueueItem)
            .where(
                ProcessingQueueItem.status == QueueStatus.PENDING,
                ProcessingQueueItem.attempts < MAX_QUEUE_ATTEMPTS,
                or_(
                    ProcessingQueueItem.next_retry_at.is_(None),
                    ProcessingQueueItem.next_retry_at <= now,
                ),
            )
            .order_by(ProcessingQueueItem.created_at.asc())
        )
        return list(result.scalars().all())

    async def mark_done(self, item_id: int) -> None:
        item = await self.session.get(ProcessingQueueItem, item_id)
        if item:
            item.status = QueueStatus.DONE
            await self.session.flush()

    async def mark_failed(self, item_id: int, error: str) -> None:
        item = await self.session.get(ProcessingQueueItem, item_id)
        if item:
            item.attempts += 1
            item.last_error = error[:500]
            if item.attempts >= MAX_QUEUE_ATTEMPTS:
                item.status = QueueStatus.FAILED
            else:
                backoff_minutes = 2**item.attempts
                item.next_retry_at = datetime.now(UTC) + timedelta(minutes=backoff_minutes)
            await self.session.flush()

    async def count_pending(self) -> int:
        val = await self.session.scalar(
            select(func.count(ProcessingQueueItem.id)).where(
                ProcessingQueueItem.status == QueueStatus.PENDING
            )
        )
        return int(val or 0)

    async def recent_errors(self, limit: int = 5) -> list[ProcessingQueueItem]:
        result = await self.session.execute(
            select(ProcessingQueueItem)
            .where(ProcessingQueueItem.last_error.is_not(None))
            .order_by(ProcessingQueueItem.updated_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


class RecurringRepository:
    """Repository for recurring bill detection and alerts."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def detect_and_upsert(self, bill: Bill) -> RecurringBill | None:
        """Detect monthly recurrence when >=2 bills from same issuer are ~25–35 days apart."""
        if not bill.beneficiario or not bill.vencimento or bill.user_id is None:
            return None
        issuer_norm = bill.issuer_normalized or normalize_issuer(bill.beneficiario)
        if not issuer_norm:
            return None

        result = await self.session.execute(
            select(Bill)
            .where(
                Bill.user_id == bill.user_id,
                Bill.deleted_at.is_(None),
                Bill.status != BillStatus.DESCARTADA,
                Bill.issuer_normalized == issuer_norm,
                Bill.vencimento.is_not(None),
            )
            .order_by(Bill.vencimento.asc())
        )
        same_issuer = list(result.scalars().all())
        if len(same_issuer) < 2:
            return None

        prev_due = same_issuer[-2].vencimento
        curr_due = same_issuer[-1].vencimento
        if prev_due is None or curr_due is None:
            return None
        gap = abs((curr_due.date() - prev_due.date()).days)
        if not (25 <= gap <= 35):
            return None

        existing_res = await self.session.execute(
            select(RecurringBill).where(
                RecurringBill.user_id == bill.user_id,
                RecurringBill.issuer_normalized == issuer_norm,
            )
        )
        rec = existing_res.scalar_one_or_none()
        valores = [b.valor_centavos for b in same_issuer if b.valor_centavos is not None]
        avg_val = int(sum(valores) / len(valores)) if valores else (bill.valor_centavos or 0)
        if rec is None:
            rec = RecurringBill(
                user_id=bill.user_id,
                pattern="mensal",
                beneficiario=bill.beneficiario,
                issuer_normalized=issuer_norm,
                valor_centavos=avg_val,
                dia_vencimento=curr_due.day,
                last_seen=curr_due.date(),
                ativo=True,
            )
            self.session.add(rec)
            await self.session.flush()
        else:
            rec.valor_centavos = avg_val
            rec.dia_vencimento = curr_due.day
            rec.last_seen = curr_due.date()
            await self.session.flush()

        bill.recurring_id = rec.id
        await self.session.flush()
        return rec

    async def get_active(self, user_id: int | None = None) -> list[RecurringBill]:
        stmt = select(RecurringBill).where(RecurringBill.ativo.is_(True))
        if user_id is not None:
            stmt = stmt.where(RecurringBill.user_id == user_id)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class AuditRepository:
    """Repository for audit logs."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def log_action(
        self, chat_id: int, action: str, bill_id: int | None = None, details: str | None = None
    ) -> AuditLog:
        """Log an audit action."""
        audit = AuditLog(chat_id=chat_id, action=action, bill_id=bill_id, details=details)
        self.session.add(audit)
        await self.session.flush()
        return audit


# Global database instance
db = Database()
