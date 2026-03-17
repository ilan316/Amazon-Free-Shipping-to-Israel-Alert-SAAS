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

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    Page,
    TimeoutError as PWTimeout,
)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Resource types to block in Playwright pages (speed optimisation)
_BLOCK_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}

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

SHIP_OUTSIDE_US_SELECTORS = [
    "#GLUXCountryList",          # already on international tab
    "span#GLUXDeliveryToAddress",
    "a#GLUXChangeAddressLink",
    "span[id*='GLUXChangeAddress']",
    "a[href*='GLUXChangeAddress']",
    "span.a-declarative[data-action*='GLUXChangeAddress']",
    "text=Ship outside the US",
    "text=Deliver outside the US",
    "text=Change",
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


# ── httpx location-setting via Amazon address-change API ──────────────────────

async def _try_set_location_httpx() -> tuple:
    """Set Israel delivery location by calling Amazon's internal address-change API.
    No browser UI interaction — much less likely to trigger CAPTCHA.
    Tries two approaches: countryCode=IL (international), then zipCode (Tel Aviv).
    Returns (success: bool, cookies: list) tuple."""
    base_headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        async with httpx.AsyncClient(
            headers=base_headers, follow_redirects=True, timeout=20.0
        ) as client:
            # Step 1: Get Amazon homepage to establish a session and grab CSRF token
            resp = await client.get("https://www.amazon.com/")
            if "validateCaptcha" in str(resp.url) or "captchacharacters" in resp.text:
                logger.warning("httpx location: CAPTCHA on Amazon homepage")
                return False, []

            soup = BeautifulSoup(resp.text, "html.parser")
            csrf_el = soup.find("input", attrs={"name": "anti-csrftoken-a2z"})
            csrf_token = csrf_el["value"] if csrf_el else ""
            logger.info(f"httpx location: session established, csrf={'found' if csrf_token else 'not found'}")

            # Step 2: Try two payload variants — country code first, then zip code
            payloads = [
                # Variant A: international country code (for "Ship outside the US")
                {
                    "locationType": "LOCATION_INPUT",
                    "zipCode": "",
                    "countryCode": "IL",
                    "storeContext": "generic",
                    "deviceType": "web",
                    "pageType": "Gateway",
                    "actionSource": "glow",
                },
                # Variant B: Tel Aviv zip code
                {
                    "locationType": "LOCATION_INPUT",
                    "zipCode": "6100001",
                    "storeContext": "generic",
                    "deviceType": "web",
                    "pageType": "Gateway",
                    "actionSource": "glow",
                },
            ]

            api_headers = {
                **base_headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.amazon.com/",
                "anti-csrftoken-a2z": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
            }

            for i, payload in enumerate(payloads):
                resp2 = await client.post(
                    "https://www.amazon.com/portal-migration/hz/glow/address-change",
                    data=payload,
                    headers=api_headers,
                )
                logger.info(f"httpx location: address-change variant {i+1} → status {resp2.status_code}")
                if resp2.status_code in (200, 302):
                    break
            else:
                return False, []

            # Step 3: Small delay then verify
            await asyncio.sleep(1.5)
            resp3 = await client.get("https://www.amazon.com/dp/B00EDR1X3O?psc=1&th=1")
            if "validateCaptcha" in str(resp3.url) or "captchacharacters" in resp3.text:
                logger.warning("httpx location: CAPTCHA on verification page")
                return False, []

            html_lower = resp3.text.lower()

            # Check "Deliver to" nav button — it shows "Israel" when location is set
            soup3 = BeautifulSoup(resp3.text, "html.parser")
            nav_text = ""
            for nav_id in ["glow-ingress-line2", "glow-ingress-line1",
                           "nav-global-location-popover-link"]:
                el = soup3.find(id=nav_id)
                if el:
                    nav_text = el.get_text(strip=True).lower()
                    break

            logger.info(f"httpx location: nav text = {nav_text!r}")

            if "israel" in nav_text or "israel" in html_lower:
                cookie_list = [{"name": k, "value": v} for k, v in client.cookies.items()]
                logger.info(f"httpx location: Israel confirmed ✓ ({len(cookie_list)} cookies)")
                return True, cookie_list

            # If "israel" not found but we got a real product page (not CAPTCHA),
            # log delivery block text for debugging
            delivery_ids = ["mir-layout-DELIVERY_BLOCK", "ddmDeliveryMessage",
                            "deliveryMessageMirId", "exports_feature_div", "buybox"]
            for el_id in delivery_ids:
                el = soup3.find(id=el_id)
                if el:
                    logger.warning(f"httpx location: delivery block = {el.get_text(strip=True)[:120]!r}")
                    break

            logger.warning("httpx location: Israel NOT confirmed in product page")
            return False, []
    except Exception as e:
        logger.warning(f"httpx location: error — {e}")
        return False, []


# ── httpx / HTML-based check (fast, parallel) ─────────────────────────────────

def _parse_html_delivery(html: str, asin: str) -> CheckResult:
    """Parse Amazon product HTML (from httpx) into a CheckResult."""
    soup = BeautifulSoup(html, "html.parser")

    # CAPTCHA detection
    if soup.find("form", attrs={"action": "/errors/validateCaptcha"}):
        return CheckResult(asin, ShippingStatus.ERROR, error_message="CAPTCHA detected")
    if soup.find("input", id="captchacharacters"):
        return CheckResult(asin, ShippingStatus.ERROR, error_message="CAPTCHA detected")
    title_text = (soup.title.string or "").lower() if soup.title else ""
    if "robot" in title_text or "captcha" in title_text or "validatecaptcha" in title_text:
        return CheckResult(asin, ShippingStatus.ERROR, error_message="CAPTCHA detected")

    # Product name
    product_name = ""
    title_el = soup.find(id="productTitle")
    if title_el:
        product_name = title_el.get_text(strip=True)
    if not product_name:
        logger.warning(f"[{asin}] httpx: productTitle not found.")
        return CheckResult(asin, ShippingStatus.ERROR, error_message="productTitle not found.")

    # Delivery text — same selector priority as Playwright path
    delivery_ids = [
        "mir-layout-DELIVERY_BLOCK",
        "ddmDeliveryMessage",
        "deliveryMessageMirId",
        "delivery-message",
        "price-shipping-message",
        "exports_feature_div",
        "shippingMessageInsideBuyBox_feature_div",
        "buybox",
        "buyBoxInner",
    ]
    raw_text = ""
    for el_id in delivery_ids:
        el = soup.find(id=el_id)
        if el:
            text = el.get_text(separator=" ", strip=True)
            if text:
                raw_text = text
                break

    if not raw_text:
        return CheckResult(asin, ShippingStatus.UNKNOWN, error_message="No delivery block found",
                           product_name=product_name)

    status = _classify(raw_text)
    logger.info(f"[{asin}] httpx: {status.value} | {raw_text[:120]!r}")
    return CheckResult(asin, status, raw_text=raw_text, product_name=product_name)


async def _check_product_httpx(asin: str, url: str, cookies: list) -> CheckResult:
    """Lightweight product check using httpx with Amazon session cookies."""
    cookie_dict = {c["name"]: c["value"] for c in cookies}
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers,
            cookies=cookie_dict,
            follow_redirects=True,
            timeout=20.0,
        ) as client:
            resp = await client.get(f"{url}?psc=1&th=1")
            if "amazon.com" not in str(resp.url):
                return CheckResult(asin, ShippingStatus.ERROR,
                                   error_message=f"Redirected away from amazon.com: {resp.url}")
            return _parse_html_delivery(resp.text, asin)
    except Exception as e:
        return CheckResult(asin, ShippingStatus.ERROR, error_message=f"httpx error: {e}")


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

    # Wait for the modal to appear and finish loading
    await _first(page, [
        "#GLUXCountryList",
        "#GLUXZipUpdateInput",
        ".a-popover-content",
        "#nav-flyout-delivery",
    ], timeout=6000)

    # Wait for loading spinner to disappear
    try:
        await page.wait_for_selector(".a-popover-loading", state="detached", timeout=5000)
        await _pause(0.5, 1.0)
    except PWTimeout:
        pass  # Loading may have already finished

    # Log page state for debugging
    try:
        nav_html = await page.inner_html("#nav-global-location-popover-link", timeout=2000)
        logger.warning(f"[DEBUG] Deliver To button HTML: {nav_html[:400]}")
    except Exception:
        pass
    try:
        body_classes = await page.evaluate("() => document.body.className")
        logger.warning(f"[DEBUG] Body classes: {body_classes[:200]}")
    except Exception:
        pass
    try:
        popup_html = await page.evaluate("""() => {
            const sel = ['#GLUXPopoverID', '.a-popover-inner', '#nav-flyout-delivery',
                         '.nav-flyout', '[data-csa-c-type=\"popover\"]', '.glow-toaster-content'];
            for (const s of sel) {
                const el = document.querySelector(s);
                if (el) return s + '::' + el.innerHTML.substring(0, 600);
            }
            return 'none found';
        }""")
        logger.warning(f"[DEBUG] Popup content: {popup_html}")
    except Exception as e:
        logger.warning(f"[DEBUG] Popup eval failed: {e}")

    # Amazon shows ZIP input by default — need to click "Ship outside the US" to get country dropdown
    dropdown = await _first(page, COUNTRY_DROPDOWN_SELECTORS, timeout=2000)
    if not dropdown:
        logger.info("Country dropdown not visible — trying 'Ship outside the US' link...")
        outside_link = await _first(page, SHIP_OUTSIDE_US_SELECTORS, timeout=4000)
        if outside_link:
            try:
                await outside_link.click()
                await _pause(1.0, 1.5)
                logger.info("Clicked 'Ship outside the US' link.")
            except Exception as e:
                logger.debug(f"Could not click outside-US link: {e}")
        else:
            logger.warning("'Ship outside the US' link not found either.")
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

    # With Israel as active location, Amazon shows "$X.XX delivery [date]" without mentioning Israel
    paid_pat_explicit = re.compile(
        r'\$[\d]+\.[\d]{2}.{0,40}israel|israel.{0,40}\$[\d]+\.[\d]{2}',
        re.IGNORECASE,
    )
    if paid_pat_explicit.search(text) and "israel" in t:
        return ShippingStatus.PAID

    # Generic paid delivery: "$X.XX delivery" or "ILS X.XX delivery" — shown when Israel is active location
    paid_pat_generic = re.compile(r'(\$|ILS|₪)\s*[\d,]+\.?\d*\s+delivery', re.IGNORECASE)
    if paid_pat_generic.search(text) and "free" not in t:
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
        self._session_cookies: list = []  # cached after each successful location refresh

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

        # Block images, media, fonts and stylesheets — we only need HTML text
        async def _block_resources(route, request):
            if request.resource_type in _BLOCK_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await self._context.route("**/*", _block_resources)

        # Set Israel delivery location once at startup (httpx first, Playwright fallback)
        logger.info("Setting delivery location to Israel (startup)...")
        success, cookies = await _try_set_location_httpx()
        if success:
            self._session_cookies = cookies
            logger.info("Startup: location set via httpx API ✓")
        else:
            logger.warning("Startup: httpx location failed — trying Playwright...")
            page = await self._context.new_page()
            try:
                await page.goto("https://www.amazon.com/dp/B00EDR1X3O?psc=1&th=1", wait_until="domcontentloaded", timeout=20000)
                await _dismiss_redirect_modal(page)
                await _pause(2.0, 3.5)
                if await _is_captcha(page):
                    logger.warning("Startup: CAPTCHA on Playwright — location NOT set, will retry on first cycle.")
                else:
                    if await _verify_location(page):
                        logger.info("Startup: location already Israel ✓ (Playwright)")
                    else:
                        set_ok = await _set_location_on_page(page, "IL")
                        if set_ok:
                            await page.reload(wait_until="domcontentloaded", timeout=15000)
                            await _pause(1.5, 2.0)
                            if await _verify_location(page):
                                logger.info("Startup: location set to Israel ✓ (Playwright)")
                            else:
                                logger.warning("Startup: location set but verify failed — will retry on first cycle.")
                        else:
                            logger.warning("Startup: could not set location — will retry on first cycle.")
            except Exception as e:
                logger.warning(f"Startup location setup failed (will retry next cycle): {e}")
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

    async def refresh_location(self) -> bool:
        """Re-set delivery location to Israel. Called at the start of each check cycle.
        Returns True if location is confirmed as Israel, False otherwise.

        Strategy:
          1. Try httpx-based API call (no browser fingerprint — less CAPTCHA risk).
          2. If that fails, fall back to Playwright UI interaction.
        """
        # ── Attempt 1: httpx API approach ─────────────────────────────────────
        logger.info("Location refresh: trying httpx API approach...")
        success, cookies = await _try_set_location_httpx()
        if success:
            self._session_cookies = cookies
            return True
        logger.warning("httpx location failed — falling back to Playwright UI")

        # ── Attempt 2: Playwright UI approach ─────────────────────────────────
        page = await self._context.new_page()
        try:
            for attempt in range(3):
                # Use a product page instead of homepage — less likely to trigger CAPTCHA
                await page.goto("https://www.amazon.com/dp/B00EDR1X3O?psc=1&th=1", wait_until="domcontentloaded", timeout=20000)
                await _pause(2.0, 3.5)
                if await _is_captcha(page):
                    logger.warning("CAPTCHA during location refresh — skipping.")
                    return False
                await _dismiss_redirect_modal(page)
                if await _verify_location(page):
                    logger.info("Location already Israel ✓ — no change needed.")
                    self._session_cookies = await self._context.cookies()
                    return True
                success = await _set_location_on_page(page, "IL")
                if success:
                    # Reload to confirm the location was saved
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
                    await _pause(1.5, 2.0)
                    if await _verify_location(page):
                        logger.info("Location verified: Israel ✓")
                        self._session_cookies = await self._context.cookies()
                        return True
                    logger.warning(f"Location verify failed (attempt {attempt + 1}/3) — retrying...")
                    await _pause(2.0, 3.0)
                else:
                    logger.warning(f"Location set failed (attempt {attempt + 1}/3) — retrying...")
                    await _pause(2.0, 3.0)
            logger.error("Failed to set Israel location after 3 attempts — skipping check cycle.")
            return False
        except Exception as e:
            logger.warning(f"Location refresh error (ignored): {e}")
            return False
        finally:
            await page.close()

    async def check(self, asin: str, url: str) -> CheckResult:
        """Check a single product via Playwright. Serialized via lock to avoid CAPTCHA triggers."""
        async with self._lock:
            page = await self._context.new_page()
            try:
                result = await check_product(page, asin, url)
            finally:
                await page.close()

        # CAPTCHA retry: wait outside the lock, then retry once
        if result.status == ShippingStatus.ERROR and "CAPTCHA" in result.error_message:
            logger.warning(f"[{asin}] Playwright CAPTCHA — waiting 45s before retry...")
            await asyncio.sleep(45)
            async with self._lock:
                page = await self._context.new_page()
                try:
                    result = await check_product(page, asin, url)
                finally:
                    await page.close()

        return result

    async def check_many(self, products: list) -> list:
        """Check multiple products in parallel.

        Strategy per product:
          1. Try httpx (fast, lightweight) using cached session cookies.
          2. If httpx returns an error or CAPTCHA → fall back to Playwright check().
        Returns results in the same order as `products`.
        """
        if not products:
            return []

        semaphore = asyncio.Semaphore(4)

        async def _check_one(idx: int, asin: str, url: str):
            async with semaphore:
                # Small random stagger to avoid request bursts
                await asyncio.sleep(random.uniform(0.0, 2.0))

                if self._session_cookies:
                    result = await _check_product_httpx(asin, url, self._session_cookies)
                    if result.status == ShippingStatus.ERROR:
                        logger.warning(
                            f"[{asin}] httpx failed ({result.error_message}) — falling back to Playwright"
                        )
                        result = await self.check(asin, url)
                else:
                    # No cookies yet (first cycle before location refresh) — use Playwright
                    result = await self.check(asin, url)

                return idx, result

        tasks = [_check_one(i, asin, url) for i, (asin, url) in enumerate(products)]
        indexed = await asyncio.gather(*tasks)
        indexed.sort(key=lambda x: x[0])
        return [r for _, r in indexed]


browser_manager = BrowserManager()
