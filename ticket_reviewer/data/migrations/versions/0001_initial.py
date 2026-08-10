"""Create the initial ticket review history schema."""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    utc = sa.DateTime(timezone=True)
    money = sa.Numeric(12, 2)
    rate = sa.Numeric(8, 4)
    roi = sa.Numeric(12, 4)

    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("team", sa.String(32), nullable=False),
        sa.Column("opponent", sa.String(255), nullable=False),
        sa.Column("venue", sa.String(255), nullable=False),
        sa.Column("starts_at", utc, nullable=False),
        sa.Column("is_home", sa.Boolean(), nullable=False),
        sa.Column("created_at", utc, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", utc, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("team", "opponent", "venue", "starts_at", name="uq_events_identity"),
    )
    op.create_table(
        "source_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("raw_name", sa.String(512), nullable=False),
        sa.Column("last_seen", utc, nullable=False),
        sa.UniqueConstraint("source", "external_id", name="uq_source_events_identity"),
    )
    op.create_table(
        "observations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("event_external_id", sa.String(255), nullable=False),
        sa.Column("observed_at", utc, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("pair_price", money),
        sa.Column("buyer_fees", money),
        sa.Column("estimated_tax", money),
        sa.Column("section", sa.String(128)),
        sa.Column("row", sa.String(128)),
        sa.Column("quantity_available", sa.Integer()),
        sa.Column("can_buy_pair", sa.Boolean()),
        sa.Column("listing_id", sa.String(255)),
        sa.Column("listing_identity", sa.String(255), nullable=False),
        sa.Column("listing_url", sa.Text()),
        sa.Column("listing_count", sa.Integer()),
        sa.Column("popularity", sa.Numeric(12, 4)),
        sa.Column("freshness_at", utc, nullable=False),
        sa.Column("created_at", utc, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("source", "event_external_id", "listing_identity", "observed_at", name="uq_observations_snapshot"),
    )
    op.create_table(
        "opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_id", sa.Integer(), sa.ForeignKey("observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("acquisition_total", money, nullable=False),
        sa.Column("exit_source", sa.String(32)),
        sa.Column("projected_resale_gross", money),
        sa.Column("seller_fee_rate", rate),
        sa.Column("projected_proceeds", money),
        sa.Column("estimated_net_profit", money),
        sa.Column("roi", roi),
        sa.Column("confidence", sa.String(32), nullable=False),
        sa.Column("risk_reasons", sa.JSON(), nullable=False),
        sa.Column("actionable", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("scenarios", sa.JSON(), nullable=False),
        sa.Column("created_at", utc, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", utc, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("fingerprint", sa.String(255), nullable=False),
        sa.Column("sent_at", utc, nullable=False),
        sa.Column("profit_at_send", money, nullable=False),
        sa.Column("provider_message_id", sa.String(255)),
        sa.UniqueConstraint("fingerprint", name="uq_alerts_fingerprint"),
    )
    op.create_table(
        "outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("actual_acquisition", money),
        sa.Column("actual_proceeds", money),
        sa.Column("actual_fees", money),
        sa.Column("notes", sa.Text()),
        sa.Column("updated_at", utc, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("opportunity_id", name="uq_outcomes_opportunity"),
    )
    op.create_table(
        "connector_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("started_at", utc, nullable=False),
        sa.Column("finished_at", utc),
        sa.Column("success", sa.Boolean()),
        sa.Column("observation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("redacted_error", sa.Text()),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", utc, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_table(
        "manual_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("screenshot_path", sa.Text(), nullable=False),
        sa.Column("ocr_text", sa.Text()),
        sa.Column("corrected_payload", sa.JSON()),
        sa.Column("confirmed_at", utc),
    )


def downgrade() -> None:
    for table in (
        "manual_reviews",
        "settings",
        "connector_runs",
        "outcomes",
        "alerts",
        "opportunities",
        "observations",
        "source_events",
        "events",
    ):
        op.drop_table(table)
