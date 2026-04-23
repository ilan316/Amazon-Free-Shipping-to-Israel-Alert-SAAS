from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, delete

from backend.database import get_db
from sqlalchemy import cast, Date
from backend.models import User, Product, UserProduct, NotificationLog, SystemSetting, EmailClick, EmailTemplate, EmailOpen, EmailSendLog, EmailSendRecipient
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
    total_users = (await db.execute(select(func.count()).select_from(User).where(User.is_admin == False, User.is_verified == True))).scalar()
    total_admins = (await db.execute(select(func.count()).select_from(User).where(User.is_admin == True))).scalar()
    total_products = (await db.execute(select(func.count()).select_from(Product))).scalar()
    today = datetime.utcnow() - timedelta(hours=24)
    notifs_today = (
        await db.execute(
            select(func.count()).select_from(NotificationLog).where(NotificationLog.sent_at >= today)
        )
    ).scalar()
    unverified = (await db.execute(select(func.count()).select_from(User).where(User.is_admin == False, User.is_verified == False))).scalar()
    return {
        "total_users": total_users,
        "total_admins": total_admins,
        "total_products": total_products,
        "notifications_24h": notifs_today,
        "unverified_users": unverified,
    }


@router.get("/users")
async def list_users(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.is_verified == True).order_by(User.created_at.desc()))
    users = result.scalars().all()

    # Batch-fetch product counts (one query instead of N)
    count_rows = await db.execute(
        select(UserProduct.user_id, func.count().label("cnt"))
        .group_by(UserProduct.user_id)
    )
    product_count_map = {row.user_id: row.cnt for row in count_rows}

    return [
        {
            "id": u.id,
            "email": u.email,
            "notify_email": u.notify_email,
            "is_active": u.is_active,
            "is_admin": u.is_admin,
            "is_verified": u.is_verified,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "product_count": product_count_map.get(u.id, 0),
            "max_products": u.max_products,
            "vacation_mode": u.vacation_mode,
        }
        for u in users
    ]


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

    # Batch-fetch watcher counts (one query instead of N)
    watcher_rows = await db.execute(
        select(UserProduct.product_id, func.count().label("cnt"))
        .group_by(UserProduct.product_id)
    )
    watcher_map = {row.product_id: row.cnt for row in watcher_rows}

    return [
        {
            "id": p.id,
            "asin": p.asin,
            "name": p.name,
            "url": p.url,
            "last_status": p.last_status,
            "last_checked": p.last_checked.isoformat() if p.last_checked else None,
            "consecutive_errors": p.consecutive_errors,
            "watchers": watcher_map.get(p.id, 0),
            "raw_text": p.raw_text[:200] if p.raw_text else "",
        }
        for p in products
    ]


@router.get("/registrations-chart")
async def registrations_chart(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(cast(User.created_at, Date).label("date"), func.count().label("count"))
        .where(User.is_admin == False)
        .group_by(cast(User.created_at, Date))
        .order_by(cast(User.created_at, Date).asc())
        .limit(30)
    )
    return [{"date": str(row.date), "count": row.count} for row in result.all()]


@router.get("/notifications-log")
async def notifications_log(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = 50,
):
    result = await db.execute(
        select(NotificationLog, User.email, Product.name, Product.asin)
        .join(User, NotificationLog.user_id == User.id)
        .join(Product, NotificationLog.product_id == Product.id)
        .order_by(NotificationLog.sent_at.desc())
        .limit(limit)
    )
    return [
        {
            "sent_at": log.sent_at.isoformat(),
            "user_email": email,
            "email_to": log.email_to,
            "product_name": name or asin,
            "asin": asin,
            "status": log.status,
            "success": log.success,
            "error_msg": log.error_msg,
        }
        for log, email, name, asin in result.all()
    ]


@router.get("/system-message")
async def get_system_message(db: Annotated[AsyncSession, Depends(get_db)]):
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "system_message"))).scalar_one_or_none()
    return {"message": row.value if row else ""}


@router.post("/system-message")
async def set_system_message(
    body: dict,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    msg = str(body.get("message", "")).strip()
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "system_message"))).scalar_one_or_none()
    if row:
        row.value = msg
    else:
        db.add(SystemSetting(key="system_message", value=msg))
    await db.commit()
    return {"message": msg}


@router.get("/global-product-limit")
async def get_global_product_limit(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "max_products_per_user"))).scalar_one_or_none()
    return {"limit": int(row.value) if row else 10}


@router.post("/global-product-limit")
async def set_global_product_limit(
    body: dict,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    limit = int(body.get("limit", 20))
    if limit < 1 or limit > 10000:
        raise HTTPException(status_code=400, detail="מגבלה לא חוקית (1–10000)")
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "max_products_per_user"))).scalar_one_or_none()
    if row:
        row.value = str(limit)
    else:
        db.add(SystemSetting(key="max_products_per_user", value=str(limit)))
    await db.commit()
    return {"limit": limit, "message": f"מגבלת מוצרים גלובלית עודכנה ל-{limit}"}


@router.patch("/users/{user_id}/product-limit")
async def set_user_product_limit(
    user_id: int,
    body: dict,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    raw = body.get("limit")
    if raw is None or raw == "":
        user.max_products = None  # revert to global
    else:
        val = int(raw)
        if val < 1 or val > 10000:
            raise HTTPException(status_code=400, detail="מגבלה לא חוקית")
        user.max_products = val
    await db.commit()
    return {"user_id": user_id, "max_products": user.max_products}


@router.get("/inactivity-days")
async def get_inactivity_days(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "inactivity_days"))).scalar_one_or_none()
    return {"days": int(row.value) if row else 90}


@router.post("/inactivity-days")
async def set_inactivity_days(
    body: dict,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    days = int(body.get("days", 90))
    if days < 0 or days > 3650:
        raise HTTPException(status_code=400, detail="ערך לא חוקי (0–3650)")
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "inactivity_days"))).scalar_one_or_none()
    if row:
        row.value = str(days)
    else:
        db.add(SystemSetting(key="inactivity_days", value=str(days)))
    await db.commit()
    msg = f"מעבר למצב חופשה אחרי {days} ימי חוסר פעילות" if days > 0 else "בדיקת חוסר פעילות מושבתת"
    return {"days": days, "message": msg}


@router.post("/trigger-summary")
async def trigger_summary(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Trigger daily summary immediately (for testing)."""
    from backend.scheduler import run_daily_summary
    import asyncio
    asyncio.create_task(run_daily_summary())
    return {"message": "Daily summary triggered"}


@router.get("/get-check-time")
async def get_check_time(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "check_time"))).scalar_one_or_none()
    return {"time": row.value if row else "06:00"}


@router.post("/set-check-time")
async def set_check_time(
    body: dict,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    time_str = body.get("time", "")
    try:
        h, m = time_str.split(":")
        h, m = int(h), int(m)
        assert 0 <= h <= 23 and 0 <= m <= 59
    except Exception:
        raise HTTPException(status_code=400, detail="פורמט שגוי. נדרש HH:MM")
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "check_time"))).scalar_one_or_none()
    if row:
        row.value = time_str
    else:
        db.add(SystemSetting(key="check_time", value=time_str))
    await db.commit()
    from backend.main import reschedule_check_job
    reschedule_check_job(h, m)
    return {"time": time_str, "message": f"בדיקה יומית עודכנה ל-{time_str}"}


@router.post("/trigger-check")
async def trigger_check(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from backend.scheduler import run_global_check_cycle
    import asyncio
    asyncio.create_task(run_global_check_cycle())
    return {"message": "Check cycle triggered"}


@router.post("/clear-cookies")
async def clear_cookies(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Clear session cookies from memory and DB."""
    from backend.checker import browser_manager
    browser_manager._session_cookies = []
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "amazon_session_cookies"))).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"message": "Cookies cleared"}


@router.get("/cookie-status")
async def cookie_status(
    admin: Annotated[User, Depends(get_current_admin)],
):
    """Return current session cookie state loaded in the running checker."""
    from backend.checker import browser_manager
    count = len(browser_manager._session_cookies)
    return {
        "loaded": count > 0,
        "count": count,
    }


@router.post("/inject-cookies")
async def inject_cookies(
    body: dict,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Inject Amazon session cookies into the running checker and trigger a check cycle.
    Accepts: {"cookies": [{"name": "...", "value": "..."}]} (JSON array from browser export)
    """
    import asyncio
    import json
    from backend.checker import browser_manager
    from backend.scheduler import run_global_check_cycle

    raw = body.get("cookies", [])
    if not raw:
        raise HTTPException(status_code=400, detail="cookies array required")

    # Support both [{name, value}] and {name: value} formats
    if isinstance(raw, list):
        cookie_list = [{"name": c["name"], "value": c["value"]} for c in raw if "name" in c and "value" in c]
    elif isinstance(raw, dict):
        cookie_list = [{"name": k, "value": v} for k, v in raw.items()]
    else:
        raise HTTPException(status_code=400, detail="Invalid cookies format")

    # Persist cookies to DB so they survive restarts
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "amazon_session_cookies"))).scalar_one_or_none()
    if row:
        row.value = json.dumps(cookie_list)
    else:
        db.add(SystemSetting(key="amazon_session_cookies", value=json.dumps(cookie_list)))
    await db.commit()

    browser_manager._session_cookies = cookie_list
    asyncio.create_task(run_global_check_cycle())
    return {"injected": len(cookie_list), "message": f"הוזרקו {len(cookie_list)} cookies — נשמרו ב-DB — בדיקה מתחילה"}


@router.post("/products/{product_id}/reset-errors")
async def reset_product_errors(
    product_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(Product).where(Product.id == product_id))
    product = result.scalar_one_or_none()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    product.consecutive_errors = 0
    await db.commit()
    return {"asin": product.asin, "consecutive_errors": 0}


class BulkDeleteRequest(BaseModel):
    product_ids: list[int]


@router.delete("/products/bulk")
async def bulk_delete_products(
    body: BulkDeleteRequest,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not body.product_ids:
        raise HTTPException(status_code=400, detail="לא נבחרו מוצרים")
    await db.execute(delete(Product).where(Product.id.in_(body.product_ids)))
    await db.commit()
    return {"deleted": len(body.product_ids)}


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


@router.get("/checks-status")
async def checks_status(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "system_paused"))).scalar_one_or_none()
    paused = row is not None and row.value == "true"
    return {"paused": paused}


@router.post("/pause-checks")
async def pause_checks(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from backend.main import scheduler
    for job_id in ("global_check", "daily_summary"):
        job = scheduler.get_job(job_id)
        if job:
            scheduler.pause_job(job_id)
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "system_paused"))).scalar_one_or_none()
    if row:
        row.value = "true"
    else:
        db.add(SystemSetting(key="system_paused", value="true"))
    await db.commit()
    return {"paused": True, "message": "הבדיקות הושהו"}


@router.post("/resume-checks")
async def resume_checks(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from backend.main import scheduler
    for job_id in ("global_check", "daily_summary"):
        job = scheduler.get_job(job_id)
        if job:
            scheduler.resume_job(job_id)
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "system_paused"))).scalar_one_or_none()
    if row:
        row.value = "false"
    else:
        db.add(SystemSetting(key="system_paused", value="false"))
    await db.commit()
    return {"paused": False, "message": "הבדיקות הופעלו מחדש"}


@router.post("/test-cookies")
async def test_cookies(
    body: dict,
    admin: Annotated[User, Depends(get_current_admin)],
):
    """Test if provided cookie string returns Israel location on Amazon.
    Accepts: {"cookies": "session-id=xxx; ubid-main=yyy; ..."}
    Returns: nav text and whether Israel was detected.
    """
    import httpx
    from bs4 import BeautifulSoup

    cookie_str = body.get("cookies", "").strip()
    if not cookie_str:
        raise HTTPException(status_code=400, detail="cookies field required")

    # Parse "name=value; name2=value2" format
    cookie_dict = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            cookie_dict[k.strip()] = v.strip()

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers, cookies=cookie_dict,
            follow_redirects=True, timeout=20.0,
        ) as client:
            resp = await client.get("https://www.amazon.com/dp/B00EDR1X3O?psc=1&th=1")
            soup = BeautifulSoup(resp.text, "html.parser")
            nav_text = ""
            for nav_id in ["glow-ingress-line2", "glow-ingress-line1", "nav-global-location-popover-link"]:
                el = soup.find(id=nav_id)
                if el:
                    nav_text = el.get_text(strip=True)
                    break
            israel_detected = "israel" in nav_text.lower() or "israel" in resp.text.lower()
            return {
                "nav_text": nav_text,
                "israel_detected": israel_detected,
                "cookies_parsed": len(cookie_dict),
                "status_code": resp.status_code,
            }
    except Exception as e:
        return {"error": str(e), "israel_detected": False}


@router.get("/clicks")
async def get_click_analytics(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: int = 7,
):
    since = datetime.utcnow() - timedelta(days=days)

    total = (
        await db.execute(
            select(func.count()).select_from(EmailClick).where(EmailClick.clicked_at >= since)
        )
    ).scalar()

    by_asin_rows = (
        await db.execute(
            select(EmailClick.asin, func.count().label("cnt"))
            .where(EmailClick.clicked_at >= since)
            .group_by(EmailClick.asin)
            .order_by(func.count().desc())
            .limit(20)
        )
    ).all()

    by_day_rows = (
        await db.execute(
            select(cast(EmailClick.clicked_at, Date).label("day"), func.count().label("cnt"))
            .where(EmailClick.clicked_at >= since)
            .group_by(cast(EmailClick.clicked_at, Date))
            .order_by(cast(EmailClick.clicked_at, Date))
        )
    ).all()

    recent_rows = (
        await db.execute(
            select(EmailClick, User.email)
            .outerjoin(User, EmailClick.user_id == User.id)
            .where(EmailClick.clicked_at >= since)
            .order_by(EmailClick.clicked_at.desc())
            .limit(50)
        )
    ).all()

    return {
        "total": total,
        "days": days,
        "by_asin": [{"asin": r.asin, "count": r.cnt} for r in by_asin_rows],
        "by_day": [{"date": str(r.day), "count": r.cnt} for r in by_day_rows],
        "recent": [
            {
                "id": r.EmailClick.id,
                "user_email": r.email or f"user#{r.EmailClick.user_id}",
                "asin": r.EmailClick.asin,
                "clicked_at": (r.EmailClick.clicked_at + timedelta(hours=3)).strftime("%d/%m/%Y %H:%M") if r.EmailClick.clicked_at else "",
                "ip": r.EmailClick.ip or "—",
            }
            for r in recent_rows
        ],
    }


@router.delete("/clicks/{click_id}")
async def delete_click(
    click_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    await db.execute(delete(EmailClick).where(EmailClick.id == click_id))
    await db.commit()
    return {"ok": True}


@router.post("/send-test-click-email")
async def send_test_click_email(
    admin: Annotated[User, Depends(get_current_admin)],
):
    import os
    from urllib.parse import urlencode
    from backend.notifier import _send_via_resend

    test_asin = "B0BG52SJ5N"
    base = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    affiliate_tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    dest = f"https://www.amazon.com/dp/{test_asin}?tag={affiliate_tag}" if affiliate_tag else f"https://www.amazon.com/dp/{test_asin}"
    params = urlencode({"u": admin.id, "a": test_asin, "url": dest})
    tracking = f"{base}/track/click?{params}"

    html = f"""
    <div dir="rtl" style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px;background:#fffaf1;border-radius:12px;">
      <h2 style="color:#e47911;">🧪 מייל בדיקה — Click Tracking</h2>
      <p style="color:#555;">לחץ על הכפתור ובדוק שמופיע click ב-<strong>/admin/clicks</strong></p>
      <a href="{tracking}"
         style="display:inline-block;background:#FF9900;color:#111;padding:12px 28px;border-radius:8px;font-weight:bold;text-decoration:none;margin-top:16px;">
        קנה עכשיו — משלוח חינם (בדיקה)
      </a>
      <p style="margin-top:16px;font-size:12px;color:#999;">ASIN: {test_asin} · user_id: {admin.id}</p>
    </div>"""

    ok = _send_via_resend(admin.notify_email, "🧪 בדיקת Click Tracking — amzfreeil", html, f"לחץ כאן: {tracking}")
    return {"sent": ok, "to": admin.notify_email, "tracking_url": tracking}


# ── Email Templates ───────────────────────────────────────────────────────────

class EmailTemplateBody(BaseModel):
    name: str
    subject: str
    body: str

class EmailTemplateSendBody(BaseModel):
    audience: str  # "all" | "active" | "vacation" | "inactive" | "single" | "custom"
    user_id: int | None = None
    products_min: int | None = None  # include users with >= this many products
    products_max: int | None = None  # include users with <= this many products
    custom_emails: list[str] | None = None  # for audience="custom"


@router.get("/email-templates")
async def list_email_templates(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(select(EmailTemplate).order_by(EmailTemplate.created_at.desc()))
    templates = result.scalars().all()
    return [
        {"id": t.id, "name": t.name, "subject": t.subject, "body": t.body,
         "created_at": t.created_at.isoformat() if t.created_at else None}
        for t in templates
    ]


@router.post("/email-templates")
async def create_email_template(
    body: EmailTemplateBody,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    existing = (await db.execute(select(EmailTemplate).where(EmailTemplate.name == body.name))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="שם תבנית כבר קיים")
    t = EmailTemplate(name=body.name.strip(), subject=body.subject.strip(), body=body.body)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": t.id, "message": "תבנית נשמרה"}


@router.put("/email-templates/{template_id}")
async def update_email_template(
    template_id: int,
    body: EmailTemplateBody,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    t = (await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="תבנית לא נמצאה")
    # Check name uniqueness if changed
    if body.name.strip() != t.name:
        dup = (await db.execute(select(EmailTemplate).where(EmailTemplate.name == body.name.strip()))).scalar_one_or_none()
        if dup:
            raise HTTPException(status_code=400, detail="שם תבנית כבר קיים")
    t.name = body.name.strip()
    t.subject = body.subject.strip()
    t.body = body.body
    await db.commit()
    return {"message": "תבנית עודכנה"}


@router.delete("/email-templates/{template_id}")
async def delete_email_template(
    template_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    t = (await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="תבנית לא נמצאה")
    await db.delete(t)
    await db.commit()
    return {"message": "תבנית נמחקה"}


# In-memory job tracker for send progress
_send_jobs: dict[str, dict] = {}


async def _execute_send_job(
    job_id: str,
    template_id: int,
    tpl_name: str,
    tpl_subject: str,
    tpl_body: str,
    audience: str,
    base_url: str,
    user_data: list,  # list of (user_id, email, notify_email, pc)
):
    import asyncio
    from backend.notifier import _send_via_resend, _pause_url
    from backend.database import AsyncSessionLocal

    job = _send_jobs[job_id]
    sent = failed = 0
    recipients_to_save = []

    for i, (uid, email, notify_email, pc) in enumerate(user_data):
        recipient = notify_email or email
        subj = tpl_subject.replace("{{email}}", email).replace("{{product_count}}", str(pc))
        pixel_url = f"{base_url}/track/email-open?uid={uid}&tid={template_id}"
        pixel = f'<img src="{pixel_url}" width="1" height="1" style="display:none;" alt="">'
        html_body = (
            tpl_body
            .replace("{{email}}", email)
            .replace("{{notify_email}}", recipient)
            .replace("{{product_count}}", str(pc))
            .replace("{{pause_url}}", _pause_url(uid))
        ) + pixel
        ok = _send_via_resend(recipient, subj, html_body, "")
        if ok:
            sent += 1
        else:
            failed += 1
        recipients_to_save.append((uid, recipient, ok))

        job["sent"] = sent
        job["failed"] = failed
        job["remaining"] = len(user_data) - (i + 1)

        await asyncio.sleep(0.55)

    # Persist to DB
    async with AsyncSessionLocal() as db:
        log = EmailSendLog(
            template_id=template_id,
            template_name=tpl_name,
            audience=audience,
            sent_count=sent,
            failed_count=failed,
        )
        db.add(log)
        await db.flush()
        for uid, email, ok in recipients_to_save:
            db.add(EmailSendRecipient(send_log_id=log.id, user_id=uid, email=email, success=ok))
        await db.commit()

    job["done"] = True
    job["message"] = f"נשלח ל-{sent} משתמשים" + (f", {failed} נכשלו" if failed else "")


@router.post("/email-templates/{template_id}/send")
async def send_email_template(
    template_id: int,
    body: EmailTemplateSendBody,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    import asyncio, uuid, os

    t = (await db.execute(select(EmailTemplate).where(EmailTemplate.id == template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="תבנית לא נמצאה")

    base_url = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")

    product_count_sub = (
        select(func.count(UserProduct.id))
        .where(UserProduct.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    q = select(User, product_count_sub.label("pc")).where(User.is_verified == True, User.is_admin == False)

    if body.audience == "self":
        q = select(User, product_count_sub.label("pc")).where(User.id == admin.id)
    elif body.audience == "active":
        q = q.where(User.is_active == True, User.vacation_mode == False)
    elif body.audience == "vacation":
        q = q.where(User.is_active == True, User.vacation_mode == True)
    elif body.audience == "inactive":
        q = q.where(User.is_active == False)
    elif body.audience == "single":
        if not body.user_id:
            raise HTTPException(status_code=400, detail="חסר user_id")
        q = select(User, product_count_sub.label("pc")).where(User.id == body.user_id)
    elif body.audience == "custom":
        if not body.custom_emails:
            raise HTTPException(status_code=400, detail="חסרה רשימת מיילים")
        emails_clean = [e.strip().lower() for e in body.custom_emails if e.strip()]
        q = select(User, product_count_sub.label("pc")).where(
            User.is_verified == True,
            func.lower(User.notify_email).in_(emails_clean) | func.lower(User.email).in_(emails_clean)
        )

    if body.products_min is not None:
        q = q.where(product_count_sub >= body.products_min)
    if body.products_max is not None:
        q = q.where(product_count_sub <= body.products_max)

    rows = (await db.execute(q)).all()
    if not rows:
        return {"job_id": None, "total": 0, "message": "לא נמצאו משתמשים התואמים את הסינון"}

    # Extract plain data before session closes
    user_data = [(r[0].id, r[0].email, r[0].notify_email or r[0].email, r[1]) for r in rows]

    job_id = str(uuid.uuid4())
    _send_jobs[job_id] = {
        "total": len(user_data), "sent": 0, "failed": 0,
        "remaining": len(user_data), "done": False, "message": "",
    }
    asyncio.create_task(_execute_send_job(
        job_id, template_id, t.name, t.subject, t.body, body.audience, base_url, user_data
    ))
    return {"job_id": job_id, "total": len(user_data)}


@router.get("/send-progress/{job_id}")
async def get_send_progress(
    job_id: str,
    admin: Annotated[User, Depends(get_current_admin)],
):
    job = _send_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/email-send-logs")
async def list_send_logs(
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(
        select(
            EmailSendLog,
            func.count(EmailOpen.id).label("opens"),
            func.count(func.distinct(EmailOpen.user_id)).label("unique_opens"),
        )
        .outerjoin(EmailOpen, EmailOpen.template_id == EmailSendLog.template_id)
        .group_by(EmailSendLog.id)
        .order_by(EmailSendLog.sent_at.desc())
        .limit(200)
    )).all()
    return [
        {
            "id": r.EmailSendLog.id,
            "template_id": r.EmailSendLog.template_id,
            "template_name": r.EmailSendLog.template_name,
            "sent_at": r.EmailSendLog.sent_at.isoformat(),
            "audience": r.EmailSendLog.audience,
            "sent_count": r.EmailSendLog.sent_count,
            "failed_count": r.EmailSendLog.failed_count,
            "opens": r.opens,
            "unique_opens": r.unique_opens,
        }
        for r in rows
    ]


@router.get("/email-send-logs/{log_id}/recipients")
async def get_send_log_recipients(
    log_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(
        select(EmailSendRecipient)
        .where(EmailSendRecipient.send_log_id == log_id)
        .order_by(EmailSendRecipient.success.desc(), EmailSendRecipient.id)
    )).scalars().all()
    return [
        {"id": r.id, "user_id": r.user_id, "email": r.email, "success": r.success}
        for r in rows
    ]


@router.post("/email-send-logs/{log_id}/resend-failed")
async def resend_failed_recipients(
    log_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from backend.notifier import _send_via_resend, _pause_url

    log = (await db.execute(select(EmailSendLog).where(EmailSendLog.id == log_id))).scalar_one_or_none()
    if not log:
        raise HTTPException(status_code=404, detail="לוג לא נמצא")
    if not log.template_id:
        raise HTTPException(status_code=400, detail="התבנית נמחקה — לא ניתן לשלוח מחדש")

    t = (await db.execute(select(EmailTemplate).where(EmailTemplate.id == log.template_id))).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="תבנית לא נמצאה")

    failed_rows = (await db.execute(
        select(EmailSendRecipient)
        .where(EmailSendRecipient.send_log_id == log_id, EmailSendRecipient.success == False)
    )).scalars().all()

    if not failed_rows:
        return {"sent": 0, "failed": 0, "message": "אין נכשלים לשליחה מחדש"}

    import os, asyncio
    base_url = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")

    product_count_sub = (
        select(func.count(UserProduct.id))
        .where(UserProduct.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )

    sent = failed = 0
    for i, r in enumerate(failed_rows):
        u = (await db.execute(select(User, product_count_sub.label("pc")).where(User.id == r.user_id))).first() if r.user_id else None
        recipient = r.email
        pc = u[1] if u else 0
        user_obj = u[0] if u else None
        subj = t.subject.replace("{{email}}", recipient).replace("{{product_count}}", str(pc))
        pixel_url = f"{base_url}/track/email-open?uid={r.user_id or 0}&tid={t.id}"
        pixel = f'<img src="{pixel_url}" width="1" height="1" style="display:none;" alt="">'
        pause = _pause_url(r.user_id) if r.user_id else "#"
        html_body = (
            t.body
            .replace("{{email}}", recipient)
            .replace("{{notify_email}}", recipient)
            .replace("{{product_count}}", str(pc))
            .replace("{{pause_url}}", pause)
        ) + pixel
        ok = _send_via_resend(recipient, subj, html_body, "")
        r.success = ok
        if ok:
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.55)

    # Update log counts
    log.sent_count += sent
    log.failed_count -= sent  # sent successfully this time
    await db.commit()
    return {"sent": sent, "failed": failed, "message": f"נשלח מחדש ל-{sent} משתמשים" + (f", {failed} נכשלו שוב" if failed else "")}


@router.get("/email-templates/{template_id}/opens")
async def get_template_opens(
    template_id: int,
    admin: Annotated[User, Depends(get_current_admin)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    rows = (await db.execute(
        select(EmailOpen, User.email)
        .join(User, User.id == EmailOpen.user_id)
        .where(EmailOpen.template_id == template_id)
        .order_by(EmailOpen.opened_at.desc())
    )).all()
    total_unique = len({r[0].user_id for r in rows})
    return {
        "total_opens": len(rows),
        "unique_openers": total_unique,
        "opens": [
            {"email": r[1], "opened_at": r[0].opened_at.isoformat(), "ip": r[0].ip}
            for r in rows
        ]
    }
