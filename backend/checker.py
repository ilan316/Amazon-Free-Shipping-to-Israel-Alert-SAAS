"""
Amazon Free Shipping to Israel Checker — Web SaaS version.
Adapted from the desktop checker.py.

Key changes vs desktop:
  - Removed PyInstaller path block
  - Removed check_all_products() (replaced by scheduler.py)
  - Added BrowserManager singleton (shared context, serialized checks)
  - headless=True always (server has no display)
  - Browser profile stored at BROWSER_PROFILE_DIR env var (Railway volume)
"""

import asyncio
import os
import random
import re
import logging
from dataclasses import dataclass
from enum import Enum

from playwright.async_api import (
    async_playwright,
    Page,
    TimeoutError as PWTimeout,
)

logger = logging.getLogger(__name__)

BROWSER_PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/app/browser_profile")

# ── Data types ────────────────────────────────────────────────────────────────

class ShippingStatus(Enum):
    FREE    = "FREE"
    PAID    = "PAID"
    NO_SHIP = "NO_SHIP"
    UNKNOWN = "UNKNOWN"
    ERROR   = "ERROR"


@dataclass
class CheckResult:
    asin: str
    status: ShippingStatus
    raw_text: str = ""
    error_message: str = ""
    product_name: str = ""
    found_in_aod: bool = False


# ── Selectors ─────────────────────────────────────────────────────────────────

DELIVER_TO_SELECTORS = [
    "#nav-global-location-popover-link",
    "#glow-ingress-line2",
    "#glow-ingress-line1",
]

COUNTRY_DROPDOWN_SELECTORS = [
    "#GLUXCountryList",
    "select.a-native-dropdown[name='countryCode']",
    "#GLUXCountryList_0",
    "select[name='countryCode']",
]

APPLY_BTN_SELECTORS = [
    "#GLUXConfirmClose",
    "input[name='glowDoneButton']",
    ".a-popover-footer .a-button-primary input",
    "#GLUXZipUpdate",
    "span#GLUXConfirmClose-announce",
]

DELIVERY_BLOCK_SELECTORS = [
    "#mir-layout-DELIVERY_BLOCK",
    "#ddmDeliveryMessage",
    "#deliveryMessageMirId",
    "#delivery-message",
    "#price-shipping-message",
    "#exports_feature_div",
    "#shippingMessageInsideBuyBox_feature_div",
    "#buybox",
    "#buyBoxInner",
]

CAPTCHA_SELECTORS = [
    "form[action='/errors/validateCaptcha']",
    "input#captchacharacters",
]

REDIRECT_DECLINE_SELECTORS = [
    "#redir-modal .a-popover-closebutton",
    "#redir-modal .a-button-close",
    "#redir-modal [data-action='a-popover-close']",
    "button[data-action='a-popover-close']",
]

SEE_ALL_BUYING_SELECTORS = [
    "#buybox-see-all-buying-choices a",
    "#buybox-see-all-buying-choices",
    "a#aod-ingress-link",
    ".a-button-buybox a",
    "text=See All Buying Options",
]

AOD_OFFER_SELECTORS = [
    "#aod-offer-list",
    "#aod-container",
    "#aod-offer",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _pause(min_s: float, max_s: float):
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _first(page: Page, selectors: list, timeout: int = 4000):
    for sel in selectors:
        try:
            el = await page.wait_for_selector(sel, timeout=timeout, state="attached")
            if el:
                return el
        except PWTimeout:
            continue
    return None


async def _is_captcha(page: Page) -> bool:
    for sel in CAPTCHA_SELECTORS:
        try:
            if await page.query_selector(sel):
                return True
        except Exception:
            pass
    title = (await page.title()).lower()
    return "robot" in title or "captcha" in title or "validateCaptcha" in page.url


# ── Location setup ────────────────────────────────────────────────────────────

async def _dismiss_redirect_modal(page: Page):
    """Dismiss the Amazon geo-redirect modal (e.g. 'Go to Amazon.sg?') if present.
    Always clicks the close/decline button — never the 'accept redirect' button.
    """
    for sel in REDIRECT_DECLINE_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                await el.click(timeout=3000)
                await _pause(0.5, 1.0)
                logger.debug(f"Redirect modal dismissed via {sel}.")
                return
        except Exception:
            continue
    try:
        await page.keyboard.press("Escape")
        await _pause(0.3, 0.6)
    except Exception:
        pass


async def _set_location_on_page(page: Page, country_code: str = "IL") -> bool:
    await _dismiss_redirect_modal(page)
    deliver_btn = await _first(page, DELIVER_TO_SELECTORS, timeout=8000)
    if not deliver_btn:
        logger.warning("Could not find 'Deliver to' button.")
        return False

    await deliver_btn.click()
    await _pause(1.5, 2.5)

    await _first(page, [
        "#GLUXCountryList",
        "#GLUXZipUpdateInput",
        ".a-popover-content",
    ], timeout=6000)

    dropdown = await _first(page, COUNTRY_DROPDOWN_SELECTORS, timeout=5000)
    if dropdown:
        try:
            await dropdown.select_option(value=country_code)
            await _pause(1.0, 1.5)
            # Click Apply/Done button to confirm the selection
            apply_btn = await _first(page, APPLY_BTN_SELECTORS, timeout=4000)
            if apply_btn:
                await apply_btn.click()
                logger.info(f"Apply button clicked for {country_code}.")
            else:
                logger.debug(f"No Apply button found — assuming auto-applied for {country_code}.")
            await _pause(2.0, 3.0)
            logger.info(f"Location set to {country_code}.")
            return True
        except Exception as e:
            logger.debug(f"Country dropdown failed: {e}")
    else:
        # Debug: log popup HTML so we can identify the correct selectors
        try:
            for popup_sel in [".a-popover-content", "#GLUXPopoverContent", "#nav-flyout-delivery"]:
                el = await page.query_selector(popup_sel)
                if el:
                    html = await el.inner_html()
                    logger.warning(f"Popup HTML (no dropdown found): {html[:800]}")
                    break
        except Exception:
            pass

    logger.warning("Could not set delivery location automatically.")
    return False


async def _verify_location(page: Page) -> bool:
    """Check that Amazon's Deliver To button shows Israel."""
    try:
        for sel in DELIVER_TO_SELECTORS:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).lower()
                if "israel" in text:
                    return True
    except Exception:
        pass
    return False


# ── Classification ────────────────────────────────────────────────────────────

def _classify(text: str) -> ShippingStatus:
    t = text.lower()

    # NO_SHIP must be checked FIRST — "cannot be shipped" always wins over any "free delivery" text
    # that may appear elsewhere on the page (recommendations, AOD panel, etc.)
    no_ship = [
        "doesn't ship to israel",
        "does not ship to israel",
        "cannot be shipped to israel",
        "not available for shipping to israel",
        "this item does not ship to your selected location",
        "item can't be shipped to your selected delivery location",
        "this item cannot be shipped to your selected delivery location",
        "cannot be shipped to your selected delivery location",
        "item cannot be shipped to your selected delivery location",
    ]
    if any(p in t for p in no_ship):
        return ShippingStatus.NO_SHIP

    # Explicit: Amazon clearly states free delivery to Israel
    if "free delivery" in t and "to israel" in t:
        return ShippingStatus.FREE

    # Fallback: When IL is the active delivery location, Amazon shows generic eligible text
    if "free delivery" in t and any(p in t for p in ("eligible orders", "eligible international", "eligible items")):
        return ShippingStatus.FREE

    paid_pat = re.compile(
        r'\$[\d]+\.[\d]{2}.{0,40}israel|israel.{0,40}\$[\d]+\.[\d]{2}',
        re.IGNORECASE,
    )
    if paid_pat.search(text) and "israel" in t:
        return ShippingStatus.PAID

    return ShippingStatus.UNKNOWN


# ── Delivery text reading ─────────────────────────────────────────────────────

async def _read_delivery_text(page: Page) -> str:
    for sel in DELIVERY_BLOCK_SELECTORS:
        try:
            el = await page.wait_for_selector(sel, timeout=3000, state="attached")
            if el:
                text = await el.inner_text()
                if text.strip():
                    return text.strip()
        except (PWTimeout, Exception):
            continue
    return ""


async def _check_all_buying_options(page: Page, asin: str) -> str:
    btn = await _first(page, SEE_ALL_BUYING_SELECTORS, timeout=3000)
    if not btn:
        return ""
    try:
        await btn.click()
        await _pause(2.0, 3.0)
        for sel in AOD_OFFER_SELECTORS:
            try:
                el = await page.wait_for_selector(sel, timeout=8000, state="visible")
                if el:
                    await _pause(1.0, 1.5)
                    text = await el.inner_text()
                    if text.strip():
                        return text.strip()
            except PWTimeout:
                continue
    except Exception as exc:
        logger.debug(f"[{asin}] AOD check failed: {exc}")
    return ""


async def check_product(page: Page, asin: str, url: str) -> CheckResult:
    try:
        await page.goto(f"{url}?psc=1&th=1", wait_until="domcontentloaded", timeout=30000)
        await _dismiss_redirect_modal(page)
        # Guard: if Amazon redirected us to a regional domain (e.g. amazon.sg), force back to .com
        if "amazon.com" not in page.url:
            logger.warning(f"[{asin}] Redirected to {page.url} — forcing amazon.com")
            await page.goto(
                f"https://www.amazon.com/dp/{asin}?psc=1&th=1",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await _dismiss_redirect_modal(page)
        product_name = ""
        try:
            await page.wait_for_selector("#productTitle", timeout=12000)
            el = await page.query_selector("#productTitle")
            if el:
                product_name = (await el.inner_text()).strip()
        except PWTimeout:
            logger.warning(f"[{asin}] productTitle not found.")

        if await _is_captcha(page):
            return CheckResult(asin, ShippingStatus.ERROR, error_message="CAPTCHA detected",
                               product_name=product_name)

        await _pause(0.8, 1.8)

        raw_text = await _read_delivery_text(page)
        status = _classify(raw_text) if raw_text else ShippingStatus.UNKNOWN
        found_in_aod = False

        if status != ShippingStatus.FREE:
            aod_text = await _check_all_buying_options(page, asin)
            if aod_text:
                aod_status = _classify(aod_text)
                if aod_status != ShippingStatus.FREE:
                    t = aod_text.lower()
                    if "free delivery" in t and "eligible orders" in t:
                        aod_status = ShippingStatus.FREE
                if aod_status == ShippingStatus.FREE or not raw_text:
                    raw_text = aod_text
                    status = aod_status
                    found_in_aod = (aod_status == ShippingStatus.FREE)

        if not raw_text:
            return CheckResult(asin, ShippingStatus.UNKNOWN, error_message="No delivery block found",
                               product_name=product_name)

        logger.info(f"[{asin}] {status.value} | {raw_text[:120]!r}")
        return CheckResult(asin, status, raw_text=raw_text, product_name=product_name,
                           found_in_aod=found_in_aod)

    except PWTimeout as e:
        return CheckResult(asin, ShippingStatus.ERROR, error_message=f"Timeout: {e}")
    except Exception as e:
        return CheckResult(asin, ShippingStatus.ERROR, error_message=str(e))


# ── BrowserManager singleton ──────────────────────────────────────────────────

class BrowserManager:
    """Singleton that owns the Playwright browser context for the server lifetime."""

    def __init__(self):
        self._pw = None
        self._context = None
        self._lock = asyncio.Lock()

    async def startup(self):
        logger.info("Starting Playwright browser...")
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=True,
            slow_mo=80,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
            ignore_default_args=["--enable-automation"],
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        # Set Israel delivery location once at startup
        logger.info("Setting delivery location to Israel...")
        page = await self._context.new_page()
        try:
            await page.goto("https://www.amazon.com", wait_until="domcontentloaded", timeout=20000)
            await _pause(2.0, 3.5)
            if not await _is_captcha(page):
                await _set_location_on_page(page, "IL")
        except Exception as e:
            logger.warning(f"Location setup failed (will retry next cycle): {e}")
        finally:
            await page.close()

        logger.info("Browser ready.")

    async def shutdown(self):
        try:
            if self._context:
                await self._context.close()
            if self._pw:
                await self._pw.stop()
        except Exception as e:
            logger.warning(f"Browser shutdown error (ignored): {e}")

    async def refresh_location(self):
        """Re-set delivery location to Israel. Called at the start of each check cycle."""
        page = await self._context.new_page()
        try:
            for attempt in range(3):
                await page.goto("https://www.amazon.com", wait_until="domcontentloaded", timeout=20000)
                await _pause(2.0, 3.5)
                if await _is_captcha(page):
                    logger.warning("CAPTCHA during location refresh — skipping.")
                    return
                await _dismiss_redirect_modal(page)
                if await _verify_location(page):
                    logger.info("Location already Israel ✓ — no change needed.")
                    return
                success = await _set_location_on_page(page, "IL")
                if success:
                    # Reload to confirm the location was saved
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
                    await _pause(1.5, 2.0)
                    if await _verify_location(page):
                        logger.info("Location verified: Israel ✓")
                        return
                    logger.warning(f"Location verify failed (attempt {attempt + 1}/3) — retrying...")
                    await _pause(2.0, 3.0)
                else:
                    logger.warning(f"Location set failed (attempt {attempt + 1}/3) — retrying...")
                    await _pause(2.0, 3.0)
            logger.error("Failed to set Israel location after 3 attempts — checks may return UNKNOWN.")
        except Exception as e:
            logger.warning(f"Location refresh error (ignored): {e}")
        finally:
            await page.close()

    async def check(self, asin: str, url: str) -> CheckResult:
        """Check a single product. Serialized via lock to avoid CAPTCHA triggers."""
        async with self._lock:
            page = await self._context.new_page()
            try:
                return await check_product(page, asin, url)
            finally:
                await page.close()


browser_manager = BrowserManager()
