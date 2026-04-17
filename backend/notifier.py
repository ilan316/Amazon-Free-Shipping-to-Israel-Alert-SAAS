"""
Email notifier — Resend version.

Key changes vs Gmail SMTP version:
  - Uses Resend API instead of smtplib
  - Sends from alerts@amzfreeil.com (authenticated domain)
  - Added send_daily_summary() for daily digest emails
"""

import os
import logging
from datetime import datetime
from urllib.parse import urlencode

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
        "quick_tip_body":       "הזמינו בין $49 ל-$130 כדי ליהנות ממשלוח חינם ללא מכס ישראלי.",
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
        "quick_tip_body":       "Order between $49–$130 to enjoy free shipping without Israeli customs fees.",
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
    },
}


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


# ── Resend sender ─────────────────────────────────────────────────────────────

def _send_via_resend(to: str, subject: str, html: str, text: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.error("RESEND_API_KEY not set")
        return False
    resend_client.api_key = api_key
    from_addr = os.environ.get("FROM_EMAIL", "alerts@amzfreeil.com")
    try:
        resend_client.Emails.send({
            "from": from_addr,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        })
        logger.info(f"Email sent via Resend → {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Resend error: {e}")
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
    lang = getattr(user, "language", "he") or "he"
    recipient = user.notify_email
    affiliate_tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    logo_url = os.environ.get("LOGO_URL", "").strip()
    checked_at = datetime.now().strftime("%d/%m/%Y")

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

    body_dir = ' dir="rtl"' if is_rtl else ""
    html_body = f"""<!DOCTYPE html>
<html{body_dir}>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>@media only screen and (max-width:600px){{.email-container{{width:100% !important;}}}}</style>
</head>
<body{body_dir} style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">{_t(lang, "preheader")}</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" class="email-container" style="max-width:600px;width:100%;">
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
                  <p style="margin:0 0 4px;font-size:16px;font-weight:bold;line-height:1.4;text-align:{txt_align};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" {txt_dir}>
                    <a href="{url}" style="color:#111111;text-decoration:none;">{name}</a>
                  </p>
                  <p style="margin:0 0 10px;font-size:13px;color:#666;text-align:{txt_align};">ASIN: {asin}</p>
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
                  <p style="margin:0;font-size:12px;color:#388e3c;line-height:1.5;">{_t(lang, "quick_tip_body")}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;border-radius:0 0 10px 10px;padding:14px 24px;text-align:center;">
            <p style="margin:0 0 6px;color:#888;font-size:12px;" {txt_dir}>{_t(lang, "footer", checked_at=checked_at)}</p>
            <p style="margin:0;color:#bbb;font-size:11px;">Amazon Free Shipping to Israel Alert</p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return _send_via_resend(recipient, subject, html_body, text_body)


# ── Daily summary ─────────────────────────────────────────────────────────────

def send_daily_summary(user, free_products: list) -> bool:
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
    checked_at = datetime.now().strftime("%d/%m/%Y")
    n = len(free_products)

    is_rtl = lang == "he"
    txt_dir = 'dir="rtl"' if is_rtl else ""
    txt_align = "right" if is_rtl else "left"
    body_dir = ' dir="rtl"' if is_rtl else ""

    subject = _t(lang, "subject_summary", n=n)

    # Plain text
    lines = [_t(lang, "plain_summary_header")]
    for p, custom_name in free_products:
        name = _short(custom_name or p.name or p.asin, _MAX_NAME_BODY)
        url = _tracking_url(user.id, p.asin)
        lines.append(f"• {name}")
        lines.append(f"  {url}")
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
    for p, custom_name in free_products:
        name = _short(custom_name or p.name or p.asin, _MAX_NAME_BODY)
        url = _tracking_url(user.id, p.asin)
        product_rows += f"""
        <table width="100%" cellpadding="0" cellspacing="0"
               style="background:#ffffff;border:1px solid #e8e8e8;border-radius:10px;margin-bottom:12px;">
          <tr>
            <td valign="top" style="padding:14px 16px;">
              <p style="margin:0 0 4px;font-size:15px;font-weight:bold;line-height:1.4;text-align:{txt_align};white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" {txt_dir}>
                <a href="{url}" style="color:#111111;text-decoration:none;">{name}</a>
              </p>
              <p style="margin:0 0 8px;font-size:12px;color:#666;text-align:{txt_align};">ASIN: {p.asin}</p>
              <p style="margin:0 0 10px;font-size:13px;font-weight:bold;color:#007600;text-align:{txt_align};" {txt_dir}>{_t(lang, "shipping_badge")}</p>
              <div style="text-align:{txt_align};">{_cta_btn(url, _t(lang, "btn_buy"), txt_align)}</div>
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
  <style>@media only screen and (max-width:600px){{.email-container{{width:100% !important;}}}}</style>
</head>
<body{body_dir} style="margin:0;padding:0;background:#f3f3f3;font-family:Arial,'Segoe UI',sans-serif;">
  <div style="display:none;max-height:0;overflow:hidden;">{_t(lang, "preheader")}</div>
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f3f3f3;padding:24px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" class="email-container" style="max-width:600px;width:100%;">
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
                  <p style="margin:0;font-size:12px;color:#388e3c;line-height:1.5;">{_t(lang, "quick_tip_body")}</p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <tr>
          <td style="background:#f8f8f8;border-radius:0 0 10px 10px;padding:14px 24px;text-align:center;">
            <p style="margin:0 0 6px;color:#888;font-size:12px;" {txt_dir}>{_t(lang, "footer", checked_at=checked_at)}</p>
            <p style="margin:0;color:#bbb;font-size:11px;">Amazon Free Shipping to Israel Alert</p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    return _send_via_resend(recipient, subject, html_body, text_body)
