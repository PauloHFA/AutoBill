"""SQLAlchemy models for Sistema-Monitoramento-Boletos."""

from datetime import date, datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
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
    CUSTOM = "CUSTOM"  # Antecedência customizada (D-n)
    REVISAO = "REVISAO"  # Lembrete de revisão 24h
    RECORRENCIA = "RECORRENCIA"  # Fatura recorrente prevista não recebida


class QueueStatus(PyEnum):
    """Status dos itens na fila de reprocessamento."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DONE = "DONE"
    FAILED = "FAILED"


class Bill(Base):
    """Modelo principal de fatura/boleto."""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hash_sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    linha_digitavel: Mapped[str | None] = mapped_column(String(48), index=True, nullable=True)
    codigo_barras: Mapped[str | None] = mapped_column(String(44), index=True, nullable=True)
    beneficiario: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issuer_normalized: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    valor_centavos: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    vencimento: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="outro", nullable=False)
    status: Mapped[BillStatus] = mapped_column(
        Enum(BillStatus, native_enum=False),
        default=BillStatus.EM_REVISAO,
        index=True,
        nullable=False,
    )
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    origem: Mapped[BillOrigin] = mapped_column(
        Enum(BillOrigin, native_enum=False), default=BillOrigin.PASTA, nullable=False
    )
    arquivo_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    valor_confirmado: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recurring_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("recurring_bills.id", ondelete="SET NULL"), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True, nullable=True
    )

    # Relationships
    alerts_sent: Mapped[list["AlertSent"]] = relationship(
        "AlertSent", back_populates="bill", cascade="all, delete-orphan"
    )
    snoozes: Mapped[list["Snooze"]] = relationship(
        "Snooze", back_populates="bill", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_bills_status_vencimento", "status", "vencimento"),
        Index("ix_bills_linha_digitavel_status", "linha_digitavel", "status"),
        UniqueConstraint("user_id", "hash_sha256", name="uq_bills_user_hash"),
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
    rule: Mapped[str] = mapped_column(String(32), default="D0", nullable=False)
    sent_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationship
    bill: Mapped["Bill"] = relationship("Bill", back_populates="alerts_sent")

    __table_args__ = (
        UniqueConstraint("bill_id", "rule", "sent_date", name="uq_bill_alert_rule_date"),
    )


class Snooze(Base):
    """Sonecas e reagendamentos de alertas de faturas."""

    __tablename__ = "snoozes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bills.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    bill: Mapped["Bill"] = relationship("Bill", back_populates="snoozes")


class UserSettings(Base):
    """Preferências persistidas por chat autorizado."""

    __tablename__ = "settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alert_time: Mapped[str] = mapped_column(String(5), default="07:30", nullable=False)
    lead_days: Mapped[str] = mapped_column(String(64), default="3,1,0", nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Fortaleza", nullable=False)
    auto_confirm: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="BRL", nullable=False)
    extractor_mode: Mapped[str] = mapped_column(String(16), default="auto", nullable=False)
    cloud_consent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    llm_model: Mapped[str] = mapped_column(String(64), default="qwen2.5-vl", nullable=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    locale: Mapped[str] = mapped_column(String(16), default="pt_BR", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RecurringBill(Base):
    """Detecção de faturas recorrentes."""

    __tablename__ = "recurring_bills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True)
    pattern: Mapped[str] = mapped_column(String(255), default="mensal", nullable=False)
    beneficiario: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    issuer_normalized: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    valor_centavos: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dia_vencimento: Mapped[int] = mapped_column(Integer, nullable=False)  # 1-31
    last_seen: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_alerted_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    ativo: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProcessingQueueItem(Base):
    """Fila persistente para reprocessamento em caso de falha externa."""

    __tablename__ = "processing_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="pasta", nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[QueueStatus] = mapped_column(
        Enum(QueueStatus, native_enum=False),
        default=QueueStatus.PENDING,
        index=True,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
