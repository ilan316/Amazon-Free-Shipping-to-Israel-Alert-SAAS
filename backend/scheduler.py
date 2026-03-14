"""
Global check cycle — runs every CHECK_INTERVAL_MINUTES (default 120).
Daily summary    — runs once a day at DAILY_SUMMARY_HOUR (Israel time, default 08:00).

Check cycle logic:
  1. Load all unique products tracked by at least one user
  2. Check each product with Playwright (via BrowserManager singleton)
  3. Update product status in DB (no emails sent here)

Daily summary logic:
  1. For each active user, find all their FREE products (not paused)
  2. Send one summary email per user listing all free products
  3. Log in NotificationLog
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal
from backend.models import Product, User, UserProduct, NotificationLog
from backend.checker import browser_manager, ShippingStatus, CheckResult
from backend.notifier import send_daily_summary

logger = logging.getLogger(__name__)

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


async def run_global_check_cycle():
    """Check all tracked products and update DB. No emails sent here."""
    logger.info("=== Check cycle started ===")
    await browser_manager.refresh_location()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Product).where(
                Product.id.in_(select(UserProduct.product_id).distinct())
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
            except Exception as e:
                logger.error(f"[{product.asin}] Unexpected error: {e}")
                product.consecutive_errors += 1
                await db.commit()

            if i < len(products) - 1:
                await asyncio.sleep(random.uniform(5.0, 12.0))

    logger.info("=== Check cycle complete ===")


async def run_daily_summary():
    """Send one daily summary email per user listing all their FREE products."""
    logger.info("=== Daily summary started ===")
    async with AsyncSessionLocal() as db:
        users_result = await db.execute(
            select(User).where(User.is_active == True)
        )
        users = users_result.scalars().all()

        sent = 0
        for user in users:
            free_products_result = await db.execute(
                select(Product)
                .join(UserProduct, Product.id == UserProduct.product_id)
                .where(
                    UserProduct.user_id == user.id,
                    UserProduct.is_paused == False,
                    Product.last_status == ShippingStatus.FREE.value,
                )
            )
            free_products = free_products_result.scalars().all()

            if not free_products:
                continue

            success = send_daily_summary(user, free_products)

            for product in free_products:
                db.add(NotificationLog(
                    user_id=user.id,
                    product_id=product.id,
                    status=ShippingStatus.FREE.value,
                    email_to=user.notify_email,
                    success=success,
                    error_msg=None if success else "send failed",
                ))

            await db.commit()
            if success:
                sent += 1
                logger.info(f"[user {user.id}] Summary sent — {len(free_products)} free product(s).")

    logger.info(f"=== Daily summary complete — {sent} email(s) sent ===")


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
        except Exception as e:
            logger.error(f"[{asin}] Immediate check error: {e}")
            product.last_checked = datetime.now(timezone.utc)
            product.last_status = ShippingStatus.ERROR.value
            product.consecutive_errors += 1
            await db.commit()
