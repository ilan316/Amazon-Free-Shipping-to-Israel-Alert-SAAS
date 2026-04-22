import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from backend.main import limiter

from backend.database import get_db
from backend.models import User
from backend.auth import hash_password, verify_password, create_access_token, SECRET_KEY, ALGORITHM
from backend.schemas import RegisterRequest, LoginRequest, TokenResponse, MessageResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def _send_meta_capi_lead(email: str, client_ip: str, user_agent: str):
    pixel_id = "1981109749468001"
    capi_token = os.environ.get("META_CAPI_TOKEN", "")
    if not capi_token:
        return
    em_hash = hashlib.sha256(email.lower().strip().encode()).hexdigest()
    payload = {
        "data": [{
            "event_name": "Lead",
            "event_time": int(time.time()),
            "action_source": "website",
            "user_data": {
                "em": [em_hash],
                "client_ip_address": client_ip,
                "client_user_agent": user_agent,
            },
        }]
    }
    import httpx
    try:
        httpx.post(
            f"https://graph.facebook.com/v19.0/{pixel_id}/events",
            params={"access_token": capi_token},
            json=payload,
            timeout=5,
        )
    except Exception:
        pass


def _make_verify_token(user_id: int) -> str:
    from jose import jwt as jose_jwt
    expire = datetime.now(timezone.utc) + timedelta(hours=24)
    return jose_jwt.encode(
        {"sub": str(user_id), "type": "email_verify", "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def _send_verify_email(user: User, token: str):
    base = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "app.amzfreeil.com")
    verify_url = f"https://{base}/auth/verify-email?token={token}"
    lang = user.language or "he"
    if lang == "he":
        subject = "אמת את האימייל שלך · Amazon Israel Alert"
        html = f"""<div dir="rtl" style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px;">
          <h2 style="color:#e47911;">ברוך הבא ל-Amazon Israel Alert!</h2>
          <p>לחץ על הכפתור כדי לאמת את האימייל ולהתחיל להשתמש בשירות:</p>
          <a href="{verify_url}" style="display:inline-block;background:#FF9900;color:#111;padding:12px 28px;border-radius:8px;font-weight:bold;text-decoration:none;margin:16px 0;">אמת אימייל</a>
          <p style="color:#888;font-size:0.85rem;">הקישור תקף ל-24 שעות.</p>
        </div>"""
    else:
        subject = "Verify your email · Amazon Israel Alert"
        html = f"""<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px;">
          <h2 style="color:#e47911;">Welcome to Amazon Israel Alert!</h2>
          <p>Click the button to verify your email and start using the service:</p>
          <a href="{verify_url}" style="display:inline-block;background:#FF9900;color:#111;padding:12px 28px;border-radius:8px;font-weight:bold;text-decoration:none;margin:16px 0;">Verify Email</a>
          <p style="color:#888;font-size:0.85rem;">Link valid for 24 hours.</p>
        </div>"""
    from backend.notifier import send_simple_email
    send_simple_email(user.email, subject, html)


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(request: Request, body: RegisterRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.email == body.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        email=body.email,
        password_hash=hash_password(body.password),
        notify_email=body.notify_email,
        language=body.language,
        is_verified=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    background_tasks.add_task(
        _send_meta_capi_lead,
        user.email,
        request.client.host,
        request.headers.get("user-agent", ""),
    )

    token = _make_verify_token(user.id)
    _send_verify_email(user, token)

    return MessageResponse(message="נשלח אימייל אימות לכתובתך. בדוק את תיבת הדואר ואמת את החשבון.")


@router.get("/verify-email", include_in_schema=False)
async def verify_email(token: str, db: AsyncSession = Depends(get_db)):
    from jose import jwt as jose_jwt, JWTError
    try:
        payload = jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "email_verify":
            raise ValueError()
        user_id = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        return HTMLResponse("""<html dir="rtl"><body style="font-family:Arial;text-align:center;padding:60px;background:#fffaf1;">
        <h2 style="color:#c62828;">❌ הקישור לא תקין או פג תוקף</h2>
        <p><a href="/">חזרה לדף הבית</a></p></body></html>""", status_code=400)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return HTMLResponse("<html><body>User not found</body></html>", status_code=404)

    already_verified = user.is_verified
    new_user_email = user.email  # save before commit (object expires after commit)
    user.is_verified = True
    await db.commit()

    # Notify all admins only on first verification (prevents duplicate emails from email-client prefetch)
    if not already_verified:
        admins_result = await db.execute(select(User).where(User.is_admin == True, User.is_active == True))
        admins = admins_result.scalars().all()
        from backend.notifier import send_admin_new_user_notification
        for admin in admins:
            send_admin_new_user_notification(admin.email, new_user_email)

    return HTMLResponse("""<html dir="rtl"><body style="font-family:Arial;text-align:center;padding:60px;background:#fffaf1;">
    <h2 style="color:#2e7d32;">✅ האימייל אומת בהצלחה!</h2>
    <p>כעת תוכל להתחבר לחשבון שלך.</p>
    <a href="/" style="display:inline-block;background:#FF9900;color:#111;padding:10px 24px;border-radius:8px;font-weight:bold;text-decoration:none;margin-top:16px;">כניסה</a>
    </body></html>""")


@router.post("/resend-verification", response_model=MessageResponse)
@limiter.limit("3/minute")
async def resend_verification(request: Request, body: dict, db: AsyncSession = Depends(get_db)):
    email = body.get("email", "").strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    # Always return success to prevent email enumeration
    if user and not user.is_verified:
        token = _make_verify_token(user.id)
        _send_verify_email(user, token)
    return MessageResponse(message="אם האימייל קיים במערכת, נשלח קישור אימות.")


@router.post("/login", response_model=TokenResponse)
@limiter.limit("10/minute")
async def login(request: Request, body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email, User.is_active == True))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_verified:
        raise HTTPException(status_code=403, detail="EMAIL_NOT_VERIFIED")
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    return TokenResponse(access_token=create_access_token(user.id))


@router.post("/google", response_model=TokenResponse)
@limiter.limit("10/minute")
async def google_login(request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    """Sign in / sign up with a Google ID token."""
    body = await request.json()
    credential = body.get("credential", "").strip()
    if not credential:
        raise HTTPException(status_code=400, detail="Missing credential")

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=503, detail="Google login not configured")

    # Verify the ID token with Google
    import httpx
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": credential},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    info = resp.json()
    if info.get("aud") != client_id:
        raise HTTPException(status_code=401, detail="Token audience mismatch")

    google_id = info.get("sub")
    email = info.get("email", "").lower().strip()
    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Missing profile info from Google")

    # Find existing user by google_id or email
    result = await db.execute(select(User).where(User.google_id == google_id))
    user = result.scalar_one_or_none()

    if not user:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

    if user:
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account disabled")
        # Link google_id if not already set
        if not user.google_id:
            user.google_id = google_id
        if not user.is_verified:
            user.is_verified = True
        user.last_login_at = datetime.now(timezone.utc)
        await db.commit()
    else:
        # Create new user — verified immediately, no password
        user = User(
            email=email,
            password_hash="",
            notify_email=email,
            language="he",
            is_verified=True,
            google_id=google_id,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        background_tasks.add_task(
            _send_meta_capi_lead,
            email,
            request.client.host,
            request.headers.get("user-agent", ""),
        )

        # Notify admins
        admins_result = await db.execute(select(User).where(User.is_admin == True, User.is_active == True))
        admins = admins_result.scalars().all()
        from backend.notifier import send_admin_new_user_notification
        for admin in admins:
            send_admin_new_user_notification(admin.email, email)

    return TokenResponse(access_token=create_access_token(user.id))
