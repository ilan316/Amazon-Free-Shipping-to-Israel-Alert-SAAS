"""
Email notifier — Web SaaS version.
Adapted from the desktop notifier.py.

Key changes vs desktop:
  - send_user_alert(user, product_orm, result) instead of send_batch_free_shipping_alert(config, items)
  - SMTP credentials come from environment variables
  - user.language and user.notify_email replace config dict
"""

import smtplib
import os
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

logger = logging.getLogger(__name__)

_MAX_NAME_SUBJECT = 72
_MAX_NAME_BODY = 88

# ── Localized strings (identical to desktop) ─────────────────────────────────
_STRINGS = {
    "he": {
        "subject_single":   "🚨 משלוח חינם לישראל: {name}",
        "preheader":        "מצאנו משלוח חינם לישראל! בדוק את המוצר שלך עכשיו",
        "header_title":     "משלוח חינם לישראל 🚚",
        "header_sub1":      "נמצא מוצר עם משלוח חינם",
        "shipping_badge":   "✅ משלוח חינם לישראל · הזמנות $49+",
        "btn_buy":          "קנה עכשיו",
        "urgency":          "⏰ המחיר עשוי להשתנות בכל עת",
        "quick_tip_title":  "💡 טיפ לחיסכון",
        "quick_tip_body":   "הזמינו בין $49 ל-$130 כדי ליהנות ממשלוח חינם ללא מכס ישראלי.",
        "disclosure":       "קישור שותף — הקנייה לא עולה לך יותר, אך אנו עשויים לקבל עמלה קטנה.",
        "footer":           "נבדק: {checked_at} · Amazon Free Shipping to Israel Alert",
        "aod_note":         "⚠️ המשלוח החינמי נמצא תחת <strong>\"כל אפשרויות הקנייה\"</strong>.<br>"
                            "פתח את עמוד המוצר ← לחץ <strong>\"ראה את כל אפשרויות הקנייה\"</strong>"
                            " ← בחר את ההצעה עם משלוח חינם.",
        "aod_plain":        "הערה: המשלוח החינמי נמצא תחת 'כל אפשרויות הקנייה'. "
                            "פתח את הקישור ← לחץ 'ראה את כל אפשרויות הקנייה' ← בחר הצעה עם משלוח חינם.",
        "plain_header":     "🚨 התראת משלוח חינם לישראל!\n",
        "plain_product":    "מוצר",
        "plain_url":        "קישור",
        "plain_urgency":    "⏰ המחיר עשוי להשתנות בכל עת",
        "plain_footer":     "נבדק: {checked_at}",
    },
    "en": {
        "subject_single":   "🚨 FREE Shipping to Israel: {name}",
        "preheader":        "Don't miss out! Price may change at any time — check now",
        "header_title":     "FREE Shipping to Israel 🚚",
        "header_sub1":      "1 product with free shipping found",
        "shipping_badge":   "✅ FREE Shipping to Israel · Orders $49+",
        "btn_buy":          "Buy Now",
        "urgency":          "⏰ Price may change at any time",
        "quick_tip_title":  "💡 Money-Saving Tip",
        "quick_tip_body":   "Order between $49–$130 to enjoy free shipping without Israeli customs fees.",
        "disclosure":       "Affiliate link — no extra cost to you, but we may earn a small commission.",
        "footer":           "Checked at: {checked_at} · Amazon Free Shipping to Israel Alert",
        "aod_note":         "⚠️ Free shipping found in <strong>All Buying Options</strong>.<br>"
                            "Open the product page → click <strong>\"See All Buying Options\"</strong>"
                            " → select the offer with free shipping.",
        "aod_plain":        "NOTE: Found in All Buying Options — open the link, "
                            "click 'See All Buying Options', select the free-shipping offer.",
        "plain_header":     "🚨 FREE Shipping to Israel Alert!\n",
        "plain_product":    "Product",
        "plain_url":        "URL    ",
        "plain_urgency":    "⏰ Price may change at any time",
        "plain_footer":     "Checked at: {checked_at}",
    },
}


def _t(lang: str, key: str, **kw) -> str:
    s = _STRINGS.get(lang, _STRINGS["en"]).get(key, _STRINGS["en"].get(key, ""))
    return s.format(**kw) if kw else s


def _short_product_name(name: str, limit: int = _MAX_NAME_BODY) -> str:
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


def send_simple_email(to: str, subject: str, body_html: str) -> bool:
    """Send a simple transactional email (not a product alert)."""
    import os, smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    sender = os.environ.get("GMAIL_SENDER", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    if not sender or not app_password:
        logger.error("GMAIL_SENDER or GMAIL_APP_PASSWORD not set")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(sender, app_password)
            server.sendmail(sender, [to], msg.as_string())
        logger.info(f"Simple email sent → {to}: {subject}")
        return True
    except Exception as e:
        logger.error(f"Failed to send simple email: {e}")
        return False


def send_user_alert(user, product, result) -> bool:
    """
    Send a free-shipping alert email to a single user.

    user    — ORM User object (notify_email, language)
    product — ORM Product object (asin, name, url)
    result  — CheckResult (found_in_aod, raw_text)

    Returns True on success, False on failure (logs the error).
    """
    lang = getattr(user, "language", "he") or "he"
    recipient = user.notify_email
    affiliate_tag = os.environ.get("AMAZON_AFFILIATE_TAG", "").strip()
    logo_url = os.environ.get("LOGO_URL", "").strip()
    checked_at = datetime.now().strftime("%d/%m/%Y")

    asin = product.asin
    name = _short_product_name(product.name or asin, _MAX_NAME_BODY)
    product_url = (
        f"https://www.amazon.com/dp/{asin}?tag={affiliate_tag}"
        if affiliate_tag else
        f"https://www.amazon.com/dp/{asin}"
    )
    found_in_aod = getattr(result, "found_in_aod", False)

    is_rtl = lang == "he"
    txt_dir = 'dir="rtl"' if is_rtl else ""
    txt_align = "right" if is_rtl else "left"

    # Subject
    subject = _t(lang, "subject_single", name=_short_product_name(product.name or asin, _MAX_NAME_SUBJECT))

    # Plain text
    aod_line = [_t(lang, "aod_plain")] if found_in_aod else []
    lines = [
        _t(lang, "plain_header"),
        f"{_t(lang, 'plain_product')} : {name}",
        f"ASIN    : {asin}",
        f"{_t(lang, 'plain_url')} : {product_url}",
        _t(lang, "plain_urgency"),
        *aod_line,
        "",
        _t(lang, "plain_footer", checked_at=checked_at),
    ]
    text_body = "\n".join(lines)

    # AOD block
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

    logo_tag = f'<img src="{logo_url}" width="140" alt="Amazon Free shipping to Israel Alert" style="display:block; margin:0 auto 12px; max-width:140px;">' if logo_url else ""
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
            {logo_tag}
            <h1 style="margin:0 0 6px;color:#e47911;font-size:22px;font-weight:bold;" {txt_dir}>{_t(lang, "header_title")}</h1>
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
                  <p style="margin:0 0 4px;font-size:16px;font-weight:bold;line-height:1.4;text-align:{txt_align};" {txt_dir}>
                    <a href="{product_url}" style="color:#0066cc;text-decoration:none;">{name}</a>
                  </p>
                  <p style="margin:0 0 10px;font-size:13px;color:#666;text-align:{txt_align};">ASIN: {asin}</p>
                  <p style="margin:0 0 12px;font-size:13px;font-weight:bold;color:#007600;text-align:{txt_align};" {txt_dir}>{_t(lang, "shipping_badge")}</p>
                  <div style="text-align:{txt_align};">{_cta_btn(product_url, _t(lang, "btn_buy"), txt_align)}</div>
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

    # Send
    sender = os.environ.get("GMAIL_SENDER", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not sender or not app_password:
        logger.error("GMAIL_SENDER or GMAIL_APP_PASSWORD not set — cannot send email.")
        return False

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text_body, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    alt["Subject"] = subject
    alt["From"] = sender
    alt["To"] = recipient

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, app_password)
            server.sendmail(sender, [recipient], alt.as_string())
        logger.info(f"Email sent → {recipient}: {subject}")
        return True
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"Gmail auth failed: {e}")
    except smtplib.SMTPException as e:
        logger.error(f"SMTP error: {e}")
    except OSError as e:
        logger.error(f"Network error sending email: {e}")
    return False
