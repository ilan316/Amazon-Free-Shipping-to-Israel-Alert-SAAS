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
from datetime import datetime, timedelta, timezone
from backend.models import SystemSetting

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import AsyncSessionLocal
from backend.models import Product, User, UserProduct, NotificationLog, EmailTemplate, EmailSendLog, EmailSendRecipient
from backend.checker import browser_manager, ShippingStatus, CheckResult
from backend.notifier import send_daily_summary, _send_via_resend

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 5


async def _update_product(db: AsyncSession, product: Product, result: CheckResult) -> bool:
    """Update product in DB. Returns True if this is the product's first error (notify admin)."""
    product.last_checked = datetime.now(timezone.utc)

    if result.status in (ShippingStatus.FREE, ShippingStatus.PAID, ShippingStatus.NO_SHIP, ShippingStatus.NOT_FOUND):
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
        return True  # notify on every error (1 → MAX_CONSECUTIVE_ERRORS)


async def _retry_check_cycle_after(minutes: int):
    """Wait and retry the check cycle once."""
    await asyncio.sleep(minutes * 60)
    logger.info(f"=== Retrying check cycle after {minutes}-minute delay ===")
    await run_global_check_cycle()


async def run_global_check_cycle():
    """Check all tracked products and update DB. No emails sent here."""
    logger.info("=== Check cycle started ===")
    # Israeli residential proxy provides location automatically — no cookie setup needed
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Product).where(
                Product.id.in_(
                    select(UserProduct.product_id)
                    .join(User, UserProduct.user_id == User.id)
                    .where(User.is_active == True, User.vacation_mode == False)
                    .distinct()
                )
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
                    await db.rollback()
                    # Product may have been deleted mid-cycle (e.g. admin bulk-delete) — skip silently
                    from sqlalchemy.orm.exc import StaleDataError
                    if isinstance(e, StaleDataError):
                        logger.warning(f"[{product.asin}] Product deleted mid-cycle, skipping.")
                        continue
                    logger.error(f"[{product.asin}] Unexpected error saving result: {e}")
                    try:
                        product.consecutive_errors += 1
                        await db.commit()
                    except Exception:
                        await db.rollback()

    if newly_failed:
        await _notify_admin_of_errors(newly_failed)
    if newly_blocked:
        await _notify_admin_of_errors(newly_blocked)

    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(SystemSetting).where(SystemSetting.key == "last_check_at")
        )).scalar_one_or_none()
        now_str = datetime.now(timezone.utc).isoformat()
        if row:
            row.value = now_str
        else:
            db.add(SystemSetting(key="last_check_at", value=now_str))
        await db.commit()

    logger.info("=== Check cycle complete ===")


async def run_daily_summary():
    """Send one daily summary email per user listing all their FREE products."""
    logger.info("=== Daily summary started ===")
    async with AsyncSessionLocal() as db:
        users_result = await db.execute(
            select(User).where(User.is_active == True, User.vacation_mode == False)
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


async def run_inactivity_check():
    """Set vacation_mode=True for users who haven't logged in for X days."""
    from backend.models import SystemSetting
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(SystemSetting).where(SystemSetting.key == "inactivity_days")
        )).scalar_one_or_none()
        days = int(row.value) if row else 90
        if days <= 0:
            return  # disabled

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        result = await db.execute(
            select(User).where(
                User.is_active == True,
                User.vacation_mode == False,
                User.is_admin == False,
                User.last_login_at < cutoff,
                User.last_login_at.isnot(None),
            )
        )
        users = result.scalars().all()
        if not users:
            return
        for user in users:
            user.vacation_mode = True
            logger.info(f"[inactivity] User {user.id} → vacation_mode (inactive {days}+ days)")
        await db.commit()
        logger.info(f"=== Inactivity check: {len(users)} user(s) moved to vacation mode ===")


def _auto_substitute(text: str, user: User) -> str:
    from backend.routes.admin import _pause_url
    return text.replace("{{email}}", user.notify_email).replace("{{pause_url}}", _pause_url(user.id))


async def _run_automation_flow(
    db,
    tpl: EmailTemplate,
    audience: str,
    users: list,
    sent_at: datetime,
    mark_sent_fn,
) -> tuple[int, int]:
    """Send one automation flow, log to EmailSendLog/EmailSendRecipient. Returns (sent, failed)."""
    if not users:
        return 0, 0

    log = EmailSendLog(
        template_id=tpl.id,
        template_name=tpl.name,
        sent_at=sent_at,
        audience=audience,
        sent_count=0,
        failed_count=0,
    )
    db.add(log)
    await db.flush()

    sent = failed = 0
    for u in users:
        ok = await _send_via_resend(
            u.notify_email,
            _auto_substitute(tpl.subject, u),
            _auto_substitute(tpl.body, u),
        )
        db.add(EmailSendRecipient(send_log_id=log.id, user_id=u.id, email=u.notify_email, success=ok))
        if ok:
            mark_sent_fn(u, sent_at)
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(0.55)

    log.sent_count = sent
    log.failed_count = failed
    return sent, failed


async def run_automation_emails():
    """Daily automation: activation + reminder for 0-product users, expansion for 1-9 product users."""
    logger.info("=== Automation emails started ===")
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        tpl_activation = (await db.execute(
            select(EmailTemplate).where(EmailTemplate.name == "לקוח לא הוסיף מוצרים - אפס מוצרים")
        )).scalar_one_or_none()

        tpl_expansion = (await db.execute(
            select(EmailTemplate).where(EmailTemplate.name == "לקוח - הוסף עוד מוצרים למעקב")
        )).scalar_one_or_none()

        product_count = (
            select(func.count(UserProduct.id))
            .where(UserProduct.user_id == User.id)
            .correlate(User)
            .scalar_subquery()
        )

        activation_sent = reminder_sent = expansion_sent = 0

        # --- Activation: 24h after signup, 0 products, not yet sent ---
        if tpl_activation:
            users = (await db.execute(
                select(User).where(
                    User.is_active == True,
                    User.is_verified == True,
                    User.vacation_mode == False,
                    User.created_at <= now - timedelta(hours=24),
                    User.automation_activation_sent_at == None,
                    product_count == 0,
                )
            )).scalars().all()

            s, _ = await _run_automation_flow(
                db, tpl_activation, "automation_activation", users, now,
                lambda u, ts: setattr(u, "automation_activation_sent_at", ts),
            )
            activation_sent = s

        # --- Reminder: 3 days after activation, still 0 products ---
        if tpl_activation:
            users = (await db.execute(
                select(User).where(
                    User.is_active == True,
                    User.is_verified == True,
                    User.vacation_mode == False,
                    User.automation_activation_sent_at != None,
                    User.automation_activation_sent_at <= now - timedelta(days=3),
                    User.automation_reminder_sent_at == None,
                    product_count == 0,
                )
            )).scalars().all()

            s, _ = await _run_automation_flow(
                db, tpl_activation, "automation_reminder", users, now,
                lambda u, ts: setattr(u, "automation_reminder_sent_at", ts),
            )
            reminder_sent = s

        # --- Expansion: 1-9 products, never sent or 30+ days ago ---
        if tpl_expansion:
            users = (await db.execute(
                select(User).where(
                    User.is_active == True,
                    User.is_verified == True,
                    User.vacation_mode == False,
                    product_count >= 1,
                    product_count <= 9,
                    or_(
                        User.automation_expansion_sent_at == None,
                        User.automation_expansion_sent_at <= now - timedelta(days=30),
                    ),
                )
            )).scalars().all()

            s, _ = await _run_automation_flow(
                db, tpl_expansion, "automation_expansion", users, now,
                lambda u, ts: setattr(u, "automation_expansion_sent_at", ts),
            )
            expansion_sent = s

        await db.commit()

    logger.info(
        f"=== Automation emails complete — activation: {activation_sent}, "
        f"reminder: {reminder_sent}, expansion: {expansion_sent} ==="
    )


async def check_single_product(asin: str, url: str):
    """Check a single product immediately (used after a user adds it)."""
    logger.info(f"[{asin}] Immediate first check triggered")
    try:
        results = await browser_manager.check_many([(asin, url)])
        check_result = results[0]
    except Exception as e:
        logger.error(f"[{asin}] Immediate check error: {e}")
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Product).where(Product.asin == asin))
        product = result.scalar_one_or_none()
        if not product:
            return
        try:
            await _update_product(db, product, check_result)
            logger.info(f"[{asin}] Immediate check → {check_result.status.value}")
        except Exception as e:
            logger.error(f"[{asin}] Immediate check save error: {e}")
            product.last_checked = datetime.now(timezone.utc)
            product.last_status = ShippingStatus.ERROR.value
            product.consecutive_errors += 1
            await db.commit()
