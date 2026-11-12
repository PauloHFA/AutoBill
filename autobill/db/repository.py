"""Database repository layer for AutoBill."""

from calendar import monthrange
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, func, select
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
    BillStatus,
)


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
        if self.engine is None:
            await self.connect()
        async with self.engine.begin() as conn:
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
        if self.session_factory is None:
            await self.connect()
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise


class BillRepository:
    """Repository for Bill operations."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, bill: Bill) -> Bill:
        """Create a new bill."""
        self.session.add(bill)
        await self.session.flush()
        await self.session.refresh(bill)
        return bill

    async def get_by_id(self, bill_id: int) -> Bill | None:
        """Get bill by ID."""
        result = await self.session.execute(
            select(Bill).where(Bill.id == bill_id).options(selectinload(Bill.alerts_sent))
        )
        return result.scalar_one_or_none()

    async def get_by_hash(self, hash_sha256: str) -> Bill | None:
        """Get bill by SHA-256 hash."""
        result = await self.session.execute(
            select(Bill).where(Bill.hash_sha256 == hash_sha256)
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
                func.count(Bill.id).label("count"),
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

        summary = {
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
                summary[status] = row.count
                summary[f"total_{status}"] = row.total

        summary["previsto"] = sum(
            summary.get(s, 0) for s in ["pendente", "pago", "vencida"]
        )
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
