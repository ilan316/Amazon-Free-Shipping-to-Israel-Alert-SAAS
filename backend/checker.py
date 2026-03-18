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
from curl_cffi.requests import AsyncSession as CurlSession
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

# Residential proxy for httpx location requests (HTTP/HTTPS format)
NORDVPN_PROXY = os.environ.get("RESIDENTIAL_PROXY", "")

# Residential proxy for Playwright browser (SOCKS5 format with sticky session)
# Format: socks5h://user-SESSION-country-us:pass@gate.decodo.com:7000
_PLAYWRIGHT_PROXY_URL = os.environ.get("PLAYWRIGHT_PROXY", "")


def _parse_playwright_proxy(url: str) -> dict | None:
    """Parse a proxy URL into Playwright's proxy dict format."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        scheme = "socks5" if p.scheme.startswith("socks5") else p.scheme
        return {
            "server": f"{scheme}://{p.hostname}:{p.port}",
            "username": p.username or "",
            "password": p.password or "",
        }
    except Exception:
        return None

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


async def _proxy_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """Quick TCP connectivity check — returns False if port is blocked/unreachable."""
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


# ── httpx location-setting via Amazon address-change API ──────────────────────

def _extract_csrf(html: str) -> str:
    """Extract Amazon anti-CSRF token from HTML via multiple known locations."""
    soup = BeautifulSoup(html, "html.parser")

    # Method 1: hidden input field
    el = soup.find("input", attrs={"name": "anti-csrftoken-a2z"})
    if el and el.get("value"):
        return el["value"]

    # Method 2: span with data attribute (used in some page variants)
    el = soup.find(attrs={"data-anti-csrftoken-a2z": True})
    if el:
        return el["data-anti-csrftoken-a2z"]

    # Method 3: script tag — "csrfToken":"<value>"
    m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', html)
    if m:
        return m.group(1)

    # Method 4: anti-csrftoken-a2z in any data attribute
    m = re.search(r'anti-csrftoken-a2z["\s:=]+([A-Za-z0-9+/=]{20,})', html)
    if m:
        return m.group(1)

    return ""


async def _try_set_location_httpx(proxy_url: str = "") -> tuple:
    """Set Israel delivery location by calling Amazon's internal address-change API.
    Uses curl_cffi to impersonate Chrome's TLS fingerprint — bypasses Amazon bot detection.
    Returns (success: bool, cookies: list) tuple."""
    base_headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else {}
    try:
        async with CurlSession(impersonate="chrome120") as session:
            # Step 1: Fetch a product page for session cookies + CSRF token
            resp = await session.get(
                "https://www.amazon.com/dp/B00EDR1X3O?psc=1&th=1",
                headers=base_headers, proxies=proxies, allow_redirects=True, timeout=30,
            )
            if "validateCaptcha" in str(resp.url) or "captchacharacters" in resp.text:
                logger.warning("curl_cffi location: CAPTCHA on initial page fetch")
                return False, []

            csrf_token = _extract_csrf(resp.text)
            logger.info(f"curl_cffi location: initial fetch status={resp.status_code}, csrf={'found' if csrf_token else 'NOT found'}")

            # Step 2: POST to address-change — try countryCode=IL, then zip
            api_headers = {
                **base_headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://www.amazon.com/",
                "anti-csrftoken-a2z": csrf_token,
                "X-Requested-With": "XMLHttpRequest",
            }
            payloads = [
                {"locationType": "LOCATION_INPUT", "zipCode": "", "countryCode": "IL",
                 "storeContext": "generic", "deviceType": "web",
                 "pageType": "Detail", "actionSource": "glow"},
                {"locationType": "LOCATION_INPUT", "zipCode": "6100001",
                 "storeContext": "generic", "deviceType": "web",
                 "pageType": "Detail", "actionSource": "glow"},
            ]

            api_resp = None
            for i, payload in enumerate(payloads):
                r = await session.post(
                    "https://www.amazon.com/portal-migration/hz/glow/address-change",
                    data=payload, headers=api_headers,
                    proxies=proxies, allow_redirects=True, timeout=30,
                )
                body_preview = r.text[:200].replace("\n", " ")
                logger.info(f"curl_cffi location: variant {i+1} → {r.status_code} | {body_preview!r}")
                if r.status_code in (200, 302):
                    api_resp = r
                    break

            if not api_resp:
                return False, []

            # Step 3: Delay then verify location on product page
            await asyncio.sleep(2.0)
            resp3 = await session.get(
                "https://www.amazon.com/dp/B00EDR1X3O?psc=1&th=1",
                headers=base_headers, proxies=proxies, allow_redirects=True, timeout=30,
            )
            if "validateCaptcha" in str(resp3.url) or "captchacharacters" in resp3.text:
                logger.warning("curl_cffi location: CAPTCHA on verification page")
                return False, []

            soup3 = BeautifulSoup(resp3.text, "html.parser")
            nav_text = ""
            for nav_id in ["glow-ingress-line2", "glow-ingress-line1",
                           "nav-global-location-popover-link"]:
                el = soup3.find(id=nav_id)
                if el:
                    nav_text = el.get_text(strip=True).lower()
                    break

            logger.info(f"curl_cffi location: verification nav text = {nav_text!r}")

            if "israel" in nav_text or "israel" in resp3.text.lower():
                cookie_list = [{"name": k, "value": v} for k, v in session.cookies.items()]
                logger.info(f"curl_cffi location: Israel confirmed ✓ ({len(cookie_list)} cookies)")
                return True, cookie_list

            logger.warning("curl_cffi location: Israel NOT confirmed")
            return False, []
    except Exception as e:
        logger.warning(f"curl_cffi location: error — {e}")
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
    """Lightweight product check using curl_cffi with Amazon session cookies and residential proxy."""
    cookie_dict = {c["name"]: c["value"] for c in cookies}
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    proxies = {"https": NORDVPN_PROXY, "http": NORDVPN_PROXY} if NORDVPN_PROXY else {}
    try:
        async with CurlSession(impersonate="chrome120") as session:
            resp = await session.get(
                f"{url}?psc=1&th=1",
                headers=headers,
                cookies=cookie_dict,
                proxies=proxies,
                allow_redirects=True,
                timeout=20,
            )
            if "amazon.com" not in str(resp.url):
                return CheckResult(asin, ShippingStatus.ERROR,
                                   error_message=f"Redirected away from amazon.com: {resp.url}")
            return _parse_html_delivery(resp.text, asin)
    except Exception as e:
        return CheckResult(asin, ShippingStatus.ERROR, error_message=f"httpx error: {e}")


async def _check_aod_httpx(asin: str, cookies: list) -> CheckResult:
    """Fetch the AOD (All Offers Display) AJAX panel via curl_cffi and parse delivery text.
    Used as a fallback for UNKNOWN products that require 'See All Buying Options'.
    """
    cookie_dict = {c["name"]: c["value"] for c in cookies}
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": f"https://www.amazon.com/dp/{asin}?psc=1&th=1",
        "X-Requested-With": "XMLHttpRequest",
    }
    proxies = {"https": NORDVPN_PROXY, "http": NORDVPN_PROXY} if NORDVPN_PROXY else {}
    try:
        async with CurlSession(impersonate="chrome120") as session:
            resp = await session.get(
                f"https://www.amazon.com/gp/aod/ajax?asin={asin}&pc=dp",
                headers=headers,
                cookies=cookie_dict,
                proxies=proxies,
                allow_redirects=True,
                timeout=20,
            )
            if resp.status_code != 200:
                return CheckResult(asin, ShippingStatus.ERROR,
                                   error_message=f"AOD status {resp.status_code}")
            if "validateCaptcha" in str(resp.url) or "captchacharacters" in resp.text:
                return CheckResult(asin, ShippingStatus.ERROR, error_message="AOD: CAPTCHA detected")

            soup = BeautifulSoup(resp.text, "html.parser")
            # Try offer-list container first, fall back to full page text
            offer_el = soup.find(id="aod-offer-list") or soup.find(id="aod-container") or soup.find(id="aod-offer")
            raw_text = offer_el.get_text(separator=" ", strip=True) if offer_el else soup.get_text(separator=" ", strip=True)
            if not raw_text:
                return CheckResult(asin, ShippingStatus.UNKNOWN, error_message="AOD: no text found")

            status = _classify(raw_text)
            logger.info(f"[{asin}] AOD httpx: {status.value} | {raw_text[:120]!r}")
            return CheckResult(asin, status, raw_text=raw_text[:500],
                               found_in_aod=(status == ShippingStatus.FREE))
    except Exception as e:
        return CheckResult(asin, ShippingStatus.ERROR, error_message=f"AOD error: {e}")


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

async def _set_location_js(page: Page) -> bool:
    """Set delivery location to Israel via in-page JS fetch.
    Faster than UI — no modal interaction, uses real CSRF token from page JS context.
    """
    try:
        result = await page.evaluate("""async () => {
            const csrf =
                (typeof ue !== 'undefined' && ue?.idb?.csrfToken) ||
                document.querySelector('[data-anti-csrftoken-a2z]')?.getAttribute('data-anti-csrftoken-a2z') ||
                document.querySelector('input[name="anti-csrftoken-a2z"]')?.value || '';
            if (!csrf) return {ok: false, reason: 'no_csrf'};
            try {
                const r = await fetch('/portal-migration/hz/glow/address-change', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'anti-csrftoken-a2z': csrf,
                        'X-Requested-With': 'XMLHttpRequest'
                    },
                    body: new URLSearchParams({
                        locationType: 'LOCATION_INPUT', zipCode: '', countryCode: 'IL',
                        storeContext: 'generic', deviceType: 'web',
                        pageType: 'Detail', actionSource: 'glow'
                    }).toString()
                });
                return {ok: r.ok, status: r.status, csrf_len: csrf.length};
            } catch(e) {
                return {ok: false, reason: 'fetch_failed', error: String(e)};
            }
        }""")
        ok = result.get("ok", False)
        logger.info(f"JS location: status={result.get('status')}, csrf_len={result.get('csrf_len', 0)}, ok={ok}")
        return ok
    except Exception as e:
        logger.warning(f"JS location set failed: {e}")
        return False


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
        # Playwright runs WITHOUT proxy — httpx handles the proxy for location-setting
        _pw_proxy = _parse_playwright_proxy(_PLAYWRIGHT_PROXY_URL)
        if _pw_proxy:
            logger.info(f"Playwright: using residential proxy {_pw_proxy['server']}")
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=BROWSER_PROFILE_DIR,
            headless=True,
            slow_mo=80,
            proxy=_pw_proxy,
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

        # Load persisted cookies from DB (saved by inject-cookies endpoint)
        try:
            import json
            from backend.database import AsyncSessionLocal
            from backend.models import SystemSetting
            from sqlalchemy import select as sa_select
            async with AsyncSessionLocal() as _db:
                row = (await _db.execute(
                    sa_select(SystemSetting).where(SystemSetting.key == "amazon_session_cookies")
                )).scalar_one_or_none()
                if row and row.value:
                    self._session_cookies = json.loads(row.value)
                    logger.info(f"Startup: loaded {len(self._session_cookies)} cookies from DB ✓")
        except Exception as e:
            logger.warning(f"Startup: failed to load cookies from DB: {e}")

        if self._session_cookies:
            logger.info("Startup: using cookies from DB — skipping curl_cffi location setup.")
        else:
            # Set Israel delivery location via httpx only at startup (non-blocking)
            # Playwright is NOT used here — it can hang for minutes through proxies.
            # If httpx fails, the first check cycle will retry via refresh_location().
            logger.info("Setting delivery location to Israel (startup via curl_cffi)...")
            cffi_ok, cookies = await _try_set_location_httpx(proxy_url=NORDVPN_PROXY)
            if cffi_ok and cookies:
                self._session_cookies = cookies
                logger.info("Startup: location set to Israel via curl_cffi ✓")
            else:
                logger.warning("Startup: curl_cffi location failed — will retry on first cycle.")

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
        Tries httpx first (less detectable), falls back to Playwright.
        Returns True if location is confirmed as Israel, False otherwise.
        """
        # 1. Try curl_cffi (Chrome TLS fingerprint impersonation)
        cffi_ok, cookies = await _try_set_location_httpx(proxy_url=NORDVPN_PROXY)
        if cffi_ok and cookies:
            self._session_cookies = cookies
            logger.info("Location set to Israel via curl_cffi ✓")
            return True

        logger.warning("curl_cffi location failed — falling back to Playwright...")

        # 2. Playwright fallback — hard timeout to prevent hanging
        try:
            return await asyncio.wait_for(self._refresh_location_playwright(), timeout=90)
        except asyncio.TimeoutError:
            logger.error("Playwright location timed out after 90s.")
            return False

    async def _refresh_location_playwright(self) -> bool:
        """Playwright-based location setter. Called only as fallback from refresh_location."""
        page = await self._context.new_page()
        try:
            for attempt in range(3):
                await page.goto("https://www.amazon.com/dp/B00EDR1X3O?psc=1&th=1",
                                wait_until="domcontentloaded", timeout=90000)
                await _pause(2.0, 3.5)
                if await _is_captcha(page):
                    logger.warning("CAPTCHA during location refresh — skipping.")
                    return False
                await _dismiss_redirect_modal(page)
                if await _verify_location(page):
                    self._session_cookies = await self._context.cookies()
                    logger.info("Location already Israel ✓ (Playwright)")
                    return True
                set_ok = await _set_location_js(page)
                if set_ok:
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
                    await _pause(1.5, 2.0)
                    if await _verify_location(page):
                        self._session_cookies = await self._context.cookies()
                        logger.info("Location set to Israel via Playwright JS ✓")
                        return True
                set_ok = await _set_location_on_page(page, "IL")
                if set_ok:
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
                    await _pause(1.5, 2.0)
                    if await _verify_location(page):
                        self._session_cookies = await self._context.cookies()
                        logger.info("Location set to Israel via Playwright UI ✓")
                        return True
                logger.warning(f"Location set failed (attempt {attempt + 1}/3)")
                await _pause(2.0, 3.0)
            logger.error("Failed to set Israel location after all attempts.")
            return False
        except Exception as e:
            logger.warning(f"Location refresh error: {e}")
            return False
        finally:
            await page.close()

    async def _inject_cookies_to_context(self):
        """Inject current session cookies into the Playwright browser context."""
        if not self._session_cookies:
            return
        try:
            pw_cookies = [
                {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": ".amazon.com",
                    "path": "/",
                }
                for c in self._session_cookies
            ]
            await self._context.add_cookies(pw_cookies)
        except Exception as e:
            logger.warning(f"Failed to inject cookies into Playwright context: {e}")

    async def check(self, asin: str, url: str) -> CheckResult:
        """Check a single product via Playwright. Serialized via lock to avoid CAPTCHA triggers."""
        async with self._lock:
            await self._inject_cookies_to_context()
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
                await self._inject_cookies_to_context()
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
                    elif result.status == ShippingStatus.UNKNOWN:
                        # Main page shows no delivery info — try AOD panel
                        logger.info(f"[{asin}] UNKNOWN — trying AOD panel via curl_cffi")
                        aod_result = await _check_aod_httpx(asin, self._session_cookies)
                        if aod_result.status != ShippingStatus.ERROR:
                            result = aod_result
                        else:
                            logger.warning(f"[{asin}] AOD also failed: {aod_result.error_message}")
                else:
                    # No cookies yet (first cycle before location refresh) — use Playwright
                    result = await self.check(asin, url)

                return idx, result

        tasks = [_check_one(i, asin, url) for i, (asin, url) in enumerate(products)]
        indexed = await asyncio.gather(*tasks)
        indexed.sort(key=lambda x: x[0])
        return [r for _, r in indexed]


browser_manager = BrowserManager()
