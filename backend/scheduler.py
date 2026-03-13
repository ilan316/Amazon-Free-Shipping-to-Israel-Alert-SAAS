"""
Global check cycle — runs every CHECK_INTERVAL_MINUTES (default 120).

Logic per cycle:
  1. Load all unique products that at least one user is tracking
  2. Check each product with Playwright (via BrowserManager singleton)
  3. Update product status in DB
  4. For products that are FREE: notify all users who haven't been notified in 24h
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal
from backend.models import Product, User, UserProduct, NotificationLog
from backend.checker import browser_manager, ShippingStatus, CheckResult
from backend.notifier import send_user_alert

logger = logging.getLogger(__name__)

COOLDOWN_HOURS = int(os.environ.get("NOTIFICATION_COOLDOWN_HOURS", "24"))
MAX_CONSECUTIVE_ERRORS = 5


async def _update_product(db: AsyncSession, product: Product, result: CheckResult):
    product.last_status = result.status.value
    product.last_checked = datetime.now(timezone.utc)
    product.raw_text = result.raw_text or ""
    product.found_in_aod = result.found_in_aod

    if result.status == ShippingStatus.ERROR:
        product.consecutive_errors += 1
    else:
        product.consecutive_errors = 0
        if result.product_name:
            product.name = result.product_name

    await db.commit()


async def _notify_subscribed_users(db: AsyncSession, product: Product, result: CheckResult):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)

    # Find users tracking this product who haven't been notified recently
    notified_users_q = (
        select(NotificationLog.user_id)
        .where(
            NotificationLog.product_id == product.id,
            NotificationLog.sent_at > cutoff,
            NotificationLog.success == True,
        )
    )
    recently_notified = (await db.execute(notified_users_q)).scalars().all()

    users_q = (
        select(User)
        .join(UserProduct, User.id == UserProduct.user_id)
        .where(
            UserProduct.product_id == product.id,
            User.is_active == True,
            UserProduct.is_paused == False,
            User.id.not_in(recently_notified),
        )
    )
    users = (await db.execute(users_q)).scalars().all()

    for user in users:
        success = send_user_alert(user, product, result)
        log = NotificationLog(
            user_id=user.id,
            product_id=product.id,
            status=result.status.value,
            email_to=user.notify_email,
            success=success,
            error_msg=None if success else "send failed",
        )
        db.add(log)

    if users:
        await db.commit()
        logger.info(f"[{product.asin}] Notified {len(users)} user(s).")


async def run_global_check_cycle():
    logger.info("=== Check cycle started ===")
    async with AsyncSessionLocal() as db:
        # Load all unique products being tracked by at least one user
        result = await db.execute(
            select(Product).where(
                Product.id.in_(
                    select(UserProduct.product_id).distinct()
                )
            )
        )
        products = result.scalars().all()

        if not products:
            logger.info("No products to check.")
            return

        logger.info(f"Checking {len(products)} product(s)...")

        for i, product in enumerate(products):
            if product.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.warning(f"[{product.asin}] Skipping — {product.consecutive_errors} consecutive errors.")
                continue

            try:
                check_result = await browser_manager.check(product.asin, product.url)
                await _update_product(db, product, check_result)

                logger.info(f"[{i+1}/{len(products)}] [{product.asin}] → {check_result.status.value}")

                if check_result.status == ShippingStatus.FREE:
                    await _notify_subscribed_users(db, product, check_result)

            except Exception as e:
                logger.error(f"[{product.asin}] Unexpected error: {e}")
                product.consecutive_errors += 1
                await db.commit()

            # Anti-detection delay between products (same as desktop app)
            if i < len(products) - 1:
                await asyncio.sleep(random.uniform(5.0, 12.0))

    logger.info("=== Check cycle complete ===")


async def check_single_product(asin: str, url: str):
    """Check a single product immediately (used after a user adds it)."""
    logger.info(f"[{asin}] Immediate first check triggered")
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Product).where(Product.asin == asin))
        product = result.scalar_one_or_none()
        if not product:
            return
        try:
            check_result = await browser_manager.check(product.asin, product.url)
            await _update_product(db, product, check_result)
            logger.info(f"[{asin}] Immediate check → {check_result.status.value}")
            if check_result.status == ShippingStatus.FREE:
                await _notify_subscribed_users(db, product, check_result)
        except Exception as e:
            logger.error(f"[{asin}] Immediate check error: {e}")
            # Still mark as checked so UI doesn't stay stuck on "טרם נבדק"
            product.last_checked = datetime.now(timezone.utc)
            product.last_status = ShippingStatus.ERROR.value
            product.consecutive_errors += 1
            await db.commit()
