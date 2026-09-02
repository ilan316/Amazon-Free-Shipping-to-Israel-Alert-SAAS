"""
Internal API — used by the local Category Scanner to sync free products.
Authentication: X-Sync-Secret header must match INTERNAL_SYNC_SECRET env var.
"""

import os
import json
import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy import select, delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import Product, User, UserProduct, SystemSetting

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
    amazon_category: str = ""
    found_at: str = ""
    last_price: str = ""
    image_url: str = ""
    image_urls: list[str] = []
    name_he: str = ""
    description: str = ""


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
        name_he = (p.name_he or "")[:290] or None
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
            image_urls=json.dumps(p.image_urls) if p.image_urls else None,
            name_he=name_he,
            amazon_category=p.amazon_category or None,
            description=p.description or None,
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
                "image_urls": stmt.excluded.image_urls,
                "name_he": func.left(stmt.excluded.name_he, 290),
                "amazon_category": stmt.excluded.amazon_category,
                "description": stmt.excluded.description,
            },
        )
        result = await db.execute(stmt)
        if result.rowcount and result.inserted_primary_key:
            added += 1
        synced += 1

    # Delete scanner products no longer in the incoming list.
    # Never touch a product a user is tracking: the upsert above stamps
    # source='scanner' on any ASIN the scanner also found, including ones a user
    # added themselves — without this guard the next sync would silently drop it
    # from their tracking list the moment it stopped being free.
    tracked = select(UserProduct.product_id).distinct()
    delete_result = await db.execute(
        delete(Product).where(
            Product.source == "scanner",
            Product.id.not_in(tracked),
            Product.asin.not_in(incoming_asins) if incoming_asins else True,
        )
    )
    removed = delete_result.rowcount

    kept = (await db.execute(
        select(func.count()).select_from(Product).where(
            Product.source == "scanner",
            Product.id.in_(tracked),
            Product.asin.not_in(incoming_asins) if incoming_asins else True,
        )
    )).scalar_one()

    # Stamp when the scanner last synced — independent of any product's
    # last_checked, which the far-more-frequent per-user checker also touches
    # for any scanner ASIN a user happens to track.
    sync_stmt = insert(SystemSetting).values(key="last_scanner_sync", value=now.isoformat())
    sync_stmt = sync_stmt.on_conflict_do_update(
        index_elements=["key"], set_={"value": sync_stmt.excluded.value}
    )
    await db.execute(sync_stmt)

    await db.commit()

    logger.info(f"sync-products: synced={synced}, removed={removed}, kept_tracked={kept}")
    return {"synced": synced, "removed": removed, "kept_tracked": kept}


@router.get("/product-stats", dependencies=[Depends(_require_secret)])
async def product_stats(db: Annotated[AsyncSession, Depends(get_db)]):
    """Return image_urls, name_he and description coverage stats."""
    rows = (await db.execute(select(Product.image_url, Product.image_urls, Product.name_he, Product.description, Product.source))).all()
    result = {}
    for image_url, image_urls, name_he, description, source in rows:
        s = result.setdefault(source, {
            "total": 0,
            "images": {0:0,1:0,2:0,3:0,4:0,5:0},
            "no_image_url": 0,
            "no_images_at_all": 0,
            "no_name_he": 0,
            "no_description_he": 0,
        })
        s["total"] += 1
        arr_count = len(json.loads(image_urls)) if image_urls else 0
        total_imgs = (1 if image_url else 0) + arr_count
        s["images"][min(total_imgs, 5)] += 1
        if not image_url:
            s["no_image_url"] += 1
        if not image_url and arr_count == 0:
            s["no_images_at_all"] += 1
        if not name_he:
            s["no_name_he"] += 1
        if not description or all(ord(c) < 1488 for c in description if c.isalpha()):
            s["no_description_he"] += 1
    return {"total": sum(s["total"] for s in result.values()), "by_source": result}


@router.get("/scanner-incomplete", dependencies=[Depends(_require_secret)])
async def scanner_incomplete(db: Annotated[AsyncSession, Depends(get_db)]):
    """Return scanner products missing images or Hebrew description."""
    from sqlalchemy import or_
    rows = (await db.execute(
        select(Product.asin, Product.name, Product.name_he, Product.description, Product.image_url, Product.image_urls)
        .where(
            Product.source == "scanner",
            or_(
                (Product.image_url == None) & ((Product.image_urls == None) | (Product.image_urls == "[]")),
                (Product.description == None) | (Product.description == ""),
            )
        )
    )).all()
    return [
        {
            "asin": r.asin,
            "name": r.name,
            "name_he": r.name_he,
            "has_description": bool(r.description),
            "has_image_url": bool(r.image_url),
            "image_urls_count": len(json.loads(r.image_urls)) if r.image_urls else 0,
        }
        for r in rows
    ]


@router.get("/last-send-log", dependencies=[Depends(_require_secret)])
async def last_send_log(db: Annotated[AsyncSession, Depends(get_db)], limit: int = 1):
    """Return the last N email send logs with per-recipient success/fail counts."""
    from backend.models import EmailSendLog, EmailSendRecipient
    logs = (await db.execute(
        select(EmailSendLog).order_by(EmailSendLog.sent_at.desc()).limit(limit)
    )).scalars().all()
    result = []
    for log in logs:
        recipients = (await db.execute(
            select(EmailSendRecipient).where(EmailSendRecipient.send_log_id == log.id)
        )).scalars().all()
        failed = [r.email for r in recipients if not r.success]
        result.append({
            "id": log.id,
            "template_name": log.template_name,
            "sent_at": log.sent_at.isoformat() if log.sent_at else None,
            "audience": log.audience,
            "sent_count": log.sent_count,
            "failed_count": log.failed_count,
            "failed_emails": failed,
        })
    return result


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


@router.post("/trigger-telegram-product", dependencies=[Depends(_require_secret)])
async def trigger_telegram_product(db: Annotated[AsyncSession, Depends(get_db)]):
    """Manually trigger sending one product to the Telegram channel (for testing)."""
    from backend.scheduler import run_send_telegram_product
    await run_send_telegram_product()
    return {"ok": True}


@router.post("/trigger-facebook-product", dependencies=[Depends(_require_secret)])
async def trigger_facebook_product(db: Annotated[AsyncSession, Depends(get_db)]):
    """Manually trigger sending one product to the Facebook page (for testing)."""
    from backend.scheduler import run_send_facebook_product
    await run_send_facebook_product()
    return {"ok": True}


@router.post("/backfill-hebrew", dependencies=[Depends(_require_secret)])
async def backfill_hebrew(db: Annotated[AsyncSession, Depends(get_db)]):
    """Generate name_he for every product in DB that is missing it."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set on server.")

    import anthropic
    from backend.blog_utils import claude_text
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
            msg = await asyncio.to_thread(
                lambda name=p.name: client.messages.create(
                    model="claude-sonnet-5",
                    max_tokens=200,
                    thinking={"type": "disabled"},
                    messages=[{"role": "user", "content": f"תרגם לעברית קצרה ומובנת (עד 7 מילים, שמור את שם המותג, ללא מרכאות): {name}"}],
                )
            )
            p.name_he = claude_text(msg).strip()
            updated += 1
        except Exception as e:
            logger.warning(f"[{p.asin}] Hebrew name generation failed: {e}")

    await db.commit()
    logger.info(f"backfill-hebrew: updated {updated}/{len(products)} products")
    return {"updated": updated, "total_missing": len(products)}


class HubWarningsRequest(BaseModel):
    warnings: list[str] = []
    run_url: str = ""


@router.post("/hub-warnings", dependencies=[Depends(_require_secret)])
async def hub_warnings(
    body: HubWarningsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """
    Alert admins when build-internal-links.js could not classify a review.

    Called by the website repo's update-sitemap workflow. The workflow only
    sends *new* warnings (diffed against the previous blog/categories.json),
    so this endpoint does not need any dedup state of its own.
    """
    items = [w.strip() for w in body.warnings if w and w.strip()]
    if not items:
        return {"sent": 0, "warnings": 0}

    from backend.notifier import send_simple_email

    rows = "".join(
        f'<li style="margin-bottom:8px;">{w}</li>' for w in items
    )
    run_link = (
        f'<p style="margin-top:20px;"><a href="{body.run_url}">צפייה בריצת ה-Action</a></p>'
        if body.run_url
        else ""
    )
    html = f"""<div dir="rtl" style="font-family:Arial,sans-serif;max-width:560px;margin:auto;padding:24px;background:#fffaf1;border-radius:12px;">
      <h2 style="color:#e47911;">⚠️ סקירות שלא סווגו לקטגוריה</h2>
      <p style="color:#555;">הפוסטים הבאים שויכו ל"שונות" ב-prices.html. להוסיף אימוג'י ל-<code>tools/category-map.json</code> או כלל ל-<code>keywords</code>:</p>
      <ul style="padding-inline-start:20px;color:#111;">{rows}</ul>
      {run_link}
    </div>"""

    result = await db.execute(
        select(User).where(User.is_admin == True, User.is_active == True)  # noqa: E712
    )
    admins = result.scalars().all()

    sent = 0
    for admin in admins:
        if send_simple_email(admin.email, f"⚠️ {len(items)} סקירות ללא קטגוריה", html):
            sent += 1

    logger.info("hub-warnings: %d warnings → %d/%d admins", len(items), sent, len(admins))
    return {"sent": sent, "warnings": len(items)}
