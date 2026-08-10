"""Add a durable conservative alert delivery state machine.

Revision ID: 0002_alert_delivery_state
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_alert_delivery_state"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alerts") as batch:
        batch.add_column(
            sa.Column(
                "delivery_state",
                sa.String(length=16),
                nullable=False,
                server_default="sent",
            )
        )
        batch.add_column(
            sa.Column(
                "retryable", sa.Boolean(), nullable=False, server_default=sa.text("0")
            )
        )
        batch.add_column(sa.Column("reserved_at", sa.DateTime(timezone=True)))
        batch.alter_column(
            "sent_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=True,
        )
    op.execute("UPDATE alerts SET reserved_at = sent_at")
    with op.batch_alter_table("alerts") as batch:
        batch.alter_column(
            "delivery_state",
            existing_type=sa.String(length=16),
            existing_nullable=False,
            server_default="pending",
        )
        batch.alter_column(
            "reserved_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    op.execute("DELETE FROM alerts WHERE sent_at IS NULL")
    with op.batch_alter_table("alerts") as batch:
        batch.alter_column(
            "sent_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch.drop_column("reserved_at")
        batch.drop_column("retryable")
        batch.drop_column("delivery_state")
