"""SQLAlchemy models for AutoBill."""

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all models."""


class BillStatus(PyEnum):
    """Status do ciclo de vida da fatura."""

    EM_REVISAO = "EM_REVISAO"
    PENDENTE = "PENDENTE"
    PAGO = "PAGO"
    VENCIDA = "VENCIDA"
    DESCARTADA = "DESCARTADA"


class BillOrigin(PyEnum):
    """Origem da fatura."""

    PASTA = "pasta"
    TELEGRAM = "telegram"
    EMAIL = "email"
    MANUAL = "manual"


class AlertType(PyEnum):
    """Tipos de alerta enviados."""

    D3 = "D3"  # 3 dias antes
    D1 = "D1"  # 1 dia antes
    D0 = "D0"  # No dia
    ATRASO = "ATRASO"  # Em atraso


class Bill(Base):
    """Modelo principal de fatura/boleto."""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hash_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    linha_digitavel: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    codigo_barras: Mapped[str | None] = mapped_column(String(44), index=True, nullable=True)
    beneficiario: Mapped[str | None] = mapped_column(String(255), nullable=True)
    valor_centavos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vencimento: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    status: Mapped[BillStatus] = mapped_column(
        Enum(BillStatus, native_enum=False),
        default=BillStatus.EM_REVISAO,
        index=True,
        nullable=False,
    )
    origem: Mapped[BillOrigin] = mapped_column(
        Enum(BillOrigin, native_enum=False), default=BillOrigin.PASTA, nullable=False
    )
    arquivo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    valor_confirmado: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    alerts_sent: Mapped[list["AlertSent"]] = relationship(
        "AlertSent", back_populates="bill", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_bills_status_vencimento", "status", "vencimento"),
        Index("ix_bills_linha_digitavel_status", "linha_digitavel", "status"),
    )


class AlertSent(Base):
    """Registro de alertas enviados para idempotência."""

    __tablename__ = "alerts_sent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    alert_type: Mapped[AlertType] = mapped_column(
        Enum(AlertType, native_enum=False), nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationship
    bill: Mapped["Bill"] = relationship("Bill", back_populates="alerts_sent")

    __table_args__ = (
        UniqueConstraint("bill_id", "alert_type", name="uq_bill_alert_type"),
    )


class RecurringBill(Base):
    """Detecção de faturas recorrentes."""

    __tablename__ = "recurring_bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pattern: Mapped[str] = mapped_column(String(255), nullable=False)  # ex: "mensal", "bimestral"
    beneficiario: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    valor_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dia_vencimento: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-31
    ativo: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AuditLog(Base):
    """Log de auditoria de ações dos usuários."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    bill_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("bills.id", ondelete="SET NULL"), nullable=True
    )
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
