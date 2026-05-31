"""
Internal API — used by the local Category Scanner to sync free products.
Authentication: X-Sync-Secret header must match INTERNAL_SYNC_SECRET env var.
"""

import os
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import Product

logger = logging.getLogger(__name__)
router = APIRouter()

_SECRET = os.environ.get("INTERNAL_SYNC_SECRET", "")


def _require_secret(x_sync_secret: Annotated[str | None, Header()] = None):
    if not _SECRET:
        raise HTTPException(status_code=503, detail="Sync not configured on server.")
    if x_sync_secret != _SECRET:
        raise HTTPException(status_code=403, detail="Invalid sync secret.")


class SyncProduct(BaseModel):
    asin: str
    name: str = ""
    url: str = ""
    category: str = ""
    found_at: str = ""
    last_price: str = ""
    image_url: str = ""
    name_he: str = ""


class SyncRequest(BaseModel):
    products: list[SyncProduct]


@router.post("/sync-products", dependencies=[Depends(_require_secret)])
async def sync_products(
    body: SyncRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Full-replace sync from local scanner.
    - Upserts all incoming products with source='scanner', last_status='FREE'.
    - Deletes scanner products whose ASIN is not in the incoming list.
    """
    now = datetime.now(timezone.utc)
    incoming_asins = {p.asin for p in body.products}

    added = 0
    synced = 0

    for p in body.products:
        url = p.url or f"https://www.amazon.com/dp/{p.asin}"
        stmt = insert(Product).values(
            asin=p.asin,
            name=p.name or p.asin,
            url=url,
            last_status="FREE",
            last_checked=now,
            source="scanner",
            raw_text="",
            consecutive_errors=0,
            last_price=p.last_price or None,
            image_url=p.image_url or None,
            name_he=p.name_he or None,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["asin"],
            set_={
                "name": stmt.excluded.name,
                "url": stmt.excluded.url,
                "last_status": "FREE",
                "last_checked": now,
                "source": "scanner",
                "consecutive_errors": 0,
                "last_price": stmt.excluded.last_price,
                "image_url": stmt.excluded.image_url,
                "name_he": stmt.excluded.name_he,
            },
        )
        result = await db.execute(stmt)
        if result.rowcount and result.inserted_primary_key:
            added += 1
        synced += 1

    # Delete scanner products no longer in the incoming list
    delete_result = await db.execute(
        delete(Product).where(
            Product.source == "scanner",
            Product.asin.not_in(incoming_asins) if incoming_asins else True,
        )
    )
    removed = delete_result.rowcount

    await db.commit()

    logger.info(f"sync-products: synced={synced}, removed={removed}")
    return {"synced": synced, "removed": removed}


@router.post("/send-telegram-invite-test", dependencies=[Depends(_require_secret)])
async def send_telegram_invite_test_endpoint(
    to: str,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Send a test Telegram invite email to a given address."""
    from backend.notifier import send_telegram_invite_test
    ok = send_telegram_invite_test(to, "")
    return {"ok": ok, "to": to}


@router.post("/seed-telegram-invite", dependencies=[Depends(_require_secret)])
async def seed_telegram_invite(db: Annotated[AsyncSession, Depends(get_db)]):
    """Create/update the Telegram invite email template in DB."""
    from backend.models import EmailTemplate
    from backend.notifier import build_telegram_invite_html, _TELEGRAM_INVITE_SUBJECT
    name = "הזמנה_טלגרם_יוני_2026"
    body = build_telegram_invite_html("{{pause_url}}")
    existing = (await db.execute(select(EmailTemplate).where(EmailTemplate.name == name))).scalar_one_or_none()
    if existing:
        existing.subject = _TELEGRAM_INVITE_SUBJECT
        existing.body = body
        await db.commit()
        return {"id": existing.id, "message": "תבנית עודכנה", "already_exists": True}
    t = EmailTemplate(name=name, subject=_TELEGRAM_INVITE_SUBJECT, body=body)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return {"id": t.id, "message": "תבנית נוצרה", "already_exists": False}


@router.post("/backfill-hebrew", dependencies=[Depends(_require_secret)])
async def backfill_hebrew(db: Annotated[AsyncSession, Depends(get_db)]):
    """Generate name_he for every product in DB that is missing it."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set on server.")

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    result = await db.execute(
        select(Product).where(
            Product.source == "user",
            (Product.name_he == None) | (Product.name_he == ""),
        )
    )
    products = result.scalars().all()

    updated = 0
    for p in products:
        if not p.name:
            continue
        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=60,
                messages=[{"role": "user", "content": f"תרגם לעברית קצרה ומובנת (עד 7 מילים, שמור את שם המותג, ללא מרכאות): {p.name}"}],
            )
            p.name_he = msg.content[0].text.strip()
            updated += 1
        except Exception as e:
            logger.warning(f"[{p.asin}] Hebrew name generation failed: {e}")

    await db.commit()
    logger.info(f"backfill-hebrew: updated {updated}/{len(products)} products")
    return {"updated": updated, "total_missing": len(products)}
