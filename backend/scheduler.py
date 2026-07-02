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
            for i, (product, check_result) in enumerate(zip(to_check, check_results)):
                status_counts[check_result.status.value] = status_counts.get(check_result.status.value, 0) + 1
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
            user.vacation_mode = True
            user.automation_reengagement_sent_at = None
            await db.execute(
                update(UserProduct).where(UserProduct.user_id == user.id).values(is_paused=True)
            )
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
    all_bullets = [f"{_RTL}• {b}" for b in description.splitlines() if b.strip()]

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
    header_lines = [
        f"{_RTL}✈️ משלוח חינם לישראל | {cat_emoji} {cat_he}",
        "",
        f"{_RTL}*{name_he}*",
        "",
    ]

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
    caption = _telegram_caption(product)
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


async def run_send_telegram_product():
    """Send one free product to the Telegram channel. Skips ASINs sent in the last 7 days."""
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

        # Products eligible: FREE + scanner source + not sent recently
        sent_recently = (
            select(TelegramSent.asin)
            .where(TelegramSent.sent_at > resend_cutoff)
            .scalar_subquery()
        )
        result = await db.execute(
            select(Product)
            .where(
                Product.last_status == "FREE",
                Product.source == "scanner",
                Product.asin.not_in(sent_recently),
            )
            .order_by(func.random())
            .limit(1)
        )
        product = result.scalar_one_or_none()

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


def _facebook_caption(product: Product) -> str:
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

    header_lines = [
        f"{_RTL}✈️ משלוח חינם לישראל | {cat_emoji} {cat_he}",
        "",
        f"{_RTL}{name_he}",
        "",
    ]
    footer_lines = [
        f"{_RTL}--",
        "",
        f"{_RTL}💰 מחיר: {price}",
        f"{_RTL}ℹ️ משלוח חינם מותנה בהזמנה מינימלית של $49 — ניתן לצרף מוצרים נוספים",
        f"{_RTL}🚚 משלוח חינם לישראל 🇮🇱",
        f"{_RTL}📅 נכון ל-{today}. המחירים משתנים — בדקו לפני רכישה.",
        "",
        f"{_RTL}👉 לרכישה באמזון: {url}",
        "",
        f"{_RTL}📱 יש עוד הרבה מוצרים שלא מגיעים לפה — כולם בטלגרם → t.me/amzfreeil",
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

    caption = _facebook_caption(product)
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
    """Send one free product to the Facebook Page. Runs twice daily (08:00 and 13:00 IL).
    Skips ASINs sent in the last 7 days."""
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
        result = await db.execute(
            select(Product)
            .where(
                Product.last_status == "FREE",
                Product.source == "scanner",
                Product.asin.not_in(sent_recently),
            )
            .order_by(func.random())
            .limit(1)
        )
        product = result.scalar_one_or_none()

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


def _blog_telegram_caption(title: str, slug: str, amazon_price: float | None, israel_price: float | None) -> str:
    url = f"https://www.amzfreeil.com/blog/{slug}.html"
    name_he = _escape_md(title)
    price = _format_price(str(amazon_price)) if amazon_price else ""
    today = datetime.now().strftime("%d/%m/%Y")

    header_lines = [
        f"{_RTL}✈️ משלוח חינם לישראל | 📝 סקירה חדשה בבלוג",
        "",
        f"{_RTL}*{name_he}*",
        "",
    ]
    footer_lines = [f"{_RTL}--", ""]
    if price:
        footer_lines.append(f"{_RTL}💰 מחיר: *{price}*")
    if israel_price and amazon_price:
        savings = round(israel_price - amazon_price)
        footer_lines.append(f"{_RTL}💸 מחיר מקביל בישראל: ~₪{israel_price:g} — חיסכון של ~₪{savings}")
    footer_lines += [
        f"{_RTL}ℹ️ משלוח חינם מותנה בהזמנה מינימלית של $49 — ניתן לצרף מוצרים נוספים",
        f"{_RTL}🚚 משלוח חינם לישראל 🇮🇱",
        f"{_RTL}📅 נכון ל-{today}. המחירים משתנים — בדקו לפני רכישה.",
        "",
        f"[👉 לקריאת הסקירה המלאה]({url})",
        "",
        f"{_RTL}📘 הצטרף לדף הפייסבוק → https://www.facebook.com/AmzFreeIL",
        "",
        f"{_RTL}📢 @amzfreeil",
    ]
    return "\n".join(header_lines + footer_lines)


def _blog_facebook_caption(title: str, slug: str, amazon_price: float | None, israel_price: float | None) -> str:
    url = f"https://www.amzfreeil.com/blog/{slug}.html"
    price = _format_price(str(amazon_price)) if amazon_price else ""
    today = datetime.now().strftime("%d/%m/%Y")

    header_lines = [
        f"{_RTL}✈️ משלוח חינם לישראל | 📝 סקירה חדשה בבלוג",
        "",
        f"{_RTL}{title}",
        "",
    ]
    footer_lines = [f"{_RTL}--", ""]
    if price:
        footer_lines.append(f"{_RTL}💰 מחיר: {price}")
    if israel_price and amazon_price:
        savings = round(israel_price - amazon_price)
        footer_lines.append(f"{_RTL}💸 מחיר מקביל בישראל: ~₪{israel_price:g} — חיסכון של ~₪{savings}")
    footer_lines += [
        f"{_RTL}ℹ️ משלוח חינם מותנה בהזמנה מינימלית של $49 — ניתן לצרף מוצרים נוספים",
        f"{_RTL}🚚 משלוח חינם לישראל 🇮🇱",
        f"{_RTL}📅 נכון ל-{today}. המחירים משתנים — בדקו לפני רכישה.",
        "",
        f"{_RTL}👉 לקריאת הסקירה המלאה: {url}",
        "",
        f"{_RTL}📱 יש עוד הרבה מוצרים שלא מגיעים לפה — כולם בטלגרם → t.me/amzfreeil",
        "",
        f"{_RTL}📢 AMZ Free Ship Alert",
    ]
    return "\n".join(header_lines + footer_lines)


async def send_blog_post_to_telegram(
    title: str, slug: str, image_url: str | None,
    amazon_price: float | None = None, israel_price: float | None = None,
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

    caption = _blog_telegram_caption(title, slug, amazon_price, israel_price)
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

    caption = _blog_facebook_caption(title, slug, amazon_price, israel_price)
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


_BLOG_SOCIAL_WINDOW_START_HOUR = 6
_BLOG_SOCIAL_WINDOW_END_HOUR = 22
_BLOG_SOCIAL_MIN_GAP_MINUTES = 60
_BLOG_SOCIAL_MAX_GAP_MINUTES = 120


def _blog_social_window_bounds() -> tuple[datetime, datetime]:
    """Return (window_start, window_end) in IL time for the active blog-social
    window, rolling to tomorrow if today's 06:00-22:00 window already closed."""
    import pytz

    il_tz = pytz.timezone("Asia/Jerusalem")
    now_il = datetime.now(il_tz)
    window_start = now_il.replace(hour=_BLOG_SOCIAL_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    window_end = now_il.replace(hour=_BLOG_SOCIAL_WINDOW_END_HOUR, minute=0, second=0, microsecond=0)

    if now_il >= window_end:
        window_start += timedelta(days=1)
        window_end += timedelta(days=1)
    elif now_il > window_start:
        window_start = now_il

    return window_start, window_end


def _random_blog_social_time(
    window_start: datetime, window_end: datetime, existing_times_utc: list[datetime],
) -> datetime:
    """Pick a random UTC datetime within [window_start, window_end] (IL time),
    kept at least 60-120 (random) minutes away from any already-queued blog post
    that day, so consecutive publishes don't cluster minutes apart.

    Only other blog-queue entries are considered here — this is intentionally
    independent of the separate scanner "product post" cron jobs.
    """
    import random

    span_seconds = int((window_end - window_start).total_seconds())

    min_gap = timedelta(minutes=random.randint(_BLOG_SOCIAL_MIN_GAP_MINUTES, _BLOG_SOCIAL_MAX_GAP_MINUTES))
    for _ in range(200):
        offset = random.randint(0, max(span_seconds, 0))
        candidate = window_start + timedelta(seconds=offset)
        if all(abs((candidate - t).total_seconds()) >= min_gap.total_seconds() for t in existing_times_utc):
            return candidate.astimezone(timezone.utc)

    # Window is packed (many posts queued for the same day) — fall back to
    # placing it right after the last existing slot, even if that spills past 22:00.
    reference = max(existing_times_utc, default=window_start)
    return (reference + min_gap).astimezone(timezone.utc)


async def queue_blog_social_post(
    asin: str, slug: str, title: str, image_url: str | None,
    amazon_price: float | None = None, israel_price: float | None = None,
) -> datetime:
    """Queue a blog post's Telegram/Facebook announcement for a random time today
    (within the 06:00-22:00 IL active window, spaced 60-120 min apart from other
    queued blog posts) instead of sending immediately."""
    from backend.models import BlogSocialQueue

    window_start, window_end = _blog_social_window_bounds()
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(
            select(BlogSocialQueue.scheduled_at).where(
                BlogSocialQueue.scheduled_at >= window_start.astimezone(timezone.utc),
                BlogSocialQueue.scheduled_at <= window_end.astimezone(timezone.utc),
            )
        )).scalars().all()

        scheduled_at = _random_blog_social_time(window_start, window_end, list(existing))
        db.add(BlogSocialQueue(
            asin=asin, slug=slug, title=title, image_url=image_url,
            amazon_price=amazon_price, israel_price=israel_price,
            scheduled_at=scheduled_at,
        ))
        await db.commit()
    logger.info(f"[blog_social_queue] queued {slug} for {scheduled_at.isoformat()}")
    return scheduled_at


async def run_send_blog_social_queue():
    """Send any due blog-post announcements. Runs every few minutes."""
    from backend.models import BlogSocialQueue

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
                        row.title, row.slug, row.image_url, row.amazon_price, row.israel_price
                    )
                except Exception as e:
                    logger.warning(f"[blog_social_queue] telegram send error for {row.slug}: {e}")
            if not row.facebook_sent:
                try:
                    row.facebook_sent = await send_blog_post_to_facebook(
                        row.title, row.slug, row.image_url, row.amazon_price, row.israel_price
                    )
                except Exception as e:
                    logger.warning(f"[blog_social_queue] facebook send error for {row.slug}: {e}")
            await db.commit()


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
                msg = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=60,
                    messages=[{"role": "user", "content": f"תרגם לעברית קצרה ומובנת (עד 7 מילים, שמור את שם המותג, ללא מרכאות): {p.name}"}],
                )
                p.name_he = msg.content[0].text.strip()
                updated += 1
            except Exception as e:
                logger.warning(f"[{p.asin}] Hebrew name failed: {e}")
        await db.commit()

    logger.info(f"Hebrew backfill: updated {updated}/{len(products)} products.")
