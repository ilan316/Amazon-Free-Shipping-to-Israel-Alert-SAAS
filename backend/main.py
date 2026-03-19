import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from backend.database import create_tables
from backend.routes import auth, products, settings, admin as admin_routes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "120"))


async def _get_check_interval() -> int:
    """Read check interval from DB (SystemSetting), fallback to env var."""
    try:
        from backend.database import AsyncSessionLocal
        from backend.models import SystemSetting
        from sqlalchemy import select
        async with AsyncSessionLocal() as db:
            row = (await db.execute(
                select(SystemSetting).where(SystemSetting.key == "check_interval_minutes")
            )).scalar_one_or_none()
            if row:
                return int(row.value)
    except Exception:
        pass
    return CHECK_INTERVAL


def reschedule_check_job(minutes: int, start_date=None):
    """Reschedule the global_check APScheduler job to run every `minutes` minutes.
    start_date: when to run the first occurrence (None = immediately).
    """
    from backend.scheduler import run_global_check_cycle
    scheduler.add_job(
        run_global_check_cycle,
        trigger="interval",
        minutes=minutes,
        start_date=start_date,
        id="global_check",
        misfire_grace_time=300,
        replace_existing=True,
    )
    logger.info(f"global_check rescheduled to every {minutes} minutes")

_db_url = os.environ.get("DATABASE_URL", "")
_jobstores = {"default": SQLAlchemyJobStore(url=_db_url)} if _db_url else {}
scheduler = AsyncIOScheduler(timezone="UTC", jobstores=_jobstores)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    await create_tables()

    # Import here to avoid circular imports at module level
    from backend.checker import browser_manager
    from backend.scheduler import run_global_check_cycle, run_daily_summary

    await browser_manager.startup()

    # Start scheduler first so the job store loads from DB — this allows
    # get_job() to correctly find previously persisted jobs.
    daily_hour = int(os.environ.get("DAILY_SUMMARY_HOUR", "8"))
    scheduler.add_job(
        run_daily_summary,
        trigger="cron",
        hour=daily_hour,
        minute=0,
        timezone="Asia/Jerusalem",
        id="daily_summary",
        misfire_grace_time=600,
        replace_existing=True,
    )
    scheduler.start()

    from backend.scheduler import run_inactivity_check
    scheduler.add_job(
        run_inactivity_check,
        trigger="cron",
        hour=3,
        minute=0,
        timezone="Asia/Jerusalem",
        id="inactivity_check",
        misfire_grace_time=600,
        replace_existing=True,
    )

    # Read effective interval: DB setting overrides env var
    effective_interval = await _get_check_interval()

    # Preserve scheduled next_run_time across deploys — read from persisted job store
    # (scheduler.start() already loaded the job; we capture its next_run_time before overwriting it)
    from datetime import datetime, timezone, timedelta
    next_run_time = None
    try:
        existing_job = scheduler.get_job("global_check")
        if existing_job and existing_job.next_run_time:
            now = datetime.now(timezone.utc)
            if existing_job.next_run_time > now + timedelta(seconds=10):
                next_run_time = existing_job.next_run_time
                logger.info(f"Resuming schedule — next check at {next_run_time.isoformat()}")
            else:
                logger.info("Scheduled time has passed — running check immediately after startup")
    except Exception as e:
        logger.warning(f"Could not read persisted job next_run_time: {e}")

    reschedule_check_job(effective_interval, start_date=next_run_time)
    logger.info(f"Scheduler started — check interval: {effective_interval} minutes, daily summary at {daily_hour:02d}:00 Israel time")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
    await browser_manager.shutdown()
    logger.info("Shutdown complete")


app = FastAPI(title="Amazon Free Shipping Israel Alert", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(settings.router)
app.include_router(admin_routes.router)


@app.get("/system-message")
async def public_system_message():
    from backend.database import AsyncSessionLocal
    from backend.models import SystemSetting
    from sqlalchemy import select
    async with AsyncSessionLocal() as db:
        row = (await db.execute(select(SystemSetting).where(SystemSetting.key == "system_message"))).scalar_one_or_none()
        return {"message": row.value if row else ""}


@app.get("/health")
async def health():
    job = scheduler.get_job("global_check")
    summary_job = scheduler.get_job("daily_summary")
    next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
    next_summary = summary_job.next_run_time.isoformat() if summary_job and summary_job.next_run_time else None
    return {
        "status": "ok",
        "scheduler_running": scheduler.running,
        "next_check_at": next_run,
        "check_interval_minutes": CHECK_INTERVAL,
        "next_summary_at": next_summary,
    }



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
