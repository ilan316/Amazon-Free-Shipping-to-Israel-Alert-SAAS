from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from backend.database import get_db
from backend.models import User, Product, UserProduct, NotificationLog
from backend.auth import get_current_admin

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
async def get_stats(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    total_users = (await db.execute(select(func.count()).select_from(User))).scalar()
    total_products = (await db.execute(select(func.count()).select_from(Product))).scalar()
    today = datetime.utcnow() - timedelta(hours=24)
    notifs_today = (
        await db.execute(
            select(func.count()).select_from(NotificationLog).where(NotificationLog.sent_at >= today)
        )
    ).scalar()
    return {
        "total_users": total_users,
        "total_products": total_products,
        "notifications_24h": notifs_today,
    }


@router.get("/users")
async def list_users(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    out = []
    for u in users:
        count = (
            await db.execute(
                select(func.count()).select_from(UserProduct).where(UserProduct.user_id == u.id)
            )
        ).scalar()
        out.append({
            "id": u.id,
            "email": u.email,
            "notify_email": u.notify_email,
            "is_active": u.is_active,
            "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "product_count": count,
        })
    return out


@router.patch("/users/{user_id}/toggle-active")
async def toggle_active(
    user_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = not user.is_active
    await db.commit()
    return {"id": user.id, "is_active": user.is_active}


@router.patch("/users/{user_id}/toggle-admin")
async def toggle_admin(
    user_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_admin = not user.is_admin
    await db.commit()
    return {"id": user.id, "is_admin": user.is_admin}


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
    return {"deleted": user_id}


@router.get("/products")
async def list_products(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Product).order_by(Product.last_checked.desc().nullslast()))
    products = result.scalars().all()

    out = []
    for p in products:
        watchers = (
            await db.execute(
                select(func.count()).select_from(UserProduct).where(UserProduct.product_id == p.id)
            )
        ).scalar()
        out.append({
            "id": p.id,
            "asin": p.asin,
            "name": p.name,
            "url": p.url,
            "last_status": p.last_status,
            "last_checked": p.last_checked.isoformat() if p.last_checked else None,
            "consecutive_errors": p.consecutive_errors,
            "watchers": watchers,
        })
    return out


@router.post("/trigger-check")
async def trigger_check(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from backend.scheduler import run_global_check_cycle
    import asyncio
    asyncio.create_task(run_global_check_cycle())
    return {"message": "Check cycle triggered"}
