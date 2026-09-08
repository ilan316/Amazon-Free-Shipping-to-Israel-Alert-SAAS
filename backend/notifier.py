"""
Email notifier — Resend version.

Key changes vs Gmail SMTP version:
  - Uses Resend API instead of smtplib
  - Sends from alerts@amzfreeil.com (authenticated domain)
  - Added send_daily_summary() for daily digest emails
"""

import os
import re
import logging
from datetime import datetime
from urllib.parse import urlencode
from backend.auth import create_pause_token, create_product_pause_token
from backend.checker import extract_free_shipping_threshold

import resend as resend_client

logger = logging.getLogger(__name__)

_MAX_NAME_SUBJECT = 72
_MAX_NAME_BODY = 88

# ── Localized strings ─────────────────────────────────────────────────────────
_STRINGS = {
    "he": {
        "subject_single":       "✅ משלוח חינם לישראל: {name}",
        "subject_summary":      "סיכום יומי: {n} מוצרים עם משלוח חינם 🚚",
        "preheader":            "מצאנו משלוח חינם לישראל! בדוק את המוצר שלך עכשיו",
        "header_title":         "משלוח חינם לישראל 🚚",
        "header_sub1":          "נמצא מוצר עם משלוח חינם",
        "header_summary":       "סיכום יומי · נימצאו {n} מוצרים עם משלוח חינם",
        "shipping_badge":       "✅ משלוח חינם לישראל · ניתן למימוש בהזמנות מעל $49",
        "btn_buy":              "קנה עכשיו — משלוח חינם",
        "urgency":              "⏰ המחיר עשוי להשתנות בכל עת",
        "quick_tip_title":      "💡 טיפ לחיסכון",
        "quick_tip_body":       "",  # populated dynamically by _daily_tip()
        "disclosure":           "קישור שותף — הקנייה לא עולה לך יותר, אך אנו עשויים לקבל עמלה קטנה.",
        "footer":               "נבדק: {checked_at} · Amazon Free Shipping to Israel Alert",
        "aod_note":             "⚠️ המשלוח החינמי נמצא תחת <strong>\"כל אפשרויות הקנייה\"</strong>.<br>"
                                "פתח את עמוד המוצר ← לחץ <strong>\"ראה את כל אפשרויות הקנייה\"</strong>"
                                " ← בחר את ההצעה עם משלוח חינם.",
        "aod_plain":            "הערה: המשלוח החינמי נמצא תחת 'כל אפשרויות הקנייה'. "
                                "פתח את הקישור ← לחץ 'ראה את כל אפשרויות הקנייה' ← בחר הצעה עם משלוח חינם.",
        "plain_header":         "✅ התראת משלוח חינם לישראל!\n",
        "plain_summary_header": "📦 סיכום יומי — משלוח חינם לישראל\n",
        "plain_product":        "מוצר",
        "plain_url":            "קישור",
        "plain_urgency":        "⏰ המחיר עשוי להשתנות בכל עת",
        "plain_footer":         "נבדק: {checked_at}",
        # Weekly PAID digest
        "subject_weekly_paid":  "📦 סיכום שבועי: {n} מוצרים במשלוח בתשלום",
        "header_weekly_paid":   "סיכום שבועי — משלוח בתשלום",
        "header_weekly_sub":    "{n} מוצרים ברשימה שלך",
        "weekly_scope_note":    "המייל הזה מרכז רק מוצרים שהמשלוח שלהם לישראל בתשלום. "
                                "מוצרים שברשימה שלך שנמצאים במשלוח חינם ממשיכים להגיע אליך בסיכום היומי.",
        "weekly_tip_title":     "💡 שווה לדעת",
        "weekly_tip_body":      "הסכומים שלמעלה הם מה שאמזון מציגה לפני התשלום — מוצר, משלוח ומיסים יחד. "
                                "אמזון גובה את הכול מראש, כך שלא מגיע חיוב נוסף כשהחבילה נכנסת לארץ.",
        "plain_weekly_header":  "📦 סיכום שבועי — מוצרים במשלוח בתשלום\n",
        "btn_view":             "צפה באמזון",
        "paid_since":           "🚚 במשלוח בתשלום כבר {days} ימים",
        "trend_flat":           "⟷ המחיר לא זז מאז השבוע שעבר",
        "trend_price_down":     "▼ ירד ב-₪{delta} מאז השבוע שעבר",
        "trend_price_up":       "▲ עלה ב-₪{delta} מאז השבוע שעבר",
        "trend_ship_down":      "▼ עלות המשלוח ירדה ב-₪{delta} מאז השבוע שעבר",
        "trend_ship_up":        "▲ עלות המשלוח עלתה ב-₪{delta} מאז השבוע שעבר",
    },
    "en": {
        "subject_single":       "✅ FREE Shipping to Israel: {name}",
        "subject_summary":      "Daily digest: {n} products with free shipping 🚚",
        "preheader":            "Don't miss out! Price may change at any time — check now",
        "header_title":         "FREE Shipping to Israel 🚚",
        "header_sub1":          "1 product with free shipping found",
        "header_summary":       "Daily digest · {n} products with free shipping",
        "shipping_badge":       "✅ FREE Shipping to Israel · Orders $49+",
        "btn_buy":              "Buy Now",
        "urgency":              "⏰ Price may change at any time",
        "quick_tip_title":      "💡 Money-Saving Tip",
        "quick_tip_body":       "",  # populated dynamically by _daily_tip()
        "disclosure":           "Affiliate link — no extra cost to you, but we may earn a small commission.",
        "footer":               "Checked at: {checked_at} · Amazon Free Shipping to Israel Alert",
        "aod_note":             "⚠️ Free shipping found in <strong>All Buying Options</strong>.<br>"
                                "Open the product page → click <strong>\"See All Buying Options\"</strong>"
                                " → select the offer with free shipping.",
        "aod_plain":            "NOTE: Found in All Buying Options — open the link, "
                                "click 'See All Buying Options', select the free-shipping offer.",
        "plain_header":         "✅ FREE Shipping to Israel Alert!\n",
        "plain_summary_header": "📦 Daily Digest — FREE Shipping to Israel\n",
        "plain_product":        "Product",
        "plain_url":            "URL    ",
        "plain_urgency":        "⏰ Price may change at any time",
        "plain_footer":         "Checked at: {checked_at}",
        # Weekly PAID digest
        "subject_weekly_paid":  "📦 Weekly digest: {n} products with paid shipping",
        "header_weekly_paid":   "Weekly digest — paid shipping",
        "header_weekly_sub":    "{n} products on your list",
        "weekly_scope_note":    "This email covers only products whose shipping to Israel is paid. "
                                "Products on your list with free shipping keep arriving in the daily digest.",
        "weekly_tip_title":     "💡 Good to know",
        "weekly_tip_body":      "The totals above are what Amazon shows before checkout — item, shipping and taxes "
                                "together. Amazon charges it all upfront, so no extra bill arrives with the parcel.",
        "plain_weekly_header":  "📦 Weekly digest — products with paid shipping\n",
        "btn_view":             "View on Amazon",
        "paid_since":           "🚚 Paid shipping for {days} days",
        "trend_flat":           "⟷ Unchanged since last week",
        "trend_price_down":     "▼ Down ILS {delta} since last week",
        "trend_price_up":       "▲ Up ILS {delta} since last week",
        "trend_ship_down":      "▼ Shipping down ILS {delta} since last week",
        "trend_ship_up":        "▲ Shipping up ILS {delta} since last week",
    },
}


_DAILY_TIPS: dict[str, list[str]] = {
    "he": [
        "הזמינו בין $49 ל-$75 כדי ליהנות ממשלוח חינם ללא מכס ישראלי.",
        "מעל $75? ייתכן מכס של 18% מע\"מ + אגרת שחרור — חשבו פעמיים לפני הקנייה.",
        "כמה פריטים קטנים? שווה לאחד להזמנה אחת מעל $49 ולחסוך בדמי משלוח.",
        "פריטים מתחת ל-$49 גובים דמי משלוח — בדקו אם הוספת פריט נוסף חוסכת כסף.",
        "מחיר נמוך מדי? בדקו שהמוכר הוא <bdi>Amazon</bdi> ולא <bdi>Third-party</bdi> עם מדיניות החזרה שונה.",
        '<bdi>"Ships from and sold by Amazon.com"</bdi> = המחיר שתראו כאן הוא אמיתי וכולל <bdi>Prime</bdi>.',
        'מיין לפי <bdi>"4 stars and up"</bdi> + <bdi>Sort by "Most Recent"</bdi> = ביקורות אמינות.',
        "<bdi>Keepa.com</bdi> מציג היסטוריית מחיר חינם — לעולם אל תקנו בלי לבדוק.",
        "ספרים אנגליים מאמזון — לרוב זולים משמעותית מחנויות בישראל, כולל משלוח.",
        "תוספי תזונה מ-<bdi>iHerb</bdi> מול <bdi>Amazon</bdi> — השוו מחירים, לעיתים יש הפרש גדול.",
        "כבלים ואביזרי טכנולוגיה — <bdi>Amazon Basics</bdi> איכות טובה במחיר שליש.",
        "מוצרי טיפוח ויופי — בדקו תאריך תפוגה בביקורות לפני קנייה.",
        '<bdi>Size Guide</bdi> של <bdi>Amazon</bdi> לרוב מדויק — השתמשו בו לפני הזמנת ביגוד ונעליים.',
    ],
    "en": [
        "Order between $49–$75 to enjoy free shipping without Israeli customs fees.",
        "Over $75? Expect 18% VAT + customs clearance fee — think twice before buying.",
        "Multiple small items? Combine into one order over $49 to save on shipping.",
        "Items under $49 charge shipping — check if adding another item saves money overall.",
        "Price too low? Verify the seller is Amazon, not a Third-party with different return policies.",
        '"Ships from and sold by Amazon.com" = the price you see is real and includes Prime.',
        'Filter "4 stars and up" + Sort by "Most Recent" = more reliable reviews.',
        "Keepa.com shows free price history — never buy without checking it first.",
        "English books from Amazon — usually much cheaper than Israeli bookstores, shipping included.",
        "Supplements on iHerb vs Amazon — compare prices, the difference can be significant.",
        "Cables and tech accessories — Amazon Basics quality at a third of the price.",
        "Beauty and personal care — check expiration dates in reviews before buying.",
        "Jackets and shoes — Amazon's Size Guide is usually accurate, always use it.",
    ],
}


def _daily_tip(lang: str) -> str:
    tips = _DAILY_TIPS.get(lang, _DAILY_TIPS["en"])
    idx = datetime.now().timetuple().tm_yday % len(tips)
    return tips[idx]


def _ils_amount(raw, ils_prefix: bool = False) -> str:
    """Render an amount in shekels: '₪18.22' with the sign to the left of the digits.

    Amazon hands us the price as 'ILS 18.22'; that string next to a '46.77₪ משלוח' made
    the reader take the ILS figure for the shipping fee. Only ILS values are rewritten —
    a dollar price is left exactly as Amazon quoted it.
    """
    if raw in (None, ""):
        return ""
    s = str(raw).strip()
    if "ILS" not in s.upper() and "₪" not in s:
        return s
    digits = re.sub(r"[^\d.,]", "", s).strip(",.")  # keep a thousands comma, drop 'ILS'/'₪'
    if not digits:
        return s
    return f"₪{digits}" if ils_prefix else f"{digits}₪"


def _israel_cost_note(product, is_rtl: bool, ils_prefix: bool = False) -> str:
    """The Israel cost note shown next to the price, e.g. '+ 154.28₪ מכס · סה"כ 840.79₪'.

    Returns '' when nothing was extracted from the product page, in which case callers
    fall back to the existing generic "price excludes shipping/taxes" wording — a
    disclaimer that says costs exist but never how much.
    """
    # '₪46.77' in the weekly digest, '46.77₪' in the daily summary — the daily email's
    # wording is unchanged, so callers opt in.
    def ils(v: str) -> str:
        return f"₪{v}" if ils_prefix else f"{v}₪"

    kind = getattr(product, "israel_cost_kind", None)
    price_raw = getattr(product, "last_price", None)
    if not price_raw:
        return ""
    try:
        price = float(re.sub(r"[^\d.]", "", str(price_raw)))
    except (ValueError, TypeError):
        return ""
    if price <= 0:
        return ""

    try:
        extra = float(re.sub(r"[^\d.]", "", str(getattr(product, "israel_extra_cost", "") or "")))
    except (ValueError, TypeError):
        extra = 0.0

    # Conditional free delivery — the same case dashboard.js handles: below ~$49 Amazon's
    # "FREE delivery to Israel" holds only above an order minimum, and the gap to it tells
    # the user how much more to add to the cart. Comes from the delivery text, so it works
    # regardless of whether the global-block extraction found anything.
    threshold_raw = extract_free_shipping_threshold(getattr(product, "raw_text", "") or "")
    if getattr(product, "last_status", "") == "FREE" and threshold_raw:
        try:
            threshold = float(threshold_raw)
        except ValueError:
            threshold = 0.0
        if threshold > price:
            gap = threshold - price
            alone_he = f" · לקנייה בודדת +{ils(f'{extra:.2f}')}" if extra > 0 else ""
            alone_en = f" · ILS {extra:.2f} if bought alone" if extra > 0 else ""
            return (f"משלוח חינם בהזמנה מעל {ils(f'{threshold:.2f}')} — חסרים עוד {ils(f'{gap:.2f}')}{alone_he}" if is_rtl
                    else f"Free delivery on orders over ILS {threshold:.2f} — ILS {gap:.2f} to go{alone_en}")

    if not kind:
        return ""

    if kind == "free":
        return "משלוח חינם, ללא עלויות נוספות" if is_rtl else "Free shipping, no extra charges"

    if extra <= 0:
        return ""

    total = f"{price + extra:.2f}"
    if kind == "import_only":
        # "משלוח חינם" leads: it's the good news and the reason the product is in the email
        # at all. Opening with "+ 149.50₪" made the line read as a charge before the reader
        # reached the word that explains it isn't shipping.
        return (f"משלוח חינם + {ils(f'{extra:.2f}')} מכס · סה\"כ {ils(total)}" if is_rtl
                else f"Free shipping + ILS {extra:.2f} import charges · total ILS {total}")

    # A FREE product can still quote a shipping fee in the global block: its free delivery
    # is conditional on an order minimum, and the fee is what you'd pay buying it alone.
    # Both are true, but printing the fee beside the "free shipping" badge reads as a
    # contradiction — so on FREE products we only show the unambiguous import charge.
    if getattr(product, "last_status", "") == "FREE":
        return ""

    # Everything else is at least partly shipping, so the percentage matters: the fee is
    # close to flat, which makes it most of a cheap product and noise on an expensive one.
    pct = round(extra / price * 100)
    if kind == "shipping_only":
        label_he, label_en = "משלוח", "shipping"
        subject_he, subject_en = "עלות המשלוח", "Shipping"
    else:  # 'shipping_import', or 'combined' where Amazon gives no split
        label_he, label_en = "משלוח ומכס", "shipping & import"
        subject_he, subject_en = "עלות המשלוח והמכס", "Shipping & import"
    # Spelled out rather than a bare "(57% מהמחיר)", matching dashboard.js: the percentage
    # sits beside two other figures, and without a subject it reads as ambiguous.
    return (f"+ {ils(f'{extra:.2f}')} {label_he} · סה\"כ {ils(total)} ({subject_he} היא {pct}% ממחיר המוצר)" if is_rtl
            else f"+ ILS {extra:.2f} {label_en} · total ILS {total} ({subject_en} is {pct}% of the item price)")


def _num(raw) -> float | None:
    """The same digits-only parse _israel_cost_note does, as a reusable helper."""
    if raw in (None, ""):
        return None
    try:
        return float(re.sub(r"[^\d.]", "", str(raw)))
    except (ValueError, TypeError):
        return None


def _price_trend(product, prev, lang: str, txt_align: str, txt_dir: str) -> str:
    """Week-over-week movement line, comparing the product against a price_history row.

    Returns '' when there is no comparison point — the expected state until a full
    week of history has accumulated. The card must read correctly without this line.
    """
    if prev is None:
        return ""

    now_price = _num(getattr(product, "last_price", None))
    was_price = _num(getattr(prev, "price", None))
    now_ship = _num(getattr(product, "israel_extra_cost", None)) or 0.0
    was_ship = _num(getattr(prev, "israel_extra_cost", None)) or 0.0

    key, delta = None, 0.0
    if now_price is not None and was_price is not None and abs(now_price - was_price) >= 0.01:
        key = "trend_price_down" if now_price < was_price else "trend_price_up"
        delta = abs(now_price - was_price)
    elif abs(now_ship - was_ship) >= 0.01:
        key = "trend_ship_down" if now_ship < was_ship else "trend_ship_up"
        delta = abs(now_ship - was_ship)

    if key is None:
        # Nothing moved — only worth saying when we actually had a price to compare.
        if now_price is None or was_price is None:
            return ""
        return (f'<p style="margin:0 0 6px;font-size:12px;color:#767676;text-align:{txt_align};" {txt_dir}>'
                f'{_t(lang, "trend_flat")}</p>')

    color = "#007600" if key.endswith("_down") else "#B12704"
    txt = _t(lang, key, delta=f"{delta:.2f}")
    return (f'<p style="margin:0 0 6px;font-size:13px;font-weight:bold;color:{color};'
            f'text-align:{txt_align};" {txt_dir}>{txt}</p>')


def _t(lang: str, key: str, **kw) -> str:
    s = _STRINGS.get(lang, _STRINGS["en"]).get(key, _STRINGS["en"].get(key, ""))
    return s.format(**kw) if kw else s


def _short(name: str, limit: int = _MAX_NAME_BODY) -> str:
    if not name:
        return ""
    clean = " ".join(str(name).split())
    if len(clean) <= limit:
        return clean
    head = clean[: max(1, limit - 1)]
    cut = head.rfind(" ")
    if cut >= int(limit * 0.6):
        head = head[:cut]
    return f"{head.rstrip()}…"


def _short_around(clean: str, split_at: int, limit: int = _MAX_NAME_BODY) -> str:
    """Truncate from the middle, keeping the segment that starts at split_at.

    Amazon titles put the distinguishing attribute (color, count, size) last, so a
    plain tail cut can render two variants of the same product identically.
    """
    tail_budget = max(16, limit // 3)
    start = clean.rfind(" ", 0, split_at + 1) + 1  # back up to a word boundary
    tail = clean[start:start + tail_budget].rstrip()
    truncated_tail = (start + tail_budget) < len(clean)
    head_budget = max(1, limit - len(tail) - 2)
    head = clean[:head_budget]
    cut = head.rfind(" ")
    if cut >= int(head_budget * 0.6):
        head = head[:cut]
    return f"{head.rstrip()}…{tail}" + ("…" if truncated_tail else "")


def _display_names(raw_names: list, limit: int = _MAX_NAME_BODY) -> list:
    """_short() over a whole email's names, disambiguating rows that collide."""
    cleans = [" ".join(str(n or "").split()) for n in raw_names]
    out = [_short(c, limit) for c in cleans]
    groups = {}
    for i, s in enumerate(out):
        groups.setdefault(s, []).append(i)
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        distinct = {cleans[i] for i in idxs}
        if len(distinct) < 2:
            continue  # genuinely the same name — nothing to disambiguate
        lcp = os.path.commonprefix(sorted(distinct))
        for i in idxs:
            out[i] = _short_around(cleans[i], len(lcp), limit)
    return out


def _cta_btn(url: str, label: str, align: str = "left") -> str:
    ml = "auto" if align == "right" else "0"
    return f"""<table cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 4px; margin-left:{ml};">
          <tr>
            <td align="center" bgcolor="#FF9900" style="border-radius:6px;">
              <a href="{url}"
                 style="display:inline-block; background:#FF9900; color:#111111;
                        font-family:Arial,sans-serif; font-size:14px; font-weight:bold;
                        text-decoration:none; padding:11px 28px; border-radius:6px;
                        letter-spacing:0.2px; white-space:nowrap;"
                 target="_blank">{label}</a>
            </td>
          </tr>
        </table>"""


def _product_url(asin: str) -> str:
    tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    return f"https://www.amazon.com/dp/{asin}?tag={tag}" if tag else f"https://www.amazon.com/dp/{asin}"


def _tracking_url(user_id: int, asin: str) -> str:
    dest = _product_url(asin)
    base = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    params = urlencode({"u": user_id, "a": asin, "url": dest})
    return f"{base}/track/click?{params}"


def _wrap_responsive(html_body: str, is_rtl: bool = True) -> str:
    body_dir = ' dir="rtl"' if is_rtl else ""
    return f"""<!DOCTYPE html>
<html{body_dir}>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width:600px){{
      .email-container{{width:100% !important;}}
      .email-container img{{max-width:100% !important;height:auto !important;}}
    }}
  </style>
</head>
<body{body_dir} style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 12px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" class="email-container"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;
                    overflow:hidden;border:1px solid #e8e8e8;">
        <tr>
          <td style="padding:24px;">
            {html_body}
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _open_pixel(user_id: int, template_name: str, template_id: int | None = None) -> str:
    base = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    params = f"uid={user_id}&tn={template_name}"
    if template_id:
        params += f"&tid={template_id}"
    return f'<img src="{base}/track/email-open?{params}" width="1" height="1" style="display:none;border:0;" alt="">'


def _pause_url(user_id: int) -> str:
    base = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    token = create_pause_token(user_id)
    return f"{base}/pause?token={token}"


def _pause_product_url(user_id: int, asin: str) -> str:
    base = os.environ.get("APP_BASE_URL", "https://app.amzfreeil.com").rstrip("/")
    token = create_product_pause_token(user_id, asin)
    return f"{base}/pause-product?token={token}"


# ── Resend sender ─────────────────────────────────────────────────────────────

def _send_via_resend(
    to: str,
    subject: str,
    html: str,
    text: str,
    extra_headers: dict | None = None,
) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.error("RESEND_API_KEY not set")
        return False
    resend_client.api_key = api_key
    from_addr = os.environ.get("FROM_EMAIL", "AMZ Free Ship Alert <alerts@amzfreeil.com>")
    payload: dict = {
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if extra_headers:
        payload["headers"] = extra_headers
    try:
        resend_client.Emails.send(payload)
        logger.info(f"Email sent via Resend → {to}: {subject}")
        return True
    except Exception as e:
        err_str = str(e)
        if "rate" in err_str.lower() or "429" in err_str:
            logger.error(f"Resend RATE LIMIT → {to}: {err_str}")
        elif "invalid" in err_str.lower() or "400" in err_str:
            logger.error(f"Resend INVALID ADDRESS → {to}: {err_str}")
        elif "quota" in err_str.lower() or "limit" in err_str.lower():
            logger.error(f"Resend QUOTA EXCEEDED → {to}: {err_str}")
        else:
            logger.error(f"Resend error → {to}: {err_str}")
        return False


# ── Simple transactional email (admin use) ───────────────────────────────────

def send_simple_email(to: str, subject: str, body_html: str) -> bool:
    return _send_via_resend(to, subject, body_html, "")


# ── Admin new-user notification ──────────────────────────────────────────────

def send_admin_new_user_notification(admin_email: str, new_user_email: str) -> bool:
    """Notify admin when a new user verifies their email."""
    registered_at = datetime.now().strftime("%d/%m/%Y %H:%M")
    html = f"""<div dir="rtl" style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:24px;background:#fffaf1;border-radius:12px;">
      <h2 style="color:#e47911;">🎉 משתמש חדש נרשם!</h2>
      <table style="width:100%;border-collapse:collapse;margin-top:16px;">
        <tr><td style="padding:8px 0;color:#555;width:120px;">מייל:</td><td style="padding:8px 0;font-weight:bold;">{new_user_email}</td></tr>
        <tr><td style="padding:8px 0;color:#555;">תאריך אימות:</td><td style="padding:8px 0;">{registered_at}</td></tr>
      </table>
      <a href="https://app.amzfreeil.com/admin" style="display:inline-block;background:#FF9900;color:#111;padding:10px 24px;border-radius:8px;font-weight:bold;text-decoration:none;margin-top:20px;">פתח פאנל ניהול</a>
    </div>"""
    return _send_via_resend(admin_email, f"🎉 משתמש חדש: {new_user_email}", html, f"משתמש חדש נרשם: {new_user_email} בתאריך {registered_at}")


# ── Admin error report ───────────────────────────────────────────────────────

def send_admin_error_report(admin_email: str, failed_items: list) -> bool:
    """
    Send error report to admin when products fail for the first time.
    failed_items: list of (Product, CheckResult)
    """
    checked_at = datetime.now().strftime("%d/%m/%Y %H:%M UTC")
    rows = ""
    plain_lines = []
    for product, result in failed_items:
        err_num = getattr(product, "consecutive_errors", 1)
        err_color = "#dc3545" if err_num >= 4 else "#856404" if err_num >= 2 else "#555"
        error_msg = (result.error_message or result.status.value)[:200]
        raw = (result.raw_text or product.raw_text or "")[:300]
        prev_status = getattr(product, "last_status", "—") or "—"
        method = "Playwright" if "Timeout" in error_msg or "playwright" in error_msg.lower() else "httpx"
        url = getattr(product, "url", "") or f"https://www.amazon.com/dp/{product.asin}"
        raw_block = (
            f'<div style="margin-top:6px;padding:6px 8px;background:#f8f8f8;border-radius:4px;'
            f'font-size:11px;color:#555;font-family:monospace;word-break:break-all;">{raw}</div>'
            if raw else ""
        )
        rows += f"""<tr>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top;">
            <a href="{url}" style="font-family:monospace;font-size:13px;color:#0066cc;">{product.asin}</a><br>
            <span style="font-size:12px;color:#555;">{product.name or "—"}</span>
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top;font-weight:bold;color:{err_color};font-size:13px;white-space:nowrap;">
            #{err_num} / 5
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top;font-size:12px;color:#555;">
            {prev_status}
          </td>
          <td style="padding:10px 12px;border-bottom:1px solid #eee;vertical-align:top;font-size:12px;">
            <span style="color:#721c24;">[{method}] {error_msg}</span>
            {raw_block}
          </td>
        </tr>"""
        plain_lines.append(
            f"ASIN: {product.asin}\n"
            f"Name: {product.name or '—'}\n"
            f"URL:  {url}\n"
            f"Error #{err_num}/5 | prev: {prev_status} | method: {method}\n"
            f"Error: {error_msg}\n"
            f"Raw:  {raw or '(none)'}\n"
        )

    n = len(failed_items)
    max_err = max((getattr(p, "consecutive_errors", 1) for p, _ in failed_items), default=1)
    warning_note = (
        "<p style='color:#dc3545;font-weight:bold;margin:8px 0;'>⚠️ מוצרים מתקרבים לחסימה (שגיאה 4+) — בדוק את הפרוקסי!</p>"
        if max_err >= 4 else ""
    )
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:760px;margin:auto;padding:24px;direction:ltr;">
      <h2 style="color:#dc3545;margin-top:0;">⚠️ Amazon Israel Alert — Product Check Errors</h2>
      <p style="color:#555;margin:0 0 4px;">
        <strong>{n}</strong> product(s) failed · cycle at <strong>{checked_at}</strong><br>
        Customer-visible status unchanged until 5 consecutive failures.
      </p>
      {warning_note}
      <table style="width:100%;border-collapse:collapse;background:#fff;border:1px solid #dee2e6;border-radius:8px;overflow:hidden;margin:16px 0;">
        <thead>
          <tr style="background:#f8d7da;color:#721c24;">
            <th style="padding:10px 12px;text-align:left;">ASIN / Name</th>
            <th style="padding:10px 12px;text-align:left;">Error #</th>
            <th style="padding:10px 12px;text-align:left;">Prev Status</th>
            <th style="padding:10px 12px;text-align:left;">Error + Raw Response</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin:0;">
        Admin panel: <a href="https://app.amzfreeil.com/admin" style="color:#0066cc;">app.amzfreeil.com/admin</a>
      </p>
    </div>"""

    plain = (
        f"Amazon Israel Alert — {n} product(s) failed · {checked_at}\n"
        + "=" * 60 + "\n\n"
        + ("\n" + "-" * 40 + "\n").join(plain_lines)
        + "\nAdmin panel: https://app.amzfreeil.com/admin"
    )

    return _send_via_resend(
        admin_email,
        f"⚠️ [{n} error{'s' if n != 1 else ''}] Amazon Israel Alert — Check Failed",
        html,
        plain,
    )


# ── Single product alert ──────────────────────────────────────────────────────

def send_user_alert(user, product, result) -> bool:
    if getattr(user, "notify_email_bounced", False):
        logger.info(f"Skipping alert for user {user.id} — email bounced/complained")
        return False
    lang = getattr(user, "language", "he") or "he"
    recipient = user.notify_email
    affiliate_tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    logo_url = os.environ.get("LOGO_URL", "").strip()
    checked_at = (product.last_checked or datetime.now()).strftime("%d/%m/%Y %H:%M")

    asin = product.asin
    name = _short(product.name or asin, _MAX_NAME_BODY)
    url = _tracking_url(user.id, asin)
    found_in_aod = getattr(result, "found_in_aod", False)

    is_rtl = lang == "he"
    txt_dir = 'dir="rtl"' if is_rtl else ""
    txt_align = "right" if is_rtl else "left"

    subject = _t(lang, "subject_single", name=_short(product.name or asin, _MAX_NAME_SUBJECT))

    aod_line = [_t(lang, "aod_plain")] if found_in_aod else []
    text_body = "\n".join([
        _t(lang, "plain_header"),
        f"{_t(lang, 'plain_product')} : {name}",
        f"ASIN    : {asin}",
        f"{_t(lang, 'plain_url')} : {url}",
        _t(lang, "plain_urgency"),
        *aod_line,
        "",
        _t(lang, "plain_footer", checked_at=checked_at),
    ])

    aod_block = ""
    if found_in_aod:
        aod_block = f"""<tr>
          <td style="padding:10px 0 0;">
            <div style="background:#fff8e1; border-{'right' if is_rtl else 'left'}:3px solid #FF9900;
                        padding:10px 14px; border-radius:4px; font-size:13px; color:#555;
                        line-height:1.6; text-align:{txt_align};" {txt_dir}>
              {_t(lang, "aod_note")}
            </div>
          </td>
        </tr>"""

    header_brand = (
        f'<img src="{logo_url}" width="180" alt="Amazon Free shipping to Israel Alert"'
        f' style="display:block; margin:0 auto 12px; max-width:180px;">'
        if logo_url
        else f'<h1 style="margin:0 0 6px;color:#e47911;font-size:22px;font-weight:bold;" {txt_dir}>{_t(lang, "header_title")}</h1>'
    )
    disclosure_row = ""
    if affiliate_tag:
        disclosure_row = f"""<tr>
          <td style="padding:12px 24px 4px; text-align:{txt_align};" {txt_dir}>
            <p style="margin:0; font-size:12px; color:#666; font-style:italic;">{_t(lang, "disclosure")}</p>
          </td>
        </tr>"""

    # Real Israel cost when we managed to extract it; otherwise the generic
    # "price excludes shipping/taxes" wording stays exactly as it was.
    # The generic fallback carries its own parentheses; the real cost note doesn't, because
    # it can already end in "(257% מהמחיר)" and wrapping it would nest parentheses.
    price_note = _israel_cost_note(product, is_rtl) or (
        "(מחיר באמזון — לא כולל משלוח, מיסים ועלויות שונות)" if is_rtl
        else "(Amazon price, excl. shipping, taxes & fees)"
    )

    body_dir = ' dir="rtl"' if is_rtl else ""
    html_body = f"""<!DOCTYPE html>
<html{body_dir}>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width:600px){{
      .email-container{{width:100% !important;}}
      .email-container img{{max-width:100% !important;height:auto !important;}}
      .product-name{{font-size:14px !important;}}
    }}
  </style>
</head>
<body{body_dir} style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">{_t(lang, "preheader")}</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 12px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" class="email-container" style="max-width:600px;width:100%;">
        <tr>
          <td style="background:#ffffff;border-radius:10px 10px 0 0;border-bottom:2px solid #FF9900;padding:24px 24px 18px;text-align:center;">
            {header_brand}
            <p style="margin:0;color:#555;font-size:14px;" {txt_dir}>{_t(lang, "header_sub1")}</p>
          </td>
        </tr>
        {disclosure_row}
        <tr>
          <td style="background:#f8f8f8;padding:20px 20px 6px;">
            <table width="100%" cellpadding="0" cellspacing="0"
                   style="background:#ffffff;border:1px solid #e8e8e8;border-radius:10px;margin-bottom:14px;">
              <tr>
                <td valign="top" style="padding:16px;">
                  <p class="product-name" style="margin:0 0 4px;font-size:16px;font-weight:bold;line-height:1.4;text-align:{txt_align};word-wrap:break-word;overflow-wrap:break-word;" {txt_dir}>
                    <a href="{url}" style="color:#111111;text-decoration:none;">{name}</a>
                  </p>
                  <p style="margin:0 0 10px;font-size:13px;color:#666;text-align:{txt_align};">ASIN: {asin}</p>
                  {f'<p style="margin:0 0 8px;font-size:13px;font-weight:bold;color:#B12704;text-align:{txt_align};" {txt_dir}>💰 {product.last_price} <span style="font-size:11px;color:#888;font-weight:normal;">{price_note}</span></p>' if getattr(product, "last_price", None) else ""}
                  <p style="margin:0 0 12px;font-size:13px;font-weight:bold;color:#007600;text-align:{txt_align};" {txt_dir}>{_t(lang, "shipping_badge")}</p>
                  <div style="text-align:{txt_align};">{_cta_btn(url, _t(lang, "btn_buy"), txt_align)}</div>
                  <p style="margin:8px 0 4px;font-size:13px;color:#555;font-style:italic;text-align:{txt_align};" {txt_dir}>{_t(lang, "urgency")}</p>
                </td>
              </tr>
              {aod_block}
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;padding:0 20px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0faf0;border-radius:8px;border:1px solid #c8e6c9;">
              <tr>
                <td style="padding:12px 16px;text-align:{txt_align};" {txt_dir}>
                  <p style="margin:0 0 3px;font-size:13px;font-weight:bold;color:#2e7d32;">{_t(lang, "quick_tip_title")}</p>
                  <p style="margin:0;font-size:12px;color:#388e3c;line-height:1.5;">{_daily_tip(lang)}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;border-radius:0 0 10px 10px;padding:14px 24px;text-align:center;">
            <p style="margin:0 0 6px;color:#888;font-size:12px;" {txt_dir}>{_t(lang, "footer", checked_at=checked_at)}</p>
            <p style="margin:0 0 4px;color:#bbb;font-size:11px;">Amazon Free Shipping to Israel Alert</p>
            <p style="margin:0;font-size:11px;"><a href="{_pause_url(user.id)}" style="color:#aaa;text-decoration:underline;">הפסק לקבל עדכונים</a></p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    unsubscribe_url = _pause_url(user.id)
    headers = {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "List-Id": "Amazon Israel Alert <alerts.amzfreeil.com>",
    }
    return _send_via_resend(recipient, subject, html_body, text_body, extra_headers=headers)


# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summary(user, free_products: list, pause_warnings: dict = None) -> bool:
    """
    Send a daily digest email listing all FREE products for a user.
    free_products: list of ORM Product objects with last_status == FREE
    """
    if not free_products:
        return False

    lang = getattr(user, "language", "he") or "he"
    recipient = user.notify_email
    affiliate_tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    logo_url = os.environ.get("LOGO_URL", "").strip()
    checked_ats = [p.last_checked for p, _ in free_products if getattr(p, "last_checked", None)]
    checked_at = (max(checked_ats) if checked_ats else datetime.now()).strftime("%d/%m/%Y %H:%M")
    n = len(free_products)

    is_rtl = lang == "he"
    txt_dir = 'dir="rtl"' if is_rtl else ""
    txt_align = "right" if is_rtl else "left"
    body_dir = ' dir="rtl"' if is_rtl else ""

    subject = _t(lang, "subject_summary", n=n)

    # Names for the whole email at once, so two variants of the same product
    # (same title up to the color/count at the end) never render identically.
    names = _display_names([cn or p.name or p.asin for p, cn in free_products])

    # Plain text
    lines = [_t(lang, "plain_summary_header")]
    for i, (p, custom_name) in enumerate(free_products):
        name = names[i]
        url = _tracking_url(user.id, p.asin)
        p_checked = p.last_checked.strftime("%d/%m/%Y %H:%M") if getattr(p, "last_checked", None) else ""
        lines.append(f"• {name}")
        lines.append(f"  {url}")
        if p_checked:
            lines.append(f"  {_t(lang, 'plain_footer', checked_at=p_checked)}")
        lines.append("")
    lines.append(_t(lang, "plain_urgency"))
    lines.append(_t(lang, "plain_footer", checked_at=checked_at))
    text_body = "\n".join(lines)

    # Product rows HTML
    disclosure_row = ""
    if affiliate_tag:
        disclosure_row = f"""<tr>
          <td style="padding:12px 24px 4px; text-align:{txt_align};" {txt_dir}>
            <p style="margin:0; font-size:12px; color:#666; font-style:italic;">{_t(lang, "disclosure")}</p>
          </td>
        </tr>"""

    product_rows = ""
    for i, (p, custom_name) in enumerate(free_products):
        name = names[i]
        url = _tracking_url(user.id, p.asin)
        img_url = p.image_url or f"https://images-na.ssl-images-amazon.com/images/P/{p.asin}.01._SL100_.jpg"
        price_html = ""
        if getattr(p, "last_price", None):
            price_note = _israel_cost_note(p, is_rtl) or (
                "(מחיר באמזון — לא כולל משלוח, מיסים ועלויות שונות)" if is_rtl
                else "(Amazon price, excl. shipping, taxes & fees)"
            )
            price_html = f'<p style="margin:0 0 6px;font-size:13px;font-weight:bold;color:#B12704;text-align:{txt_align};" {txt_dir}>💰 {p.last_price} <span style="font-size:11px;color:#888;font-weight:normal;">{price_note}</span></p>'
        checked_html = ""
        if getattr(p, "last_checked", None):
            p_checked = p.last_checked.strftime("%d/%m/%Y %H:%M")
            checked_html = f'<p style="margin:0 0 8px;font-size:11px;color:#999;text-align:{txt_align};" {txt_dir}>{_t(lang, "plain_footer", checked_at=p_checked)}</p>'
        warning_html = ""
        if pause_warnings and p.asin in pause_warnings:
            days_left = pause_warnings[p.asin]
            if lang == "he":
                warn_txt = "יושהה מחר אם לא תלחץ" if days_left == 1 else f"יושהה בעוד {days_left} ימים אם לא תלחץ"
            else:
                warn_txt = "Will be paused tomorrow if not clicked" if days_left == 1 else f"Will be paused in {days_left} days if not clicked"
            warning_html = f'<p style="margin:0 0 8px;font-size:12px;font-weight:bold;color:#E47911;text-align:{txt_align};" {txt_dir}>⏰ {warn_txt}</p>'
        product_rows += f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border:1px solid #e8e8e8;border-radius:10px;margin-bottom:12px;">
          <tr>
            <td class="prod-img-td" style="padding:14px 16px 4px;text-align:{txt_align};">
              <a href="{url}">
                <img src="{img_url}" width="100" height="100"
                     style="display:inline-block;border-radius:8px;border:1px solid #eeeeee;"
                     alt="{name}">
              </a>
            </td>
          </tr>
          <tr>
            <td valign="top" style="padding:6px 16px 14px;">
              <p class="product-name" style="margin:0 0 4px;font-size:15px;font-weight:bold;line-height:1.4;text-align:{txt_align};word-wrap:break-word;overflow-wrap:break-word;" {txt_dir}>
                <a href="{url}" style="color:#111111;text-decoration:none;">{name}</a>
              </p>
              <p style="margin:0 0 8px;font-size:12px;color:#666;text-align:{txt_align};">ASIN: {p.asin}</p>
              {price_html}
              <p style="margin:0 0 10px;font-size:13px;font-weight:bold;color:#007600;text-align:{txt_align};" {txt_dir}>{_t(lang, "shipping_badge")}</p>
              {checked_html}{warning_html}<div style="text-align:{txt_align};">{_cta_btn(url, _t(lang, "btn_buy"), txt_align)}</div>
            </td>
          </tr>
        </table>"""

    header_brand = (
        f'<img src="{logo_url}" width="180" alt="Amazon Free shipping to Israel Alert"'
        f' style="display:block; margin:0 auto 12px; max-width:180px;">'
        if logo_url
        else f'<h1 style="margin:0 0 6px;color:#e47911;font-size:22px;font-weight:bold;" {txt_dir}>{_t(lang, "header_title")}</h1>'
    )

    html_body = f"""<!DOCTYPE html>
<html{body_dir}>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width:600px){{
      .email-container{{width:100% !important;}}
      .email-container img{{max-width:100% !important;height:auto !important;}}
      .product-name{{font-size:14px !important;}}
      .prod-img-td img{{width:80px !important;height:80px !important;}}
    }}
  </style>
</head>
<body{body_dir} style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">{_t(lang, "preheader")}</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 12px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" class="email-container" style="max-width:600px;width:100%;">
        <tr>
          <td style="background:#ffffff;border-radius:10px 10px 0 0;border-bottom:2px solid #FF9900;padding:24px 24px 18px;text-align:center;">
            {header_brand}
            <p style="margin:0;color:#555;font-size:14px;" {txt_dir}>{_t(lang, "header_summary", n=n)}</p>
          </td>
        </tr>
        {disclosure_row}
        <tr>
          <td style="background:#f8f8f8;padding:20px 20px 6px;">
            {product_rows}
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;padding:0 20px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0faf0;border-radius:8px;border:1px solid #c8e6c9;">
              <tr>
                <td style="padding:12px 16px;text-align:{txt_align};" {txt_dir}>
                  <p style="margin:0 0 3px;font-size:13px;font-weight:bold;color:#2e7d32;">{_t(lang, "quick_tip_title")}</p>
                  <p style="margin:0;font-size:12px;color:#388e3c;line-height:1.5;">{_daily_tip(lang)}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;padding:16px 24px 8px;text-align:center;">
            <a href="https://app.amzfreeil.com/dashboard"
               style="display:inline-block;background:#FF9900;color:#111111;font-size:14px;font-weight:bold;
                      text-decoration:none;padding:11px 32px;border-radius:8px;">
              {'כניסה לחשבון' if is_rtl else 'Go to My Account'}
            </a>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;border-radius:0 0 10px 10px;padding:8px 24px 14px;text-align:center;">
            <p style="margin:0 0 6px;color:#888;font-size:12px;" {txt_dir}>{_t(lang, "footer", checked_at=checked_at)}</p>
            <p style="margin:0 0 4px;color:#bbb;font-size:11px;">Amazon Free Shipping to Israel Alert</p>
            <p style="margin:0;font-size:11px;"><a href="{_pause_url(user.id)}" style="color:#aaa;text-decoration:underline;">הפסק לקבל עדכונים</a></p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    html_body = html_body.replace("</body>", f"{_open_pixel(user.id, 'daily_summary')}\n</body>")

    unsubscribe_url = _pause_url(user.id)
    headers = {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "List-Id": "Amazon Israel Alert <alerts.amzfreeil.com>",
    }
    return _send_via_resend(recipient, subject, html_body, text_body, extra_headers=headers)


# ── Weekly PAID summary ───────────────────────────────────────────────────────

def send_weekly_paid_summary(user, paid_products: list, history: dict | None = None) -> bool:
    """
    Weekly digest for products whose shipping to Israel costs money.

    These products never appear in the daily summary — it only fires on FREE — so for
    users holding only PAID products this is the single email they get from us.

    paid_products: list of (Product, custom_name) tuples, same shape as send_daily_summary.
    history:       optional {product_id: PriceHistory} of the newest row at least a week
                   old, used for the movement line. Missing entries render without it.
    """
    if not paid_products:
        return False

    lang = getattr(user, "language", "he") or "he"
    recipient = user.notify_email
    affiliate_tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    logo_url = os.environ.get("LOGO_URL", "").strip()
    history = history or {}
    checked_ats = [p.last_checked for p, _ in paid_products if getattr(p, "last_checked", None)]
    checked_at = (max(checked_ats) if checked_ats else datetime.now()).strftime("%d/%m/%Y %H:%M")
    n = len(paid_products)

    is_rtl = lang == "he"
    txt_dir = 'dir="rtl"' if is_rtl else ""
    txt_align = "right" if is_rtl else "left"
    body_dir = ' dir="rtl"' if is_rtl else ""

    subject = _t(lang, "subject_weekly_paid", n=n)
    names = _display_names([cn or p.name or p.asin for p, cn in paid_products])

    # Plain text
    lines = [_t(lang, "plain_weekly_header"), _t(lang, "weekly_scope_note"), ""]
    for i, (p, _cn) in enumerate(paid_products):
        url = _tracking_url(user.id, p.asin)
        lines.append(f"• {names[i]}")
        if getattr(p, "last_price", None):
            note = _israel_cost_note(p, is_rtl, ils_prefix=is_rtl)
            disp = _ils_amount(p.last_price, ils_prefix=True) if is_rtl else p.last_price
            lines.append(f"  {disp}" + (f" · {note}" if note else ""))
        lines.append(f"  {url}")
        lines.append("")
    lines.append(_t(lang, "plain_footer", checked_at=checked_at))
    text_body = "\n".join(lines)

    disclosure_row = ""
    if affiliate_tag:
        disclosure_row = f"""<tr>
          <td style="padding:12px 24px 4px; text-align:{txt_align};" {txt_dir}>
            <p style="margin:0; font-size:12px; color:#666; font-style:italic;">{_t(lang, "disclosure")}</p>
          </td>
        </tr>"""

    product_rows = ""
    now = datetime.now()
    for i, (p, _cn) in enumerate(paid_products):
        name = names[i]
        url = _tracking_url(user.id, p.asin)
        # No images-na fallback here: that legacy URL answers 200 with a 43-byte pixel
        # instead of 404, so onerror never fires and the reader sees an empty square.
        img_html = ""
        if getattr(p, "image_url", None):
            img_html = f"""<tr>
            <td class="prod-img-td" style="padding:14px 16px 4px;text-align:{txt_align};">
              <a href="{url}">
                <img src="{p.image_url}" width="100" height="100"
                     style="display:inline-block;border-radius:8px;border:1px solid #eeeeee;"
                     alt="{name}">
              </a>
            </td>
          </tr>"""

        price_html = ""
        if getattr(p, "last_price", None):
            price_note = _israel_cost_note(p, is_rtl, ils_prefix=is_rtl) or (
                "(מחיר באמזון — לא כולל משלוח, מיסים ועלויות שונות)" if is_rtl
                else "(Amazon price, excl. shipping, taxes & fees)"
            )
            price_disp = _ils_amount(p.last_price, ils_prefix=True) if is_rtl else p.last_price
            price_html = f'<p style="margin:0 0 6px;font-size:13px;font-weight:bold;color:#B12704;text-align:{txt_align};" {txt_dir}>💰 {price_disp} <span style="font-size:11px;color:#888;font-weight:normal;">{price_note}</span></p>'

        trend_html = _price_trend(p, history.get(p.id), lang, txt_align, txt_dir)

        since_html = ""
        since = getattr(p, "status_since", None)
        if since:
            days = (now - since.replace(tzinfo=None)).days
            if days >= 1:
                since_html = (f'<p style="margin:0 0 10px;font-size:12px;color:#767676;'
                              f'text-align:{txt_align};" {txt_dir}>{_t(lang, "paid_since", days=days)}</p>')

        product_rows += f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border:1px solid #e8e8e8;border-radius:10px;margin-bottom:12px;">
          {img_html}
          <tr>
            <td valign="top" style="padding:6px 16px 14px;">
              <p class="product-name" style="margin:0 0 4px;font-size:15px;font-weight:bold;line-height:1.4;text-align:{txt_align};word-wrap:break-word;overflow-wrap:break-word;" {txt_dir}>
                <a href="{url}" style="color:#111111;text-decoration:none;">{name}</a>
              </p>
              <p style="margin:0 0 8px;font-size:12px;color:#666;text-align:{txt_align};">ASIN: {p.asin}</p>
              {price_html}
              {trend_html}
              {since_html}
              <div style="text-align:{txt_align};">{_cta_btn(url, _t(lang, "btn_view"), txt_align)}</div>
            </td>
          </tr>
        </table>"""

    header_brand = (
        f'<img src="{logo_url}" width="180" alt="Amazon Free shipping to Israel Alert"'
        f' style="display:block; margin:0 auto 12px; max-width:180px;">'
        if logo_url
        else f'<h1 style="margin:0 0 6px;color:#e47911;font-size:22px;font-weight:bold;" {txt_dir}>{_t(lang, "header_weekly_paid")}</h1>'
    )

    html_body = f"""<!DOCTYPE html>
<html{body_dir}>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width:600px){{
      .email-container{{width:100% !important;}}
      .email-container img{{max-width:100% !important;height:auto !important;}}
      .product-name{{font-size:14px !important;}}
      .prod-img-td img{{width:80px !important;height:80px !important;}}
    }}
  </style>
</head>
<body{body_dir} style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">{_t(lang, "header_weekly_sub", n=n)}</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 12px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" class="email-container" style="max-width:600px;width:100%;">
        <tr>
          <td style="background:#ffffff;border-radius:10px 10px 0 0;border-bottom:2px solid #FF9900;padding:24px 24px 18px;text-align:center;">
            {header_brand}
            <p style="margin:0 0 8px;color:#555;font-size:14px;" {txt_dir}>{_t(lang, "header_weekly_sub", n=n)}</p>
            <p style="margin:0;color:#767676;font-size:12px;line-height:1.5;" {txt_dir}>{_t(lang, "weekly_scope_note")}</p>
          </td>
        </tr>
        {disclosure_row}
        <tr>
          <td style="background:#f8f8f8;padding:20px 20px 6px;">
            {product_rows}
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;padding:0 20px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#fff8e1;border-radius:8px;border:1px solid #ffe0a3;">
              <tr>
                <td style="padding:12px 16px;text-align:{txt_align};" {txt_dir}>
                  <p style="margin:0 0 3px;font-size:13px;font-weight:bold;color:#8a5a00;">{_t(lang, "weekly_tip_title")}</p>
                  <p style="margin:0;font-size:12px;color:#8a5a00;line-height:1.5;">{_t(lang, "weekly_tip_body")}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;padding:16px 24px 8px;text-align:center;">
            <a href="https://app.amzfreeil.com/dashboard"
               style="display:inline-block;background:#FF9900;color:#111111;font-size:14px;font-weight:bold;
                      text-decoration:none;padding:11px 32px;border-radius:8px;">
              {'כניסה לחשבון' if is_rtl else 'Go to My Account'}
            </a>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;border-radius:0 0 10px 10px;padding:8px 24px 14px;text-align:center;">
            <p style="margin:0 0 6px;color:#888;font-size:12px;" {txt_dir}>{_t(lang, "footer", checked_at=checked_at)}</p>
            <p style="margin:0 0 4px;color:#bbb;font-size:11px;">Amazon Free Shipping to Israel Alert</p>
            <p style="margin:0;font-size:11px;"><a href="{_pause_url(user.id)}" style="color:#aaa;text-decoration:underline;">הפסק לקבל עדכונים</a></p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    html_body = html_body.replace("</body>", f"{_open_pixel(user.id, 'weekly_paid')}\n</body>")

    unsubscribe_url = _pause_url(user.id)
    headers = {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "List-Id": "Amazon Israel Alert <alerts.amzfreeil.com>",
    }
    return _send_via_resend(recipient, subject, html_body, text_body, extra_headers=headers)


# ── No-click reminder ─────────────────────────────────────────────────────────

def send_no_click_reminder(user, product, days_free: int) -> bool:
    """
    Sent once when a FREE product has not been clicked for 7+ days.
    Warns that checks will be auto-paused in 2 days if no action is taken.
    """
    if getattr(user, "notify_email_bounced", False):
        return False

    lang = getattr(user, "language", "he") or "he"
    is_rtl = lang == "he"
    txt_dir = 'dir="rtl"' if is_rtl else ""
    txt_align = "right" if is_rtl else "left"
    body_dir = ' dir="rtl"' if is_rtl else ""

    asin = product.asin
    name = _short(product.name or asin, _MAX_NAME_BODY)
    name_subject = _short(product.name or asin, _MAX_NAME_SUBJECT)
    buy_url = _tracking_url(user.id, asin)
    pause_url = _pause_product_url(user.id, asin)
    logo_url = os.environ.get("LOGO_URL", "").strip()
    affiliate_tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    checked_at = (product.last_checked or datetime.now()).strftime("%d/%m/%Y %H:%M")

    if lang == "he":
        subject = f"⏰ {name_subject} — חינם כבר {days_free} ימים, עדיין מתכנן לקנות?"
        preheader = "בעוד יומיים נפסיק לבדוק את המוצר הזה אוטומטית — אלא אם תפעל"
        heading = f"המוצר הזה חינם כבר {days_free} ימים 🤔"
        body_text = (
            f"שמנו לב שלא לחצת על <strong>{name}</strong> מאז שהוא במשלוח חינם.<br><br>"
            "אם אתה מתכנן לקנות — זה הזמן. המחיר יכול להשתנות בכל רגע.<br>"
            "אם הוא כבר לא רלוונטי — אין בעיה, נשהה את הבדיקות אוטומטית בעוד יומיים."
        )
        btn_buy = "קנה עכשיו — משלוח חינם"
        btn_pause = "השהה מוצר זה"
        warning = "⏱️ בעוד יומיים — אם לא תלחץ, הבדיקות למוצר זה יופסקו אוטומטית."
        footer_txt = f"נבדק: {checked_at} · Amazon Free Shipping to Israel Alert"
        plain = (
            f"⏰ {name} חינם כבר {days_free} ימים\n\n"
            f"קנה עכשיו: {buy_url}\n"
            f"השהה מוצר: {pause_url}\n\n"
            f"בעוד יומיים נפסיק לבדוק אוטומטית אם לא תפעל.\n\n{footer_txt}"
        )
    else:
        subject = f"⏰ {name_subject} — free for {days_free} days, still planning to buy?"
        preheader = "We'll stop checking this product in 2 days — unless you take action"
        heading = f"This product has been free for {days_free} days 🤔"
        body_text = (
            f"We noticed you haven't clicked on <strong>{name}</strong> since it became free shipping.<br><br>"
            "If you're planning to buy — now's the time. The price can change at any moment.<br>"
            "If it's no longer relevant — no problem, we'll auto-pause checks in 2 days."
        )
        btn_buy = "Buy Now — Free Shipping"
        btn_pause = "Pause this product"
        warning = "⏱️ In 2 days — if you don't click, checks for this product will be auto-paused."
        footer_txt = f"Checked: {checked_at} · Amazon Free Shipping to Israel Alert"
        plain = (
            f"⏰ {name} has been free for {days_free} days\n\n"
            f"Buy now: {buy_url}\n"
            f"Pause product: {pause_url}\n\n"
            f"We'll stop checking automatically in 2 days if you take no action.\n\n{footer_txt}"
        )

    header_brand = (
        f'<img src="{logo_url}" width="180" alt="Amazon Free shipping to Israel Alert"'
        f' style="display:block;margin:0 auto 12px;max-width:180px;">'
        if logo_url
        else f'<h1 style="margin:0 0 6px;color:#e47911;font-size:22px;font-weight:bold;" {txt_dir}>Amazon Free Shipping 🚚</h1>'
    )

    disclosure_html = ""
    if affiliate_tag:
        disc = "קישור שותף — הקנייה לא עולה לך יותר." if lang == "he" else "Affiliate link — no extra cost to you."
        disclosure_html = (
            f'<p style="margin:0 0 12px;font-size:12px;color:#999;font-style:italic;text-align:{txt_align};" {txt_dir}>{disc}</p>'
        )

    price_html = ""
    if getattr(product, "last_price", None):
        price_html = f'<p style="margin:0 0 16px;font-size:13px;font-weight:bold;color:#B12704;text-align:{txt_align};" {txt_dir}>💰 {product.last_price}</p>'

    html_body = f"""<!DOCTYPE html>
<html{body_dir}>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width:600px){{.email-container{{width:100% !important;}}}}
  </style>
</head>
<body{body_dir} style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">{preheader}</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 12px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" class="email-container"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e8e8e8;">
        <tr>
          <td style="background:#ffffff;border-bottom:2px solid #FF9900;padding:24px 24px 18px;text-align:center;">
            {header_brand}
            <p style="margin:6px 0 0;font-size:18px;font-weight:bold;color:#333;" {txt_dir}>{heading}</p>
          </td>
        </tr>
        <tr>
          <td style="padding:24px;">
            <p style="margin:0 0 16px;font-size:15px;color:#444;line-height:1.7;text-align:{txt_align};" {txt_dir}>{body_text}</p>
            <p style="margin:0 0 6px;font-size:13px;font-weight:bold;color:#007600;text-align:{txt_align};" {txt_dir}>
              ✅ {'משלוח חינם לישראל · הזמנות מעל $49' if lang == 'he' else 'FREE Shipping to Israel · Orders $49+'}
            </p>
            {price_html}
            {disclosure_html}
            <table cellpadding="0" cellspacing="0" border="0" style="margin:12px 0;">
              <tr>
                <td align="center" bgcolor="#FF9900" style="border-radius:6px;">
                  <a href="{buy_url}"
                     style="display:inline-block;background:#FF9900;color:#111111;font-family:Arial,sans-serif;
                            font-size:14px;font-weight:bold;text-decoration:none;padding:11px 28px;
                            border-radius:6px;white-space:nowrap;"
                     target="_blank">{btn_buy}</a>
                </td>
                <td width="12"></td>
                <td align="center" style="border-radius:6px;border:1px solid #ccc;">
                  <a href="{pause_url}"
                     style="display:inline-block;background:#ffffff;color:#666;font-family:Arial,sans-serif;
                            font-size:13px;text-decoration:none;padding:10px 20px;border-radius:6px;white-space:nowrap;"
                     target="_blank">{btn_pause}</a>
                </td>
              </tr>
            </table>
            <p style="margin:20px 0 0;font-size:12px;color:#7a5c00;background:#fff8e1;border-radius:6px;
                      padding:10px 14px;border-{'right' if is_rtl else 'left'}:3px solid #FF9900;text-align:{txt_align};" {txt_dir}>
              {warning}
            </p>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;border-top:1px solid #eee;padding:14px 24px;text-align:center;">
            <p style="margin:0;color:#888;font-size:12px;" {txt_dir}>{footer_txt}</p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    html_body = html_body.replace("</body>", f"{_open_pixel(user.id, 'no_click_reminder')}\n</body>")

    unsubscribe_url = _pause_url(user.id)
    extra_headers = {
        "List-Unsubscribe": f"<{unsubscribe_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "List-Id": "Amazon Israel Alert <alerts.amzfreeil.com>",
    }
    return _send_via_resend(user.notify_email, subject, html_body, plain, extra_headers=extra_headers)


# ── Product update newsletter ─────────────────────────────────────────────────

_NEWSLETTER_SUBJECT = "🚀 חידושים חדשים ב-AMZ Free IL — תוסף כרום, חיפוש ועוד"

_NEWSLETTER_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width:600px){
      .email-container{width:100% !important;}
      .feature-title{font-size:16px !important;}
      .hero-title{font-size:22px !important;}
    }
  </style>
</head>
<body dir="rtl" style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">3 חידושים חדשים שיעזרו לך למצוא עוד יותר משלוח חינם מאמזון לישראל 🚀</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 12px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" class="email-container"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e8e8e8;">
        <tr>
          <td style="background:#ffffff;border-bottom:3px solid #FF9900;padding:28px 24px 20px;text-align:center;">
            <p style="margin:0 0 6px;font-size:13px;color:#999;letter-spacing:1px;text-transform:uppercase;">עדכון מוצר</p>
            <h1 class="hero-title" style="margin:0 0 10px;color:#e47911;font-size:26px;font-weight:bold;line-height:1.3;">🚀 חידושים חדשים ב-<bdi>AMZ Free IL</bdi></h1>
            <p style="margin:0;color:#555;font-size:15px;line-height:1.5;">שלושה כלים חדשים שיעזרו לך להשיג עוד יותר מוצרים עם משלוח חינם לישראל</p>
          </td>
        </tr>
        <tr>
          <td style="padding:24px 28px 8px;">
            <p style="margin:0;font-size:15px;color:#333;line-height:1.8;text-align:right;">היי 👋<br><br>עבדנו קשה בחודשים האחרונים כדי להרחיב את הכלים שיעזרו לך לחסוך בקניות באמזון. הנה מה שחדש:</p>
          </td>
        </tr>
        <tr><td style="padding:16px 28px 0;"><hr style="border:none;border-top:1px solid #f0f0f0;margin:0;"></td></tr>
        <tr>
          <td style="padding:24px 28px;">
            <div style="display:inline-block;background:#FFF3E0;border-radius:12px;padding:12px 16px;margin-bottom:14px;"><span style="font-size:32px;">🧩</span></div>
            <h2 class="feature-title" style="margin:0 0 8px;font-size:18px;color:#111;font-weight:bold;">תוסף לכרום — הוסף מוצר למעקב בלחיצה אחת</h2>
            <p style="margin:0 0 12px;font-size:14px;color:#555;line-height:1.7;">גולש באמזון ומצאת מוצר שמעניין אותך? עד היום היית צריך להעתיק את כתובת המוצר או את ה-<bdi>ASIN</bdi> ולהדביק ידנית בחשבון. עכשיו זה נגמר — התוסף <strong>שולח את המוצר ישירות לחשבון שלך</strong> בלחיצה אחת, בלי לעזוב את אמזון.</p>
            <ul style="margin:0 0 16px;padding-right:20px;font-size:14px;color:#555;line-height:2;">
              <li>✅ גלוש באמזון → לחץ על התוסף → המוצר נוסף אוטומטית</li>
              <li>✅ אין צורך להעתיק <bdi>ASIN</bdi> או כתובת ידנית</li>
              <li>✅ תקבל התראה ברגע שיהיה משלוח חינם</li>
            </ul>
            <table cellpadding="0" cellspacing="0" border="0"><tr><td align="center" bgcolor="#FF9900" style="border-radius:6px;">
              <a href="https://chromewebstore.google.com/detail/amz-free-ship-alert/mbickhgdhofaefhibfbgpacejhbelddn"
                 style="display:inline-block;background:#FF9900;color:#111111;font-family:Arial,sans-serif;font-size:14px;font-weight:bold;text-decoration:none;padding:11px 28px;border-radius:6px;white-space:nowrap;"
                 target="_blank">הורד את התוסף לכרום ←</a>
            </td></tr></table>
          </td>
        </tr>
        <tr><td style="padding:0 28px;"><hr style="border:none;border-top:1px solid #f0f0f0;margin:0;"></td></tr>
        <tr>
          <td style="padding:24px 28px;">
            <div style="display:inline-block;background:#E8F5E9;border-radius:12px;padding:12px 16px;margin-bottom:14px;"><span style="font-size:32px;">🔍</span></div>
            <h2 class="feature-title" style="margin:0 0 8px;font-size:18px;color:#111;font-weight:bold;">חיפוש ישיר באמזון — עם פילטר משלוח חינם לישראל</h2>
            <p style="margin:0 0 12px;font-size:14px;color:#555;line-height:1.7;">רוצה לחפש מוצר ספציפי ולוודא שהוא נשלח חינם לישראל? הכלי החדש שלנו מאפשר לך <strong>לחפש ישירות באמזון</strong> עם פילטר משלוח חינם לישראל מוכן ומוגדר — בלי להתעסק עם הגדרות ידנית.</p>
            <ul style="margin:0 0 16px;padding-right:20px;font-size:14px;color:#555;line-height:2;">
              <li>✅ הכנס מונח חיפוש + טווח מחיר אופציונלי</li>
              <li>✅ התוצאות נפתחות ישירות באמזון עם פילטר <bdi>"Free Shipping"</bdi> מופעל</li>
              <li>✅ תראה רק מוצרים עם <bdi>"FREE delivery"</bdi> + <bdi>"Ships to Israel"</bdi></li>
            </ul>
            <table cellpadding="0" cellspacing="0" border="0"><tr><td align="center" bgcolor="#FF9900" style="border-radius:6px;">
              <a href="{{track_search}}"
                 style="display:inline-block;background:#FF9900;color:#111111;font-family:Arial,sans-serif;font-size:14px;font-weight:bold;text-decoration:none;padding:11px 28px;border-radius:6px;white-space:nowrap;"
                 target="_blank">חפש מוצרים עם משלוח חינם ←</a>
            </td></tr></table>
          </td>
        </tr>
        <tr><td style="padding:0 28px;"><hr style="border:none;border-top:1px solid #f0f0f0;margin:0;"></td></tr>
        <tr>
          <td style="padding:24px 28px;">
            <div style="display:inline-block;background:#E3F2FD;border-radius:12px;padding:12px 16px;margin-bottom:14px;"><span style="font-size:32px;">📦</span></div>
            <h2 class="feature-title" style="margin:0 0 8px;font-size:18px;color:#111;font-weight:bold;">רשימת מוצרים — מה יש עכשיו במשלוח חינם?</h2>
            <p style="margin:0 0 12px;font-size:14px;color:#555;line-height:1.7;">לא צריך לחכות להתראה — העמוד החדש מציג את <strong>כל המוצרים שכרגע נשלחים חינם לישראל</strong>, מתעדכן יומית באופן אוטומטי. פשוט נכנסים ורואים.</p>
            <ul style="margin:0 0 16px;padding-right:20px;font-size:14px;color:#555;line-height:2;">
              <li>✅ רשימה מתעדכנת יומית — תמיד עדכנית</li>
              <li>✅ כל מוצר ברשימה אומת אוטומטית עם משלוח חינם לישראל</li>
              <li>✅ לחץ על מוצר כדי לקנות ישירות באמזון</li>
            </ul>
            <table cellpadding="0" cellspacing="0" border="0"><tr><td align="center" bgcolor="#FF9900" style="border-radius:6px;">
              <a href="{{track_free_products}}"
                 style="display:inline-block;background:#FF9900;color:#111111;font-family:Arial,sans-serif;font-size:14px;font-weight:bold;text-decoration:none;padding:11px 28px;border-radius:6px;white-space:nowrap;"
                 target="_blank">ראה מה חינם עכשיו ←</a>
            </td></tr></table>
          </td>
        </tr>
        <tr><td style="padding:0 28px 8px;"><hr style="border:none;border-top:2px solid #FF9900;margin:0;"></td></tr>
        <tr>
          <td style="padding:28px 28px 24px;text-align:center;background:#FFFBF3;">
            <p style="margin:0 0 6px;font-size:13px;color:#999;letter-spacing:0.5px;">כל הכלים זמינים בחשבון שלך</p>
            <p style="margin:0 0 20px;font-size:16px;color:#333;font-weight:bold;">כנס לחשבון כדי לנצל את כל החידושים 👇</p>
            <table cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;"><tr><td align="center" bgcolor="#e47911" style="border-radius:8px;">
              <a href="{{track_dashboard}}"
                 style="display:inline-block;background:#e47911;color:#ffffff;font-family:Arial,sans-serif;font-size:16px;font-weight:bold;text-decoration:none;padding:13px 40px;border-radius:8px;white-space:nowrap;"
                 target="_blank">כניסה לחשבון שלי</a>
            </td></tr></table>
          </td>
        </tr>
        <tr>
          <td style="padding:0 24px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0faf0;border-radius:8px;border:1px solid #c8e6c9;">
              <tr><td style="padding:14px 18px;text-align:right;">
                <p style="margin:0 0 3px;font-size:13px;font-weight:bold;color:#2e7d32;">💡 טיפ לחיסכון</p>
                <p style="margin:0;font-size:13px;color:#388e3c;line-height:1.6;">הזמינו בין <bdi>$49</bdi> ל-<bdi>$75</bdi> כדי ליהנות ממשלוח חינם ללא מכס ישראלי. מעל <bdi>$75</bdi>? ייתכן מכס של 18% מע"מ + אגרת שחרור.</p>
              </td></tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;border-radius:0 0 10px 10px;border-top:1px solid #eee;padding:16px 24px 20px;text-align:center;">
            <p style="margin:0 0 4px;color:#888;font-size:12px;"><bdi>Amazon Free Shipping to Israel Alert</bdi> · <bdi>amzfreeil.com</bdi></p>
            <p style="margin:0 0 8px;color:#bbb;font-size:11px;">נשלח ב-__SEND_DATE__</p>
            <p style="margin:0;font-size:11px;">
              <a href="{{pause_url}}" style="color:#aaa;text-decoration:underline;">הפסק לקבל עדכונים</a>
              &nbsp;·&nbsp;
              <a href="{{track_dashboard}}" style="color:#aaa;text-decoration:underline;">ניהול העדפות</a>
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
{{open_pixel}}
</body>
</html>"""


def build_newsletter_html(pause_url_val: str) -> str:
    date_str = datetime.now().strftime("%d/%m/%Y")
    return _NEWSLETTER_HTML_TEMPLATE.replace("{{pause_url}}", pause_url_val).replace("__SEND_DATE__", date_str)


def send_newsletter_test(to_email: str, pause_url_val: str) -> bool:
    html = build_newsletter_html(pause_url_val)
    return _send_via_resend(to_email, _NEWSLETTER_SUBJECT, html, "")


# ── Telegram invite ───────────────────────────────────────────────────────────

_TELEGRAM_INVITE_SUBJECT = "📣 הצטרפו לערוץ הטלגרם שלנו — מוצרים חינם ישירות לנייד"

_TELEGRAM_INVITE_HTML = """\
<!DOCTYPE html>
<html dir="rtl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    @media only screen and (max-width:600px){
      .email-container{width:100% !important;}
      .hero-title{font-size:22px !important;}
    }
  </style>
</head>
<body dir="rtl" style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">המוצרים החינמיים הכי טובים — עכשיו גם בטלגרם, ישירות לנייד שלך 📱</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 12px;">
    <tr><td align="center">
      <table width="100%" cellpadding="0" cellspacing="0" class="email-container"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;border:1px solid #e8e8e8;">

        <!-- Header -->
        <tr>
          <td style="background:#ffffff;border-bottom:3px solid #FF9900;padding:28px 24px 20px;text-align:center;">
            <p style="margin:0 0 6px;font-size:13px;color:#999;letter-spacing:1px;">חדש מ-AMZ Free IL</p>
            <h1 class="hero-title" style="margin:0 0 10px;color:#e47911;font-size:26px;font-weight:bold;line-height:1.3;">📱 ערוץ טלגרם חדש — <bdi>@amzfreeil</bdi></h1>
            <p style="margin:0;color:#555;font-size:15px;line-height:1.5;">מוצרים עם משלוח חינם לישראל — ישירות לנייד שלך, בלי לפתוח אתר</p>
          </td>
        </tr>

        <!-- Intro -->
        <tr>
          <td style="padding:24px 28px 8px;">
            <p style="margin:0;font-size:15px;color:#333;line-height:1.8;text-align:right;">היי 👋<br><br>
            פתחנו ערוץ טלגרם רשמי — <strong><bdi>AMZ Free Ship 🇮🇱</bdi></strong>.<br>
            כל יום אנחנו מפרסמים שם מוצרים נבחרים עם משלוח חינם לישראל — עם תמונה, תיאור ומחיר, ישירות בפיד שלך.</p>
          </td>
        </tr>
        <tr><td style="padding:0 28px;"><hr style="border:none;border-top:1px solid #f0f0f0;margin:16px 0;"></td></tr>

        <!-- Benefits -->
        <tr>
          <td style="padding:0 28px 8px;">
            <h2 style="margin:0 0 14px;font-size:17px;color:#111;font-weight:bold;">מה תקבלו בערוץ?</h2>
            <table width="100%" cellpadding="0" cellspacing="0">
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f5f5f5;font-size:14px;color:#333;line-height:1.6;">
                  <span style="font-size:20px;margin-left:10px;">🛍️</span>
                  <strong>מוצרים נבחרים יומית</strong> — לא הכל, רק הכי טובים
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f5f5f5;font-size:14px;color:#333;line-height:1.6;">
                  <span style="font-size:20px;margin-left:10px;">📸</span>
                  <strong>תמונה + תיאור + מחיר</strong> — כל המידע במקום אחד
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f5f5f5;font-size:14px;color:#333;line-height:1.6;">
                  <span style="font-size:20px;margin-left:10px;">✈️</span>
                  <strong>משלוח חינם לישראל בלבד</strong> — כל מוצר אומת
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;font-size:14px;color:#333;line-height:1.6;">
                  <span style="font-size:20px;margin-left:10px;">🔔</span>
                  <strong>הודעות ישירות לנייד</strong> — לא צריך להיכנס לאתר
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr><td style="padding:0 28px;"><hr style="border:none;border-top:1px solid #f0f0f0;margin:16px 0;"></td></tr>

        <!-- CTA -->
        <tr>
          <td style="padding:8px 28px 28px;text-align:center;">
            <p style="margin:0 0 20px;font-size:16px;color:#333;font-weight:bold;">הצטרפו עכשיו — בחינם, ניתן לעזוב בכל עת 👇</p>
            <table cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">
              <tr>
                <td align="center" bgcolor="#0088cc" style="border-radius:8px;">
                  <a href="https://t.me/amzfreeil"
                     style="display:inline-block;background:#0088cc;color:#ffffff;font-family:Arial,sans-serif;
                            font-size:16px;font-weight:bold;text-decoration:none;padding:13px 40px;
                            border-radius:8px;white-space:nowrap;"
                     target="_blank">📱 הצטרפו לערוץ הטלגרם ←</a>
                </td>
              </tr>
            </table>
            <p style="margin:16px 0 0;font-size:13px;color:#888;">@amzfreeil · בחינם לחלוטין</p>
          </td>
        </tr>

        <!-- Tip -->
        <tr>
          <td style="padding:0 24px 20px;">
            <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0faf0;border-radius:8px;border:1px solid #c8e6c9;">
              <tr><td style="padding:14px 18px;text-align:right;">
                <p style="margin:0 0 3px;font-size:13px;font-weight:bold;color:#2e7d32;">💡 טיפ לחיסכון</p>
                <p style="margin:0;font-size:13px;color:#388e3c;line-height:1.6;">הזמינו בין <bdi>$49</bdi> ל-<bdi>$75</bdi> כדי ליהנות ממשלוח חינם ללא מכס ישראלי.</p>
              </td></tr>
            </table>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f8f8f8;border-radius:0 0 10px 10px;border-top:1px solid #eee;padding:16px 24px 20px;text-align:center;">
            <p style="margin:0 0 4px;color:#888;font-size:12px;"><bdi>Amazon Free Shipping to Israel Alert</bdi> · <bdi>amzfreeil.com</bdi></p>
            <p style="margin:0 0 8px;color:#bbb;font-size:11px;">נשלח ב-__SEND_DATE__</p>
            <p style="margin:0;font-size:11px;">
              <a href="{{pause_url}}" style="color:#aaa;text-decoration:underline;">הפסק לקבל עדכונים</a>
              &nbsp;·&nbsp;
              <a href="https://app.amzfreeil.com/dashboard" style="color:#aaa;text-decoration:underline;">ניהול העדפות</a>
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
{{open_pixel}}
</body>
</html>"""


def build_telegram_invite_html(pause_url_val: str) -> str:
    date_str = datetime.now().strftime("%d/%m/%Y")
    return _TELEGRAM_INVITE_HTML.replace("{{pause_url}}", pause_url_val).replace("__SEND_DATE__", date_str)


def send_telegram_invite_test(to_email: str, pause_url_val: str) -> bool:
    html = build_telegram_invite_html(pause_url_val)
    return _send_via_resend(to_email, _TELEGRAM_INVITE_SUBJECT, html, "")
