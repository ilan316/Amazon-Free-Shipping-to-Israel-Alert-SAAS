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
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal
from backend.models import Product, User, UserProduct, NotificationLog
from backend.checker import browser_manager, ShippingStatus, CheckResult
from backend.notifier import send_daily_summary

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 5


async def _update_product(db: AsyncSession, product: Product, result: CheckResult) -> bool:
    """Update product in DB. Returns True if this is the product's first error (notify admin)."""
    product.last_checked = datetime.now(timezone.utc)

    if result.status in (ShippingStatus.FREE, ShippingStatus.PAID, ShippingStatus.NO_SHIP):
        # Definitive result — update visible status
        product.last_status = result.status.value
        product.raw_text = result.raw_text or ""
        product.found_in_aod = result.found_in_aod
        product.consecutive_errors = 0
        if result.product_name:
            product.name = result.product_name
        await db.commit()
        return False
    elif result.status == ShippingStatus.UNKNOWN:
        # Delivery text found but unclassifiable — update status visibly, not a scraping failure
        product.last_status = result.status.value
        product.raw_text = result.raw_text or ""
        product.consecutive_errors = 0
        await db.commit()
        return False
    else:
        # ERROR — true scraping/network failure; keep existing last_status for customers
        prev_errors = product.consecutive_errors
        product.consecutive_errors += 1
        if result.raw_text:
            product.raw_text = result.raw_text  # save for admin debugging
        await db.commit()
        return prev_errors == 0  # True only on first failure


async def run_global_check_cycle():
    """Check all tracked products and update DB. No emails sent here."""
    logger.info("=== Check cycle started ===")
    location_ok = await browser_manager.refresh_location()
    if not location_ok:
        logger.warning("Location not set to Israel (CAPTCHA?) — skipping check cycle to avoid UNKNOWN results.")
        logger.info("=== Check cycle skipped ===")
        return
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
        newly_failed = []
        newly_blocked = []

        # Separate products to skip (too many consecutive errors) from those to check
        to_check = []
        for product in products:
            if product.consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                logger.warning(f"[{product.asin}] Skipping — {product.consecutive_errors} consecutive errors.")
                if product.consecutive_errors == MAX_CONSECUTIVE_ERRORS:
                    from backend.checker import CheckResult
                    newly_blocked.append((product, CheckResult(product.asin, ShippingStatus.ERROR,
                        error_message=f"Product blocked after {MAX_CONSECUTIVE_ERRORS} consecutive errors — no longer being checked.")))
                    product.consecutive_errors += 1
                    await db.commit()
            else:
                to_check.append(product)

        # Check all eligible products in parallel (httpx-first with Playwright fallback)
        if to_check:
            check_results = await browser_manager.check_many(
                [(p.asin, p.url) for p in to_check]
            )
            for i, (product, check_result) in enumerate(zip(to_check, check_results)):
                try:
                    is_first_error = await _update_product(db, product, check_result)
                    if is_first_error:
                        newly_failed.append((product, check_result))
                    logger.info(f"[{i+1}/{len(to_check)}] [{product.asin}] → {check_result.status.value}")
                except Exception as e:
                    logger.error(f"[{product.asin}] Unexpected error saving result: {e}")
                    if product.consecutive_errors == 0:
                        from backend.checker import CheckResult
                        newly_failed.append((product, CheckResult(product.asin, ShippingStatus.ERROR, error_message=str(e))))
                    product.consecutive_errors += 1
                    await db.commit()

    if newly_failed:
        await _notify_admin_of_errors(newly_failed)
    if newly_blocked:
        await _notify_admin_of_errors(newly_blocked)

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
                select(Product, UserProduct.custom_name)
                .join(UserProduct, Product.id == UserProduct.product_id)
                .where(
                    UserProduct.user_id == user.id,
                    UserProduct.is_paused == False,
                    Product.last_status == ShippingStatus.FREE.value,
                )
            )
            free_products = free_products_result.all()  # list of (Product, custom_name)

            if not free_products:
                continue

            success = send_daily_summary(user, free_products)

            for product, _ in free_products:
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


async def _notify_admin_of_errors(failed_items: list):
    """Send a single error-report email to all admin users."""
    from backend.notifier import send_admin_error_report
    async with AsyncSessionLocal() as db:
        admins = (await db.execute(
            select(User).where(User.is_admin == True, User.is_active == True)
        )).scalars().all()
    for admin in admins:
        send_admin_error_report(admin.email, failed_items)
        logger.info(f"Admin error report sent to {admin.email} ({len(failed_items)} product(s))")


async def check_single_product(asin: str, url: str):
    """Check a single product immediately (used after a user adds it)."""
    logger.info(f"[{asin}] Immediate first check triggered")
    location_ok = await browser_manager.refresh_location()
    if not location_ok:
        logger.warning(f"[{asin}] Location not set to Israel — skipping immediate check.")
        return
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
