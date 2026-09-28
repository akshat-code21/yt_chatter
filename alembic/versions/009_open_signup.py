"""Open signup - activate legacy pending users, default new users to active.

Revision ID: 009_open_signup
Revises: 008_perf_indexes
Create Date: 2026-09-28
"""

from alembic import op

# revision identifiers
revision = "009_open_signup"
down_revision = "008_perf_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Flip any pre-existing invite-gated accounts to active so nobody is
    # locked out after the invite barrier is removed.
    op.execute("UPDATE users SET status = 'active' WHERE status = 'pending_invite'")
    # New rows default to active at the DB level too (model default already
    # changed to ACTIVE; this keeps raw SQL inserts consistent).
    op.execute("ALTER TABLE users ALTER COLUMN status SET DEFAULT 'active'")


def downgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN status SET DEFAULT 'pending_invite'")
    # No data downgrade: pending_invite accounts that were auto-activated
    # cannot be distinguished, so they stay active.
