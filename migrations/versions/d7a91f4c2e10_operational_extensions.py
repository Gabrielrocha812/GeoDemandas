"""monitoring, knowledge, reports and configurable SLA

Revision ID: d7a91f4c2e10
Revises: c060c2b494d8
"""
from alembic import op
import sqlalchemy as sa


revision = "d7a91f4c2e10"
down_revision = "c060c2b494d8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_articles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("slug", sa.String(180), nullable=False),
        sa.Column("title", sa.String(220), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(120), nullable=False),
        sa.Column("tags", sa.String(500)),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ux_knowledge_articles_slug", "knowledge_articles", ["slug"], unique=True)
    op.create_index("ix_knowledge_articles_title", "knowledge_articles", ["title"])
    op.create_index("ix_knowledge_articles_category", "knowledge_articles", ["category"])
    op.create_index("ix_knowledge_articles_published", "knowledge_articles", ["is_published"])

    op.create_table(
        "report_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("frequency", sa.String(20), nullable=False),
        sa.Column("report_format", sa.String(10), nullable=False, server_default="xlsx"),
        sa.Column("filters_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(), nullable=False),
        sa.Column("last_run_at", sa.DateTime()),
        sa.Column("last_status", sa.String(30)),
        sa.Column("last_error", sa.String(500)),
        sa.Column("created_by_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_report_schedules_recipient", "report_schedules", ["recipient"])
    op.create_index("ix_report_schedules_active", "report_schedules", ["is_active"])
    op.create_index("ix_report_schedules_next_run", "report_schedules", ["next_run_at"])

    op.create_table(
        "sla_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("priority", sa.String(30), nullable=False),
        sa.Column("category", sa.String(120)),
        sa.Column("project_code", sa.Integer()),
        sa.Column("first_response_hours", sa.Integer(), nullable=False),
        sa.Column("resolution_hours", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_sla_policy_priority", "sla_policies", ["priority"])
    op.create_index("ix_sla_policy_category", "sla_policies", ["category"])
    op.create_index("ix_sla_policy_project", "sla_policies", ["project_code"])

    op.create_table(
        "business_holidays",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
    )
    op.create_index("ux_business_holiday_date", "business_holidays", ["holiday_date"], unique=True)

    op.create_table(
        "system_alerts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("fingerprint", sa.String(160), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("title", sa.String(180), nullable=False),
        sa.Column("message", sa.String(1000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime()),
        sa.Column("resolved_at", sa.DateTime()),
    )
    op.create_index("ux_system_alert_fingerprint", "system_alerts", ["fingerprint"], unique=True)
    op.create_index("ix_system_alert_status", "system_alerts", ["status"])


def downgrade():
    op.drop_table("system_alerts")
    op.drop_table("business_holidays")
    op.drop_table("sla_policies")
    op.drop_table("report_schedules")
    op.drop_table("knowledge_articles")
