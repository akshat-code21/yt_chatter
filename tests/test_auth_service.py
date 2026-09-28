"""Tests for JIT user provisioning and invite redemption rules."""

import pytest
from sqlalchemy import select as _select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.service import (
    InviteError,
    get_user_by_email,
    link_identity,
    provision_user,
    redeem_invite,
)
from src.models.user import Invite, User, UserRole, UserStatus


@pytest.fixture(autouse=True)
def _dev_mode(monkeypatch):
    """Provisioning auto-activates users only in development; force it on."""
    from src.config import get_settings

    monkeypatch.setattr(type(get_settings()), "is_development", property(lambda self: False))


async def _make_user(
    db: AsyncSession,
    clerk_user_id: str = "user_1",
    email: str = "u1@example.com",
) -> User:
    user = await provision_user(db, clerk_user_id=clerk_user_id, email=email)
    await db.commit()
    return user


async def _make_invite(db: AsyncSession, **kwargs) -> Invite:
    defaults = {"code": "CODE123", "invited_email": None, "max_uses": 1}
    defaults.update(kwargs)
    invite = Invite(**defaults)
    db.add(invite)
    await db.commit()
    return invite


class TestRoleSyncFromClerk:
    async def test_provisioned_user_promoted_via_env_ids(self, session_factory, monkeypatch):
        """ADMIN_CLERK_USER_IDS env var still works for bootstrap admins."""
        from src.config import get_settings

        monkeypatch.setattr(
            type(get_settings()),
            "admin_clerk_user_id_set",
            property(lambda self: {"clerk_admin_1"}),
        )
        async with session_factory() as db:
            user = await provision_user(
                db, clerk_user_id="clerk_admin_1", email="admin@example.com"
            )
            assert user.role == UserRole.ADMIN
            assert user.status == UserStatus.ACTIVE


class TestAccountLinking:
    async def test_get_user_by_email_case_insensitive(self, session_factory):
        async with session_factory() as db:
            await _make_user(db, email="Pilot@Example.com")
            found = await get_user_by_email(db, "pilot@example.com")
            assert found is not None

    async def test_link_identity_rebinds_clerk_id(self, session_factory):
        async with session_factory() as db:
            user = await _make_user(db, clerk_user_id="old_id")
            await link_identity(db, user, clerk_user_id="google_new_id")
            await db.commit()

            refetched = (
                await db.execute(_select(User).where(User.email == "u1@example.com"))
            ).scalar_one()
            assert refetched.clerk_user_id == "google_new_id"

    async def test_link_identity_same_id_is_noop(self, session_factory):
        async with session_factory() as db:
            user = await _make_user(db, clerk_user_id="same_id")
            await link_identity(db, user, clerk_user_id="same_id")
            assert user.clerk_user_id == "same_id"


class TestProvisionUser:
    async def test_first_user_is_regular_and_active(self, session_factory):
        """Open signup: everyone starts as an active regular user."""
        async with session_factory() as db:
            user = await _make_user(db)
            assert user.role == UserRole.USER
            assert user.status == UserStatus.ACTIVE

    async def test_second_user_is_active_regular(self, session_factory):
        async with session_factory() as db:
            await _make_user(db, clerk_user_id="user_1")
            second = await _make_user(db, clerk_user_id="user_2", email="u2@example.com")
            assert second.role == UserRole.USER
            assert second.status == UserStatus.ACTIVE


class TestRedeemInvite:
    async def test_valid_code_activates_user(self, session_factory):
        async with session_factory() as db:
            user = await _make_user(db)
            invite = await _make_invite(db)

            result = await redeem_invite(db, user, " CODE123 ")  # whitespace tolerated
            await db.commit()

            assert result.status == UserStatus.ACTIVE
            assert result.invite_id == invite.id

            refreshed = (
                await db.execute(_select(Invite).where(Invite.code == "CODE123"))
            ).scalar_one()
            assert refreshed.uses_count == 1

    async def test_email_bound_invite_rejects_other_email(self, session_factory):
        async with session_factory() as db:
            user = await _make_user(db, email="someone@example.com")
            await _make_invite(db, invited_email="other@example.com")

            with pytest.raises(InviteError) as exc:
                await redeem_invite(db, user, "CODE123")
            assert exc.value.status_code == 403

    async def test_unknown_code_404(self, session_factory):
        async with session_factory() as db:
            user = await _make_user(db)
            with pytest.raises(InviteError) as exc:
                await redeem_invite(db, user, "NOPE")
            assert exc.value.status_code == 404

    async def test_exhausted_invite_rejected(self, session_factory):
        async with session_factory() as db:
            await _make_user(db)
            await _make_invite(db, max_uses=2)
            other = await _make_user(db, clerk_user_id="user_2", email="u2@example.com")
            await redeem_invite(db, other, "CODE123")  # use 1 of 2
            # Exhaust remaining uses via a third user
            third = await _make_user(db, clerk_user_id="user_3", email="u3@example.com")
            await redeem_invite(db, third, "CODE123")  # use 2 of 2

            fourth = await _make_user(db, clerk_user_id="user_4", email="u4@example.com")
            with pytest.raises(InviteError) as exc:
                await redeem_invite(db, fourth, "CODE123")
            assert exc.value.status_code == 410

    async def test_revoked_invite_rejected(self, session_factory):
        from datetime import UTC, datetime

        async with session_factory() as db:
            user = await _make_user(db)
            invite = await _make_invite(db, revoked_at=datetime.now(UTC))
            with pytest.raises(InviteError) as exc:
                await redeem_invite(db, user, invite.code)
            assert exc.value.status_code == 410

    async def test_invite_never_grants_admin_role(self, session_factory):
        async with session_factory() as db:
            user = await _make_user(db)
            await _make_invite(db)

            result = await redeem_invite(db, user, "CODE123")
            await db.commit()

            assert result.status == UserStatus.ACTIVE
            assert result.role == UserRole.USER  # invites never promote
