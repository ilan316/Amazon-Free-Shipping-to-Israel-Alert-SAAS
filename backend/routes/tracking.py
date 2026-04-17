import logging
from typing import Annotated

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import EmailClick

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tracking"])

_ALLOWED_PREFIXES = ("https://www.amazon.com/",)


@router.get("/track/click", include_in_schema=False)
async def track_click(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    url: str = "",
    u: int | None = None,
    a: str = "",
):
    # Security: block open redirects — only allow Amazon URLs
    if not url or not any(url.startswith(p) for p in _ALLOWED_PREFIXES):
        return RedirectResponse("https://www.amazon.com/", status_code=302)

    # Record the click (best-effort — never block the redirect on DB errors)
    try:
        ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else None)
        if ip:
            ip = ip.split(",")[0].strip()[:64]
        click = EmailClick(user_id=u, asin=a[:10] if a else "", ip=ip, dest_url=url[:512])
        db.add(click)
        await db.commit()
    except Exception as exc:
        logger.warning(f"Failed to record email click: {exc}")

    return RedirectResponse(url, status_code=302)
