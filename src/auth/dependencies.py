"""FastAPI dependencies - current user extraction and admin gates."""

import logging
import time

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.service import analytics, current_user_id
from src.auth.clerk import get_clerk_user_profile, verify_session_token
from src.auth.service import (
    get_user_by_clerk_id,
    get_user_by_email,
    link_identity,
    provision_user,
    touch_last_seen,
)
from src.config import get_settings
from src.database import get_db
from src.models.user import User, UserRole, UserStatus

logger = logging.getLogger(__name__)

# In-process throttle so last_seen_at isn't rewritten on every single request.
_LAST_TOUCH: dict[str, float] = {}
_TOUCH_INTERVAL_SECONDS = 60.0

# Throttle for Clerk Backend API profile/role re-sync (per clerk_user_id).
_LAST_ROLE_SYNC: dict[str, float] = {}
_ROLE_SYNC_INTERVAL_SECONDS = 60.0

# Dedupe account-linking work when parallel requests race (same app user +
# new clerk identity seen by N simultaneous requests).
_LINKED_SEEN: set[tuple[str, str]] = set()

# One-shot diagnostic: session token template missing the role claim.
_warned_no_role_claim = False


class InviteRequiredError(HTTPException):
    """Legacy 403 kept for backward-compat (no longer raised).

    The invite-only beta is retired; open signup is enabled. Retained so any
    stale imports keep working.
    """

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "invite_required",
                "message": "This is an invite-only beta. Redeem an invite code to continue.",
            },
        )


def _extract_claim(claims: dict, *names: str) -> str | None:
    """Read first present claim (supports customised Clerk session templates)."""
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def get_current_authenticated_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authenticate via Clerk session token and JIT-provision the app user row.

    Does not enforce active status - allows pending users to check their profile
    and redeem invites.
    - 401 when no valid session token is presented.
    - 403 ``account_deactivated`` for disabled accounts.

    Also stores ``request.state.user_id`` for downstream analytics middleware.
    """
    settings = get_settings()
    global _warned_no_role_claim
    claims = verify_session_token(request)
    clerk_user_id = _extract_claim(claims, "sub")
    if not clerk_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "Token missing subject"},
        )

    user = await get_user_by_clerk_id(db, clerk_user_id)

    if user is None:
        # Identity not seen before - but it may be an existing person signing
        # in with a new method (e.g. pre-provisioned password creds + Google).
        # Clerk guarantees verified emails are unique per instance, so matching
        # on the verified email is safe. Default session tokens carry no email
        # claim, so fetch the profile from the Clerk Backend API when needed.
        email = _extract_claim(claims, "email")
        profile = None
        if not email:
            profile = await get_clerk_user_profile(clerk_user_id)
            email = (profile or {}).get("email")
        if email:
            user = await get_user_by_email(db, email)
            if user is not None and (str(user.id), clerk_user_id) not in _LINKED_SEEN:
                _LINKED_SEEN.add((str(user.id), clerk_user_id))
                await link_identity(db, user, clerk_user_id=clerk_user_id)
                analytics.record_event(
                    "account_linked",
                    user_id=user.id,
                    payload={"email": user.email, "new_clerk_id": clerk_user_id},
                )

    if user is None:
        if profile is None:
            profile = await get_clerk_user_profile(clerk_user_id)
            if profile is None:
                # Transient Clerk API failure - the token signature already
                # proves this user exists in our instance, so retry-able 503
                # beats persisting a placeholder row.
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "code": "clerk_unavailable",
                        "message": "Could not load your profile - please retry.",
                    },
                )
        email = email or profile.get("email") or f"{clerk_user_id}@unknown.invalid"
        full_name = (
            _extract_claim(claims, "full_name", "name")
            or " ".join(
                filter(
                    None,
                    [_extract_claim(claims, "first_name"), _extract_claim(claims, "last_name")],
                )
            )
            or profile.get("full_name")
        )
        user = await provision_user(
            db,
            clerk_user_id=clerk_user_id,
            email=email,
            full_name=full_name,
            image_url=_extract_claim(claims, "image_url", "image", "picture")
            or profile.get("image_url"),
            signup_method=_extract_claim(claims, "sign_up_method", "signup_method"),
        )
        analytics.record_event(
            "user_signed_up",
            user_id=user.id,
            payload={"email": user.email, "signup_method": user.signup_method},
        )
        analytics.record_new_user(user.id)

    # ── Role sync from Clerk ────────────────────────────────────────────
    # Admins are promoted manually via Clerk public metadata
    # ({"role": "admin"}) or the ADMIN_CLERK_USER_IDS env var. Source of truth, in order:
    # 1. Configured ADMIN_CLERK_USER_IDS env var
    # 2. Customised session token claim (if configured)
    # 3. Clerk Backend API profile - re-synced at most once a minute per user
    is_configured_admin = clerk_user_id in settings.admin_clerk_user_id_set
    claimed_role = _extract_claim(claims, "role")
    if not claimed_role and isinstance(claims.get("public_metadata"), dict):
        meta_role = claims["public_metadata"].get("role")
        if isinstance(meta_role, str):
            claimed_role = meta_role.strip()

    now = time.monotonic()
    if (
        not claimed_role
        and not is_configured_admin
        and now - _LAST_ROLE_SYNC.get(clerk_user_id, 0.0) > _ROLE_SYNC_INTERVAL_SECONDS
    ):
        _LAST_ROLE_SYNC[clerk_user_id] = now
        profile = await get_clerk_user_profile(clerk_user_id)
        meta_role = (profile or {}).get("public_metadata", {}).get("role")
        if isinstance(meta_role, str) and meta_role.strip():
            claimed_role = meta_role.strip()

    if not claimed_role and not is_configured_admin and not _warned_no_role_claim:
        _warned_no_role_claim = True
        logger.info(
            "Session token carries no 'role' claim; syncing admin role via "
            "Clerk Backend API public metadata instead (Configure → Sessions → "
            'Customize session token → {"role": "{{user.public_metadata.role}}"} '
            "avoids the per-minute API lookup). Present claims: %s",
            sorted(claims.keys()),
        )

    if is_configured_admin or claimed_role == "admin":
        user.role = UserRole.ADMIN
        user.status = UserStatus.ACTIVE
    elif claimed_role == "user":
        user.role = UserRole.USER

    # Open signup: invite enforcement is disabled. Lazily activate any legacy
    # ``pending_invite`` rows so existing users are not locked out. Also covers
    # the /api/auth/me path, which uses this dependency directly.
    _raw_status = user.status.value if hasattr(user.status, "value") else str(user.status)
    if _raw_status in (UserStatus.PENDING_INVITE.value, "pending_invite"):
        user.status = UserStatus.ACTIVE
        # Don't commit here - get_db commits at request end; in the /me path
        # there may be no active transaction yet and commit would be a no-op
        # flush risk. get_current_user handles the commit for gated routes.
        await db.flush()

    # Expose identity to analytics middleware + route handlers
    request.state.user_id = str(user.id)
    request.state.user_role = user.role.value if hasattr(user.role, "value") else str(user.role)
    status_str = user.status.value if hasattr(user.status, "value") else str(user.status)
    request.state.user_status = status_str
    request.state.user_email = user.email

    # Attribute deep service-layer calls (LLM etc.) to this user
    current_user_id.set(str(user.id))

    # Deactivated check
    status_value = user.status.value if hasattr(user.status, "value") else str(user.status)
    if status_value == UserStatus.DEACTIVATED.value or status_value == "deactivated":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "account_deactivated", "message": "This account has been deactivated."},
        )

    # Throttled activity stamp (own short session; never blocks the response path)
    now = time.monotonic()
    if now - _LAST_TOUCH.get(clerk_user_id, 0.0) > _TOUCH_INTERVAL_SECONDS:
        _LAST_TOUCH[clerk_user_id] = now
        await touch_last_seen(user)

    return user


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_authenticated_user),
) -> User:
    """Gate requiring an authenticated, non-deactivated account.

    Open signup: invite enforcement is disabled. Any legacy
    ``pending_invite`` rows are lazily activated on first request so existing
    users are not locked out.
    """
    status_value = user.status.value if hasattr(user.status, "value") else str(user.status)

    if status_value in (UserStatus.PENDING_INVITE.value, "pending_invite"):
        user.status = UserStatus.ACTIVE
        await db.commit()

    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Gate for admin-only operations (pipelines, invites, platform metrics)."""
    role_value = user.role.value if hasattr(user.role, "value") else user.role
    if role_value != UserRole.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "admin_only", "message": "Admin privileges required."},
        )
    return user


# Re-export for convenience in routers
get_db_dep = get_db
