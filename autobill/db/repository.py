"""Database repository layer for AutoBill."""

from calendar import monthrange
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from sqlalchemy import and_, func, or_, select
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
)

if TYPE_CHECKING:
    from autobill.extract.pipeline import ExtractionResult


def _raise_repository_error(message: str) -> NoReturn:
    raise ValueError(message)


class Database:
    """Database connection manager."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or str(settings.db_path)
        self.engine: AsyncEngine | None = None
        self.session_factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> None:
        """Initialize database connection."""
        # Ensure directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

        # SQLite with WAL mode for better concurrency
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}",
            echo=False,
            connect_args={
                "check_same_thread": False,
                "timeout": 30,
            },
        )

        # Enable WAL mode
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql("PRAGMA journal_mode=WAL;")
            await conn.exec_driver_sql("PRAGMA busy_timeout=30000;")
            await conn.exec_driver_sql("PRAGMA synchronous=NORMAL;")

        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def create_tables(self) -> None:
        """Create all tables."""
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

    async def get_bill(self, bill_id: int, *, user_id: int) -> Bill | None:
        """Get a bill owned by the requesting user."""
        result = await self.session.execute(
            select(Bill).where(Bill.id == bill_id, Bill.user_id == user_id)
        )
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
        query = select(Bill).where(Bill.user_id == user_id)
        if status_filter:
            status = status_by_filter.get(status_filter.lower())
            if status is None:
                return [], 0
            query = query.where(Bill.status == status)

        total = await self.session.scalar(select(func.count()).select_from(query.subquery()))
        result = await self.session.execute(
            query.order_by(Bill.created_at.desc())
            .offset(max(0, (page - 1) * per_page))
            .limit(per_page)
        )
        return list(result.scalars().all()), total or 0

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
    ) -> Bill:
        """Create a reviewable bill from extracted fields."""
        existing_bill = await self.get_by_hash(source_hash, user_id=user_id)
        if existing_bill is not None:
            return existing_bill
        vencimento = (
            datetime.fromisoformat(extraction_result.vencimento)
            if extraction_result.vencimento
            else None
        )
        bill = Bill(
            hash_sha256=source_hash,
            linha_digitavel=extraction_result.linha_digitavel,
            codigo_barras=extraction_result.codigo_barras,
            beneficiario=extraction_result.beneficiario,
            valor_centavos=extraction_result.valor,
            vencimento=vencimento,
            status=BillStatus.EM_REVISAO,
            user_id=user_id,
            origem=BillOrigin.TELEGRAM,
            arquivo_path=source_file,
            confidence=extraction_result.confidence,
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
        elif field_name in ("linha_digitavel", "codigo_barras", "beneficiario"):
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
        bill.snoozed_until = datetime.now(UTC) + timedelta(days=days)
        await self.session.flush()
        await self._commit_if_owned()
        return bill

    async def create(self, bill: Bill) -> Bill:
        """Create a new bill."""
        self.session.add(bill)
        await self.session.flush()
        await self.session.refresh(bill)
        await self._commit_if_owned()
        return bill

    async def get_by_id(self, bill_id: int) -> Bill | None:
        """Get bill by ID."""
        result = await self.session.execute(
            select(Bill).where(Bill.id == bill_id).options(selectinload(Bill.alerts_sent))
        )
        return result.scalar_one_or_none()

    async def get_by_hash(self, hash_sha256: str, *, user_id: int) -> Bill | None:
        """Get a user's bill by SHA-256 hash."""
        result = await self.session.execute(
            select(Bill).where(Bill.hash_sha256 == hash_sha256, Bill.user_id == user_id)
        )
        return result.scalar_one_or_none()

    async def get_by_linha_digitavel(self, linha_digitavel: str) -> Bill | None:
        """Get bill by linha digitável."""
        result = await self.session.execute(
            select(Bill).where(Bill.linha_digitavel == linha_digitavel)
        )
        return result.scalar_one_or_none()

    async def get_pending_bills(self) -> Sequence[Bill]:
        """Get all pending bills (PENDENTE or VENCIDA)."""
        result = await self.session.execute(
            select(Bill)
            .where(Bill.status.in_([BillStatus.PENDENTE, BillStatus.VENCIDA]))
            .order_by(Bill.vencimento.asc().nulls_last())
        )
        return result.scalars().all()

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
            target_date = today - timedelta(days=1)  # Overdue bills
        else:
            return []

        # Get bills that need this alert (not already sent)
        subquery = select(AlertSent.bill_id).where(AlertSent.alert_type == alert_type)

        if alert_type == AlertType.ATRASO:
            # Overdue: vencimento < today and status PENDENTE
            query = (
                select(Bill)
                .where(
                    and_(
                        Bill.status == BillStatus.PENDENTE,
                        or_(
                            Bill.snoozed_until.is_(None),
                            Bill.snoozed_until <= datetime.now(UTC),
                        ),
                        Bill.vencimento < today,
                        Bill.id.not_in(subquery),
                    )
                )
                .order_by(Bill.vencimento.asc())
            )
        else:
            # Future alerts: vencimento == target_date and status PENDENTE
            query = (
                select(Bill)
                .where(
                    and_(
                        Bill.status == BillStatus.PENDENTE,
                        or_(
                            Bill.snoozed_until.is_(None),
                            Bill.snoozed_until <= datetime.now(UTC),
                        ),
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
            select(Bill).where(Bill.status == status).order_by(Bill.vencimento.asc().nulls_last())
        )
        return result.scalars().all()

    async def get_month_summary(self, year: int, month: int) -> dict:
        """Get summary for a specific month."""
        start_date = date(year, month, 1)
        end_date = date(year, month, monthrange(year, month)[1])

        result = await self.session.execute(
            select(
                Bill.status,
                func.count(Bill.id).label("bill_count"),
                func.coalesce(func.sum(Bill.valor_centavos), 0).label("total"),
            )
            .where(
                and_(
                    Bill.vencimento >= start_date,
                    Bill.vencimento <= end_date,
                )
            )
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


class AlertRepository:
    """Repository for AlertSent operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def mark_sent(self, bill_id: int, alert_type: AlertType) -> AlertSent:
        """Mark an alert as sent."""
        alert = AlertSent(bill_id=bill_id, alert_type=alert_type)
        self.session.add(alert)
        await self.session.flush()
        return alert

    async def was_sent(self, bill_id: int, alert_type: AlertType) -> bool:
        """Check if alert was already sent."""
        result = await self.session.execute(
            select(AlertSent).where(
                and_(AlertSent.bill_id == bill_id, AlertSent.alert_type == alert_type)
            )
        )
        return result.scalar_one_or_none() is not None


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
