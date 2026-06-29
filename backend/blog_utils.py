"""
Blog draft generation utilities.
Amazon API → Claude → build HTML → commit to GitHub.
"""
import json
import logging
import re
import base64
import os
from datetime import date

import httpx
import anthropic

logger = logging.getLogger(__name__)


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
                ],
            },
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "x-marketplace": marketplace,
            },
        )
        product_resp.raise_for_status()
        data = product_resp.json()

    logger.info("fetch_amazon_product raw response keys: %s", list(data.keys()))
    items = data.get("itemsResult", {}).get("items", [])
    if not items:
        logger.error("fetch_amazon_product: no items for ASIN %s — response: %s", asin, json.dumps(data)[:500])
        raise ValueError(f"ASIN {asin} not found in Amazon Creators API response")
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
    aff_url = f"https://www.amazon.com/dp/{asin}?tag={partner_tag}"

    return {
        "asin": asin,
        "title": title,
        "features": features,
        "image": image_url,
        "model": model,
        "url": aff_url,
    }


async def generate_with_claude(product: dict, israel_price: float | None, amazon_price: float) -> dict:
    today_display = date.today().strftime("%d/%m/%Y")
    features_text = "\n".join(f"- {f}" for f in product.get("features", []))

    if israel_price is not None:
        savings = round(israel_price - amazon_price)
        price_context = f"""מחיר בישראל: ₪{israel_price}
מחיר באמזון (כולל מע"מ ייבוא + משלוח חינם): ₪{amazon_price}
חיסכון: ~₪{savings}"""
        angle = "השוואת מחירים — המוצר נמכר בישראל אך זול יותר באמזון"
    else:
        price_context = f"""מחיר באמזון (כולל מע"מ ייבוא + משלוח חינם): ₪{amazon_price}
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

החזר JSON בדיוק בפורמט הבא (ללא markdown, ללא טקסט לפני/אחרי):
{{
  "slug": "שם-קובץ-קצר-באנגלית-amazon-israel",
  "title_he": "כותרת מלאה בעברית לפוסט — {'כדאי לקנות מאמזון לישראל?' if israel_price is not None else 'המוצר שלא תמצאו בישראל — רק באמזון'} (2026)",
  "title_short": "שם קצר של המוצר (עברית+אנגלית)",
  "description_he": "תיאור SEO בעברית, עד 155 תווים",
  "eyebrow": "אייקון + קטגוריה (למשל: 💻 ביקורת מוצר)",
  "reading_time": "כ-5 דקות",
  "section1_p1": "<p>פסקה ראשונה — מה המוצר ולמה פופולרי (HTML, <bdi> לאנגלית)</p>",
  "section1_p2": "<p>פסקה שנייה — ייחודיות המוצר</p>",
  "specs_rows": [
    {{"label": "מפרט", "value": "ערך"}}
  ],
  "who_profile1_title": "🎮 כותרת פרופיל 1",
  "who_profile1_text": "<p>טקסט פרופיל 1 (HTML)</p>",
  "who_profile2_title": "🖥️ כותרת פרופיל 2",
  "who_profile2_text": "<p>טקסט פרופיל 2</p>",
  "who_profile3_title": "💼 כותרת פרופיל 3",
  "who_profile3_text": "<p>טקסט פרופיל 3</p>",
  "tip_html": "<p class=\\"blog-tip\\">💡 <strong>טיפ:</strong> טקסט טיפ שימושי</p>",
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
- עטוף מונחים אנגליים ב-<bdi></bdi>
- אל תמציא מחירים — רק המספרים שקיבלת
- אל תמציא מפרטים — רק מה שמופיע ב"מאפיינים"
- slug: קצר, אנגלית, מקפים, מסתיים ב-amazon-israel
- FAQs: בדיוק 4 שאלות
- specs_rows: חלץ מהמאפיינים (4-8 שורות)
- {"אם המוצר לא נמכר בישראל: הדגש את הבלעדיות והעובדה שזו הדרך היחידה להשיגו. אל תמציא מחיר ישראלי." if israel_price is None else "הדגש את החיסכון הכספי ביחס לקנייה בישראל."}"""

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    message = await client.messages.create(
        model="claude-opus-4-8",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return json.loads(raw)


def build_post_html(product: dict, content: dict, israel_price: float | None, amazon_price: float) -> str:
    partner_tag = os.getenv("AMAZON_AFFILIATE_TAG") or os.getenv("AMAZON_PARTNER_TAG", "amzfreeil-20")
    today = date.today()
    today_he = f"{today.day} ב{MONTHS_HE[today.month - 1]} {today.year}"
    today_iso = today.isoformat()
    today_display = today.strftime("%d/%m/%Y")
    savings = round(israel_price - amazon_price) if israel_price is not None else None
    asin = product["asin"]
    image = product.get("image", "")
    aff_url = f"https://www.amazon.com/dp/{asin}?tag={partner_tag}"
    slug = content["slug"]
    blog_url = f"https://www.amzfreeil.com/blog/{slug}.html"

    specs_rows_html = ""
    for i, row in enumerate(content.get("specs_rows", [])):
        bg = "background:rgba(0,0,0,.02);" if i % 2 == 1 else ""
        specs_rows_html += (
            f'            <tr style="{bg}border-bottom:1px solid rgba(23,32,51,.07);">\n'
            f'              <td style="padding:10px 14px;">{row["label"]}</td>\n'
            f'              <td style="padding:10px 14px;"><bdi>{row["value"]}</bdi></td>\n'
            f"            </tr>\n"
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
            "brand": {"@type": "Brand", "name": product["title"].split()[0]},
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
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "דף הבית", "item": "https://www.amzfreeil.com/"},
                {"@type": "ListItem", "position": 2, "name": "בלוג", "item": "https://www.amzfreeil.com/blog/"},
                {"@type": "ListItem", "position": 3, "name": content["breadcrumb_label"], "item": blog_url},
            ],
        },
    ], ensure_ascii=False, indent=2)

    return f"""<!doctype html>
<html lang="he" dir="rtl">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{content['title_he']} | amzfreeil</title>
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
        <a class="brand" href="../index.html">
          <picture>
            <source srcset="../logo-new.webp" type="image/webp">
            <img src="../logo-new.png" alt="AMZ Free Ship Alert — לוגו" class="brand-logo-img" width="36" height="36" />
          </picture>
          <span>AMZ Free Ship Alert</span>
        </a>
        <nav id="main-nav">
          <a href="../index.html#features">יכולות</a>
          <a href="../index.html#how">איך זה עובד</a>
          <a href="../web-guide.html">מדריך מקוון</a>
          <a href="../index.html#faq">שאלות נפוצות</a>
          <span class="nav-break"></span>
          <a href="../free-products.html">מוצרים בחינם 🚚</a>
          <a href="../search.html">חיפוש מוצרים 🔍</a>
          <a href="../about.html">אודות</a>
          <a href="../blog/" class="nav-active">בלוג</a>
          <a href="../prices.html">השוואת מחירים</a>
          <a href="../index.html#contact">צרו קשר</a>
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
        <nav class="blog-breadcrumb" aria-label="ניווט קווי">
          <a href="../index.html">דף הבית</a>
          <span aria-hidden="true">›</span>
          <a href="../blog/">בלוג</a>
          <span aria-hidden="true">›</span>
          <span>{content['breadcrumb_label']}</span>
        </nav>
        <p class="eyebrow">{content['eyebrow']}</p>
        <h1>{content['title_he']}</h1>
        <div class="blog-meta">
          <span>{today_he}</span>
          <span class="blog-meta-sep">·</span>
          <span>זמן קריאה: {content['reading_time']}</span>
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
      style="object-fit:contain;background:#f5f5f5;width:auto;max-width:min(500px,100%);aspect-ratio:unset;"
    />

    <article class="blog-body">

      <div class="blog-takeaway">
        <p class="blog-takeaway__title">✅ בקצרה — מה חשוב לדעת</p>
        <ul>
          {"<li>נכון ל-" + today_display + ": בישראל ₪" + str(israel_price) + " | באמזון (כולל מע\"מ + משלוח חינם) ₪" + str(amazon_price) + " — חיסכון של ~₪" + str(savings) + "</li>" if israel_price is not None else "<li>המוצר <strong>אינו נמכר בישראל</strong> — ניתן להשיגו רק דרך אמזון</li><li>מחיר באמזון (כולל מע\"מ + משלוח חינם): ₪" + str(amazon_price) + " נכון ל-" + today_display + "</li>"}
          <li>המשלוח החינם <strong>זמני ומשתנה</strong> — בדקו לפני רכישה</li>
        </ul>
      </div>

      <section>
        <h2>מה זה {content['title_short']} ולמה כולם מדברים עליו?</h2>
        {content['section1_p1']}
        {content['section1_p2']}
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

      <section>
        <h2>למי זה מתאים?</h2>
        <h3>{content['who_profile1_title']}</h3>
        {content['who_profile1_text']}
        <h3>{content['who_profile2_title']}</h3>
        {content['who_profile2_text']}
        <h3>{content['who_profile3_title']}</h3>
        {content['who_profile3_text']}
        {content['tip_html']}
      </section>

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
              <td style="padding:10px 14px;">אמזון <small style="color:#4d5a70;">(כולל מע"מ ייבוא + משלוח חינם)</small></td>
              <td style="padding:10px 14px;font-weight:700;color:#167d46;">₪{amazon_price}</td>
            </tr>
            {"" if savings is None else f'<tr style="border-bottom:1px solid rgba(23,32,51,.07);"><td style="padding:10px 14px;">חיסכון</td><td style="padding:10px 14px;font-weight:700;color:#167d46;">~₪' + str(savings) + '</td></tr>'}
          </tbody>
        </table>
        {"<p style=\"font-size:.8rem;color:#4d5a70;margin:0 0 16px;\">* נכון ל-" + today_display + ". המחירים משתנים — בדקו לפני רכישה.</p>" if israel_price is not None else "<p style=\"font-size:.8rem;color:#4d5a70;margin:0 0 16px;\">* מחיר באמזון נכון ל-" + today_display + ". עשוי להשתנות — בדקו לפני רכישה.</p>"}

        <div style="background:rgba(255,153,0,.08);border:1.5px solid rgba(255,153,0,.4);border-radius:12px;padding:14px 18px;margin:16px 0;font-size:.9rem;line-height:1.7;">
          <strong>⚠️ חשוב: המחיר באמזון כולל כבר את מע"מ הייבוא</strong><br>
          אמזון מציג מחיר סופי לישראל כולל <bdi>"Import Fees Deposit"</bdi> — אין הפתעות במכס. <a href="../mekhs-umaam-amazon-israel.html" style="color:var(--brand-deep, #ff6a00);">מדריך מלא על מכס ומע"מ ←</a>
        </div>

        <div style="text-align:center;margin:24px 0;">
          <a href="{aff_url}" target="_blank" rel="noopener sponsored"
             style="display:inline-block;background:linear-gradient(135deg,#ff9900,#ff6a00);color:#172033;font-weight:800;padding:14px 32px;border-radius:14px;text-decoration:none;font-size:1.05rem;box-shadow:0 8px 24px rgba(255,153,0,.3);">
            בדוק מחיר נוכחי באמזון ←
          </a>
          <p style="font-size:.78rem;color:#4d5a70;margin:10px 0 0;">קישור שותף — לא עולה לכם יותר</p>
        </div>
      </section>

      <section>
        <h2>רוצה לדעת כשיש משלוח חינם על המוצר הזה?</h2>
        <p>המשלוח החינם של אמזון לישראל מופיע ונעלם — לפעמים ליום, לפעמים לשבוע. במקום לבדוק ידנית כל יום, תנו לנו לעשות את זה בשבילכם.</p>
        <div style="background:rgba(22,125,70,.07);border:1.5px solid rgba(22,125,70,.3);border-radius:16px;padding:24px 26px;margin:20px 0;">
          <p style="margin:0 0 6px;font-weight:700;font-size:1.05rem;">📬 קבל התראה ברגע שמשלוח חינם זמין</p>
          <p style="margin:0 0 18px;font-size:.9rem;color:#4d5a70;">תוסיף את המוצר לניטור — נשלח לך מייל ברגע שהמשלוח מתעדכן לחינם.</p>
          <a href="https://app.amzfreeil.com" target="_blank" rel="noopener"
             style="display:inline-block;background:linear-gradient(135deg,#ff9900,#ff6a00);color:#172033;font-weight:800;padding:13px 28px;border-radius:12px;text-decoration:none;font-size:1rem;">
            הירשם לקבל התראה ←
          </a>
        </div>
      </section>

      <section>
        <h2>איך לקנות — מדריך קצר</h2>
        <h3>שלב 1: ודאו שהמוצר מתאים לכם</h3>
        <p>קראו את המפרט למעלה וודאו שהמוצר תואם לציוד שלכם.</p>
        <h3>שלב 2: ודאו שיש חשבון אמזון</h3>
        <p>אם אין — פתחו אחד. אין עלות. כתובת המשלוח תהיה הכתובת שלכם בישראל.</p>
        <h3>שלב 3: בדקו שהמוצר מציג <bdi>"FREE Shipping to Israel"</bdi></h3>
        <p>בדף המוצר, תחת סעיף <bdi>"Delivery"</bdi>, חפשו את הכיתוב הזה. אם הוא מופיע — אתם מוכנים לרכישה.</p>
        <h3>שלב 4: קנו</h3>
        <p>לחצו על הכפתור למטה. תוודאו שהמוכר הוא <bdi>Amazon.com</bdi> או <bdi>Sold by Amazon</bdi> — לא מוכר צד שלישי.</p>
        <div style="text-align:center;margin:28px 0;">
          <a href="{aff_url}" target="_blank" rel="noopener sponsored"
             style="display:inline-block;background:linear-gradient(135deg,#ff9900,#ff6a00);color:#172033;font-weight:800;padding:14px 32px;border-radius:14px;text-decoration:none;font-size:1.05rem;box-shadow:0 8px 24px rgba(255,153,0,.35);">
            צפה במוצר באמזון ←
          </a>
          <p style="font-size:.78rem;color:#4d5a70;margin:10px 0 0;">קישור שותף — לא עולה לכם יותר</p>
        </div>
      </section>

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
        <strong>גילוי נאות:</strong> הקישורים לאמזון בעמוד זה הם קישורי שותף של תוכנית <bdi>Amazon Associates</bdi>. אם תרכשו מוצר דרכם, אנו עשויים לקבל עמלה קטנה — ללא כל עלות נוספת מצידכם. זהו המודל שמאפשר לנו לספק את השירות ללא תשלום.
      </div>

      <div class="author-bio">
        <div class="author-bio__avatar">א</div>
        <div class="author-bio__info">
          <p class="author-bio__name">אילן</p>
          <p class="author-bio__desc">מפתח Python עצמאי עם 5+ שנות ניסיון. יצר את <strong>AMZ Free Ship Alert</strong> כדי לעזור לישראלים לחסוך בקניות מאמזון — בלי לפספס הזדמנויות משלוח חינם. <a href="../about.html">קרא עוד אודותי →</a></p>
        </div>
      </div>
    </article>

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
      <a class="brand" href="../index.html">
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
          <a href="../index.html#disclosure">גילוי נאות</a>
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
    israel_price: float,
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
    savings = round(israel_price - amazon_price)
    aff_url = f"https://www.amazon.com/dp/{asin}?tag={partner_tag}"

    card = f"""
      <!-- {title_short} -->
      <div class="price-card">
        <div class="price-card-img">
          <a href="{aff_url}" target="_blank" rel="noopener sponsored">
            <img src="{image_url}" alt="{title_short}" width="110" height="110" loading="lazy" />
          </a>
        </div>
        <div class="price-card-body">
          <h2 class="price-card-title">{title_short}</h2>
          <table class="price-table">
            <thead><tr><th>מקור</th><th>מחיר</th></tr></thead>
            <tbody>
              <tr><td>בישראל (הזול ביותר)</td><td>₪{israel_price}</td></tr>
              <tr class="amazon-row"><td>אמזון <small style="font-weight:400;color:#4d5a70;">(כולל מע"מ + משלוח חינם)</small></td><td>₪{amazon_price}</td></tr>
              <tr class="saving"><td>חיסכון</td><td>~₪{savings}</td></tr>
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

    marker = "\n    </div>\n\n    <!-- Alert CTA -->"
    if marker not in current:
        raise ValueError("prices.html: insertion marker not found")

    updated = current.replace(marker, card + "\n    </div>\n\n    <!-- Alert CTA -->")
    return await commit_to_github(path, updated, f"prices: add {title_short}", sha=sha)


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
