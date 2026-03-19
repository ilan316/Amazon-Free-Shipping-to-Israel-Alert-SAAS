from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from sqlalchemy import select

from backend.models import User, Product, NotificationLog, SystemSetting
from backend.auth import get_current_user, verify_password, hash_password
from backend.schemas import UserResponse, UpdateSettingsRequest, ChangePasswordRequest, DeleteAccountRequest, MessageResponse

router = APIRouter(prefix="/me", tags=["settings"])


@router.get("", response_model=UserResponse)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    effective_limit = current_user.max_products
    if effective_limit is None:
        row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "max_products_per_user"))).scalar_one_or_none()
        effective_limit = int(row.value) if row else 20
    base = UserResponse.model_validate(current_user)
    data = base.model_dump()
    data["effective_product_limit"] = effective_limit
    return data


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


@router.patch("/password", response_model=MessageResponse)
async def change_password(
    body: ChangePasswordRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(body.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="סיסמה נוכחית שגויה")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="סיסמה חדשה חייבת להכיל לפחות 6 תווים")
    current_user.password_hash = hash_password(body.new_password)
    await db.commit()
    return MessageResponse(message="הסיסמה שונתה בהצלחה")


@router.get("/notifications")
async def get_notifications(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    limit: int = 20,
):
    result = await db.execute(
        select(NotificationLog, Product.name, Product.asin)
        .join(Product, NotificationLog.product_id == Product.id)
        .where(NotificationLog.user_id == current_user.id, NotificationLog.success == True)
        .order_by(NotificationLog.sent_at.desc())
        .limit(limit)
    )
    rows = result.all()
    return [
        {
            "sent_at": log.sent_at.isoformat(),
            "product_name": name or asin,
            "asin": asin,
            "status": log.status,
        }
        for log, name, asin in rows
    ]


@router.patch("/vacation", response_model=UserResponse)
async def set_vacation_mode(
    body: dict,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    current_user.vacation_mode = bool(body.get("vacation_mode", False))
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
