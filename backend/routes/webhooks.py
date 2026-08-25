import hmac
import logging
import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, or_, func
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
    deleted = 0

    for email in to_list:
        email = email.strip().lower()
        result = await db.execute(
            select(User).where(
                or_(func.lower(User.notify_email) == email, func.lower(User.email) == email)
            )
        )
        users = result.scalars().all()
        for user in users:
            # A bounce means the mailbox does not exist — there is no user behind the
            # row, only a typo or a bot, so the account goes rather than lingering as a
            # flagged ghost that inflates every count. Admins are exempt (a bounce on an
            # admin address is an infrastructure problem, not a fake signup), and a spam
            # complaint is a real person choosing to leave: flag, never delete.
            if bounce_type == "bounce" and not user.is_admin:
                logger.warning(f"Bounce: deleting user {user.id} ({email}) — mailbox does not exist")
                await db.delete(user)
                deleted += 1
                continue
            if not user.notify_email_bounced:
                user.notify_email_bounced = True
                user.notify_email_bounced_at = datetime.now(timezone.utc)
                user.notify_email_bounce_type = bounce_type
                updated += 1
                logger.warning(f"Bounce/complaint recorded for user {user.id} ({email}) type={bounce_type}")

    await db.commit()
    return {"ok": True, "updated": updated, "deleted": deleted}
