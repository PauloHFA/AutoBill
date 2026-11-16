"""Initial schema for AutoBill.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-16 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "recurring_bills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("pattern", sa.String(length=255), nullable=False),
        sa.Column("beneficiario", sa.String(length=255), nullable=False),
        sa.Column("issuer_normalized", sa.String(length=255), nullable=False),
        sa.Column("valor_centavos", sa.BigInteger(), nullable=False),
        sa.Column("dia_vencimento", sa.Integer(), nullable=False),
        sa.Column("last_seen", sa.Date(), nullable=True),
        sa.Column("last_alerted_at", sa.Date(), nullable=True),
        sa.Column("ativo", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recurring_bills_beneficiario", "recurring_bills", ["beneficiario"])
    op.create_index(
        "ix_recurring_bills_issuer_normalized", "recurring_bills", ["issuer_normalized"]
    )
    op.create_index("ix_recurring_bills_user_id", "recurring_bills", ["user_id"])

    op.create_table(
        "bills",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("hash_sha256", sa.String(length=64), nullable=False),
        sa.Column("linha_digitavel", sa.String(length=48), nullable=True),
        sa.Column("codigo_barras", sa.String(length=44), nullable=True),
        sa.Column("beneficiario", sa.String(length=255), nullable=True),
        sa.Column("issuer_normalized", sa.String(length=255), nullable=True),
        sa.Column("valor_centavos", sa.BigInteger(), nullable=True),
        sa.Column("vencimento", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("origem", sa.String(length=8), nullable=False),
        sa.Column("arquivo_path", sa.String(length=512), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("valor_confirmado", sa.Boolean(), nullable=False),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("review_reminded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_by", sa.String(length=128), nullable=True),
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recurring_id", sa.Integer(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["recurring_id"], ["recurring_bills.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "hash_sha256", name="uq_bills_user_hash"),
    )
    op.create_index("ix_bills_codigo_barras", "bills", ["codigo_barras"])
    op.create_index("ix_bills_deleted_at", "bills", ["deleted_at"])
    op.create_index("ix_bills_hash_sha256", "bills", ["hash_sha256"])
    op.create_index("ix_bills_issuer_normalized", "bills", ["issuer_normalized"])
    op.create_index("ix_bills_linha_digitavel", "bills", ["linha_digitavel"])
    op.create_index("ix_bills_linha_digitavel_status", "bills", ["linha_digitavel", "status"])
    op.create_index("ix_bills_status", "bills", ["status"])
    op.create_index("ix_bills_status_vencimento", "bills", ["status", "vencimento"])
    op.create_index("ix_bills_user_id", "bills", ["user_id"])
    op.create_index("ix_bills_vencimento", "bills", ["vencimento"])

    op.create_table(
        "alerts_sent",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("alert_type", sa.String(length=11), nullable=False),
        sa.Column("rule", sa.String(length=32), nullable=False),
        sa.Column("sent_date", sa.Date(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bill_id", "rule", "sent_date", name="uq_bill_alert_rule_date"),
    )
    op.create_index("ix_alerts_sent_bill_id", "alerts_sent", ["bill_id"])

    op.create_table(
        "snoozes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("remind_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_snoozes_bill_id", "snoozes", ["bill_id"])
    op.create_index("ix_snoozes_chat_id", "snoozes", ["chat_id"])
    op.create_index("ix_snoozes_done", "snoozes", ["done"])
    op.create_index("ix_snoozes_remind_at", "snoozes", ["remind_at"])

    op.create_table(
        "settings",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("alert_time", sa.String(length=5), nullable=False),
        sa.Column("lead_days", sa.String(length=64), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("auto_confirm", sa.Boolean(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("extractor_mode", sa.String(length=16), nullable=False),
        sa.Column("cloud_consent", sa.Boolean(), nullable=False),
        sa.Column("llm_model", sa.String(length=64), nullable=False),
        sa.Column("muted", sa.Boolean(), nullable=False),
        sa.Column("locale", sa.String(length=16), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )

    op.create_table(
        "processing_queue",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("file_hash", sa.String(length=64), nullable=False),
        sa.Column("file_path", sa.String(length=512), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_processing_queue_file_hash", "processing_queue", ["file_hash"])
    op.create_index("ix_processing_queue_status", "processing_queue", ["status"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("bill_id", sa.Integer(), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_chat_id", "audit_logs", ["chat_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("processing_queue")
    op.drop_table("settings")
    op.drop_table("snoozes")
    op.drop_table("alerts_sent")
    op.drop_table("bills")
    op.drop_table("recurring_bills")
