"""Add GitHub OAuth state and QQ account bindings."""

# ruff: noqa: INP001

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "92df9b64c51a"
down_revision: str | Sequence[str] | None = "ab229262bb01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade(name: str = "") -> None:
    if name:
        return

    op.create_table(
        "github_oauth_state",
        sa.Column("qq_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("verifier", sa.String(length=128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("qq_id", name=op.f("pk_github_oauth_state")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_github_oauth_state_state_hash")),
        info={"bind_key": ""},
    )
    op.create_index(op.f("ix_github_oauth_state_expires_at"), "github_oauth_state", ["expires_at"])
    op.create_table(
        "github_account_binding",
        sa.Column("qq_id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("github_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("qq_id", name=op.f("pk_github_account_binding")),
        sa.UniqueConstraint("github_id", name=op.f("uq_github_account_binding_github_id")),
        info={"bind_key": ""},
    )


def downgrade(name: str = "") -> None:
    if name:
        return

    op.drop_table("github_account_binding")
    op.drop_index(op.f("ix_github_oauth_state_expires_at"), table_name="github_oauth_state")
    op.drop_table("github_oauth_state")
