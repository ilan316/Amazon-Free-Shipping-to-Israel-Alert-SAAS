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
