import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload

from backend.database import get_db
from backend.models import User, Product, UserProduct, NotificationLog
from backend.auth import get_current_user
from backend.schemas import AddProductRequest, ProductResponse, MessageResponse


class RenameRequest(BaseModel):
    custom_name: str

router = APIRouter(prefix="/me/products", tags=["products"])


# ── ASIN extraction (ported from desktop config.py) ───────────────────────────

def _follow_redirects(url: str, timeout: int = 8) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.url
    except Exception:
        return url


def extract_asin(value: str) -> str:
    value = value.strip()
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"/product/([A-Z0-9]{10})",
        r"ASIN=([A-Z0-9]{10})",
    ]

    def _try(s: str) -> str | None:
        for p in patterns:
            m = re.search(p, s, re.IGNORECASE)
            if m:
                return m.group(1).upper()
        return None

    asin = _try(value)
    if asin:
        return asin

    if re.fullmatch(r"[A-Z0-9]{10}", value, re.IGNORECASE):
        return value.upper()

    if value.lower().startswith("http"):
        final = _follow_redirects(value)
        if final != value:
            asin = _try(final)
            if asin:
                return asin

    raise ValueError(f"Could not extract ASIN from: '{value}'")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_model=list[ProductResponse])
async def list_products(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserProduct)
        .options(selectinload(UserProduct.product))
        .where(UserProduct.user_id == current_user.id)
        .order_by(UserProduct.added_at.desc())
    )
    user_products = result.scalars().all()

    import os as _os
    _affiliate_tag = _os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()

    # Batch-fetch last notification times (one query instead of N)
    product_ids = [up.product_id for up in user_products]
    last_notified_map: dict = {}
    if product_ids:
        notif_rows = await db.execute(
            select(NotificationLog.product_id, func.max(NotificationLog.sent_at).label("last_sent"))
            .where(
                NotificationLog.user_id == current_user.id,
                NotificationLog.product_id.in_(product_ids),
                NotificationLog.success == True,
            )
            .group_by(NotificationLog.product_id)
        )
        last_notified_map = {row.product_id: row.last_sent for row in notif_rows}

    items = []
    for up in user_products:
        p = up.product
        _aff_url = f"{p.url}?tag={_affiliate_tag}" if _affiliate_tag else p.url
        items.append(ProductResponse(
            asin=p.asin,
            name=p.name,
            custom_name=up.custom_name,
            url=p.url,
            last_status=p.last_status,
            last_checked=p.last_checked,
            found_in_aod=p.found_in_aod,
            last_notified=last_notified_map.get(p.id),
            added_at=up.added_at,
            is_paused=up.is_paused,
            raw_text=p.raw_text or "",
            affiliate_url=_aff_url,
        ))
    return items


@router.post("", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def add_product(
    body: AddProductRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    try:
        asin = extract_asin(body.url_or_asin)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Get or create global product
    result = await db.execute(select(Product).where(Product.asin == asin))
    product = result.scalar_one_or_none()
    if not product:
        product = Product(
            asin=asin,
            url=f"https://www.amazon.com/dp/{asin}",
        )
        db.add(product)
        await db.flush()

    # Check if user already tracks this product
    existing = await db.execute(
        select(UserProduct).where(
            UserProduct.user_id == current_user.id,
            UserProduct.product_id == product.id,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="המוצר כבר קיים ברשימה שלך")

    up = UserProduct(
        user_id=current_user.id,
        product_id=product.id,
        custom_name=body.custom_name,
    )
    db.add(up)
    await db.commit()
    await db.refresh(up)

    return ProductResponse(
        asin=product.asin,
        name=product.name,
        custom_name=up.custom_name,
        url=product.url,
        last_status=product.last_status,
        last_checked=product.last_checked,
        found_in_aod=product.found_in_aod,
        last_notified=None,
        added_at=up.added_at,
        is_paused=False,
    )


async def _check_product_soon(asin: str, url: str):
    """Run a single-product check shortly after it's added."""
    await __import__("asyncio").sleep(2)
    try:
        from backend.scheduler import check_single_product
        await check_single_product(asin, url)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Immediate check failed for {asin}: {e}")



@router.post("/check-new", response_model=MessageResponse)
async def check_new_products(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Check all products that have never been checked (last_checked is None)."""
    result = await db.execute(
        select(Product).where(
            Product.id.in_(select(UserProduct.product_id).distinct()),
            Product.last_checked.is_(None),
        )
    )
    new_products = result.scalars().all()
    if not new_products:
        return MessageResponse(message="אין מוצרים חדשים לבדיקה")

    import asyncio
    asyncio.create_task(_check_new_products_soon([(p.asin, p.url) for p in new_products]))
    return MessageResponse(message=f"בודק {len(new_products)} מוצר(ים) חדשים...")


async def _check_new_products_soon(items: list[tuple[str, str]]):
    await __import__("asyncio").sleep(1)
    try:
        from backend.scheduler import check_single_product
        import asyncio
        await asyncio.gather(*[check_single_product(asin, url) for asin, url in items])
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Batch new-product check failed: {e}")


@router.post("/{asin}/check-now", response_model=MessageResponse)
async def check_now(
    asin: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    asin = asin.upper().strip()
    result = await db.execute(
        select(UserProduct)
        .join(Product, UserProduct.product_id == Product.id)
        .where(UserProduct.user_id == current_user.id, Product.asin == asin)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Product not found in your list")
    product_result = await db.execute(select(Product).where(Product.asin == asin))
    product = product_result.scalar_one()

    if product.last_checked:
        cooldown = timedelta(minutes=30)
        elapsed = datetime.now(timezone.utc) - product.last_checked.replace(tzinfo=timezone.utc)
        if elapsed < cooldown:
            remaining = int((cooldown - elapsed).total_seconds() / 60) + 1
            raise HTTPException(status_code=429, detail=f"המוצר נבדק לאחרונה. ניתן לבדוק שוב בעוד {remaining} דקות.")

    import asyncio
    asyncio.create_task(_check_product_soon(product.asin, product.url))
    return MessageResponse(message="בדיקה הופעלה")


@router.patch("/{asin}/toggle-pause", response_model=MessageResponse)
async def toggle_pause(
    asin: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    asin = asin.upper().strip()
    result = await db.execute(
        select(UserProduct)
        .join(Product, UserProduct.product_id == Product.id)
        .where(UserProduct.user_id == current_user.id, Product.asin == asin)
    )
    up = result.scalar_one_or_none()
    if not up:
        raise HTTPException(status_code=404, detail="Product not found in your list")
    up.is_paused = not up.is_paused
    await db.commit()
    state = "מושהה" if up.is_paused else "פעיל"
    return MessageResponse(message=f"Product {asin} is now {state}")


@router.patch("/{asin}/name", response_model=MessageResponse)
async def rename_product(
    asin: str,
    body: RenameRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    asin = asin.upper().strip()
    result = await db.execute(
        select(UserProduct)
        .join(Product, UserProduct.product_id == Product.id)
        .where(UserProduct.user_id == current_user.id, Product.asin == asin)
    )
    up = result.scalar_one_or_none()
    if not up:
        raise HTTPException(status_code=404, detail="Product not found in your list")
    up.custom_name = body.custom_name.strip() or None
    await db.commit()
    return MessageResponse(message="שם עודכן")


@router.delete("/{asin}", response_model=MessageResponse)
async def remove_product(
    asin: str,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    asin = asin.upper().strip()
    result = await db.execute(
        select(UserProduct)
        .join(Product, UserProduct.product_id == Product.id)
        .where(UserProduct.user_id == current_user.id, Product.asin == asin)
    )
    up = result.scalar_one_or_none()
    if not up:
        raise HTTPException(status_code=404, detail="Product not found in your list")

    await db.delete(up)
    await db.commit()
    return MessageResponse(message=f"Product {asin} removed")
