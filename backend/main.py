import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.database import create_tables
from backend.routes import auth, products, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_MINUTES", "120"))
ADMIN_TOKEN = os.environ.get("ADMIN_SECRET_TOKEN", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    await create_tables()

    # Import here to avoid circular imports at module level
    from backend.checker import browser_manager
    from backend.scheduler import run_global_check_cycle

    await browser_manager.startup()

    scheduler.add_job(
        run_global_check_cycle,
        trigger="interval",
        minutes=CHECK_INTERVAL,
        id="global_check",
        misfire_grace_time=300,
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started — check interval: {CHECK_INTERVAL} minutes")

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


@app.get("/health")
async def health():
    return {"status": "ok", "scheduler_running": scheduler.running}


@app.post("/admin/trigger-check")
async def trigger_check(token: str = ""):
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    from backend.scheduler import run_global_check_cycle
    import asyncio
    asyncio.create_task(run_global_check_cycle())
    return {"message": "Check cycle triggered"}


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
