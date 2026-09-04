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
import httpx
import logging
import os
from datetime import datetime, timedelta, timezone
from backend.models import SystemSetting

from sqlalchemy import select, func, or_, update, text, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from backend.database import AsyncSessionLocal
from backend.models import Product, User, UserProduct, NotificationLog, EmailTemplate, EmailSendLog, EmailSendRecipient, EmailClick
from backend.checker import browser_manager, ShippingStatus, CheckResult, save_buybox_snapshot
from backend.notifier import send_daily_summary, _send_via_resend, _wrap_responsive, _open_pixel

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_ERRORS = 5


async def _take_and_save_screenshot(product_id: int, asin: str, html: str | None, raw_text: str):
    """Save a buybox snapshot for a FREE product and store the path in DB."""
    try:
        path = save_buybox_snapshot(asin, html or "", raw_text)
    except Exception as e:
        logger.error(f"[{asin}] _take_and_save_screenshot error: {e}", exc_info=True)
        return
    if path:
        async with AsyncSessionLocal() as db:
            product = await db.get(Product, product_id)
            if product:
                product.screenshot_path = path
                await db.commit()


async def cleanup_old_screenshots():
    """Delete screenshots older than 72 hours and clear their DB paths."""
    import time
    from backend.checker import BROWSER_PROFILE_DIR
    screenshots_dir = os.path.join(BROWSER_PROFILE_DIR, "screenshots")
    if not os.path.isdir(screenshots_dir):
        return
    cutoff = time.time() - 72 * 3600
    deleted = 0
    for fname in os.listdir(screenshots_dir):
        fpath = os.path.join(screenshots_dir, fname)
        try:
            if os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                deleted += 1
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(Product).where(Product.screenshot_path == fpath).values(screenshot_path=None)
                    )
                    await db.commit()
        except Exception as e:
            logger.warning(f"Screenshot cleanup error for {fname}: {e}")
    if deleted:
        logger.info(f"Screenshot cleanup: deleted {deleted} old file(s)")


async def _update_product(db: AsyncSession, product: Product, result: CheckResult) -> bool:
    """Update product in DB. Returns True if this is the product's first error (notify admin)."""
    product.last_checked = datetime.now(timezone.utc)

    if result.status in (ShippingStatus.FREE, ShippingStatus.PAID, ShippingStatus.NO_SHIP, ShippingStatus.NOT_FOUND):
        # Definitive result — update visible status
        if result.status == ShippingStatus.FREE and product.last_status != ShippingStatus.FREE.value:
            product.free_since = datetime.now(timezone.utc)
        elif result.status != ShippingStatus.FREE:
            product.free_since = None
        if result.status.value != product.last_status:
            product.status_since = datetime.now(timezone.utc)
        product.last_status = result.status.value
        product.raw_text = result.raw_text or ""
        product.found_in_aod = result.found_in_aod
        product.consecutive_errors = 0
        if result.product_name:
            product.name = result.product_name
        product.last_price = result.last_price  # '' clears stale buybox price
        # Israel extra cost: keep the previous value when this run couldn't read
        # #amazonGlobal_feature_div (absent block ≠ "no extra cost"). The one case we
        # must clear is a product that just turned FREE — a stale 'combined' figure
        # there would show the user a shipping fee they no longer pay.
        if result.israel_cost_kind:
            product.israel_extra_cost = result.israel_extra_cost
            product.israel_cost_kind = result.israel_cost_kind
        elif result.status == ShippingStatus.FREE:
            product.israel_extra_cost = None
            product.israel_cost_kind = None
        if result.image_url:
            product.image_url = result.image_url
        if result.amazon_category and not product.amazon_category:
            product.amazon_category = result.amazon_category
        await db.commit()
        if result.status == ShippingStatus.FREE:
            asyncio.create_task(_take_and_save_screenshot(product.id, product.asin, result.raw_html, result.raw_text))
        return False
    elif result.status == ShippingStatus.UNKNOWN:
        # Delivery text found but unclassifiable — update status visibly, not a scraping failure
        if result.status.value != product.last_status:
            product.status_since = datetime.now(timezone.utc)
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
    cycle_start = datetime.now(timezone.utc)
    logger.info("=== Check cycle started ===")
    from backend.checker import browser_manager
    cookies_count = len(browser_manager._session_cookies)
    if cookies_count:
        logger.info(f"Location refresh skipped — {cookies_count} session cookies already available")
    else:
        logger.warning("No session cookies — attempting location refresh via curl_cffi")
        loc_ok = await browser_manager.refresh_location()
        if not loc_ok:
            logger.warning("Location refresh failed — checks may show USD prices instead of ILS")
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            select(Product).where(
                Product.last_status != ShippingStatus.NOT_FOUND.value,
                Product.id.in_(
                    select(UserProduct.product_id)
                    .join(User, UserProduct.user_id == User.id)
                    .where(
                        User.is_active == True,
                        User.vacation_mode == False,
                        User.is_admin == False,
                        or_(
                            UserProduct.is_paused == False,
                            (UserProduct.paused_until != None) & (UserProduct.paused_until <= now),
                        ),
                    )
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
            elif product.last_status in ("PAID", "NO_SHIP") and product.status_since:
                # Alternating check schedule:
                # PAID:    7 days check / 7 days skip  (cycle=14)
                # NO_SHIP: 7 days check / 14 days skip (cycle=21)
                days = (now - product.status_since).days
                cycle = 14 if product.last_status == "PAID" else 21
                if days % cycle >= 7:
                    logger.debug(f"[{product.asin}] Skipping — {product.last_status} day {days % cycle}/{cycle} (rest phase)")
                else:
                    to_check.append(product)
            else:
                to_check.append(product)

        # Check all eligible products in parallel (httpx-first with Playwright fallback)
        if to_check:
            check_results = await browser_manager.check_many(
                [(p.asin, p.url) for p in to_check]
            )
            status_counts: dict = {}
            # A rollback expires every instance in the session (regardless of
            # expire_on_commit=False), so any later attribute access would fire a lazy
            # reload — illegal outside a greenlet, and fatal if the row was deleted.
            # Snapshot the identity of each product up front so failure handling never
            # has to touch an expired instance.
            identities = [(p.id, p.asin) for p in to_check]
            for i, (product, check_result) in enumerate(zip(to_check, check_results)):
                status_counts[check_result.status.value] = status_counts.get(check_result.status.value, 0) + 1
                asin = identities[i][1]
                try:
                    is_first_error = await _update_product(db, product, check_result)
                    if is_first_error:
                        newly_failed.append((product, check_result))
                    logger.info(f"[{i+1}/{len(to_check)}] [{asin}] → {check_result.status.value}")
                    continue
                except Exception as e:
                    # Product may have been deleted mid-cycle (e.g. admin bulk-delete)
                    deleted = isinstance(e, StaleDataError)
                    if deleted:
                        logger.warning(f"[{asin}] Product deleted mid-cycle, skipping.")
                    else:
                        logger.error(f"[{asin}] Unexpected error saving result: {e}")

                # --- failure path: rollback, then repopulate the expired instances ---
                try:
                    await db.rollback()
                    if not deleted:
                        # Bump the error counter by id — the ORM instance is expired here
                        await db.execute(
                            update(Product)
                            .where(Product.id == identities[i][0])
                            .values(consecutive_errors=Product.consecutive_errors + 1)
                        )
                        await db.commit()
                    # Eagerly reload every product in one query: the ones ahead of us so the
                    # next iterations don't lazy-load, and the ones behind us because
                    # newly_failed is consumed after this session closes.
                    await db.execute(
                        select(Product).where(Product.id.in_([pid for pid, _ in identities]))
                    )
                except Exception as inner:
                    # Error handling must never abort the whole cycle
                    logger.error(f"[{asin}] Error while recovering session: {inner}")
                    try:
                        await db.rollback()
                    except Exception:
                        pass

    if to_check:
        logger.info(f"=== Cycle result breakdown: {status_counts} | skipped={len(products)-len(to_check)} ===")

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

    duration = (datetime.now(timezone.utc) - cycle_start).total_seconds()
    logger.info(f"=== Check cycle complete in {duration/60:.1f} min ===")


async def run_daily_summary():
    """Send one daily summary email per user listing all their FREE products.

    Pre-step: auto-pause products free for 5+ days since last click (or free_since if never clicked).
    A click resets the 5-day countdown. Warning badges shown at days 3-4.
    """
    logger.info("=== Daily summary started ===")
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        cutoff_pause = now - timedelta(days=5)
        cutoff_warn  = now - timedelta(days=3)

        # Auto-pause: countdown = max(free_since, last_click_at). A click resets the 5-day window.
        auto_pause_rows = (await db.execute(text("""
            SELECT up.id, up.user_id, p.asin
            FROM user_products up
            JOIN products p ON p.id = up.product_id
            JOIN users u ON u.id = up.user_id
            WHERE up.is_paused = FALSE
              AND p.last_status = 'FREE'
              AND p.free_since IS NOT NULL
              AND u.is_active = TRUE
              AND u.is_admin = FALSE
              AND GREATEST(p.free_since, COALESCE(
                  (SELECT MAX(ec.clicked_at) FROM email_clicks ec
                   WHERE ec.user_id = up.user_id AND ec.asin = p.asin),
                  p.free_since
              )) <= :cutoff_pause
        """), {"cutoff_pause": cutoff_pause})).fetchall()

        auto_paused = 0
        for row in auto_pause_rows:
            up = (await db.execute(select(UserProduct).where(UserProduct.id == row[0]))).scalar_one_or_none()
            if up:
                up.is_paused = True
                up.paused_reason = "auto"
                auto_paused += 1
                logger.info(f"Auto-paused user_id={row[1]} asin={row[2]} (5+ days free, no click)")
        if auto_paused:
            await db.commit()

        users_result = await db.execute(
            select(User).where(
                User.is_active == True,
                User.vacation_mode == False,
                User.notify_email_bounced == False,
                User.is_admin == False,
            )
        )
        users = users_result.scalars().all()

        send_log = None
        recipients_buffer = []
        sent = 0

        for user in users:
            # Last click time per ASIN for this user (used to reset the 5-day countdown)
            click_rows = (await db.execute(
                select(EmailClick.asin, func.max(EmailClick.clicked_at).label("last_click"))
                .where(EmailClick.user_id == user.id)
                .group_by(EmailClick.asin)
            )).all()
            last_click_map = {r.asin: r.last_click for r in click_rows}

            free_products_result = await db.execute(
                select(Product, UserProduct.custom_name)
                .join(UserProduct, Product.id == UserProduct.product_id)
                .where(
                    UserProduct.user_id == user.id,
                    or_(
                        UserProduct.is_paused == False,
                        (UserProduct.paused_until != None) & (UserProduct.paused_until <= now),
                    ),
                    Product.last_status == ShippingStatus.FREE.value,
                )
            )
            free_products = free_products_result.all()

            if not free_products:
                continue

            # Build pause warnings: countdown starts from max(free_since, last_click)
            pause_warnings = {}
            for product, _ in free_products:
                if not product.free_since:
                    continue
                last_click = last_click_map.get(product.asin)
                countdown_start = max(product.free_since, last_click) if last_click else product.free_since
                if countdown_start <= cutoff_warn:
                    days_elapsed = (now - countdown_start).days
                    days_until_pause = max(1, 5 - days_elapsed)
                    pause_warnings[product.asin] = days_until_pause

            success = send_daily_summary(user, free_products, pause_warnings=pause_warnings)
            recipients_buffer.append((user, success))

            for product, _ in free_products:
                db.add(NotificationLog(
                    user_id=user.id,
                    product_id=product.id,
                    status=ShippingStatus.FREE.value,
                    email_to=user.notify_email,
                    success=success,
                    error_msg=None if success else "send failed",
                ))

            if success:
                sent += 1
                logger.info(f"[user {user.id}] Summary sent — {len(free_products)} free product(s), {len(pause_warnings)} with pause warning.")

            await db.commit()

        if recipients_buffer:
            failed_count = sum(1 for _, ok in recipients_buffer if not ok)
            send_log = EmailSendLog(
                template_id=None,
                template_name="daily_summary",
                sent_at=now,
                audience="all",
                sent_count=sent,
                failed_count=failed_count,
            )
            db.add(send_log)
            await db.flush()
            for user, ok in recipients_buffer:
                db.add(EmailSendRecipient(send_log_id=send_log.id, user_id=user.id, email=user.notify_email, success=ok))
            await db.commit()

    logger.info(f"=== Daily summary complete — {sent} email(s) sent, {auto_paused} product(s) auto-paused ===")


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
    """
    Two-phase inactivity check based on last email click (fallback: last_login_at).
    Phase 1 (days-15): send re-engagement warning email.
    Phase 2 (days):    move to vacation_mode.
    """
    from backend.models import SystemSetting, EmailClick
    from backend.vacation import enter_vacation
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(SystemSetting).where(SystemSetting.key == "inactivity_days")
        )).scalar_one_or_none()
        days = int(row.value) if row else 90
        if days <= 0:
            return  # disabled

        now = datetime.now(timezone.utc)
        vacation_cutoff = now - timedelta(days=days)
        warning_cutoff  = now - timedelta(days=days - 15)

        # Last email click per user
        clicks_result = await db.execute(
            select(EmailClick.user_id, func.max(EmailClick.clicked_at).label("last_click"))
            .group_by(EmailClick.user_id)
        )
        last_clicks = {r.user_id: r.last_click for r in clicks_result.all()}

        users = (await db.execute(
            select(User).where(
                User.is_active == True,
                User.vacation_mode == False,
                User.is_admin == False,
                User.notify_email_bounced == False,
            )
        )).scalars().all()

        tpl = (await db.execute(
            select(EmailTemplate).where(EmailTemplate.name == "לקוח לא פעיל - האם אתה עדיין פה?")
        )).scalar_one_or_none()

        to_vacation, to_warn, to_reset = [], [], []

        for user in users:
            last_click = last_clicks.get(user.id)
            candidates = [dt for dt in [user.last_login_at, last_click] if dt is not None]
            if not candidates:
                continue  # no activity data yet
            last_activity = max(candidates)

            # If user clicked after the warning was sent → they re-engaged, reset flag
            if user.automation_reengagement_sent_at and last_click and last_click > user.automation_reengagement_sent_at:
                to_reset.append(user)

            if last_activity < vacation_cutoff:
                to_vacation.append(user)
            elif last_activity < warning_cutoff and user.automation_reengagement_sent_at is None:
                to_warn.append(user)

        for user in to_reset:
            user.automation_reengagement_sent_at = None

        for user in to_vacation:
            await enter_vacation(db, user, auto=True)
            user.automation_reengagement_sent_at = None
            logger.info(f"[inactivity] User {user.id} → vacation_mode (inactive {days}+ days)")

        if tpl and to_warn:
            sent, _ = await _run_automation_flow(
                db, tpl, "automation_reengagement", to_warn, now,
                lambda u, ts: setattr(u, "automation_reengagement_sent_at", ts),
            )
            logger.info(f"[inactivity] Re-engagement warning sent to {sent} user(s)")

        await db.commit()
        logger.info(f"=== Inactivity check: {len(to_vacation)} → vacation, {len(to_warn)} warned, {len(to_reset)} reset ===")


def _auto_substitute(text: str, user: User, product_count: int = 0, label: str = "cta") -> str:
    from backend.notifier import _pause_url
    base_url = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    dashboard_url = f"{base_url}/dashboard"
    tracked_dashboard = f"{base_url}/track/click?u={user.id}&a={label}&url={dashboard_url}"
    return (text
        .replace("{{email}}", user.notify_email)
        .replace("{{pause_url}}", _pause_url(user.id))
        .replace("{{product_count}}", str(product_count))
        .replace(f'href="{dashboard_url}"', f'href="{tracked_dashboard}"')
    )


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
        count = (await db.execute(
            select(func.count(UserProduct.id)).where(UserProduct.user_id == u.id)
        )).scalar() or 0
        raw_body = _auto_substitute(tpl.body, u, count, label=audience)
        wrapped = _wrap_responsive(raw_body, is_rtl=True)
        wrapped = wrapped.replace("</body>", f"{_open_pixel(u.id, tpl.name, tpl.id)}\n</body>")
        ok = _send_via_resend(
            u.notify_email,
            _auto_substitute(tpl.subject, u, count, label=audience),
            wrapped,
            "",
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


WINBACK_DAILY_CAP = 25
WINBACK_REPEAT_DAYS = 180


async def run_winback_emails():
    """Wake auto-parked users. Runs daily at 11:00 IL, after the summary has gone out.

    An auto-vacation user is excluded from the daily summary, from the inactivity
    check and from the re-engagement warning, so they receive nothing at all — and
    the only two things that pull them back out, a login or an email click, both
    require them to hear from us first. This is the one message that reaches them.

    Capped at WINBACK_DAILY_CAP a day: these addresses have been silent for months,
    and dumping the whole backlog at once is exactly the pattern that costs a domain
    its reputation. Manual vacation is never touched — they asked to be left alone.
    """
    logger.info("=== Win-back emails started ===")
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        tpl = (await db.execute(
            select(EmailTemplate).where(EmailTemplate.name == "לקוח בחופשה - החזרה")
        )).scalar_one_or_none()
        if not tpl:
            logger.warning("=== Win-back: template missing, nothing sent ===")
            return

        users = (await db.execute(
            select(User).where(
                User.vacation_mode == True,
                User.vacation_auto == True,
                User.is_active == True,
                User.is_verified == True,
                User.is_admin == False,
                User.notify_email_bounced == False,
                or_(
                    User.automation_winback_sent_at == None,
                    User.automation_winback_sent_at <= now - timedelta(days=WINBACK_REPEAT_DAYS),
                ),
            )
            # Never-contacted first, then whoever went quiet most recently — the
            # warmest addresses go out while the sending reputation is still fresh.
            .order_by(User.automation_winback_sent_at.asc().nulls_first(),
                      User.last_login_at.desc().nulls_last())
            .limit(WINBACK_DAILY_CAP)
        )).scalars().all()

        sent, failed = await _run_automation_flow(
            db, tpl, "automation_winback", users, now,
            lambda u, ts: setattr(u, "automation_winback_sent_at", ts),
        )
        await db.commit()

    logger.info(f"=== Win-back emails complete — sent: {sent}, failed: {failed} ===")


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
                    User.is_admin == False,
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
                    User.is_admin == False,
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
                    User.is_admin == False,
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



async def send_telegram(message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": int(chat_id), "text": message},
            )
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


async def run_telegram_report():
    """Daily Telegram status report — mirrors the GitHub Actions railway-monitor workflow.
    Queries Railway GraphQL for deployments + log errors in the last 24h."""
    logger.info("=== Telegram report started ===")

    # RAILWAY_TOKEN (auto-injected project token) can't call the GraphQL API —
    # deployments/environmentLogs require an account-level Personal Access Token.
    railway_token = os.environ.get("RAILWAY_API_TOKEN", "")
    project_id = os.environ.get("RAILWAY_PROJECT_ID", "")
    service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
    env_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
    railway_url = "https://backboard.railway.app/graphql/v2"

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=24)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    current_time = now.strftime("%Y-%m-%d %H:%M UTC")

    if not railway_token:
        await send_telegram(f"Railway Monitor - aware-wisdom\n\nStatus: ERROR\nFailed: RAILWAY_API_TOKEN not set\nTime: {current_time}")
        return

    headers = {
        "Authorization": f"Bearer {railway_token}",
        "Content-Type": "application/json",
    }

    def _unwrap(resp: httpx.Response, field: str):
        body = resp.json() or {}
        if body.get("errors"):
            raise RuntimeError(f"Railway GraphQL error on {field}: {body['errors']}")
        return (body.get("data") or {}).get(field)

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            # Deployments in last 24h
            dep_resp = await client.post(railway_url, headers=headers, json={"query":
                f'{{ deployments(input: {{ projectId: "{project_id}", serviceId: "{service_id}" }}) '
                f'{{ edges {{ node {{ id status createdAt }} }} }} }}'
            })
            deployments = (_unwrap(dep_resp, "deployments") or {}).get("edges", [])
            recent = [d["node"] for d in deployments
                      if datetime.fromisoformat(d["node"]["createdAt"].replace("Z", "+00:00")) >= since]
            failed = [d for d in recent if d["status"] in ("FAILED", "CRASHED")]

            # Fetch logs with pagination
            all_logs = []
            after_date = since_str
            while True:
                log_resp = await client.post(railway_url, headers=headers, json={"query":
                    f'{{ environmentLogs(environmentId: "{env_id}", afterDate: "{after_date}", beforeLimit: 500) '
                    f'{{ message severity timestamp }} }}'
                })
                page = _unwrap(log_resp, "environmentLogs") or []
                if not page:
                    break
                all_logs.extend(page)
                if len(page) < 500:
                    break
                last_ts = page[-1].get("timestamp", "")
                if not last_ts or last_ts == after_date:
                    break
                after_date = last_ts

            # Filter to last 24h only
            all_logs = [l for l in all_logs if l.get("timestamp") and
                        datetime.fromisoformat(l["timestamp"].replace("Z", "+00:00")) >= since]

            keywords = ["error", "fatal", "crash", "exception", "unhandled", "timeout"]
            errors_found = [l.get("message", "")[:120] for l in all_logs
                            if any(k in l.get("message", "").lower() for k in keywords)
                            and "NO_SHIP" not in l.get("message", "")]

        # DB health checks
        db_issues = []
        try:
            async with AsyncSessionLocal() as db:
                # Check for USD prices (location/cookie issue)
                # Valid ILS prices start with either ₪ or "ILS" — flag anything else
                usd_rows = (await db.execute(
                    select(Product.asin, Product.last_price)
                    .where(
                        Product.last_price != "",
                        ~Product.last_price.startswith("₪"),
                        ~Product.last_price.startswith("ILS"),
                    )
                    .limit(5)
                )).all()
                if usd_rows:
                    items = ", ".join(f"{r.asin}={r.last_price}" for r in usd_rows)
                    db_issues.append(f"⚠️ מחירי USD (בעיית cookies): {items}")
                else:
                    db_issues.append("✅ כל המחירים בשקלים")

                # Check for products with consecutive errors
                err_rows = (await db.execute(
                    select(Product.asin, Product.consecutive_errors)
                    .where(Product.consecutive_errors > 0)
                    .order_by(Product.consecutive_errors.desc())
                    .limit(5)
                )).all()
                if err_rows:
                    items = ", ".join(f"{r.asin}({r.consecutive_errors}x)" for r in err_rows)
                    db_issues.append(f"⚠️ שגיאות רצופות: {items}")
                else:
                    db_issues.append("✅ אין שגיאות רצופות")
        except Exception as db_err:
            db_issues.append(f"⚠️ DB check failed: {db_err}")

        has_db_issues = any(line.startswith("⚠️") for line in db_issues)
        db_section = "DB Health:\n" + "\n".join(db_issues)

        has_warning = bool(failed or errors_found or has_db_issues)
        status = "WARNING" if has_warning else "OK"

        if not has_warning:
            message = (
                f"Railway Monitor - aware-wisdom\n\n"
                f"Status: {status}\n"
                f"Period: Last 24 hours\n"
                f"Total logs checked: {len(all_logs)}\n"
                f"Deployments: {len(recent)} total, 0 failed\n"
                f"Errors in logs: None\n"
                f"{db_section}\n"
                f"Time: {current_time}"
            )
        else:
            top_errors = "\n".join(errors_found[:5]) if errors_found else "None"
            message = (
                f"Railway Monitor - aware-wisdom\n\n"
                f"Status: {status}\n"
                f"Period: Last 24 hours\n"
                f"Total logs checked: {len(all_logs)}\n"
                f"Deployments: {len(recent)} total, {len(failed)} failed\n"
                f"Errors in logs: {len(errors_found)}\n"
                f"Top issues:\n{top_errors}\n"
                f"{db_section}\n"
                f"Time: {current_time}"
            )

        await send_telegram(message)
        logger.info(f"=== Telegram report sent — {len(all_logs)} logs checked, {len(errors_found)} errors, db_issues={has_db_issues} ===")

    except Exception as e:
        msg = f"Railway Monitor - aware-wisdom\n\nStatus: ERROR\nFailed: {str(e)}\nTime: {current_time}"
        await send_telegram(msg)
        logger.error(f"Telegram report error: {e}")


_CATEGORY_MAP = {
    "Electronics": ("🔌", "אלקטרוניקה"), "Computers": ("💻", "מחשבים"),
    "Camera": ("📷", "מצלמות"), "Home": ("🏠", "בית"), "Kitchen": ("🍳", "מטבח"),
    "Sports": ("⚽", "ספורט"), "Toys": ("🧸", "צעצועים"), "Beauty": ("💄", "יופי"),
    "Health": ("💊", "בריאות"), "Clothing": ("👕", "ביגוד"), "Books": ("📚", "ספרים"),
    "Automotive": ("🚗", "רכב"), "Garden": ("🌿", "גינה"), "Pet": ("🐾", "חיות מחמד"),
    "Office": ("🖊️", "משרד"), "Tools": ("🔧", "כלים"),
}
_TELEGRAM_RESEND_DAYS = 7
_RTL = "‏"


def _get_image_urls(product: Product) -> list[str]:
    """Return up to 4 image URLs for a product. Falls back to single image_url."""
    import json as _json
    if product.image_urls:
        try:
            urls = _json.loads(product.image_urls)
            if isinstance(urls, list) and urls:
                return urls[:4]
        except Exception:
            pass
    return [product.image_url] if product.image_url else []


def _format_price(raw: str | None) -> str:
    """Normalize Amazon price string to 'X.XX ש"ח' format."""
    p = (raw or "").strip()
    p = p.replace("ILS", "").replace("₪", "").strip()
    return f'{p} ש"ח' if p else ""


def _escape_md(text: str) -> str:
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, f"\\{ch}")
    return text


# ---------------------------------------------------------------------------
# Dynamic marketing hook — one Claude-generated opening line per product,
# so social posts don't all look like the same rigid template (kills reach).
# ---------------------------------------------------------------------------

_anthropic_client = None
_HOOK_CACHE: dict[str, str] = {}  # asin -> hook, so FB + Telegram reuse the same line

_HOOK_SYSTEM = (
    "אתה קופירייטר ישראלי שכותב שורת פתיח (hook) לפוסט מכירתי בפייסבוק/טלגרם. "
    "מקבל שם מוצר בעברית, מחזיר שורה אחת קצרה שעוצרת את הגלילה.\n\n"
    "כללים נוקשים:\n"
    "- שורה אחת בלבד, עד 10 מילים, עברית טבעית של דובר-שפת-אם\n"
    "- אמוג'י אחד בלבד בסוף השורה\n"
    "- התבסס אך ורק על שם המוצר. אל תמציא נתונים\n"
    "- אסור לחלוטין: מחירים, אחוזי הנחה, 'מבצע נגמר', דד-ליין, דירוגים, "
    "'כולם קונים', 'הכי נמכר', כמויות שאזלו — כל טענה שלא נובעת מהשם\n"
    "- זווית מותרת: תועלת/בעיה-פתרון/סקרנות שנגזרת ישירות מהמוצר\n"
    "- בלי מרכאות, בלי הקדמות, בלי הסבר — רק שורת הפתיח\n"
)


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return None
        try:
            import anthropic
            _anthropic_client = anthropic.Anthropic(api_key=api_key)
        except Exception as e:
            logger.warning(f"[hook] anthropic client error: {e}")
    return _anthropic_client


def _product_hook(product: Product) -> str:
    """Generate a single dynamic opening line for a product. Non-blocking:
    returns '' on any failure (caption then falls back to no hook)."""
    asin = product.asin
    if asin in _HOOK_CACHE:
        return _HOOK_CACHE[asin]

    name_he = product.name_he or product.name or ""
    if not name_he:
        return ""
    client = _get_anthropic_client()
    if not client:
        return ""
    try:
        category = product.amazon_category or ""
        msg = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            thinking={"type": "disabled"},
            system=_HOOK_SYSTEM,
            messages=[{"role": "user", "content": f"מוצר: {name_he}\nקטגוריה: {category}"}],
        )
        parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        hook = "".join(parts).strip()
        # QA — שורה אחת, בלי מרכאות עוטפות, אורך שפוי, חייב עברית
        hook = hook.splitlines()[0].strip().strip('"').strip("'") if hook else ""
        if len(hook) > 90 or not any('א' <= c <= 'ת' for c in hook):
            hook = ""
        _HOOK_CACHE[asin] = hook
        return hook
    except Exception as e:
        logger.warning(f"[hook] generation failed for {asin}: {e}")
        return ""


def _telegram_caption(product: Product) -> str:
    tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    url = f"https://www.amazon.com/dp/{product.asin}?tag={tag}" if tag else f"https://www.amazon.com/dp/{product.asin}"
    name_he = _escape_md(product.name_he or product.name or product.asin)
    price = _format_price(product.last_price)
    category = product.amazon_category or ""
    cat_emoji, cat_he = next(
        (v for k, v in _CATEGORY_MAP.items() if k.lower() in category.lower()),
        ("📦", category or "כללי"),
    )
    today = datetime.now().strftime("%d/%m/%Y")
    description = product.description or ""
    all_bullets = [f"{_RTL}• {_escape_md(b)}" for b in description.splitlines() if b.strip()]

    footer_lines = [
        f"{_RTL}--",
        "",
        f"{_RTL}💰 מחיר: *{price}*",
        f"{_RTL}ℹ️ משלוח חינם מותנה בהזמנה מינימלית של $49 — ניתן לצרף מוצרים נוספים",
        f"{_RTL}🚚 משלוח חינם לישראל 🇮🇱",
        f"{_RTL}📅 נכון ל-{today}. המחירים משתנים — בדקו לפני רכישה.",
        "",
        f"[👉 לרכישה באמזון]({url})",
        "",
        f"{_RTL}📘 הצטרף לדף הפייסבוק → https://www.facebook.com/AmzFreeIL",
        "",
        f"{_RTL}📢 @amzfreeil",
    ]
    hook = _product_hook(product)
    header_lines = [f"{_RTL}✈️ משלוח חינם לישראל | {cat_emoji} {cat_he}", ""]
    if hook:
        header_lines += [f"{_RTL}{_escape_md(hook)}", ""]
    header_lines += [f"{_RTL}*{name_he}*", ""]

    base = "\n".join(header_lines) + "\n".join(footer_lines)
    kept = []
    for bullet in all_bullets[:5]:
        candidate = "\n".join(header_lines) + "\n".join(kept + [bullet, ""]) + "\n".join(footer_lines)
        if len(candidate) <= 1024:
            kept.append(bullet)
        else:
            break

    lines = header_lines[:]
    if kept:
        lines += kept
        lines.append("")
    lines += footer_lines
    return "\n".join(lines)


async def _send_telegram_product_message(product: Product) -> bool:
    token = os.environ.get("TELEGRAM_PRODUCT_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_PRODUCT_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_PRODUCT_BOT_TOKEN or TELEGRAM_PRODUCT_CHAT_ID not set — skipping product send")
        return False
    caption = await asyncio.to_thread(_telegram_caption, product)
    image_url = product.image_url
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if image_url:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "photo": image_url,
                          "caption": caption, "parse_mode": "Markdown"},
                )
            else:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": caption, "parse_mode": "Markdown"},
                )
        if resp.status_code == 200:
            return True
        logger.warning(f"[telegram_product] send failed for {product.asin}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"[telegram_product] send error for {product.asin}: {e}")
        return False


_POST_VERIFY_ATTEMPTS = 3


async def _pick_verified_free_product(db: AsyncSession, sent_recently, channel: str) -> Product | None:
    """Draw a random eligible scanner product and re-check it live before posting.

    `last_status` is only as fresh as the last scanner run — and scanner products
    nobody tracks never enter the global check cycle, so a FREE row can be days
    old. A wrong post costs a paid impression and the channel's credibility, so
    verify first: on a non-FREE result save the new status (which also drops the
    product out of the eligible pool) and draw another. On ERROR/UNKNOWN there is
    no answer, only the absence of one — skip the candidate rather than post blind.
    Returns None when no candidate verified; the caller skips the slot.
    """
    tried: set[str] = set()
    for _ in range(_POST_VERIFY_ATTEMPTS):
        query = select(Product).where(
            Product.last_status == "FREE",
            Product.source == "scanner",
            Product.asin.not_in(sent_recently),
        )
        if tried:
            query = query.where(Product.asin.not_in(tried))
        product = (
            await db.execute(query.order_by(func.random()).limit(1))
        ).scalar_one_or_none()
        if not product:
            return None

        # Snapshot the identity: a rollback below expires the instance, and any
        # later attribute access would fire a lazy reload from async context.
        asin, url = product.asin, product.url
        tried.add(asin)

        try:
            check_result = (await browser_manager.check_many([(asin, url)]))[0]
        except Exception as e:
            logger.warning(f"[{channel}] verify failed for {asin}: {e} — trying another")
            continue

        try:
            await _update_product(db, product, check_result)
        except Exception as e:
            logger.error(f"[{channel}] verify save error for {asin}: {e}")
            try:
                await db.rollback()
            except Exception:
                pass
            continue

        # The live check may have flipped the status either way; drop the public
        # cache so free-products.html matches what we are about to claim in the post.
        try:
            from backend.main import invalidate_free_products_cache
            invalidate_free_products_cache()
        except Exception:
            pass

        if check_result.status == ShippingStatus.FREE:
            return product
        logger.info(f"[{channel}] {asin} no longer FREE ({check_result.status.value}) — trying another")

    logger.warning(
        f"[{channel}] no verified FREE product after {_POST_VERIFY_ATTEMPTS} attempts — skipping slot"
    )
    return None


async def run_send_telegram_product():
    """Send one free product to the Telegram channel.

    Runs on the fixed cron times in TELEGRAM_PRODUCT_POST_TIMES (IL). Skips
    ASINs sent in the last 7 days.
    """
    if os.environ.get("TELEGRAM_PRODUCT_ENABLED", "true").lower() == "false":
        logger.info("[telegram_product] disabled via TELEGRAM_PRODUCT_ENABLED=false — skipping")
        return

    import pytz
    _il_tz = pytz.timezone("Asia/Jerusalem")
    _il_hour = datetime.now(_il_tz).hour
    if not (6 <= _il_hour < 22):
        logger.info(f"[telegram_product] outside active hours (hour={_il_hour}) — skipping")
        return

    from backend.models import TelegramSent
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    logger.info("=== Telegram product send started ===")
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        resend_cutoff = now - timedelta(days=_TELEGRAM_RESEND_DAYS)

        # Products eligible: FREE + scanner source + not sent recently, then
        # re-checked live — see _pick_verified_free_product.
        sent_recently = (
            select(TelegramSent.asin)
            .where(TelegramSent.sent_at > resend_cutoff)
            .scalar_subquery()
        )
        product = await _pick_verified_free_product(db, sent_recently, "telegram_product")

        if not product:
            logger.info("=== Telegram product send: no eligible products ===")
            return

        ok = await _send_telegram_product_message(product)
        if ok:
            stmt = pg_insert(TelegramSent).values(asin=product.asin, sent_at=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=["asin"],
                set_={"sent_at": now},
            )
            await db.execute(stmt)
            await db.commit()
            logger.info(f"=== Telegram product sent: {product.asin} — {product.name_he or product.name} ===")
        else:
            logger.warning(f"=== Telegram product send failed: {product.asin} ===")


_FACEBOOK_RESEND_DAYS = 7


def _facebook_caption(product: Product, channel: str = "facebook") -> str:
    url = f"https://app.amzfreeil.com/go/{product.asin}"
    name_he = product.name_he or product.name or product.asin
    price = _format_price(product.last_price)
    category = product.amazon_category or ""
    cat_emoji, cat_he = next(
        (v for k, v in _CATEGORY_MAP.items() if k.lower() in category.lower()),
        ("📦", category or "כללי"),
    )
    today = datetime.now().strftime("%d/%m/%Y")
    description = product.description or ""
    all_bullets = [f"{_RTL}• {b}" for b in description.splitlines() if b.strip()]

    hook = _product_hook(product)
    header_lines = [f"{_RTL}✈️ משלוח חינם לישראל | {cat_emoji} {cat_he}", ""]
    if hook:
        header_lines += [f"{_RTL}{hook}", ""]
    header_lines += [f"{_RTL}{name_he}", ""]
    # Instagram never renders a URL in a feed caption as a tappable link, so the
    # Facebook CTA is dead text there. Point at the bio link (the only clickable
    # one) and at the live free-products page instead — the picker verifies the
    # product against Amazon right before posting, so the claim holds at post time.
    if channel == "instagram":
        cta_lines = [
            f"{_RTL}✅ המוצר מופיע ברשימת המוצרים עם משלוח חינם — נכון ל-{today}",
            f"{_RTL}🔗 לרכישה ולעוד עשרות מוצרים: בביו ☝️ → \"מוצרים במשלוח חינם\"",
            f"{_RTL}www.amzfreeil.com/free-products.html",
            "",
            f"{_RTL}📱 כל המוצרים גם בטלגרם → t.me/amzfreeil",
        ]
    else:
        cta_lines = [
            f"{_RTL}👉 לרכישה באמזון: {url}",
            "",
            f"{_RTL}📱 יש עוד הרבה מוצרים שלא מגיעים לפה — כולם בטלגרם → t.me/amzfreeil",
        ]

    footer_lines = [
        f"{_RTL}--",
        "",
        f"{_RTL}💰 מחיר: {price}",
        f"{_RTL}ℹ️ משלוח חינם מותנה בהזמנה מינימלית של $49 — ניתן לצרף מוצרים נוספים",
        f"{_RTL}🚚 משלוח חינם לישראל 🇮🇱",
        f"{_RTL}📅 נכון ל-{today}. המחירים משתנים — בדקו לפני רכישה.",
        "",
        *cta_lines,
        "",
        f"{_RTL}📢 AMZ Free Ship Alert",
    ]

    lines = header_lines[:]
    if all_bullets:
        lines += all_bullets
        lines.append("")
    lines += footer_lines
    return "\n".join(lines)


async def _get_facebook_page_token(user_token: str, page_id: str) -> str | None:
    """Get Page Access Token from System User Token via /me/accounts."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://graph.facebook.com/v19.0/me/accounts",
                params={"access_token": user_token},
            )
        data = resp.json()
        pages = data.get("data", [])
        if not pages:
            logger.warning(f"[facebook] /me/accounts returned no pages: {data}")
            return None
        # Find matching page or use first
        for page in pages:
            if str(page.get("id")) == str(page_id):
                return page.get("access_token")
        # fallback: use first page token
        logger.warning(f"[facebook] page_id {page_id} not found in accounts, using first: {pages[0].get('id')}")
        return pages[0].get("access_token")
    except Exception as e:
        logger.warning(f"[facebook] page token exchange error: {e}")
        return None


async def _send_facebook_product_message(product: Product) -> bool:
    user_token = os.environ.get("FACEBOOK_PAGE_TOKEN", "")
    page_id = os.environ.get("FACEBOOK_PAGE_ID", "")
    if not user_token or not page_id:
        logger.warning("FACEBOOK_PAGE_TOKEN or FACEBOOK_PAGE_ID not set — skipping Facebook send")
        return False

    page_token = await _get_facebook_page_token(user_token, page_id)
    if not page_token:
        logger.warning("[facebook] could not obtain page access token — skipping")
        return False

    caption = await asyncio.to_thread(_facebook_caption, product)
    import json as _json
    secondary = []
    if product.image_urls:
        try:
            parsed = _json.loads(product.image_urls)
            if isinstance(parsed, list):
                secondary = parsed
        except Exception:
            pass
    image_urls = ([product.image_url] if product.image_url else []) + secondary
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if len(image_urls) > 1:
                # Upload each photo unpublished, collect IDs, then post as multi-image feed
                photo_ids = []
                for img_url in image_urls:
                    upload_resp = await client.post(
                        f"https://graph.facebook.com/v19.0/{page_id}/photos",
                        data={"url": img_url, "published": "false", "access_token": page_token},
                    )
                    if upload_resp.status_code == 200:
                        pid = upload_resp.json().get("id")
                        if pid:
                            photo_ids.append(pid)
                if not photo_ids:
                    logger.warning(f"[facebook] no photos uploaded for {product.asin}")
                    return False
                attached = [{"media_fbid": pid} for pid in photo_ids]
                resp = await client.post(
                    f"https://graph.facebook.com/v19.0/{page_id}/feed",
                    json={"message": caption, "attached_media": attached, "access_token": page_token},
                )
            elif image_urls:
                resp = await client.post(
                    f"https://graph.facebook.com/v19.0/{page_id}/photos",
                    data={"url": image_urls[0], "caption": caption, "access_token": page_token},
                )
            else:
                resp = await client.post(
                    f"https://graph.facebook.com/v19.0/{page_id}/feed",
                    data={"message": caption, "access_token": page_token},
                )
        if resp.status_code == 200:
            return True
        logger.warning(f"[facebook] send failed for {product.asin}: {resp.text[:300]}")
        return False
    except Exception as e:
        logger.warning(f"[facebook] send error for {product.asin}: {e}")
        return False


async def run_send_facebook_product():
    """Send one free product to the Facebook Page. Runs once per entry in
    FACEBOOK_PRODUCT_POST_TIMES (IL). Skips ASINs sent in the last 7 days."""
    if os.environ.get("FACEBOOK_PRODUCT_ENABLED", "true").lower() == "false":
        logger.info("[facebook] disabled via FACEBOOK_PRODUCT_ENABLED=false — skipping")
        return

    from backend.models import FacebookSent
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    logger.info("=== Facebook product send started ===")
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        resend_cutoff = now - timedelta(days=_FACEBOOK_RESEND_DAYS)

        sent_recently = (
            select(FacebookSent.asin)
            .where(FacebookSent.sent_at > resend_cutoff)
            .scalar_subquery()
        )
        product = await _pick_verified_free_product(db, sent_recently, "facebook")

        if not product:
            logger.info("=== Facebook product send: no eligible products ===")
            return

        ok = await _send_facebook_product_message(product)
        if ok:
            stmt = pg_insert(FacebookSent).values(asin=product.asin, sent_at=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=["asin"],
                set_={"sent_at": now},
            )
            await db.execute(stmt)
            await db.commit()
            logger.info(f"=== Facebook product sent: {product.asin} — {product.name_he or product.name} ===")
        else:
            logger.warning(f"=== Facebook product send failed: {product.asin} ===")


_INSTAGRAM_RESEND_DAYS = 7


async def _send_instagram_product_message(product: Product) -> bool:
    """Publish a product to Instagram via the two-step Graph API flow:
    create a media container, then publish it. Reuses the same System User /
    Page token as Facebook — Instagram permissions ride on the same token."""
    user_token = os.environ.get("FACEBOOK_PAGE_TOKEN", "")
    page_id = os.environ.get("FACEBOOK_PAGE_ID", "")
    if not user_token or not page_id:
        logger.warning("FACEBOOK_PAGE_TOKEN or FACEBOOK_PAGE_ID not set — skipping Instagram send")
        return False

    ig_user_id = os.environ.get("INSTAGRAM_BUSINESS_ID", "17841431920060212")

    if not product.image_url:
        logger.warning(f"[instagram] {product.asin} has no image_url — Instagram requires media, skipping")
        return False

    page_token = await _get_facebook_page_token(user_token, page_id)
    if not page_token:
        logger.warning("[instagram] could not obtain page access token — skipping")
        return False

    caption = await asyncio.to_thread(_facebook_caption, product, "instagram")
    app_base_url = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    ig_image_url = f"{app_base_url}/ig-image/{product.asin}.jpg"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            create_resp = await client.post(
                f"https://graph.facebook.com/v19.0/{ig_user_id}/media",
                data={"image_url": ig_image_url, "caption": caption, "access_token": page_token},
            )
            if create_resp.status_code != 200:
                logger.warning(f"[instagram] media create failed for {product.asin}: {create_resp.text[:300]}")
                return False
            creation_id = create_resp.json().get("id")
            if not creation_id:
                logger.warning(f"[instagram] media create returned no id for {product.asin}: {create_resp.text[:300]}")
                return False

            # Instagram processes the container asynchronously (image fetch + transcode) —
            # publishing before status_code=FINISHED fails with "Media ID is not available".
            for _ in range(5):
                status_resp = await client.get(
                    f"https://graph.facebook.com/v19.0/{creation_id}",
                    params={"fields": "status_code", "access_token": page_token},
                )
                status = status_resp.json().get("status_code") if status_resp.status_code == 200 else None
                if status == "FINISHED":
                    break
                if status == "ERROR":
                    logger.warning(f"[instagram] media processing errored for {product.asin}: {status_resp.text[:300]}")
                    return False
                await asyncio.sleep(2)

            publish_resp = await client.post(
                f"https://graph.facebook.com/v19.0/{ig_user_id}/media_publish",
                data={"creation_id": creation_id, "access_token": page_token},
            )
        if publish_resp.status_code == 200:
            return True
        logger.warning(f"[instagram] media_publish failed for {product.asin}: {publish_resp.text[:300]}")
        return False
    except Exception as e:
        logger.warning(f"[instagram] send error for {product.asin}: {e}")
        return False


async def run_send_instagram_product():
    """Send one free product to Instagram (@amzfreeil). Runs once per entry in
    INSTAGRAM_PRODUCT_POST_TIMES (IL). Skips ASINs sent in the last 7 days."""
    if os.environ.get("INSTAGRAM_PRODUCT_ENABLED", "true").lower() == "false":
        logger.info("[instagram] disabled via INSTAGRAM_PRODUCT_ENABLED=false — skipping")
        return

    from backend.models import InstagramSent
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    logger.info("=== Instagram product send started ===")
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)
        resend_cutoff = now - timedelta(days=_INSTAGRAM_RESEND_DAYS)

        sent_recently = (
            select(InstagramSent.asin)
            .where(InstagramSent.sent_at > resend_cutoff)
            .scalar_subquery()
        )
        product = await _pick_verified_free_product(db, sent_recently, "instagram")

        if not product:
            logger.info("=== Instagram product send: no eligible products ===")
            return

        ok = await _send_instagram_product_message(product)
        if ok:
            stmt = pg_insert(InstagramSent).values(asin=product.asin, sent_at=now)
            stmt = stmt.on_conflict_do_update(
                index_elements=["asin"],
                set_={"sent_at": now},
            )
            await db.execute(stmt)
            await db.commit()
            logger.info(f"=== Instagram product sent: {product.asin} — {product.name_he or product.name} ===")
        else:
            logger.warning(f"=== Instagram product send failed: {product.asin} ===")


def _blog_telegram_caption(
    title: str, slug: str, amazon_price: float | None, israel_price: float | None,
    kind: str = "review",
) -> str:
    """Caption for a blog announcement. `kind="guide"` drops every price-related
    line — an editorial guide has no product, no price and no $49 threshold."""
    from backend.blog_utils import strip_tags

    guide = kind == "guide"
    url = f"https://www.amzfreeil.com/blog/{slug}.html"
    name_he = _escape_md(strip_tags(title))
    price = "" if guide else (_format_price(str(amazon_price)) if amazon_price else "")
    today = datetime.now().strftime("%d/%m/%Y")

    kicker = "📚 מדריך חדש בבלוג" if guide else "📝 סקירה חדשה בבלוג"
    header_lines = [
        f"{_RTL}✈️ משלוח חינם לישראל | {kicker}",
        "",
        f"{_RTL}*{name_he}*",
        "",
    ]
    footer_lines = [f"{_RTL}--", ""]
    if price:
        footer_lines.append(f"{_RTL}💰 מחיר: *{price}*")
    if not guide and israel_price and amazon_price:
        savings = round(israel_price - amazon_price)
        footer_lines.append(f"{_RTL}💸 מחיר מקביל בישראל: ~₪{israel_price:g} — חיסכון של ~₪{savings}")
    if not guide:
        footer_lines.append(f"{_RTL}ℹ️ משלוח חינם מותנה בהזמנה מינימלית של $49 — ניתן לצרף מוצרים נוספים")
    footer_lines.append(f"{_RTL}🚚 משלוח חינם לישראל 🇮🇱")
    if not guide:
        footer_lines.append(f"{_RTL}📅 נכון ל-{today}. המחירים משתנים — בדקו לפני רכישה.")
    cta = "לקריאת המדריך המלא" if guide else "לקריאת הסקירה המלאה"
    footer_lines += [
        "",
        f"[👉 {cta}]({url})",
        "",
        f"{_RTL}📘 הצטרף לדף הפייסבוק → https://www.facebook.com/AmzFreeIL",
        "",
        f"{_RTL}📢 @amzfreeil",
    ]
    return "\n".join(header_lines + footer_lines)


def _blog_facebook_caption(
    title: str, slug: str, amazon_price: float | None, israel_price: float | None,
    kind: str = "review",
) -> str:
    from backend.blog_utils import strip_tags

    guide = kind == "guide"
    url = f"https://www.amzfreeil.com/blog/{slug}.html"
    title = strip_tags(title)
    price = "" if guide else (_format_price(str(amazon_price)) if amazon_price else "")
    today = datetime.now().strftime("%d/%m/%Y")

    kicker = "📚 מדריך חדש בבלוג" if guide else "📝 סקירה חדשה בבלוג"
    header_lines = [
        f"{_RTL}✈️ משלוח חינם לישראל | {kicker}",
        "",
        f"{_RTL}{title}",
        "",
    ]
    footer_lines = [f"{_RTL}--", ""]
    if price:
        footer_lines.append(f"{_RTL}💰 מחיר: {price}")
    if not guide and israel_price and amazon_price:
        savings = round(israel_price - amazon_price)
        footer_lines.append(f"{_RTL}💸 מחיר מקביל בישראל: ~₪{israel_price:g} — חיסכון של ~₪{savings}")
    if not guide:
        footer_lines.append(f"{_RTL}ℹ️ משלוח חינם מותנה בהזמנה מינימלית של $49 — ניתן לצרף מוצרים נוספים")
    footer_lines.append(f"{_RTL}🚚 משלוח חינם לישראל 🇮🇱")
    if not guide:
        footer_lines.append(f"{_RTL}📅 נכון ל-{today}. המחירים משתנים — בדקו לפני רכישה.")
    cta = "לקריאת המדריך המלא" if guide else "לקריאת הסקירה המלאה"
    footer_lines += [
        "",
        f"{_RTL}👉 {cta}: {url}",
        "",
        f"{_RTL}📱 יש עוד הרבה מוצרים שלא מגיעים לפה — כולם בטלגרם → t.me/amzfreeil",
        "",
        f"{_RTL}📢 AMZ Free Ship Alert",
    ]
    return "\n".join(header_lines + footer_lines)


async def send_blog_post_to_telegram(
    title: str, slug: str, image_url: str | None,
    amazon_price: float | None = None, israel_price: float | None = None,
    kind: str = "review",
) -> bool:
    """Announce a newly published blog post in the @amzfreeil Telegram channel."""
    if os.environ.get("TELEGRAM_BLOG_ENABLED", "true").lower() == "false":
        logger.info("[telegram_blog] disabled via TELEGRAM_BLOG_ENABLED=false — skipping")
        return False

    token = os.environ.get("TELEGRAM_PRODUCT_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_PRODUCT_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("TELEGRAM_PRODUCT_BOT_TOKEN or TELEGRAM_PRODUCT_CHAT_ID not set — skipping blog send")
        return False

    caption = _blog_telegram_caption(title, slug, amazon_price, israel_price, kind)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if image_url:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "photo": image_url,
                          "caption": caption, "parse_mode": "Markdown"},
                )
            else:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": caption, "parse_mode": "Markdown"},
                )
        if resp.status_code == 200:
            return True
        logger.warning(f"[telegram_blog] send failed for {slug}: {resp.text[:200]}")
        return False
    except Exception as e:
        logger.warning(f"[telegram_blog] send error for {slug}: {e}")
        return False


async def send_blog_post_to_facebook(
    title: str, slug: str, image_url: str | None,
    amazon_price: float | None = None, israel_price: float | None = None,
    kind: str = "review",
) -> bool:
    """Announce a newly published blog post on the AmzFreeIL Facebook page."""
    if os.environ.get("FACEBOOK_BLOG_ENABLED", "true").lower() == "false":
        logger.info("[facebook_blog] disabled via FACEBOOK_BLOG_ENABLED=false — skipping")
        return False

    user_token = os.environ.get("FACEBOOK_PAGE_TOKEN", "")
    page_id = os.environ.get("FACEBOOK_PAGE_ID", "")
    if not user_token or not page_id:
        logger.warning("FACEBOOK_PAGE_TOKEN or FACEBOOK_PAGE_ID not set — skipping blog send")
        return False

    page_token = await _get_facebook_page_token(user_token, page_id)
    if not page_token:
        logger.warning("[facebook_blog] could not obtain page access token — skipping")
        return False

    caption = _blog_facebook_caption(title, slug, amazon_price, israel_price, kind)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if image_url:
                resp = await client.post(
                    f"https://graph.facebook.com/v19.0/{page_id}/photos",
                    data={"url": image_url, "caption": caption, "access_token": page_token},
                )
            else:
                resp = await client.post(
                    f"https://graph.facebook.com/v19.0/{page_id}/feed",
                    data={"message": caption, "access_token": page_token},
                )
        if resp.status_code == 200:
            return True
        logger.warning(f"[facebook_blog] send failed for {slug}: {resp.text[:300]}")
        return False
    except Exception as e:
        logger.warning(f"[facebook_blog] send error for {slug}: {e}")
        return False


# Fixed IL times of the scanner "product post" Facebook cron jobs. Single source
# of truth — main.py registers the cron jobs from this list, and the blog-social
# draw below keeps its distance from these same times.
FACEBOOK_PRODUCT_POST_TIMES = [(8, 0), (10, 30), (13, 0), (16, 0), (19, 0)]

# Same idea for the Telegram channel. Mostly aligned with the Facebook times on
# purpose: the blog-social draw keeps its distance from the union of both lists,
# and spreading these out evenly would leave the window with almost no free slots.
TELEGRAM_PRODUCT_POST_TIMES = [(7, 0), (8, 0), (10, 30), (13, 0), (14, 30), (16, 0), (19, 0), (20, 30)]

# Instagram mirrors the Facebook times exactly — same cadence decision as
# keeping shared hours between channels, no new cadence to design.
INSTAGRAM_PRODUCT_POST_TIMES = list(FACEBOOK_PRODUCT_POST_TIMES)

_BLOG_SOCIAL_WINDOW_START_HOUR = 6
_BLOG_SOCIAL_WINDOW_END_HOUR = 22
_BLOG_SOCIAL_MIN_GAP_MINUTES = 60
_BLOG_SOCIAL_MAX_GAP_MINUTES = 120
# A week was enough while the queue only carried the odd product review. The
# editorial guides arrive in batches, so the horizon has to be deep enough that a
# batch never runs off the end and lands on the fallback slot.
_BLOG_SOCIAL_MAX_LOOKAHEAD_DAYS = 21
# Deliberately smaller than the blog-to-blog gap: the product posts are fixed
# points, and a 60-120 min buffer around each would leave almost no room in the
# window.
_BLOG_SOCIAL_PRODUCT_GAP_MINUTES = 45
_BLOG_SOCIAL_MAX_PER_DAY = 3


def _blog_social_window_bounds(day_offset: int = 0) -> tuple[datetime, datetime]:
    """Return (window_start, window_end) in IL time for a blog-social window.

    day_offset=0 is the active window: today's 06:00-22:00, rolled to tomorrow
    if it already closed and clamped to "now" if it is already underway.
    day_offset>0 is the full 06:00-22:00 window that many days later, used when
    the active window is too packed to fit another post.
    """
    import pytz

    il_tz = pytz.timezone("Asia/Jerusalem")
    now_il = datetime.now(il_tz)
    window_start = now_il.replace(hour=_BLOG_SOCIAL_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    window_end = now_il.replace(hour=_BLOG_SOCIAL_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)

    if now_il >= window_end:
        window_start += timedelta(days=1)
        window_end += timedelta(days=1)
    elif now_il > window_start and day_offset == 0:
        window_start = now_il

    if day_offset:
        window_start = window_start.replace(
            hour=_BLOG_SOCIAL_WINDOW_START_HOUR, minute=0, second=0, microsecond=0
        ) + timedelta(days=day_offset)
        window_end += timedelta(days=day_offset)

    return window_start, window_end


def _all_product_post_times() -> list[tuple[int, int]]:
    """The union of the Facebook and Telegram fixed product-post times (IL).

    Both channels are announced from the same blog-social queue row, so a blog
    slot has to keep its distance from either channel's product posts. Times
    shared by both lists are deduped so they only count once.
    """
    return sorted(set(FACEBOOK_PRODUCT_POST_TIMES) | set(TELEGRAM_PRODUCT_POST_TIMES) | set(INSTAGRAM_PRODUCT_POST_TIMES))


def _product_post_times_utc(window_start: datetime, window_end: datetime) -> list[datetime]:
    """The day's fixed product-post times (IL, both channels) as UTC datetimes.

    Only times falling inside [window_start, window_end] are returned, so a
    window that was clamped to "now" mid-day does not reserve slots around
    product posts that already went out.
    """
    times = []
    for hour, minute in _all_product_post_times():
        t = window_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if window_start <= t <= window_end:
            times.append(t.astimezone(timezone.utc))
    return times


def parse_manual_blog_social_time(value: str) -> datetime:
    """Parse an admin-entered datetime-local string ("2026-07-20T09:30") as
    Israel local time and return it in UTC.

    Deliberately not the browser's timezone — the broadcast window is defined in
    IL time, so an admin travelling abroad still means "09:30 in Israel".
    Raises ValueError on a bad format or a time in the past.
    """
    import pytz

    il_tz = pytz.timezone("Asia/Jerusalem")
    try:
        naive = datetime.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise ValueError("פורמט תאריך לא תקין")
    if naive.tzinfo is not None:
        naive = naive.astimezone(il_tz).replace(tzinfo=None)

    local = il_tz.localize(naive)
    if local <= datetime.now(il_tz):
        raise ValueError("הזמן שנבחר כבר עבר")
    return local.astimezone(timezone.utc)


def blog_social_time_warnings(dt_utc: datetime, existing_times_utc: list[datetime]) -> list[str]:
    """Non-blocking warnings for a manually chosen broadcast time: outside the
    06:00-22:00 IL window, too close to another queued post or to a fixed
    product post, or on a day that already hit the blog-post cap.

    Warnings only — an admin-picked time is always honoured as-is.
    """
    import pytz

    il_tz = pytz.timezone("Asia/Jerusalem")
    local = dt_utc.astimezone(il_tz)
    warnings = []
    if not (_BLOG_SOCIAL_WINDOW_START_HOUR <= local.hour < _BLOG_SOCIAL_WINDOW_END_HOUR):
        warnings.append(
            f"השעה {local:%H:%M} מחוץ לחלון "
            f"{_BLOG_SOCIAL_WINDOW_START_HOUR:02d}:00-{_BLOG_SOCIAL_WINDOW_END_HOUR:02d}:00"
        )

    gap_seconds = _BLOG_SOCIAL_MIN_GAP_MINUTES * 60
    closest = min(
        (abs((dt_utc - t).total_seconds()) for t in existing_times_utc),
        default=None,
    )
    if closest is not None and closest < gap_seconds:
        warnings.append(
            f"רק {int(closest // 60)} דקות מפוסט אחר בתור (מומלץ לפחות {_BLOG_SOCIAL_MIN_GAP_MINUTES})"
        )

    for hour, minute in _all_product_post_times():
        product_local = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta = abs((dt_utc - product_local.astimezone(timezone.utc)).total_seconds())
        if delta < _BLOG_SOCIAL_PRODUCT_GAP_MINUTES * 60:
            warnings.append(
                f"רק {int(delta // 60)} דקות מפוסט מוצר קבוע ב-{hour:02d}:{minute:02d} "
                f"(מומלץ לפחות {_BLOG_SOCIAL_PRODUCT_GAP_MINUTES})"
            )

    same_day = sum(
        1 for t in existing_times_utc if t.astimezone(il_tz).date() == local.date()
    )
    if same_day >= _BLOG_SOCIAL_MAX_PER_DAY:
        warnings.append(
            f"ביום {local:%d/%m} כבר מתוכננים {same_day} פוסטי בלוג "
            f"(התקרה היומית היא {_BLOG_SOCIAL_MAX_PER_DAY})"
        )
    return warnings


def _random_blog_social_time(
    window_start: datetime, window_end: datetime, existing_times_utc: list[datetime],
    product_times_utc: list[datetime] | None = None,
) -> datetime | None:
    """Pick a random UTC datetime within [window_start, window_end] (IL time),
    kept at least 60-120 (random) minutes away from any already-queued blog post
    that day, so consecutive publishes don't cluster minutes apart.

    `product_times_utc` are the day's fixed scanner "product post" times; the
    candidate is additionally kept _BLOG_SOCIAL_PRODUCT_GAP_MINUTES away from
    each so the two independent schedules don't land back-to-back on the page.
    That gap is deliberately a separate, smaller constant — reusing the random
    60-120 min blog gap here would block nearly the whole window.

    Returns None when the window is too packed to honour those gaps — the caller
    then tries the next day's window rather than spilling past 22:00.
    """
    import random

    span_seconds = int((window_end - window_start).total_seconds())
    product_gap = timedelta(minutes=_BLOG_SOCIAL_PRODUCT_GAP_MINUTES)
    product_times_utc = product_times_utc or []

    min_gap = timedelta(minutes=random.randint(_BLOG_SOCIAL_MIN_GAP_MINUTES, _BLOG_SOCIAL_MAX_GAP_MINUTES))
    for _ in range(200):
        offset = random.randint(0, max(span_seconds, 0))
        candidate = window_start + timedelta(seconds=offset)
        if not all(abs((candidate - t).total_seconds()) >= min_gap.total_seconds() for t in existing_times_utc):
            continue
        if not all(abs((candidate - t).total_seconds()) >= product_gap.total_seconds() for t in product_times_utc):
            continue
        return candidate.astimezone(timezone.utc)

    return None


async def queue_blog_social_post(
    asin: str | None, slug: str, title: str, image_url: str | None,
    amazon_price: float | None = None, israel_price: float | None = None,
    manual_at: datetime | None = None, kind: str = "review",
) -> tuple[datetime, list[str]]:
    """Queue a blog post's Telegram/Facebook announcement for a random time
    (within the 06:00-22:00 IL active window, spaced 60-120 min apart from other
    queued blog posts and 45 min from the fixed product-post times) instead of
    sending immediately. At most _BLOG_SOCIAL_MAX_PER_DAY blog posts go out per
    day; when today's window is full or capped the post rolls to the next day.

    An admin-supplied `manual_at` (UTC) overrides the draw entirely; the returned
    warnings then describe how it deviates from the usual window/gap rules.
    """
    from backend.models import BlogSocialQueue

    windows = [_blog_social_window_bounds(d) for d in range(_BLOG_SOCIAL_MAX_LOOKAHEAD_DAYS)]
    horizon_start = windows[0][0].astimezone(timezone.utc)
    horizon_end = windows[-1][1].astimezone(timezone.utc)

    warnings: list[str] = []
    day_offset = 0
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(BlogSocialQueue.scheduled_at).where(
                BlogSocialQueue.scheduled_at >= horizon_start,
                BlogSocialQueue.scheduled_at <= horizon_end,
            )
        )).scalars().all()

        if manual_at is not None:
            scheduled_at = manual_at
            warnings = blog_social_time_warnings(manual_at, list(existing))
        else:
            scheduled_at = None
            for day_offset, (window_start, window_end) in enumerate(windows):
                same_day = [
                    t for t in existing
                    if window_start.astimezone(timezone.utc) <= t <= window_end.astimezone(timezone.utc)
                ]
                if len(same_day) >= _BLOG_SOCIAL_MAX_PER_DAY:
                    continue
                scheduled_at = _random_blog_social_time(
                    window_start, window_end, same_day,
                    _product_post_times_utc(window_start, window_end),
                )
                if scheduled_at:
                    break

            if scheduled_at is None:
                # Every window in the lookahead is packed — shouldn't happen in
                # practice. Draw a random time in the last window rather than
                # pinning to its start, so a batch that overflows spreads out
                # instead of firing all at once.
                day_offset = _BLOG_SOCIAL_MAX_LOOKAHEAD_DAYS - 1
                last_start, last_end = windows[-1]
                scheduled_at = _random_blog_social_time(
                    last_start, last_end, [], _product_post_times_utc(last_start, last_end)
                ) or last_start.astimezone(timezone.utc)
                logger.warning(
                    f"[blog_social_queue] no free slot in {_BLOG_SOCIAL_MAX_LOOKAHEAD_DAYS} days for {slug}"
                )

        db.add(BlogSocialQueue(
            asin=asin, kind=kind, slug=slug, title=title, image_url=image_url,
            amazon_price=amazon_price, israel_price=israel_price,
            scheduled_at=scheduled_at, manual=manual_at is not None,
        ))
        await db.commit()
    if manual_at is not None:
        note = " (manual)"
    else:
        note = f" (rolled +{day_offset}d, today's window full)" if day_offset else ""
    logger.info(f"[blog_social_queue] queued {slug} for {scheduled_at.isoformat()}{note}")
    return scheduled_at, warnings


async def run_send_blog_social_queue():
    """Send any due blog-post announcements. Runs every few minutes."""
    from backend.models import BlogSocialQueue, BlogPublishedAsin

    async def _stamp_published(db, asin: str | None, *, telegram: bool = False, facebook: bool = False):
        """Record on the published-post row when it was actually broadcast, so the
        'פורסמו' tab can show the social status even after the queue row is deleted.
        Editorial guides have no ASIN and no published-post row — nothing to stamp."""
        if not asin:
            return
        try:
            pub = (
                await db.execute(select(BlogPublishedAsin).where(BlogPublishedAsin.asin == asin))
            ).scalar_one_or_none()
            if not pub:
                return
            stamp = datetime.now(timezone.utc)
            if telegram and pub.telegram_sent_at is None:
                pub.telegram_sent_at = stamp
            if facebook and pub.facebook_sent_at is None:
                pub.facebook_sent_at = stamp
        except Exception as e:
            logger.warning(f"[blog_social_queue] could not stamp published row for {asin}: {e}")

    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BlogSocialQueue).where(
                BlogSocialQueue.scheduled_at <= now,
                or_(BlogSocialQueue.telegram_sent.is_(False), BlogSocialQueue.facebook_sent.is_(False)),
            )
        )
        due = result.scalars().all()
        if not due:
            return

        logger.info(f"=== Blog social queue: {len(due)} due ===")
        for row in due:
            if not row.telegram_sent:
                try:
                    row.telegram_sent = await send_blog_post_to_telegram(
                        row.title, row.slug, row.image_url, row.amazon_price, row.israel_price,
                        row.kind or "review",
                    )
                    if row.telegram_sent:
                        await _stamp_published(db, row.asin, telegram=True)
                except Exception as e:
                    logger.warning(f"[blog_social_queue] telegram send error for {row.slug}: {e}")
            if not row.facebook_sent:
                try:
                    row.facebook_sent = await send_blog_post_to_facebook(
                        row.title, row.slug, row.image_url, row.amazon_price, row.israel_price,
                        row.kind or "review",
                    )
                    if row.facebook_sent:
                        await _stamp_published(db, row.asin, facebook=True)
                except Exception as e:
                    logger.warning(f"[blog_social_queue] facebook send error for {row.slug}: {e}")
            if row.telegram_sent and row.facebook_sent:
                await db.delete(row)
            await db.commit()


UNVERIFIED_TTL_DAYS = 14


async def run_purge_unverified():
    """Delete accounts that never confirmed their email. Runs daily at 02:30 IL.

    A row that sat unverified for two weeks is not a user — it is a typo, a bot,
    or someone who changed their mind. Keeping them inflated every count in the
    admin panel and blocked the address from being registered again. All user_id
    foreign keys are ON DELETE CASCADE / SET NULL, so a plain delete is enough.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=UNVERIFIED_TTL_DAYS)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(User.id, User.email).where(
                User.is_verified == False,
                User.is_admin == False,
                User.created_at < cutoff,
            )
        )).all()
        if not rows:
            logger.info("=== Unverified purge: nothing to delete ===")
            return
        await db.execute(delete(User).where(User.id.in_([r.id for r in rows])))
        await db.commit()
    logger.info(f"=== Unverified purge: deleted {len(rows)} account(s) older than "
                f"{UNVERIFIED_TTL_DAYS}d: {[r.email for r in rows]} ===")


async def run_cleanup_orphans():
    """Delete user-source products with no watchers. Runs once daily at 02:00 IL."""
    logger.info("=== Orphan cleanup started ===")
    async with AsyncSessionLocal() as db:
        watched_ids = select(UserProduct.product_id).distinct()
        orphans = (await db.execute(
            select(Product.id, Product.asin).where(
                Product.source == "user",
                Product.id.not_in(watched_ids),
            )
        )).all()
        if not orphans:
            logger.info("=== Orphan cleanup: no orphans found ===")
            return
        orphan_ids = [row.id for row in orphans]
        await db.execute(delete(NotificationLog).where(NotificationLog.product_id.in_(orphan_ids)))
        await db.execute(delete(Product).where(Product.id.in_(orphan_ids)))
        await db.commit()
    logger.info(f"=== Orphan cleanup: deleted {len(orphans)} product(s): {[r.asin for r in orphans]} ===")


async def check_single_product(asin: str, url: str):
    """Check a single product immediately (used after a user adds it or manual re-check)."""
    logger.info(f"[{asin}] Immediate check triggered")
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


async def check_decodo_quota():
    """Check Decodo proxy usage via statistics API — logs daily MB and 7-day average.
    Logs WARNING if today > 200 MB (abnormal spike)."""
    logger.info("=== Decodo quota check started ===")
    api_key = os.environ.get("DECODO_API_KEY", "")
    if not api_key:
        logger.info("DECODO_API_KEY not set — skipping quota check.")
        return
    try:
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=7)).strftime("%Y-%m-%d 00:00:00")
        end = now.strftime("%Y-%m-%d 23:59:59")
        today_key = now.strftime("%Y-%m-%d 00:00:00")

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.decodo.com/api/v2/statistics/traffic",
                headers={"Authorization": api_key, "Content-Type": "application/json"},
                json={"proxyType": "residential_proxies", "startDate": start, "endDate": end, "groupBy": "day"},
            )
        if resp.status_code != 200:
            logger.warning(f"Decodo stats API returned {resp.status_code}: {resp.text[:200]}")
            return

        data = resp.json() or {}
        days = data.get("data", [])
        if not days:
            logger.warning("Decodo stats API returned empty data.")
            return

        today_mb = 0.0
        total_mb = 0.0
        for day in days:
            mb = day.get("rx_tx_bytes", 0) / (1024 * 1024)
            total_mb += mb
            if day.get("key", "").startswith(now.strftime("%Y-%m-%d")):
                today_mb = mb

        avg_mb = total_mb / len(days)

        if today_mb > 200:
            logger.warning(
                f"🚨 DECODO SPIKE: היום {today_mb:.1f} MB (ממוצע 7 ימים: {avg_mb:.1f} MB/day). בדוק deployments או גידול חריג במשתמשים."
            )
        else:
            logger.info(
                f"✅ Decodo: היום {today_mb:.1f} MB | ממוצע 7 ימים: {avg_mb:.1f} MB/day"
            )
    except Exception as e:
        logger.warning(f"Decodo quota check error: {e}")
    logger.info("=== Decodo quota check complete ===")


async def run_hebrew_backfill():
    """Generate name_he for user-sourced products that are missing it."""
    import os, anthropic as _anthropic
    from backend.database import AsyncSessionLocal
    from backend.models import Product
    from sqlalchemy import select

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Product).where(
                Product.source == "user",
                (Product.name_he == None) | (Product.name_he == ""),
            )
        )
        products = result.scalars().all()

    if not products:
        logger.info("Hebrew backfill: no user products missing name_he — skipping.")
        return

    logger.info(f"Hebrew backfill: generating name_he for {len(products)} user product(s).")
    from backend.blog_utils import claude_text
    client = _anthropic.Anthropic(api_key=api_key)
    updated = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Product).where(
                Product.source == "user",
                (Product.name_he == None) | (Product.name_he == ""),
            )
        )
        products = result.scalars().all()
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
                logger.warning(f"[{p.asin}] Hebrew name failed: {e}")
        await db.commit()

    logger.info(f"Hebrew backfill: updated {updated}/{len(products)} products.")
