import os
import time
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

limiter = Limiter(key_func=get_remote_address)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://accounts.google.com https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://accounts.google.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://oauth2.googleapis.com https://accounts.google.com; "
            "frame-src https://accounts.google.com; "
            "frame-ancestors 'none'"
        )
        return response

from backend.database import create_tables, fix_gmail_template, seed_default_templates, migrate_garmin_draft_from_dismissed
from backend.routes import auth, products, settings, admin as admin_routes, tracking, pause as pause_route, webhooks as webhooks_route
from backend.routes import internal as internal_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

def _setup_persistent_log():
    """Add a rotating file handler that writes to the Railway volume, surviving redeploys."""
    import os
    from logging.handlers import RotatingFileHandler
    log_dir = os.path.join(os.environ.get("BROWSER_PROFILE_DIR", "/app/browser_profile"), "logs")
    os.makedirs(log_dir, exist_ok=True)
    fh = RotatingFileHandler(
        os.path.join(log_dir, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=9,               # keep 10 files = up to 100 MB total
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)

_setup_persistent_log()

async def _get_check_time() -> tuple:
    """Read daily check time from DB (SystemSetting key 'check_time'), fallback to 06:00."""
    try:
        from backend.database import AsyncSessionLocal
        from backend.models import SystemSetting
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(SystemSetting).where(SystemSetting.key == "check_time")
            )).scalar_one_or_none()
            if row and row.value:
                h, m = row.value.split(":")
                return int(h), int(m)
    except Exception:
        pass
    return 6, 0  # default 06:00 Israel time


def reschedule_check_job(hour: int, minute: int):
    """Schedule the global_check job as a daily cron at the given time (Israel time)."""
    from backend.scheduler import run_global_check_cycle
    _upsert_job(run_global_check_cycle, "global_check", dict(
        trigger="cron", hour=hour, minute=minute, timezone="Asia/Jerusalem", misfire_grace_time=300
    ))
    logger.info(f"global_check scheduled daily at {hour:02d}:{minute:02d} Israel time")

_db_url = os.environ.get("DATABASE_URL", "")
_jobstores = {"default": SQLAlchemyJobStore(url=_db_url)} if _db_url else {}
scheduler = AsyncIOScheduler(timezone="UTC", jobstores=_jobstores)


def _upsert_job(func, job_id: str, kwargs: dict):
    """Add or reschedule a job, avoiding duplicate key errors during rolling deploys.

    During Railway rolling restarts, two processes may run concurrently.
    Using add_job(replace_existing=True) does DELETE+INSERT which can race.
    This helper uses reschedule_job (UPDATE) when the job already exists in
    memory (loaded from DB by scheduler.start()), and add_job (INSERT) only
    when the job is genuinely new.

    For interval jobs: only reschedules if the interval actually changed —
    preserves the existing next_run_time so restarts don't reset the countdown.
    """
    existing = scheduler.get_job(job_id)
    if existing:
        trigger_type = kwargs.get("trigger", "cron")
        if trigger_type == "interval":
            from apscheduler.triggers.interval import IntervalTrigger
            from datetime import timedelta
            trigger_kwargs = {k: v for k, v in kwargs.items() if k in ("minutes", "seconds", "hours")}
            desired_interval = timedelta(**trigger_kwargs)
            current_interval = getattr(existing.trigger, "interval", None)
            if current_interval != desired_interval:
                scheduler.reschedule_job(job_id, trigger=IntervalTrigger(**trigger_kwargs))
                logger.debug(f"Rescheduled interval job (interval changed): {job_id}")
            else:
                logger.debug(f"Kept existing interval job schedule (unchanged): {job_id}")
        else:
            from apscheduler.triggers.cron import CronTrigger
            trigger_kwargs = {k: v for k, v in kwargs.items()
                             if k in ("hour", "minute", "second", "timezone")}
            scheduler.reschedule_job(job_id, trigger=CronTrigger(**trigger_kwargs))
            logger.debug(f"Rescheduled existing job: {job_id}")
    else:
        scheduler.add_job(func, **kwargs, id=job_id)
        logger.debug(f"Added new job: {job_id}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    await create_tables()
    await seed_default_templates()
    await fix_gmail_template()
    await migrate_garmin_draft_from_dismissed()

    # Import here to avoid circular imports at module level
    from backend.checker import browser_manager
    from backend.scheduler import run_global_check_cycle, run_daily_summary

    await browser_manager.startup()

    # Start scheduler first so the job store loads existing jobs from DB into memory.
    # Then use _upsert_job() which does reschedule_job (UPDATE) if the job already
    # exists, and add_job (INSERT) only for new jobs. This avoids the duplicate key
    # error that occurs during Railway rolling restarts when two processes both try
    # to DELETE+INSERT the same job simultaneously.
    daily_hour = int(os.environ.get("DAILY_SUMMARY_HOUR", "8"))

    # Clean up stale APScheduler job references before loading from DB
    _stale_job_ids = ["no_click_automation", "telegram_product"]
    if _db_url and _stale_job_ids:
        try:
            from sqlalchemy import create_engine, text as _sql_text
            _sync_url = _db_url.replace("+asyncpg", "")
            _sync_engine = create_engine(_sync_url, pool_pre_ping=False)
            with _sync_engine.connect() as _conn:
                for _jid in _stale_job_ids:
                    _conn.execute(_sql_text("DELETE FROM apscheduler_jobs WHERE id = :id"), {"id": _jid})
                _conn.commit()
            _sync_engine.dispose()
            logger.info(f"Cleaned up stale APScheduler jobs: {_stale_job_ids}")
        except Exception as _e:
            logger.debug(f"Stale job cleanup skipped: {_e}")

    scheduler.start()

    from backend.scheduler import run_inactivity_check, run_automation_emails, check_decodo_quota, run_telegram_report, run_hebrew_backfill, run_send_telegram_product, run_send_facebook_product, run_cleanup_orphans, cleanup_old_screenshots, run_send_blog_social_queue, FACEBOOK_PRODUCT_POST_TIMES, TELEGRAM_PRODUCT_POST_TIMES

    telegram_hour = int(os.environ.get("TELEGRAM_REPORT_HOUR", "8"))
    telegram_minute = int(os.environ.get("TELEGRAM_REPORT_MINUTE", "0"))

    _upsert_job(run_daily_summary, "daily_summary", dict(
        trigger="cron", hour=daily_hour, minute=0, timezone="Asia/Jerusalem", misfire_grace_time=600
    ))
    _upsert_job(run_inactivity_check, "inactivity_check", dict(
        trigger="cron", hour=3, minute=0, timezone="Asia/Jerusalem", misfire_grace_time=600
    ))
    _upsert_job(run_cleanup_orphans, "cleanup_orphans", dict(
        trigger="cron", hour=2, minute=0, timezone="Asia/Jerusalem", misfire_grace_time=600
    ))
    _upsert_job(run_automation_emails, "automation_emails", dict(
        trigger="cron", hour=9, minute=0, timezone="Asia/Jerusalem", misfire_grace_time=600
    ))
    _upsert_job(check_decodo_quota, "decodo_quota_check", dict(
        trigger="cron", hour=7, minute=30, timezone="Asia/Jerusalem", misfire_grace_time=600
    ))
    _upsert_job(run_telegram_report, "telegram_report", dict(
        trigger="cron", hour=telegram_hour, minute=telegram_minute, timezone="Asia/Jerusalem", misfire_grace_time=600
    ))
    _upsert_job(run_hebrew_backfill, "hebrew_backfill", dict(
        trigger="cron", hour=8, minute=10, timezone="Asia/Jerusalem", misfire_grace_time=600
    ))
    # Telegram product posts — times live in scheduler.TELEGRAM_PRODUCT_POST_TIMES.
    # Was an every-45-min interval job, which flooded the channel with ~21 posts
    # a day and gave the blog-social draw no predictable times to avoid; the old
    # "telegram_product" job id is dropped in _stale_job_ids above.
    for _tg_hour, _tg_minute in TELEGRAM_PRODUCT_POST_TIMES:
        _upsert_job(run_send_telegram_product, f"telegram_product_{_tg_hour:02d}{_tg_minute:02d}", dict(
            trigger="cron", hour=_tg_hour, minute=_tg_minute, timezone="Asia/Jerusalem", misfire_grace_time=1800
        ))
    # Facebook product posts — times live in scheduler.FACEBOOK_PRODUCT_POST_TIMES,
    # which the blog-social queue also reads to keep its distance from them.
    for _fb_hour, _fb_minute in FACEBOOK_PRODUCT_POST_TIMES:
        _upsert_job(run_send_facebook_product, f"facebook_product_{_fb_hour:02d}{_fb_minute:02d}", dict(
            trigger="cron", hour=_fb_hour, minute=_fb_minute, timezone="Asia/Jerusalem", misfire_grace_time=1800
        ))

    _upsert_job(cleanup_old_screenshots, "screenshot_cleanup", dict(
        trigger="interval", hours=1, misfire_grace_time=300
    ))
    _upsert_job(run_send_blog_social_queue, "blog_social_queue", dict(
        trigger="interval", minutes=5, misfire_grace_time=300
    ))

    # Read daily check time from DB (cron trigger — no timer reset on deploy)
    check_hour, check_minute = await _get_check_time()
    reschedule_check_job(check_hour, check_minute)
    logger.info(f"Scheduler started — daily check at {check_hour:02d}:{check_minute:02d} Israel time, summary at {daily_hour:02d}:00, Decodo quota check at 07:30")

    # Re-apply pause state from DB (survives deployments)
    try:
        from backend.database import AsyncSessionLocal
        from backend.models import SystemSetting
        from sqlalchemy import select as _select
        async with AsyncSessionLocal() as _db:
            _row = (await _db.execute(_select(SystemSetting).where(SystemSetting.key == "system_paused"))).scalar_one_or_none()
            if _row and _row.value == "true":
                for job_id in ("global_check", "daily_summary"):
                    if scheduler.get_job(job_id):
                        scheduler.pause_job(job_id)
                logger.info("Checks paused on startup (system_paused=true in DB)")
    except Exception as e:
        logger.warning(f"Could not read system_paused from DB: {e}")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
    await browser_manager.shutdown()
    logger.info("Shutdown complete")


app = FastAPI(title="Amazon Free Shipping Israel Alert", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SecurityHeadersMiddleware)

_raw_origins = os.environ.get("ALLOWED_ORIGINS", "https://app.amzfreeil.com,https://amzfreeil.com,https://www.amzfreeil.com")
allowed_origins = [o.strip() for o in _raw_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(settings.router)
app.include_router(admin_routes.router)
app.include_router(tracking.router)
app.include_router(pause_route.router)
app.include_router(webhooks_route.router)
app.include_router(internal_routes.router, prefix="/internal")


@app.get("/api/config")
async def public_config():
    return {"google_client_id": os.environ.get("GOOGLE_CLIENT_ID", "")}


async def _get_category_he(db, english_name: str) -> str:
    """Return Hebrew translation for an Amazon category, auto-translating via Claude if unknown."""
    from backend.models import CategoryTranslation
    from sqlalchemy import select
    row = (await db.execute(
        select(CategoryTranslation).where(CategoryTranslation.english_name == english_name)
    )).scalar_one_or_none()
    if row:
        return row.hebrew_name

    # New category — translate via Claude API.
    # The anthropic client is synchronous/blocking, so run it in a thread to
    # avoid stalling the event loop of the single worker.
    def _translate_sync() -> str:
        import anthropic
        from backend.blog_utils import claude_text
        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-sonnet-5",
            max_tokens=200,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": f'תרגם את שם קטגוריית המוצר הזו מאמזון לעברית קצרה ומדויקת. ענה רק עם התרגום, ללא הסברים: "{english_name}"'
            }]
        )
        return claude_text(msg).strip().strip('"')

    hebrew_name = english_name  # fallback
    try:
        hebrew_name = await asyncio.to_thread(_translate_sync)
    except Exception as e:
        logging.warning(f"Category translation failed for '{english_name}': {e}")

    # Save to DB for next time
    try:
        db.add(CategoryTranslation(english_name=english_name, hebrew_name=hebrew_name))
        await db.commit()
    except Exception:
        await db.rollback()

    return hebrew_name


# In-memory cache for the public free-products response. The backend runs with
# --workers 1, so a module-level dict is shared across all requests. The product
# list is refreshed by the scanner once a day, so a 30-minute TTL is safe and
# turns virtually every visitor request into an instant cache hit.
_free_products_cache: dict = {"data": None, "ts": 0.0}
_FREE_PRODUCTS_TTL = 1800  # seconds (30 min)


@app.get("/api/public/free-products")
async def public_free_products():
    """Public endpoint — returns all products currently with FREE shipping to Israel.
    Used by amzfreeil.com/free-products.html (no auth required, CORS open)."""
    # Serve from cache when fresh.
    if _free_products_cache["data"] is not None and \
            time.time() - _free_products_cache["ts"] < _FREE_PRODUCTS_TTL:
        return _free_products_cache["data"]

    from backend.database import AsyncSessionLocal
    from backend.models import Product, CategoryTranslation
    from sqlalchemy import select, or_
    from datetime import datetime, timedelta, timezone
    tag = os.environ.get("AMAZON_AFFILIATE_TAG", "amzfreeil-20").strip()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=26)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Product)
            .where(Product.last_status == "FREE")
            .where(
                or_(
                    Product.source == "scanner",
                    Product.last_checked >= cutoff,
                )
            )
            .order_by(Product.last_checked.desc())
        )
        products = result.scalars().all()

        # Fetch all known category translations in a single query (no N+1).
        unique_cats = {p.amazon_category for p in products if p.amazon_category}
        cat_map: dict[str, str] = {}
        if unique_cats:
            rows = (await db.execute(
                select(CategoryTranslation)
                .where(CategoryTranslation.english_name.in_(unique_cats))
            )).scalars().all()
            cat_map = {r.english_name: r.hebrew_name for r in rows}

            # Translate any genuinely new categories (usually none). This only
            # happens on a cache miss, and _get_category_he runs the blocking
            # Claude call in a thread so it won't stall the event loop.
            for cat in unique_cats - cat_map.keys():
                cat_map[cat] = await _get_category_he(db, cat)

    data = [
        {
            "asin": p.asin,
            "name": p.name or p.asin,
            "url": f"https://www.amazon.com/dp/{p.asin}?tag={tag}",
            "image": p.image_url or f"https://images-na.ssl-images-amazon.com/images/P/{p.asin}.01._SL200_.jpg",
            "last_price": p.last_price,
            "found_in_aod": p.found_in_aod,
            "last_checked": p.last_checked.isoformat() if p.last_checked else None,
            "name_he": p.name_he,
            "amazon_category": p.amazon_category,
            "category_he": cat_map.get(p.amazon_category, p.amazon_category) if p.amazon_category else "",
        }
        for p in products
    ]

    _free_products_cache["data"] = data
    _free_products_cache["ts"] = time.time()
    return data


@app.get("/system-message")
async def public_system_message():
    from backend.database import AsyncSessionLocal
    from backend.models import SystemSetting
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "system_message"))).scalar_one_or_none()
        return {"message": row.value if row else ""}


class ContactRequest(BaseModel):
    name: str = Field(..., max_length=30)
    email: str = Field(..., max_length=50)
    message: str = Field(..., max_length=3000)


@app.post("/api/contact")
@limiter.limit("5/hour")
async def contact_form(request: Request, body: ContactRequest):
    from backend.database import AsyncSessionLocal
    from backend.models import User
    from backend.notifier import send_simple_email
    from sqlalchemy import select
    import html as html_lib
    safe_name = html_lib.escape(body.name)
    safe_email = html_lib.escape(body.email)
    safe_message = html_lib.escape(body.message).replace("\n", "<br>")
    html = (
        f"<p><strong>שם:</strong> {safe_name}</p>"
        f"<p><strong>אימייל:</strong> {safe_email}</p>"
        f"<p><strong>הודעה:</strong><br>{safe_message}</p>"
    )
    async with AsyncSessionLocal() as db:
        admins = (await db.execute(
            select(User).where(User.is_admin == True, User.is_active == True)
        )).scalars().all()
    for admin in admins:
        send_simple_email(admin.email, f"[צרו קשר] {body.name}", html)
    return {"ok": True}


_health_cache: dict = {"ts": 0.0, "data": None}
_HEALTH_TTL = 30.0  # seconds


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    # scheduler.get_job() hits the SQLAlchemyJobStore (Postgres) synchronously,
    # blocking the single-worker event loop (~215ms/round-trip to the DB region).
    # Railway health-checks this frequently, so cache the jobstore reads with a
    # short TTL to keep the DB hit to at most once per _HEALTH_TTL seconds.
    now = time.monotonic()
    cached = _health_cache["data"]
    if cached is not None and (now - _health_cache["ts"]) < _HEALTH_TTL:
        return {"status": "ok", "scheduler_running": scheduler.running, **cached}

    job = scheduler.get_job("global_check")
    summary_job = scheduler.get_job("daily_summary")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    next_summary = summary_job.next_run_time.isoformat() if summary_job and summary_job.next_run_time else None
    data = {"next_check_at": next_run, "next_summary_at": next_summary}
    _health_cache["data"] = data
    _health_cache["ts"] = now
    return {"status": "ok", "scheduler_running": scheduler.running, **data}



# ── Serve frontend static files ───────────────────────────────────────────────
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.isdir(frontend_dir):
    app.mount("/static", StaticFiles(directory=os.path.join(frontend_dir, "static")), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_index():
        return FileResponse(os.path.join(frontend_dir, "index.html"))

    @app.get("/dashboard", include_in_schema=False)
    async def serve_dashboard():
        return FileResponse(os.path.join(frontend_dir, "dashboard.html"))

    @app.get("/settings", include_in_schema=False)
    async def serve_settings():
        return FileResponse(os.path.join(frontend_dir, "settings.html"))

    @app.get("/admin", include_in_schema=False)
    async def serve_admin():
        return FileResponse(os.path.join(frontend_dir, "admin.html"))

    @app.get("/admin/login", include_in_schema=False)
    async def serve_admin_login():
        return FileResponse(os.path.join(frontend_dir, "admin-login.html"))

    @app.get("/privacy", include_in_schema=False)
    async def serve_privacy():
        return FileResponse(os.path.join(frontend_dir, "privacy.html"))

    @app.get("/terms", include_in_schema=False)
    async def serve_terms():
        return FileResponse(os.path.join(frontend_dir, "terms.html"))

    @app.get("/guide", include_in_schema=False)
    async def serve_guide():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="https://www.amzfreeil.com/web-guide.html", status_code=301)

    @app.get("/about", include_in_schema=False)
    async def serve_about():
        return FileResponse(os.path.join(frontend_dir, "about.html"))

    @app.get("/robots.txt", include_in_schema=False)
    async def serve_robots():
        return FileResponse(os.path.join(frontend_dir, "robots.txt"), media_type="text/plain")

    @app.get("/sitemap.xml", include_in_schema=False)
    async def serve_sitemap():
        return FileResponse(os.path.join(frontend_dir, "sitemap.xml"), media_type="application/xml")
