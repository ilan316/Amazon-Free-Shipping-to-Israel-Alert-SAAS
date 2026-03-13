import re
import urllib.request
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
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

    items = []
    for up in user_products:
        p = up.product
        # Get last notification time for this user+product
        notif_result = await db.execute(
            select(NotificationLog.sent_at)
            .where(
                NotificationLog.user_id == current_user.id,
                NotificationLog.product_id == p.id,
                NotificationLog.success == True,
            )
            .order_by(NotificationLog.sent_at.desc())
            .limit(1)
        )
        last_notified = notif_result.scalar_one_or_none()

        items.append(ProductResponse(
            asin=p.asin,
            name=p.name,
            custom_name=up.custom_name,
            url=p.url,
            last_status=p.last_status,
            last_checked=p.last_checked,
            found_in_aod=p.found_in_aod,
            last_notified=last_notified,
            added_at=up.added_at,
            is_paused=up.is_paused,
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
        raise HTTPException(status_code=409, detail="Product already in your list")

    up = UserProduct(
        user_id=current_user.id,
        product_id=product.id,
        custom_name=body.custom_name,
    )
    db.add(up)
    await db.commit()
    await db.refresh(up)

    # Trigger immediate first check in background (only if product has never been checked)
    if product.last_checked is None:
        import asyncio
        asyncio.create_task(_check_product_soon(product.asin, product.url))

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
