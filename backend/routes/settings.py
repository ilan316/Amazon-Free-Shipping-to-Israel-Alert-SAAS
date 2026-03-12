from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import User
from backend.auth import get_current_user, verify_password
from backend.schemas import UserResponse, UpdateSettingsRequest, DeleteAccountRequest, MessageResponse

router = APIRouter(prefix="/me", tags=["settings"])


@router.get("", response_model=UserResponse)
async def get_me(current_user: Annotated[User, Depends(get_current_user)]):
    return current_user


@router.patch("/settings", response_model=UserResponse)
async def update_settings(
    body: UpdateSettingsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if body.notify_email is not None:
        current_user.notify_email = body.notify_email
    if body.language is not None:
        if body.language not in ("he", "en"):
            raise HTTPException(status_code=400, detail="Language must be 'he' or 'en'")
        current_user.language = body.language
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.delete("", response_model=MessageResponse)
async def delete_account(
    body: DeleteAccountRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect password")
    await db.delete(current_user)
    await db.commit()
    return MessageResponse(message="Account deleted")
