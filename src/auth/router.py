"""Authentication endpoints - current user profile (invite redeem kept for compat)."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from src.analytics.service import analytics
from src.auth.dependencies import get_current_authenticated_user
from src.auth.service import InviteError, redeem_invite
from src.database import get_db
from src.models.user import User
from src.schemas import RedeemInviteRequest, RedeemInviteResponse, UserProfileResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["Auth"])


@router.get("/me", response_model=UserProfileResponse)
async def read_current_user(user: User = Depends(get_current_authenticated_user)) -> User:
    """Return the authenticated user's profile, role and account status."""
    return user


@router.post("/redeem-invite", response_model=RedeemInviteResponse)
async def redeem_invite_endpoint(
    body: RedeemInviteRequest,
    request: Request,
    user: User = Depends(get_current_authenticated_user),
    db: AsyncSession = Depends(get_db),
) -> RedeemInviteResponse:
    """Legacy no-op kept for backward-compat; open signup needs no code."""
    try:
        updated = await redeem_invite(db, user, body.code)
    except InviteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    analytics.record_event(
        "invite_redeemed",
        user_id=updated.id,
        payload={"invite_code_prefix": body.code[:4]},
        commit=True,
    )

    return RedeemInviteResponse(
        ok=True,
        user=UserProfileResponse.model_validate(updated),
    )
