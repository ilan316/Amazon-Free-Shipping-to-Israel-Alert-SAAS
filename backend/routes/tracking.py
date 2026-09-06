import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import EmailClick, EmailOpen

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tracking"])

# Known email security scanner / bot UA fragments — all lowercase
_BOT_UA_FRAGMENTS = (
    "bot", "crawl", "spider", "scan",
    "microsoft", "outlook", "office", "feedfetch",
    "barracuda", "proofpoint", "mimecast", "symantec",
    "trendmicro", "ironport", "postini", "cisco",
    "googlebot", "bingpreview",
    "safebrowsing", "phishtank", "urlscan",
)
# NOTE: googleimageproxy / ggpht.com are intentionally NOT blocked —
# Gmail fires its image proxy on every real user open (not on arrival),
# so these hits represent actual opens and should be counted.

_CLICK_DEDUP_MINUTES = 5

# A blog announcement has no single price, so its brand bar carries this label
# in the price slot instead. Keys match BlogSocialQueue.kind.
_KIND_LABEL = {"review": "סקירה", "guide": "מדריך"}

# Apple Mail Privacy Protection: Apple pre-fetches images on delivery (not on open).
# Detected by Apple's allocated IP block (17.0.0.0/8) or AppleMail UA strings.
_APPLE_MPP_UA_FRAGMENTS = ("applemail", "apple mail")


def _client_ip(request: Request) -> str | None:
    """Extract the caller's IP and immediately truncate it.

    A full IP is personal data, and the privacy policy states we do not store
    location data. Truncating to /24 (IPv4) or /48 (IPv6) keeps everything the
    tracking code actually needs — the Apple MPP (17.x) and Google Image Proxy
    (66.249.x) prefix checks are unaffected — while dropping the part that
    identifies an individual subscriber.
    """
    raw = request.headers.get("X-Forwarded-For", request.client.host if request.client else None)
    if not raw:
        return None
    raw = raw.split(",")[0].strip()[:64]
    if ":" in raw:  # IPv6 — keep the first three hextets (/48)
        parts = [p for p in raw.split(":") if p]
        return ":".join(parts[:3]) + "::" if parts else None
    octets = raw.split(".")
    if len(octets) == 4:
        return ".".join(octets[:3]) + ".0"
    return None


def _is_bot(ua: str) -> bool:
    """Return True if User-Agent looks like an email security scanner or bot."""
    if not ua or len(ua) < 5:
        return True
    lower = ua.lower()
    return any(frag in lower for frag in _BOT_UA_FRAGMENTS)


def _is_apple_mpp(ua: str, ip: str | None) -> bool:
    """Return True if this open looks like a machine pre-fetch (Apple MPP or Google Image Proxy)."""
    if ip and ip.startswith("17."):
        return True
    # Google Image Proxy range (66.249.x.x) — fires on delivery, not on real open
    if ip and ip.startswith("66.249."):
        return True
    ua_lower = ua.lower()
    return any(frag in ua_lower for frag in _APPLE_MPP_UA_FRAGMENTS)

_ALLOWED_PREFIXES = (
    "https://www.amazon.com/",
    "https://app.amzfreeil.com/",
    "https://amzfreeil.com/",
    "https://www.amzfreeil.com/",
)


@router.get("/go/{asin}", include_in_schema=False)
async def go_asin(asin: str):
    """Short redirect for Facebook/social posts: /go/ASIN → Amazon affiliate URL."""
    import os, re
    if not re.fullmatch(r"[A-Z0-9]{10}", asin.upper()):
        return RedirectResponse("https://www.amazon.com/", status_code=302)
    tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    asin = asin.upper()
    dest = f"https://www.amazon.com/dp/{asin}?tag={tag}" if tag else f"https://www.amazon.com/dp/{asin}"
    return RedirectResponse(dest, status_code=302)


async def _normalized_ig_jpeg(source_url: str, label: str, *,
                              name: str | None = None,
                              price: str | None = None,
                              badge: str | None = None) -> Response:
    """Fetch an image URL and re-serve it as a valid JPEG padded to 1:1, for Instagram's
    Graph API — unlike Facebook/Telegram, IG rejects non-JPEG formats and aspect
    ratios outside 4:5-1.91:1, and Amazon's raw product images routinely violate both.

    When `name`/`price`/`badge` are given, brand bars are drawn over the top and bottom;
    the product is then shrunk into the band between them so the bars don't crop it."""
    import re
    from io import BytesIO
    import httpx
    from PIL import Image

    def _hires(u: str) -> str:
        """Amazon's CDN serves any size via the URL modifier token; the scraped URL is
        often a ~300px thumbnail, far too small for IG's 1080px feed rendering."""
        return re.sub(r"\._[^/]*?(\.(?:jpg|jpeg|png|webp))$", r"._SL1500_\1",
                      u, flags=re.I)

    def _fit(im: "Image.Image", box_w: int, box_h: int) -> "Image.Image":
        """Like Image.thumbnail, but scales up as well as down — many catalog assets
        top out at 500px even at _SL1500_, and the canvas is no longer their size."""
        k = min(box_w / im.width, box_h / im.height)
        return im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))),
                         Image.LANCZOS)

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = None
            hires_url = _hires(source_url)
            if hires_url != source_url:
                try:
                    hires_resp = await client.get(hires_url)
                    if hires_resp.status_code == 200:
                        resp = hires_resp
                except Exception:
                    pass
            if resp is None:
                resp = await client.get(source_url)
        if resp.status_code != 200:
            return Response(status_code=404)

        img = Image.open(BytesIO(resp.content))
        if img.mode in ("RGBA", "P", "LA"):
            rgba = img.convert("RGBA")
            flattened = Image.new("RGB", rgba.size, (255, 255, 255))
            flattened.paste(rgba, mask=rgba.split()[-1])
            img = flattened
        elif img.mode != "RGB":
            img = img.convert("RGB")

        side = min(max(img.width, img.height), 1440)  # IG recommended max edge
        overlay = bool(name or price or badge)
        if overlay:
            # The bars and their Hebrew text are drawn at canvas scale, so a 500px
            # source would ship 500px-worth of type for IG to blow up to 1080 in the
            # feed. Floor the canvas at IG's feed width so the type is born sharp.
            side = max(side, 1080)
        canvas = Image.new("RGB", (side, side), (255, 255, 255))

        if overlay:
            from backend.image_overlay import FREE_BAND, draw_bars, _TOP_H
            band_h = int(side * FREE_BAND)
            fitted = _fit(img, side, band_h)
            canvas.paste(fitted, ((side - fitted.width) // 2,
                                  int(side * _TOP_H) + (band_h - fitted.height) // 2))
            try:
                draw_bars(canvas, name, price, badge)
            except Exception as e:
                # A post without bars beats a post that never goes out.
                logger.warning(f"[ig-image] overlay failed for {label}: {e}")
                canvas = Image.new("RGB", (side, side), (255, 255, 255))
                fitted = _fit(img, side, side)
                canvas.paste(fitted, ((side - fitted.width) // 2,
                                      (side - fitted.height) // 2))
        else:
            img.thumbnail((side, side))
            canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))

        buf = BytesIO()
        canvas.save(buf, format="JPEG", quality=85)
        return Response(content=buf.getvalue(), media_type="image/jpeg",
                         headers={"Cache-Control": "public, max-age=3600"})
    except Exception as e:
        logger.warning(f"[ig-image] failed for {label}: {e}")
        return Response(status_code=404)


@router.get("/ig-image/blog/{queue_id}.jpg", include_in_schema=False)
async def ig_image_blog(queue_id: int, db: Annotated[AsyncSession, Depends(get_db)]):
    """Same normalization as /ig-image/{asin}.jpg, but for blog announcements — the
    source URL comes from the queue row itself (guides have no ASIN). Reading the URL
    from the DB rather than a query param keeps this from becoming an open image proxy."""
    from backend.models import BlogSocialQueue

    row = (await db.execute(
        select(BlogSocialQueue).where(BlogSocialQueue.id == queue_id)
    )).scalar_one_or_none()
    if not row or not row.image_url:
        return Response(status_code=404)
    return await _normalized_ig_jpeg(row.image_url, f"blog:{queue_id}",
                                     name=row.title,
                                     badge=_KIND_LABEL.get(row.kind or "review", "סקירה"))


@router.get("/ig-image/{asin}.jpg", include_in_schema=False)
async def ig_image(asin: str, db: Annotated[AsyncSession, Depends(get_db)]):
    """Instagram-safe JPEG for a scanner product image, by ASIN."""
    import re
    from backend.models import Product

    if not re.fullmatch(r"[A-Z0-9]{10}", asin.upper()):
        return Response(status_code=404)
    result = await db.execute(select(Product).where(Product.asin == asin.upper()))
    product = result.scalar_one_or_none()
    if not product or not product.image_url:
        return Response(status_code=404)
    return await _normalized_ig_jpeg(product.image_url, asin,
                                     name=product.name_he, price=product.last_price)


@router.get("/track/click", include_in_schema=False)
async def track_click(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    url: str = "",
    u: int | None = None,
    a: str = "",
):
    # Security: block open redirects — only allow Amazon URLs
    if not url or not any(url.startswith(p) for p in _ALLOWED_PREFIXES):
        return RedirectResponse("https://www.amazon.com/", status_code=302)

    # Record the click (best-effort — never block the redirect on DB errors)
    try:
        ua = request.headers.get("User-Agent", "")
        if _is_bot(ua):
            logger.info(f"click BLOCKED (bot UA): u={u} a={a} ua={ua[:120]}")
        else:
            ip = _client_ip(request)
            # Dedup: same user + ASIN within 5 min = scanner duplicate, skip
            if u and a:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=_CLICK_DEDUP_MINUTES)
                recent = (await db.execute(
                    select(EmailClick).where(
                        EmailClick.user_id == u,
                        EmailClick.asin == a[:10],
                        EmailClick.clicked_at >= cutoff,
                    ).limit(1)
                )).scalar_one_or_none()
                if recent:
                    logger.debug(f"click DEDUP (same user+ASIN <{_CLICK_DEDUP_MINUTES}m): u={u} a={a}")
                    return RedirectResponse(url, status_code=302)
            click = EmailClick(user_id=u, asin=a[:10] if a else "", ip=ip, dest_url=url[:512])
            db.add(click)
            # A real click is a sign of life — pull the user out of auto-vacation
            if u:
                from backend.models import User
                from backend.vacation import resume_if_auto
                clicker = (await db.execute(select(User).where(User.id == u))).scalar_one_or_none()
                if clicker:
                    await resume_if_auto(db, clicker)
            await db.commit()
    except Exception as exc:
        logger.warning(f"Failed to record email click: {exc}")

    return RedirectResponse(url, status_code=302)


# 1x1 transparent GIF
_PIXEL = bytes([
    0x47,0x49,0x46,0x38,0x39,0x61,0x01,0x00,0x01,0x00,0x80,0x00,0x00,
    0xff,0xff,0xff,0x00,0x00,0x00,0x21,0xf9,0x04,0x00,0x00,0x00,0x00,0x00,
    0x2c,0x00,0x00,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0x02,0x02,0x44,0x01,0x00,0x3b
])


@router.get("/track/email-open", include_in_schema=False)
async def track_email_open(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    uid: int | None = None,
    tid: int | None = None,
    tn: str | None = None,
):
    try:
        if uid and (tid or tn):
            ua = request.headers.get("User-Agent", "")
            if _is_bot(ua):
                logger.info(f"email-open BLOCKED (bot UA): uid={uid} tid={tid} tn={tn} ua={ua[:120]}")
            else:
                ip = _client_ip(request)
                suspicious = _is_apple_mpp(ua, ip)
                logger.info(f"email-open {'SUSPICIOUS(MPP)' if suspicious else 'ALLOWED'}: uid={uid} tid={tid} tn={tn} ua={ua[:120]} ip={ip}")
                from datetime import datetime, timezone
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
                dedup_filter = [
                    EmailOpen.user_id == uid,
                    EmailOpen.opened_at >= cutoff,
                ]
                if tid:
                    dedup_filter.append(EmailOpen.template_id == tid)
                elif tn:
                    dedup_filter.append(EmailOpen.template_name == tn)
                recent = (await db.execute(
                    select(EmailOpen).where(*dedup_filter).limit(1)
                )).scalar_one_or_none()
                if not recent:
                    db.add(EmailOpen(
                        user_id=uid, template_id=tid,
                        template_name=tn[:100] if tn else None,
                        ip=ip, user_agent=ua[:300] if ua else None,
                        is_suspicious=suspicious,
                    ))
                    await db.commit()
                else:
                    logger.debug(f"email-open ignored (dedup): uid={uid} tid={tid} tn={tn}")
    except Exception as exc:
        logger.warning(f"Failed to record email open: {exc}")

    return Response(content=_PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})
