from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from backend.database import get_db
from backend.models import User, Product, UserProduct, NotificationLog
from backend.auth import get_current_admin, hash_password, verify_password, SECRET_KEY, ALGORITHM


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class RequestEmailChangeRequest(BaseModel):
    new_email: str
    current_password: str

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/stats")
async def get_stats(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    total_users = (await db.execute(select(func.count()).select_from(User).where(User.is_admin == False))).scalar()
    total_admins = (await db.execute(select(func.count()).select_from(User).where(User.is_admin == True))).scalar()
    total_products = (await db.execute(select(func.count()).select_from(Product))).scalar()
    today = datetime.utcnow() - timedelta(hours=24)
    notifs_today = (
        await db.execute(
            select(func.count()).select_from(NotificationLog).where(NotificationLog.sent_at >= today)
        )
    ).scalar()
    return {
        "total_users": total_users,
        "total_admins": total_admins,
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
            "raw_text": p.raw_text[:200] if p.raw_text else "",
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


@router.delete("/products/{product_id}")
async def delete_product(
    product_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    await db.execute(delete(Product).where(Product.id == product_id))
    await db.commit()
    return {"deleted": product_id}


@router.delete("/products-orphans")
async def delete_orphan_products(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Delete all products with 0 watchers."""
    result = await db.execute(select(Product))
    products = result.scalars().all()
    deleted = []
    for p in products:
        count = (await db.execute(
            select(func.count()).select_from(UserProduct).where(UserProduct.product_id == p.id)
        )).scalar()
        if count == 0:
            await db.execute(delete(Product).where(Product.id == p.id))
            deleted.append(p.asin)
    await db.commit()
    return {"deleted": deleted, "count": len(deleted)}


# ── Admin profile management ──────────────────────────────────────────────────

@router.patch("/profile/password")
async def change_password(
    body: ChangePasswordRequest,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status_code=400, detail="הסיסמה הנוכחית שגויה")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="הסיסמה החדשה קצרה מדי (מינימום 6 תווים)")
    admin.password_hash = hash_password(body.new_password)
    await db.commit()
    return {"message": "הסיסמה עודכנה בהצלחה"}


@router.post("/profile/request-email-change")
async def request_email_change(
    body: RequestEmailChangeRequest,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from jose import jwt as jose_jwt
    from datetime import datetime, timedelta
    import os

    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(status_code=400, detail="הסיסמה שגויה")

    # Check new email not already taken
    existing = (await db.execute(select(User).where(User.email == body.new_email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="אימייל זה כבר בשימוש")

    # Create verification token (valid 1 hour)
    expire = datetime.utcnow() + timedelta(hours=1)
    token = jose_jwt.encode(
        {"sub": str(admin.id), "new_email": body.new_email, "type": "email_change", "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM
    )

    base_url = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "app.amzfreeil.com")
    verify_url = f"https://{base_url}/admin/verify-email?token={token}"

    from backend.notifier import send_simple_email
    html = f"""
    <div dir="rtl" style="font-family:Arial,sans-serif; max-width:480px; margin:auto; padding:24px;">
      <h2 style="color:#e47911;">אימות שינוי אימייל · Amazon Israel Alert</h2>
      <p>קיבלנו בקשה לשנות את כתובת האימייל של חשבון המנהל שלך ל:</p>
      <p style="font-size:1.1rem; font-weight:bold; direction:ltr;">{body.new_email}</p>
      <p>לאישור השינוי לחץ על הכפתור:</p>
      <a href="{verify_url}" style="display:inline-block; background:#FF9900; color:#111;
         padding:12px 28px; border-radius:8px; font-weight:bold; text-decoration:none; margin:16px 0;">
        אשר שינוי אימייל
      </a>
      <p style="color:#888; font-size:0.85rem;">הקישור תקף לשעה אחת. אם לא ביקשת שינוי זה, התעלם.</p>
    </div>
    """
    send_simple_email(body.new_email, "אימות שינוי אימייל · Amazon Israel Alert", html)
    return {"message": f"קישור אימות נשלח ל-{body.new_email}"}


@router.get("/verify-email")
async def verify_email_change(
    token: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from jose import jwt as jose_jwt, JWTError
    try:
        payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "email_change":
            raise HTTPException(status_code=400, detail="טוקן לא תקין")
        user_id = int(payload["sub"])
        new_email = payload["new_email"]
    except JWTError:
        raise HTTPException(status_code=400, detail="הקישור פג תוקף או לא תקין")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")

    user.email = new_email
    user.notify_email = new_email
    await db.commit()
    from fastapi.responses import HTMLResponse
    return HTMLResponse("""
    <html dir="rtl"><body style="font-family:Arial; text-align:center; padding:60px; background:#fffaf1;">
    <h2 style="color:#2e7d32;">✅ האימייל עודכן בהצלחה!</h2>
    <p>כתובת האימייל שלך עודכנה. <a href="/admin/login">לחץ כאן לכניסה מחדש</a></p>
    </body></html>
    """)
