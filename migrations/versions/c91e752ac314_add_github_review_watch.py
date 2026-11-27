"""Track GitHub pull request review reminders and timeouts."""

# ruff: noqa: INP001

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c91e752ac314"
down_revision: str | Sequence[str] | None = "92df9b64c51a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    op.create_table(
        "github_review_watch",
        sa.Column("repository_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("pull_number", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("owner", sa.String(), nullable=False),
        sa.Column("repo", sa.String(), nullable=False),
        sa.Column("app_id", sa.String(), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("first_requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_review_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("has_activity", sa.Boolean(), nullable=False),
        sa.Column("has_volunteer", sa.Boolean(), nullable=False),
        sa.Column("finished", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("repository_id", "pull_number", name=op.f("pk_github_review_watch")),
        info={"bind_key": ""},
    )
    op.create_table(
        "github_review_reminder",
        sa.Column("repository_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("pull_number", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("reviewer_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("reviewer_login", sa.String(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requester", sa.String(), nullable=False),
        sa.Column("warnings", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("repository_id", "pull_number", "reviewer_id", name=op.f("pk_github_review_reminder")),
        info={"bind_key": ""},
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.drop_table("github_review_reminder")
    op.drop_table("github_review_watch")
