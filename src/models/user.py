"""User and Invite models - Clerk-backed identity with app-side roles/status."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from src.database import Base


class UserRole(enum.StrEnum):
    """Application role. Admins can trigger pipelines and manage invites."""

    ADMIN = "admin"
    USER = "user"


class UserStatus(enum.StrEnum):
    """Lifecycle state.

    pending_invite: legacy value from the retired invite-only beta
                    (auto-activated to active on next request).
    active: full access.
    deactivated: blocked from the app (retains history for analytics).
    """

    PENDING_INVITE = "pending_invite"
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class Invite(Base):
    """Invite code that gates signup.

    - If ``invited_email`` is set only a Clerk user with exactly that email
      may redeem it; otherwise anyone with the code can.
    - ``max_uses`` allows multi-use invite codes (e.g. a cohort of 10 testers).
    """

    __tablename__ = "invites"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    invited_email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    # LEGACY: invites no longer grant roles; admins are promoted via Clerk
    # public metadata. Column kept to avoid a destructive migration.
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", native_enum=False),
        nullable=False,
        default=UserRole.USER,
    )
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    uses_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    redeemed_by = relationship(
        "User",
        foreign_keys="User.invite_id",
        back_populates="invite",
        viewonly=True,
    )
    created_by = relationship(
        "User",
        foreign_keys=[created_by_user_id],
        viewonly=True,
    )


class User(Base):
    """Application user, provisioned on first authenticated request (JIT).

    Identity lives in Clerk (``clerk_user_id``); this row mirrors profile data
    and holds app-specific state (role, status, usage linkage).
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("clerk_user_id", name="uq_users_clerk_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    clerk_user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role", native_enum=False),
        nullable=False,
        default=UserRole.USER,
    )
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status", native_enum=False),
        nullable=False,
        default=UserStatus.ACTIVE,
    )

    signup_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    invite_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("invites.id"), nullable=True
    )

    invite = relationship(
        "Invite",
        foreign_keys=[invite_id],
        back_populates="redeemed_by",
        viewonly=True,
    )

    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # HFI (Hedge Fund Intelligence) relationships
    investors = relationship("Investor", back_populates="user", cascade="all, delete-orphan")
    hfi_reports = relationship("HfiReport", back_populates="user", cascade="all, delete-orphan")
    hfi_alerts = relationship("HfiAlert", back_populates="user", cascade="all, delete-orphan")
