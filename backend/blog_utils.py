"""
Blog draft generation utilities.
Amazon API → Claude → build HTML → commit to GitHub.
"""
import asyncio
import json
import logging
import re
import base64
import os
from datetime import date
from html import escape, unescape

import httpx
import anthropic

logger = logging.getLogger(__name__)


def claude_text(message) -> str:
    """Extract the first text block from a Claude response.

    Newer models (claude-sonnet-5) can prepend a ThinkingBlock to
    message.content, so content[0] is no longer guaranteed to be text.
    Iterate and return the first block that actually carries text.
    """
    for block in message.content:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    kinds = [getattr(block, "type", "?") for block in message.content]
    raise ValueError(
        f"No text block in Claude response "
        f"(stop_reason={message.stop_reason}, blocks={kinds})"
    )


_TAG_RE = re.compile(r"<[^>]+>")


def strip_tags(text: str) -> str:
    """Plain-text form of a generated blog title.

    Drafts are written with English terms wrapped in <bdi> so the blog HTML
    renders correctly inside RTL Hebrew. Anywhere the title is consumed as plain
    text — Telegram/Facebook captions, an img alt — that markup would show up
    verbatim, so it is stripped at those boundaries rather than in the prompt
    (the <h2> headings genuinely need it).
    """
    return unescape(_TAG_RE.sub("", text or "")).strip()


MONTHS_HE = [
    "ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
    "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר",
]


async def fetch_amazon_product(asin: str) -> dict:
    client_id = os.getenv("AMAZON_CLIENT_ID")
    client_secret = os.getenv("AMAZON_CLIENT_SECRET")
    partner_tag = os.getenv("AMAZON_AFFILIATE_TAG") or os.getenv("AMAZON_PARTNER_TAG", "amzfreeil-20")
    marketplace = os.getenv("AMAZON_MARKETPLACE", "www.amazon.com")

    async with httpx.AsyncClient(timeout=30) as client:
        token_resp = await client.post(
            "https://api.amazon.com/auth/o2/token",
            json={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": "creatorsapi::default",
            },
            headers={"Content-Type": "application/json"},
        )
        token_resp.raise_for_status()
        token = token_resp.json()["access_token"]

        # Retry on 429/ThrottleException with exponential backoff.
        # These are transient rate-limit errors (not ItemNotAccessible), so
        # a short wait + retry usually succeeds.
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            product_resp = await client.post(
                "https://creatorsapi.amazon/catalog/v1/getItems",
                json={
                    "itemIds": [asin],
                    "itemIdType": "ASIN",
                    "partnerTag": partner_tag,
                    "partnerType": "Associates",
                    "marketplace": marketplace,
                    "resources": [
                        "images.primary.large",
                        "images.primary.medium",
                        "images.primary.small",
                        "itemInfo.title",
                        "itemInfo.features",
                        "itemInfo.manufactureInfo",
                        "itemInfo.byLineInfo",
                        "browseNodeInfo.browseNodes",
                    ],
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "x-marketplace": marketplace,
                },
            )
            if product_resp.status_code == 429 and attempt < max_attempts:
                wait = 2 ** attempt  # 2s, 4s
                logger.warning(
                    "fetch_amazon_product: 429 throttled for ASIN %s — retry %d/%d in %ds",
                    asin, attempt, max_attempts, wait,
                )
                await asyncio.sleep(wait)
                continue
            break

        product_resp.raise_for_status()
        data = product_resp.json()

    items = data.get("itemsResult", {}).get("items", [])
    if not items:
        errors = data.get("errors", [])
        err_code = errors[0].get("code", "Unknown") if errors else "NoItems"
        logger.error("fetch_amazon_product: no items for ASIN %s — code: %s", asin, err_code)
        if err_code == "ItemNotAccessible":
            raise ValueError(f"המוצר {asin} חסום ב-Amazon API (ItemNotAccessible) — נסה ASIN אחר")
        raise ValueError(f"לא נמצא מוצר עבור ASIN {asin} (קוד: {err_code})")
    item = items[0]
    title = item.get("itemInfo", {}).get("title", {}).get("displayValue", "")
    features = item.get("itemInfo", {}).get("features", {}).get("displayValues", [])
    primary = item.get("images", {}).get("primary", {})
    image_url = (
        primary.get("large", {}).get("url", "")
        or primary.get("medium", {}).get("url", "")
        or primary.get("small", {}).get("url", "")
    )
    mfr = item.get("itemInfo", {}).get("manufactureInfo", {}) or {}
    model = mfr.get("model", {}).get("displayValue", "") if mfr else ""
    brand = item.get("itemInfo", {}).get("byLineInfo", {}).get("brand", {}).get("displayValue", "")
    browse_nodes = item.get("browseNodeInfo", {}).get("browseNodes", [])
    category = browse_nodes[0].get("displayName", "") if browse_nodes else ""
    aff_url = f"https://www.amazon.com/dp/{asin}?tag={partner_tag}"

    return {
        "asin": asin,
        "title": title,
        "features": features,
        "image": image_url,
        "model": model,
        "brand": brand,
        "category": category,
        "url": aff_url,
    }


async def generate_with_claude(product: dict, israel_price: float | None, amazon_price: float, min_order_49: bool = False) -> dict:
    today_display = date.today().strftime("%d/%m/%Y")
    features_text = "\n".join(f"- {f}" for f in product.get("features", []))

    amazon_price_label = "מחיר המוצר בלבד — משלוח חינם רק בקנייה מעל $49" if min_order_49 else "מחיר סופי כולל מיסים ומשלוח"

    if israel_price is not None:
        savings = round(israel_price - amazon_price)
        price_context = f"""מחיר בישראל: ₪{israel_price}
מחיר באמזון ({amazon_price_label}): ₪{amazon_price}
חיסכון: ~₪{savings}"""
        angle = "השוואת מחירים — המוצר נמכר בישראל אך זול יותר באמזון"
    else:
        price_context = f"""מחיר באמזון ({amazon_price_label}): ₪{amazon_price}
זמינות בישראל: המוצר אינו נמכר בחנויות בישראל — ניתן להשיגו רק דרך אמזון"""
        angle = "בלעדיות אמזון — המוצר אינו זמין בישראל"

    prompt = f"""אתה כותב תוכן בעברית לאתר amzfreeil.com — אתר ישראלי שעוזר לאנשים לקנות מאמזון.

מוצר: {product['title']}
דגם: {product.get('model', '')}
ASIN: {product['asin']}
מאפיינים (מדף אמזון הרשמי):
{features_text}

{price_context}
תאריך: {today_display}
זווית הפוסט: {angle}

חשוב: seo_title חייב להיות עד 52 תווים כולל השנה — זו הכותרת שגוגל מציג בתוצאות החיפוש, וכותרת ארוכה נחתכת.
החזר JSON בדיוק בפורמט הבא (ללא markdown, ללא טקסט לפני/אחרי):
{{
  "slug": "שם-קובץ-קצר-באנגלית-amazon-israel",
  "title_he": "כותרת מלאה בעברית לפוסט — {'כדאי לקנות מאמזון לישראל?' if israel_price is not None else 'המוצר שלא תמצאו בישראל — רק באמזון'} (2026)",
  "seo_title": "כותרת קצרה ל-<title> של גוגל — עד 52 תווים כולל השנה. שם המוצר + זווית קצרה מאוד, בלי 'כדאי לקנות מאמזון לישראל'. דוגמה: 'Samsung 990 PRO SSD 2TB — כדאי? (2026)'",
  "title_short": "שם קצר של המוצר — חובה במבנה: מתאר עברי + השם/דגם באנגלית. תמיד להתחיל במילה עברית שמתארת מה המוצר (למשל 'ערכת כלים', 'מסך נייד', 'אוזניות'), ואז שם המותג/דגם באנגלית. אסור אנגלית בלבד. דוגמאות: 'ערכת כלים iFixit Pro Tech Toolkit', 'מסך נייד InnoView 15.6 אינץ''",
  "description_he": "תיאור SEO בעברית, עד 155 תווים",
  "eyebrow": "אייקון + קטגוריה (למשל: 💻 ביקורת מוצר)",
  "intro_p1": "<p>2-3 משפטים בשפה פשוטה: מה המוצר, מה הוא עושה, ולמי הוא מתאים — בלי מפרט טכני יבש (HTML, <bdi> לאנגלית)</p>",
  "use_cases": ["שימוש/קהל יעד 1", "שימוש 2", "שימוש 3", "שימוש 4"],
  "section1_p1": "<p>פסקה עובדתית אחת — תאר את המוצר לפי הספציפיקציות בלבד (HTML, <bdi> לאנגלית). אל תוסיף 'פופולרי', 'אהוב', 'כולם מדברים'. השתמש במספרים מהמאפיינים. סיים עם: — לפי מפרט היצרן</p>",
  "specs_rows": [
    {{"label": "מפרט", "value": "ערך"}}
  ],
  "pros": ["יתרון 1", "יתרון 2", "יתרון 3", "יתרון 4", "יתרון 5"],
  "cons": ["מה לשים לב 1", "מה לשים לב 2", "מה לשים לב 3", "מה לשים לב 4"],
  "faqs": [
    {{"q": "שאלה?", "a": "תשובה (HTML, <bdi> לאנגלית)"}}
  ],
  "summary_p1": "<p>פסקת סיכום ראשונה (HTML)</p>",
  "summary_p2": "<p>פסקת סיכום שנייה עם אזכור המשלוח (HTML)</p>",
  "cta_h3": "רוצה לדעת ברגע שיש משלוח חינם על המוצר הזה?",
  "cta_p": "הוסף את המוצר לניטור — ואנחנו נשלח לך מייל ברגע שהמשלוח חינם. ללא עלות, ללא כרטיס אשראי.",
  "breadcrumb_label": "שם מוצר קצר — ביקורת",
  "product_schema_description": "תיאור קצר של המוצר בעברית לסכמה"
}}

כללים:
- כתוב עברית טבעית ומקצועית
- כתוב לקהל רחב בעברית פשוטה — הימנע ממונחים מקצועיים/לועזיים לא-מוכרים (למשל "סוטה", "בליץ'", "דיהיידרציה"). אם אין תחליף, הסבר במילים פשוטות (למשל: "הקפצת ירקות במחבת" ולא "סוטה ירקות")
- עטוף מונחים אנגליים ב-<bdi></bdi>
- אל תמציא מחירים — רק המספרים שקיבלת
- אל תמציא מפרטים — רק מה שמופיע ב"מאפיינים"
- slug: קצר, אנגלית, מקפים, מסתיים ב-amazon-israel
- FAQs: בדיוק 4 שאלות
- intro_p1: הסבר פשוט מה המוצר עושה ולמי הוא מתאים — לא מפרט. מותר להשתמש בשימושים אופייניים של קטגוריית המוצר (למשל: כלי רוטרי → חיתוך/חריטה/ליטוש). אל תמציא מחירים.
- use_cases: 3-5 שימושים או קהלי יעד קונקרטיים
- specs_rows: חלץ מהמאפיינים (4-8 שורות)
- {"אם המוצר לא נמכר בישראל: הדגש את הבלעדיות והעובדה שזו הדרך היחידה להשיגו. אל תמציא מחיר ישראלי." if israel_price is None else "הדגש את החיסכון הכספי ביחס לקנייה בישראל."}"""

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    message = await client.messages.create(
        model="claude-sonnet-5",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = claude_text(message).strip()
    return await _parse_claude_json(raw, client)


def _extract_json_block(raw: str) -> str:
    """Strip markdown fences and any prose around the JSON object."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]
    return raw.strip()


async def _parse_claude_json(raw: str, client: "anthropic.AsyncAnthropic", _attempts: int = 2) -> dict:
    """
    Parse JSON returned by Claude. LLM output occasionally contains an
    unescaped quote/newline inside a Hebrew string, which breaks json.loads.
    On failure, ask Claude to repair its own output into valid JSON, up to
    a few times, before giving up.
    """
    cleaned = _extract_json_block(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as first_err:
        last_err = first_err
        current = cleaned
        for attempt in range(_attempts):
            logger.warning(
                "blog-draft: Claude JSON invalid (%s) — repair attempt %d/%d",
                last_err, attempt + 1, _attempts,
            )
            try:
                repair = await client.messages.create(
                    model="claude-sonnet-5",
                    max_tokens=8192,
                    messages=[{
                        "role": "user",
                        "content": (
                            "הטקסט הבא אמור להיות JSON תקין אך יש בו שגיאת תחביר "
                            f"({last_err.msg} בסביבות תו {last_err.pos}). "
                            "החזר את אותו תוכן בדיוק כ-JSON תקין בלבד — ללא markdown, "
                            "ללא הסבר, ללא טקסט לפני או אחרי. הקפד להבריח (escape) "
                            "מרכאות כפולות ותווי שורה חדשה בתוך ערכי מחרוזת.\n\n"
                            + current
                        ),
                    }],
                )
                current = _extract_json_block(claude_text(repair).strip())
                return json.loads(current)
            except json.JSONDecodeError as e:
                last_err = e
                continue
        raise last_err


def build_post_html(product: dict, content: dict, israel_price: float | None, amazon_price: float, min_order_49: bool = False, voltage_warning: bool = False) -> str:
    partner_tag = os.getenv("AMAZON_AFFILIATE_TAG") or os.getenv("AMAZON_PARTNER_TAG", "amzfreeil-20")
    today = date.today()
    today_he = f"{today.day} ב{MONTHS_HE[today.month - 1]} {today.year}"
    today_iso = today.isoformat()
    today_display = today.strftime("%d/%m/%Y")
    # הצג מחיר שלם בלי ".0" (91 במקום 91.0)
    if amazon_price is not None and float(amazon_price).is_integer():
        amazon_price = int(amazon_price)
    if israel_price is not None and float(israel_price).is_integer():
        israel_price = int(israel_price)
    savings = round(israel_price - amazon_price) if israel_price is not None else None
    # מוצר מתחת לסף $49: המחיר הוא מחיר המוצר בלבד, משלוח חינם רק בקנייה מעל $49
    amazon_price_small = "(מחיר המוצר בלבד)" if min_order_49 else "(מחיר סופי כולל מיסים ומשלוח)"
    ship_note_text = ' משלוח חינם בקנייה מעל <bdi>$49</bdi>.' if min_order_49 else ''
    takeaway_price_suffix = ' — מותנה במשלוח חינם בקנייה מעל <bdi>$49</bdi>' if min_order_49 else ''
    takeaway_price_label = 'מחיר המוצר' if min_order_49 else 'באמזון (סופי כולל מיסים ומשלוח)'
    takeaway_amazon_prefix = 'מחיר באמזון' if min_order_49 else 'מחיר באמזון (סופי כולל מיסים ומשלוח)'
    # תיבת אזהרת מע"מ ייבוא — לא רלוונטית למוצרים מתחת ל-$49 (מתחת גם לסף הפטור $75; המחיר בלי משלוח)
    import_vat_box = '' if min_order_49 else (
        '<div style="background:rgba(255,153,0,.08);border:1.5px solid rgba(255,153,0,.4);border-radius:12px;padding:14px 18px;margin:16px 0;font-size:.9rem;line-height:1.7;">\n'
        '          <strong>⚠️ חשוב: המחיר באמזון כולל כבר את מע"מ הייבוא</strong><br>\n'
        '          אמזון מציג מחיר סופי לישראל כולל <bdi>"Import Fees Deposit"</bdi> — אין הפתעות במכס. '
        # הקובץ הזה כבר יושב תחת /blog/ — "../" היה מוביל ל-404 בשורש
        '<a href="mekhs-umaam-amazon-israel.html" style="color:var(--brand-deep, #ff6a00);">מדריך מלא על מכס ומע"מ ←</a>\n'
        '        </div>'
    )
    asin = product["asin"]
    image = product.get("image", "")
    aff_url = f"https://www.amazon.com/dp/{asin}?tag={partner_tag}"
    slug = content["slug"]
    blog_url = f"https://www.amzfreeil.com/blog/{slug}.html"

    extra_specs_rows = []
    if product.get("brand"):
        extra_specs_rows.append({"label": "מותג", "value": product["brand"]})
    if product.get("category"):
        extra_specs_rows.append({"label": "קטגוריה", "value": product["category"]})
    all_specs_rows = extra_specs_rows + content.get("specs_rows", [])

    specs_rows_html = ""
    for i, row in enumerate(all_specs_rows):
        bg = "background:rgba(0,0,0,.02);" if i % 2 == 1 else ""
        specs_rows_html += (
            f'            <tr style="{bg}border-bottom:1px solid rgba(23,32,51,.07);">\n'
            f'              <td style="padding:10px 14px;">{row["label"]}</td>\n'
            f'              <td style="padding:10px 14px;"><bdi>{row["value"]}</bdi></td>\n'
            f"            </tr>\n"
        )

    voltage_warning_html = ""
    if voltage_warning:
        voltage_warning_html = """
      <div style="background:rgba(255,153,0,.08);border:1.5px solid rgba(255,153,0,.4);border-radius:12px;padding:14px 18px;margin:16px 0;font-size:.9rem;line-height:1.7;">
        <strong>⚠️ התאמת חשמל לישראל:</strong><br>
        מוצר זה עשוי להגיע עם תקע אמריקאי ולפעול במתח <bdi>110V</bdi>. יש לבדוק במפרט האם הוא תומך ב-<bdi>100–240V</bdi>. אם לא, יידרש ממיר מתח בנוסף למתאם תקע.
      </div>
"""

    use_cases_html = "\n".join(f"              <li>{u}</li>" for u in content.get("use_cases", []))
    who_box_html = ""
    if use_cases_html:
        who_box_html = (
            '        <div style="background:rgba(22,125,70,.06);border:1px solid rgba(22,125,70,.2);border-radius:14px;padding:16px 20px;margin:16px 0;">\n'
            '          <p style="font-weight:700;margin:0 0 10px;color:#167d46;">👤 למי זה מתאים?</p>\n'
            '          <ul style="margin:0;padding-right:18px;line-height:1.9;font-size:.92rem;">\n'
            f"{use_cases_html}\n"
            "          </ul>\n"
            "        </div>\n"
        )
    pros_html = "\n".join(f"              <li>{p}</li>" for p in content.get("pros", []))
    cons_html = "\n".join(f"              <li>{c}</li>" for c in content.get("cons", []))

    faqs_html = ""
    faq_schema = []
    for faq in content.get("faqs", []):
        faqs_html += (
            f'        <div class="blog-faq-item">\n'
            f'          <p class="blog-faq-q">{faq["q"]}</p>\n'
            f'          <p class="blog-faq-a">{faq["a"]}</p>\n'
            f"        </div>\n"
        )
        faq_schema.append({
            "@type": "Question",
            "name": faq["q"],
            "acceptedAnswer": {"@type": "Answer", "text": faq["a"]},
        })

    schema = json.dumps([
        {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": content["title_he"],
            "description": content["description_he"],
            "url": blog_url,
            "datePublished": today_iso,
            "dateModified": today_iso,
            "inLanguage": "he",
            "image": image,
            "author": {"@type": "Person", "name": "אילן", "url": "https://www.amzfreeil.com/about.html"},
            "publisher": {
                "@type": "Organization",
                "name": "AMZ Free Ship Alert",
                "url": "https://www.amzfreeil.com",
                "logo": {"@type": "ImageObject", "url": "https://www.amzfreeil.com/logo-new.png"},
            },
        },
        {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": product["title"],
            "description": content["product_schema_description"],
            "brand": {"@type": "Brand", "name": product.get("brand") or product["title"].split()[0]},
            "category": product.get("category", ""),
            "sku": asin,
            "offers": {
                "@type": "Offer",
                "url": aff_url,
                "priceCurrency": "ILS",
                "availability": "https://schema.org/InStock",
                "seller": {"@type": "Organization", "name": "Amazon"},
            },
        },
        {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": faq_schema,
        },
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            # tools/build-internal-links.js באתר מזריק כאן פריט קטגוריה רביעי
            # (prices.html#cat-x) אחרי הפוש, כדי שהסכמה תתאים לפירורי הלחם הגלויים.
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "דף הבית", "item": "https://www.amzfreeil.com/"},
                {"@type": "ListItem", "position": 2, "name": "סקירות מוצרים", "item": "https://www.amzfreeil.com/prices.html"},
                {"@type": "ListItem", "position": 3, "name": content["breadcrumb_label"], "item": blog_url},
            ],
        },
    ], ensure_ascii=False, indent=2)

    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{content.get('seo_title') or content['title_he']} | AMZ Free Ship Alert</title>
  <meta name="description" content="{content['description_he']}" />

  <meta property="og:title" content="{content['title_he']}" />
  <meta property="og:description" content="{content['description_he']}" />
  <meta property="og:image" content="{image}" />
  <meta property="og:image:width" content="1000" />
  <meta property="og:image:height" content="1000" />
  <meta property="og:type" content="article" />
  <meta property="og:url" content="{blog_url}" />
  <meta property="og:locale" content="he_IL" />
  <meta property="article:published_time" content="{today_iso}" />
  <meta property="article:modified_time" content="{today_iso}" />

  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{content['title_he']}" />
  <meta name="twitter:description" content="{content['description_he']}" />
  <meta name="twitter:image" content="{image}" />

  <link rel="icon" type="image/png" href="../logo-new.png" />
  <meta name="robots" content="noindex,nofollow" />
  <link rel="canonical" href="{blog_url}" />
  <link rel="stylesheet" href="../styles.css" media="print" onload="this.media='all'" />
  <noscript><link rel="stylesheet" href="../styles.css" /></noscript>

  <script type="application/ld+json">
  {schema}
  </script>
</head>
<body>
  <a href="#main-content" class="skip-nav">דלג לתוכן הראשי</a>
  <div class="bg-glow bg-glow-a"></div>
  <div class="bg-glow bg-glow-b"></div>

  <div id="fixed-header">
    <div class="urgency-bar">
      <span class="urgency-dot"></span>
      לא כל מוצר באמזון נשלח חינם לישראל — <strong>קבל התראה ברגע שמוצר מציע משלוח חינם</strong>
    </div>
    <div class="topbar-outer">
      <header class="topbar" id="topbar">
        <a class="brand" href="/">
          <picture>
            <source srcset="../logo-new.webp" type="image/webp">
            <img src="../logo-new.png" alt="AMZ Free Ship Alert — לוגו" class="brand-logo-img" width="36" height="36" />
          </picture>
          <span>AMZ Free Ship Alert</span>
        </a>
        <nav id="main-nav">
          <a href="/#features">יכולות</a>
          <a href="/#how">איך זה עובד</a>
          <a href="../web-guide.html">מדריך מקוון</a>
          <a href="/#faq">שאלות נפוצות</a>
          <span class="nav-break"></span>
          <a href="../free-products.html">מוצרים בחינם 🚚</a>
          <a href="../search.html">חיפוש מוצרים 🔍</a>
          <a href="../prices.html">סקירות 📝</a>
          <a href="../about.html">אודות</a>
          <a href="../blog/" class="nav-active">בלוג</a>
          <a href="/#contact">צרו קשר</a>
        </nav>
        <div class="nav-cta-group">
          <a class="btn btn-primary btn-sm" href="https://app.amzfreeil.com" target="_blank" id="nav-web-btn" aria-label="כניסה למוניטור" style="padding:12px 24px;">🌐 כניסה למוניטור</a>
        </div>
        <button class="hamburger" id="hamburger-btn" aria-label="פתח תפריט" aria-expanded="false" aria-controls="main-nav">
          <svg width="22" height="22" viewBox="0 0 22 22" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect y="4" width="22" height="2" rx="1" fill="currentColor"/>
            <rect y="10" width="22" height="2" rx="1" fill="currentColor"/>
            <rect y="16" width="22" height="2" rx="1" fill="currentColor"/>
          </svg>
        </button>
      </header>
    </div>
  </div>

  <main id="main-content">

    <section class="blog-hero">
      <div class="blog-hero-inner">
        <nav class="blog-breadcrumb" aria-label="פירורי לחם">
          <a href="/">דף הבית</a>
          <span aria-hidden="true">›</span>
          <a href="../prices.html">סקירות מוצרים</a>
          <span aria-hidden="true">›</span>
          <span aria-current="page">{content['breadcrumb_label']}</span>
        </nav>
        <p class="eyebrow">{content['eyebrow']}</p>
        <h1>{content['title_he']}</h1>
        <div class="blog-meta">
          <span>{today_he}</span>
          <span class="blog-meta-sep">·</span>
          <span>כתב: <a href="https://www.amzfreeil.com/about.html" style="color:inherit">אילן</a></span>
        </div>
      </div>
    </section>

    <img
      src="{image}"
      alt="{product['title']}"
      class="blog-hero-img"
      loading="eager"
      style="object-fit:contain;background:#f5f5f5;width:auto;max-width:min(500px,100%);max-height:360px;aspect-ratio:unset;"
    />

    <article class="blog-body">

      <div class="blog-takeaway">
        <p class="blog-takeaway__title">✅ בקצרה — מה חשוב לדעת</p>
        <ul>
          {"<li>נכון ל-" + today_display + ": בישראל ₪" + str(israel_price) + " | " + takeaway_price_label + " ₪" + str(amazon_price) + " — חיסכון של ~₪" + str(savings) + takeaway_price_suffix + "</li>" if israel_price is not None else "<li>המוצר <strong>אינו נמכר בישראל</strong> — ניתן להשיגו רק דרך אמזון</li><li>" + takeaway_amazon_prefix + ": ₪" + str(amazon_price) + " נכון ל-" + today_display + takeaway_price_suffix + "</li>"}
          <li>המשלוח החינם <strong>זמני ומשתנה</strong> — בדקו לפני רכישה</li>
        </ul>
      </div>

      <section>
        <h2>מה זה {content['title_short']} ולמי הוא מתאים?</h2>
        {content.get('intro_p1', '')}
{who_box_html}        {content['section1_p1']}
      </section>

      <section>
        <h2>מפרט טכני — כל מה שצריך לדעת</h2>
        <table style="width:100%;border-collapse:collapse;font-size:.93rem;margin:16px 0;">
          <thead>
            <tr style="background:rgba(255,153,0,.1);font-weight:700;">
              <th style="padding:10px 14px;text-align:right;border-bottom:2px solid rgba(23,32,51,.12);">מפרט</th>
              <th style="padding:10px 14px;text-align:right;border-bottom:2px solid rgba(23,32,51,.12);">ערך</th>
            </tr>
          </thead>
          <tbody>
{specs_rows_html}          </tbody>
        </table>
      </section>
{voltage_warning_html}

      <section>
        <h2>כמה עולה {content['title_short']}?</h2>
        <table style="width:100%;border-collapse:collapse;font-size:.95rem;margin:16px 0;">
          <thead>
            <tr style="background:rgba(255,153,0,.1);font-weight:700;">
              <th style="padding:10px 14px;text-align:right;border-bottom:2px solid rgba(23,32,51,.12);">מקור</th>
              <th style="padding:10px 14px;text-align:right;border-bottom:2px solid rgba(23,32,51,.12);">מחיר</th>
            </tr>
          </thead>
          <tbody>
            {"" if israel_price is None else f'<tr style="border-bottom:1px solid rgba(23,32,51,.07);"><td style="padding:10px 14px;">בישראל (הזול ביותר)</td><td style="padding:10px 14px;font-weight:700;">₪' + str(israel_price) + '</td></tr>'}
            <tr style="background:rgba(22,125,70,.05);border-bottom:1px solid rgba(23,32,51,.07);">
              <td style="padding:10px 14px;">אמזון <small style="color:#4d5a70;">{amazon_price_small}</small></td>
              <td style="padding:10px 14px;font-weight:700;color:#167d46;">₪{amazon_price}</td>
            </tr>
            {"" if savings is None else f'<tr style="border-bottom:1px solid rgba(23,32,51,.07);"><td style="padding:10px 14px;">חיסכון</td><td style="padding:10px 14px;font-weight:700;color:#167d46;">~₪' + str(savings) + '</td></tr>'}
          </tbody>
        </table>
        {"<p style=\"font-size:.8rem;color:#4d5a70;margin:0 0 16px;\">* נכון ל-" + today_display + ". המחירים משתנים — בדקו לפני רכישה." + ship_note_text + "</p>" if israel_price is not None else "<p style=\"font-size:.8rem;color:#4d5a70;margin:0 0 16px;\">* מחיר באמזון נכון ל-" + today_display + ". עשוי להשתנות — בדקו לפני רכישה." + ship_note_text + "</p>"}

        {import_vat_box}

        <div style="text-align:center;margin:24px 0;">
          <a href="{aff_url}" target="_blank" rel="noopener sponsored"
             style="display:inline-block;background:linear-gradient(135deg,#ff9900,#ff6a00);color:#172033;font-weight:800;padding:14px 32px;border-radius:14px;text-decoration:none;font-size:1.05rem;box-shadow:0 8px 24px rgba(255,153,0,.3);">
            בדוק מחיר נוכחי באמזון ←
          </a>
          <p style="font-size:.78rem;color:#4d5a70;margin:10px 0 0;">קישור שותף — לא עולה לכם יותר</p>
        </div>
      </section>

      <!-- אין כאן CTA. היה כאן סקשן "רוצה לדעת כשיש משלוח חינם" שהיה כפילות
           כמעט מילולית של blog-cta-box שבסוף המאמר — אותה כותרת, אותו כפתור.
           הוא הוסר מ-155 הפוסטים הקיימים ומהתבנית (28/07/2026) כדי לצמצם
           boilerplate זהה בין הפוסטים. אל תחזיר אותו. -->

      <section>
        <h2>יתרונות וחסרונות</h2>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0;">
          <div style="background:rgba(22,125,70,.06);border:1px solid rgba(22,125,70,.2);border-radius:14px;padding:18px;">
            <p style="font-weight:700;margin:0 0 12px;color:#167d46;">✅ יתרונות</p>
            <ul style="margin:0;padding-right:18px;line-height:2;font-size:.9rem;">
{pros_html}
            </ul>
          </div>
          <div style="background:rgba(220,50,50,.04);border:1px solid rgba(220,50,50,.15);border-radius:14px;padding:18px;">
            <p style="font-weight:700;margin:0 0 12px;color:#b91c1c;">⚠️ מה לשים לב</p>
            <ul style="margin:0;padding-right:18px;line-height:2;font-size:.9rem;">
{cons_html}
            </ul>
          </div>
        </div>
      </section>

      <section class="blog-faq">
        <h2>שאלות נפוצות</h2>
{faqs_html}      </section>

      <section>
        <h2>סיכום</h2>
        {content['summary_p1']}
        {content['summary_p2']}
      </section>

      <div class="blog-cta-box">
        <p class="blog-cta-box__icon">🔔</p>
        <h3>{content['cta_h3']}</h3>
        <p>{content['cta_p']}</p>
        <a class="btn btn-primary btn-xl" href="https://app.amzfreeil.com" target="_blank" rel="noopener">
          <span>הירשם חינם ←</span>
          <small>עובד מיד · ללא כרטיס אשראי</small>
        </a>
      </div>

      <div style="background:rgba(23,32,51,.04);border:1px solid rgba(23,32,51,.1);border-radius:12px;padding:14px 18px;margin:24px 0;font-size:.82rem;color:#4d5a70;line-height:1.7;">
        <!-- גרסה מקוצרת: הנוסח המלא יושב ב-/#disclosure. עומד בדרישת Amazon
             Associates לגילוי גלוי בעמוד, בלי 60 מילים זהות בכל פוסט. -->
        <strong>גילוי נאות:</strong> עמוד זה מכיל קישורי שותף של <bdi>Amazon Associates</bdi> — ללא עלות נוספת עבורכם. <a href="/#disclosure" style="color:var(--brand-deep, #ff6a00);">פרטים ←</a>
      </div>

      <div class="author-bio">
        <div class="author-bio__avatar">א</div>
        <div class="author-bio__info">
          <p class="author-bio__name">אילן</p>
          <p class="author-bio__desc">מפתח Python עצמאי עם 5+ שנות ניסיון. יצר את <strong>AMZ Free Ship Alert</strong> כדי לעזור לישראלים לחסוך בקניות מאמזון — בלי לפספס הזדמנויות משלוח חינם. <a href="../about.html">קרא עוד אודותי →</a></p>
        </div>
      </div>
    </article>

    <!-- RELATED:START -->
    <!-- RELATED:END -->

    <section class="section" style="max-width:860px;margin:0 auto 64px;">
      <h2 style="font-family:Rubik,sans-serif;font-size:1.25rem;margin-bottom:24px;">מאמרים קשורים</h2>
      <div class="blog-index-grid" style="grid-template-columns:repeat(auto-fill,minmax(260px,1fr));">
        <a class="blog-index-card" href="hamutzarim-hakhi-kedaim-laknot-bamazon-israel.html">
          <div class="blog-card-body" style="padding:20px;">
            <div class="blog-index-card__cat">🛒 מדריך קנייה</div>
            <h3 class="blog-index-card__title">המוצרים הכי כדאיים לקנות באמזון לישראל</h3>
            <div class="blog-index-card__footer"><span class="blog-index-card__read">קריאה: ~5 דקות ←</span></div>
          </div>
        </a>
        <a class="blog-index-card" href="mishloach-hinam-amazon-israel.html">
          <div class="blog-card-body" style="padding:20px;">
            <div class="blog-index-card__cat">📦 מדריך</div>
            <h3 class="blog-index-card__title">משלוח חינם מאמזון לישראל: המדריך המלא</h3>
            <div class="blog-index-card__footer"><span class="blog-index-card__read">קריאה: ~4 דקות ←</span></div>
          </div>
        </a>
        <a class="blog-index-card" href="mekhs-umaam-amazon-israel.html">
          <div class="blog-card-body" style="padding:20px;">
            <div class="blog-index-card__cat">💰 מסים</div>
            <h3 class="blog-index-card__title">מכס ומע"מ על קניות מאמזון לישראל</h3>
            <div class="blog-index-card__footer"><span class="blog-index-card__read">קריאה: ~5 דקות ←</span></div>
          </div>
        </a>
      </div>
    </section>
  </main>

  <footer>
    <div class="footer-topbar">
      <a class="brand" href="/">
        <picture>
          <source srcset="../logo-new.webp" type="image/webp">
          <img src="../logo-new.png" alt="לוגו AMZ Free Ship Alert" class="brand-logo-img" width="36" height="36" loading="lazy" />
        </picture>
        <span>AMZ Free Ship Alert</span>
      </a>
      <div class="footer-links-wrap">
        <div class="footer-links">
          <a href="../privacy.html">מדיניות פרטיות</a>
          <a href="../terms.html">תנאי שימוש</a>
          <a href="../about.html">אודות</a>
          <a href="../web-guide.html">מדריך מקוון</a>
          <a href="/#disclosure">גילוי נאות</a>
        </div>
        <div class="footer-social">
          <a href="https://www.facebook.com/AmzFreeIL/" target="_blank" rel="noopener noreferrer">פייסבוק</a>
          <a href="https://www.instagram.com/amzfreeil/" target="_blank" rel="noopener noreferrer">אינסטגרם</a>
          <a href="https://t.me/amzfreeil" target="_blank" rel="noopener noreferrer">טלגרם</a>
        </div>
      </div>
    </div>
    <p class="footer-copy">© 2026 AMZ Free Ship Alert · אין לאתר זה כל זיקה, שותפות או שיוך ל-Amazon Inc</p>
  </footer>

  <script src="../script.js"></script>
</body>
</html>"""


async def publish_draft(slug: str) -> dict:
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    path = f"blog/{slug}.html"
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        sha = data["sha"]
        current = base64.b64decode(data["content"]).decode("utf-8")

    updated = current.replace(
        '<meta name="robots" content="noindex,nofollow" />\n', ""
    ).replace(
        '<meta name="robots" content="noindex,nofollow" />', ""
    )

    return await commit_to_github(path, updated, f"blog: publish {slug}", sha=sha)


async def add_to_prices_page(
    asin: str,
    slug: str,
    title_short: str,
    israel_price: float | None,
    amazon_price: float,
    image_url: str,
) -> dict:
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    path = "prices.html"
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        sha = data["sha"]
        current = base64.b64decode(data["content"]).decode("utf-8")

    partner_tag = os.getenv("AMAZON_AFFILIATE_TAG") or os.getenv("AMAZON_PARTNER_TAG", "amzfreeil-20")
    today_display = date.today().strftime("%d/%m/%Y")
    aff_url = f"https://www.amazon.com/dp/{asin}?tag={partner_tag}"
    # הצג מחיר שלם בלי ".0" (91 במקום 91.0)
    if amazon_price is not None and float(amazon_price).is_integer():
        amazon_price = int(amazon_price)
    if israel_price is not None and float(israel_price).is_integer():
        israel_price = int(israel_price)

    if israel_price is not None:
        savings = round(israel_price - amazon_price)
        price_rows = f"""
              <tr><td>בישראל (הזול ביותר)</td><td>₪{israel_price}</td></tr>
              <tr class="amazon-row"><td>אמזון</td><td>₪{amazon_price}</td></tr>
              <tr class="saving"><td>חיסכון</td><td>~₪{savings}</td></tr>"""
    else:
        price_rows = f"""
              <tr class="amazon-row"><td>אמזון</td><td>₪{amazon_price}</td></tr>
              <tr><td>זמינות בישראל</td><td>לא נמכר בישראל</td></tr>"""

    card = f"""
      <!-- {title_short} -->
      <div class="price-card">
        <div class="price-card-img">
          <a href="blog/{slug}.html">
            <img src="{image_url}" alt="{escape(strip_tags(title_short), quote=True)}" width="110" height="110" loading="lazy" />
          </a>
        </div>
        <div class="price-card-body">
          <h2 class="price-card-title">{title_short}</h2>
          <table class="price-table">
            <thead><tr><th>מקור</th><th>מחיר</th></tr></thead>
            <tbody>{price_rows}
            </tbody>
          </table>
          <span class="price-date">* נכון ל-{today_display}</span>
          <div class="price-card-footer">
            <a href="{aff_url}" target="_blank" rel="noopener sponsored" class="btn-amazon">קנה באמזון ←</a>
            <a href="blog/{slug}.html" class="btn-review">קרא ביקורת מלאה →</a>
          </div>
        </div>
      </div>
"""

    marker = '<div class="prices-grid">\n'
    if marker not in current:
        raise ValueError("prices.html: insertion marker not found")

    # A card for this slug may already exist — either this ASIN is being
    # refreshed, or a *different* ASIN was given the same slug by Claude and
    # overwrote its review file. Either way a second card would point at the
    # same review, so drop the old one and let the fresh card replace it.
    current, dropped = strip_price_cards(current, slug)
    if dropped:
        logger.info("prices: replacing %d existing card(s) for slug %s (asin %s)", dropped, slug, asin)

    # Insert newest card at the top of the grid (newest → oldest)
    updated = current.replace(marker, marker + card, 1)
    return await commit_to_github(path, updated, f"prices: add {title_short}", sha=sha)


async def get_github_file(path: str) -> tuple[str, str]:
    """Fetch a file from the repo. Returns (decoded_content, sha)."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]


async def commit_to_github(path: str, content: str, message: str, sha: str | None = None) -> dict:
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    url = f"https://api.github.com/repos/{repo}/contents/{path}"

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    encoded = base64.b64encode(content.encode("utf-8")).decode()

    async with httpx.AsyncClient(timeout=30) as client:
        if sha is None:
            r = await client.get(url, headers=headers)
            sha = r.json().get("sha") if r.is_success else None

        payload = {"message": message, "content": encoded}
        if sha:
            payload["sha"] = sha

        r = await client.put(url, json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


async def delete_github_file(path: str, message: str) -> bool:
    """Delete a file from the repo. Returns True if deleted, False if it didn't exist."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(url, headers=headers)
        if r.status_code == 404:
            return False
        r.raise_for_status()
        sha = r.json()["sha"]

        req = client.build_request(
            "DELETE", url, headers=headers, json={"message": message, "sha": sha}
        )
        r = await client.send(req)
        r.raise_for_status()
        return True


def strip_price_cards(html: str, slug: str) -> tuple[str, int]:
    """Drop every prices.html card that links to blog/{slug}.html.

    Each card is wrapped as:  \n      <!-- title -->\n      <div class="price-card">...\n      </div>\n
    It contains anchors href="blog/{slug}.html". Match the whole card block that
    references this slug, anchored on the price-card open/close at 6-space indent.
    Returns (updated_html, cards_removed).
    """
    pattern = re.compile(
        r"\n[ ]{6}<!--[^\n]*-->\n[ ]{6}<div class=\"price-card\">"
        r"(?:(?!<div class=\"price-card\">).)*?"
        r"href=\"blog/" + re.escape(slug) + r"\.html\""
        r".*?\n[ ]{6}</div>\n",
        re.DOTALL,
    )
    return pattern.subn("\n", html)


async def remove_from_prices_page(slug: str) -> bool:
    """Remove a product card from prices.html by its slug. Idempotent — returns
    True if a card was removed, False if none matched."""
    path = "prices.html"
    try:
        current, sha = await get_github_file(path)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return False
        raise

    updated, n = strip_price_cards(current, slug)
    if n == 0:
        return False

    await commit_to_github(path, updated, f"prices: remove {slug}", sha=sha)
    return True
