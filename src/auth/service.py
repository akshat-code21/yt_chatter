"""Auth service - JIT user provisioning and invite redemption logic."""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import get_settings
from src.models.user import Invite, User, UserRole, UserStatus

logger = logging.getLogger(__name__)


class InviteError(Exception):
    """Invite code cannot be redeemed; ``code`` maps to an HTTP status."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def get_user_by_clerk_id(db: AsyncSession, clerk_user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.clerk_user_id == clerk_user_id))
    return result.scalar_one_or_none()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(func.lower(User.email) == email.lower()))
    return result.scalar_one_or_none()


async def link_identity(db: AsyncSession, user: User, *, clerk_user_id: str) -> None:
    """Bind a new Clerk identity onto an existing app user row.

    Covers the case where someone was pre-provisioned with password
    credentials but signs in with Google using the same verified email.
    """
    if user.clerk_user_id == clerk_user_id:
        return
    logger.info("Linking Clerk identity %s -> existing user %s", clerk_user_id, user.email)
    user.clerk_user_id = clerk_user_id
    await db.flush()


async def provision_user(
    db: AsyncSession,
    *,
    clerk_user_id: str,
    email: str,
    full_name: str | None = None,
    image_url: str | None = None,
    signup_method: str | None = None,
) -> User:
    """Create the app-side user row on first authenticated request (JIT).

    Open signup: everyone gets immediate active access. Admin promotions
    happen out-of-band via Clerk public metadata (synced on login) or the
    ``ADMIN_CLERK_USER_IDS`` env var.
    """
    settings = get_settings()
    is_configured_admin = clerk_user_id in settings.admin_clerk_user_id_set

    user = User(
        clerk_user_id=clerk_user_id,
        email=email.lower(),
        full_name=full_name,
        image_url=image_url,
        role=UserRole.ADMIN if is_configured_admin else UserRole.USER,
        status=UserStatus.ACTIVE,
        signup_method=signup_method,
        last_seen_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    logger.info("Provisioned new user %s (%s)", user.email, user.id)
    return user


async def touch_last_seen(user: User) -> None:
    """Best-effort activity timestamp update (called on its own session)."""
    from src.database import async_session_factory

    try:
        async with async_session_factory() as session:
            await session.execute(
                update(User).where(User.id == user.id).values(last_seen_at=datetime.now(UTC))
            )
            await session.commit()
    except Exception:
        logger.debug("Failed to update last_seen_at", exc_info=True)


async def redeem_invite(db: AsyncSession, user: User, code: str) -> User:
    """Validate an invite code and activate the pending user.

    Raises :class:`InviteError` with a user-safe message when invalid.
    """
    now = datetime.now(UTC)

    result = await db.execute(select(Invite).where(Invite.code == code.strip()))
    invite = result.scalar_one_or_none()

    if not invite:
        raise InviteError("Invalid invite code.", 404)
    if invite.revoked_at is not None:
        raise InviteError("This invite has been revoked.", 410)
    if invite.expires_at is not None and invite.expires_at < now:
        raise InviteError("This invite has expired.", 410)
    if invite.uses_count >= invite.max_uses:
        raise InviteError("This invite has already been fully used.", 410)
    if invite.invited_email and user.email.lower() != invite.invited_email.lower():
        raise InviteError("This invite was issued to a different email address.", 403)

    # Invites never grant roles - admins are promoted manually via Clerk
    # public metadata (synced at login) or ADMIN_CLERK_USER_IDS.
    user.status = UserStatus.ACTIVE
    user.invite_id = invite.id
    invite.uses_count += 1

    await db.flush()
    logger.info("User %s redeemed invite %s", user.email, invite.code)
    return user


async def create_invite(
    db: AsyncSession,
    *,
    created_by_user_id: uuid.UUID | None,
    invited_email: str | None = None,
    max_uses: int = 1,
    expires_in_days: int | None = None,
) -> Invite:
    """Generate a new invite code. Invites never carry roles."""
    import secrets
    from datetime import timedelta

    expires_at = None
    if expires_in_days is not None and expires_in_days > 0:
        expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

    invite = Invite(
        code=secrets.token_urlsafe(12),
        invited_email=invited_email.lower() if invited_email else None,
        max_uses=max(1, max_uses),
        expires_at=expires_at,
        created_by_user_id=created_by_user_id,
    )
    db.add(invite)
    await db.flush()
    return invite
