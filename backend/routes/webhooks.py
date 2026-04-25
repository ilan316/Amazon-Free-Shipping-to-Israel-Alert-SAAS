import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/resend")
async def resend_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "")
    token = request.query_params.get("token", "")
    if not secret or not hmac.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = payload.get("type", "")
    if event_type not in ("email.bounced", "email.complained"):
        return {"ok": True, "ignored": event_type}

    data = payload.get("data", {})
    to_list = data.get("to", [])
    if isinstance(to_list, str):
        to_list = [to_list]

    bounce_type = "complaint" if event_type == "email.complained" else "bounce"
    updated = 0

    for email in to_list:
        email = email.strip().lower()
        result = await db.execute(
            select(User).where(
                or_(User.notify_email == email, User.email == email)
            )
        )
        users = result.scalars().all()
        for user in users:
            if not user.notify_email_bounced:
                user.notify_email_bounced = True
                user.notify_email_bounced_at = datetime.now(timezone.utc)
                user.notify_email_bounce_type = bounce_type
                updated += 1
                logger.warning(f"Bounce/complaint recorded for user {user.id} ({email}) type={bounce_type}")

    await db.commit()
    return {"ok": True, "updated": updated}
