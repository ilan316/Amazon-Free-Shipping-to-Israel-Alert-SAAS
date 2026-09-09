// Dashboard page logic

let products = [];
let checkingAsins = new Set();
let currentFilter = 'ALL';
let userLimit = null;
let userId = null;
let catalogItems = [];   // full free-shipping catalog, fetched once per page load

// Builds the "final cost to Israel" line under the price.
// The point of the percentage: shipping to Israel is close to flat (~46-52₪ in our data),
// so on a cheap product it dominates the price and on an expensive one it's noise.
// Without the percentage the user only sees a binary "no free shipping" and waits forever.
// Returns '' when we have no extracted figure — the caller then keeps the old wording.
function israelCostLine(p) {
  if (!p.last_price) return '';
  const price = parseFloat(String(p.last_price).replace(/[^\d.]/g, ''));
  const extra = parseFloat(String(p.israel_extra_cost || '').replace(/[^\d.]/g, ''));
  if (!isFinite(price) || price <= 0) return '';

  // Conditional free delivery: below ~$49 Amazon's "FREE delivery to Israel" holds only
  // above an order minimum. Both phrasings classify as FREE, so without this the user is
  // promised free shipping and finds a fee at checkout. The gap to the minimum is the
  // number worth showing — it's how much more to put in the cart, not a figure to compute.
  // Independent of israel_cost_kind: it comes from the delivery text, not the global block.
  const threshold = parseFloat(String(p.israel_free_threshold || '').replace(/[^\d.]/g, ''));
  if (p.last_status === 'FREE' && isFinite(threshold) && threshold > price) {
    const gap = (threshold - price).toFixed(2);
    // Four amounts in one run read as noise, and the one thing to *do* — put more in the
    // cart — sat buried in the middle. So: the action alone on the first line with a single
    // number, and the figures that merely explain it (the minimum, which is anyway just
    // price + gap, and the solo shipping fee) demoted to a quiet second line.
    // The single-purchase fee only appears alongside the minimum that explains it.
    const alone = isFinite(extra) && extra > 0
      ? ` · לקנייה בודדת משלוח ₪${extra.toFixed(2)}` : '';
    return `<span style="color:#007600;font-size:13px;margin-right:4px;"><b>הוסף עוד ₪${gap} לעגלה</b> — והמשלוח חינם</span>` +
           `<span style="display:block;color:#888;font-size:11px;margin-top:2px;">מינימום להזמנה ₪${threshold.toFixed(2)}${alone}</span>`;
  }

  if (!p.israel_cost_kind) return '';

  if (p.israel_cost_kind === 'free') {
    return '<span style="color:#007600;font-size:13px;margin-right:4px;">· משלוח חינם, ללא עלויות נוספות</span>';
  }
  if (!isFinite(extra) || extra <= 0) return '';

  const total = (price + extra).toFixed(2);
  const pct = Math.round((extra / price) * 100);

  if (p.israel_cost_kind === 'import_only') {
    // "משלוח חינם" leads — it's the good news, and opening with "+ ₪149.50" read as a
    // charge before the reader reached the word that explains it isn't shipping.
    return `<span style="color:#555;font-size:13px;margin-right:4px;">משלוח חינם + ₪${extra.toFixed(2)} מכס · <b>סה"כ ₪${total}</b></span>`;
  }
  // A FREE product can still quote a shipping fee here: its free delivery is conditional
  // on an order minimum, and the fee is what you'd pay buying it alone. Printing it next
  // to the "free shipping" badge reads as a contradiction, so it's left out.
  if (p.last_status === 'FREE') return '';

  // Anything else includes a shipping fee, so the percentage is shown alongside it.
  // 'combined' is Amazon's merged figure with no split available — the label must not
  // claim it's shipping alone.
  const label = p.israel_cost_kind === 'shipping_only' ? 'משלוח' : 'משלוח ומכס';
  // Spelled out rather than "(57% מהמחיר)": the bare percentage next to two other numbers
  // reads as ambiguous — percentage of what — and generated support questions.
  const pctSubject = p.israel_cost_kind === 'shipping_only' ? 'עלות המשלוח' : 'עלות המשלוח והמכס';
  return `<span style="color:#555;font-size:13px;margin-right:4px;">+ ₪${extra.toFixed(2)} ${label} · <b>סה"כ ₪${total}</b> <span style="color:#888;">(${pctSubject} היא ${pct}% ממחיר המוצר)</span></span>`;
}

// Week-over-week movement, the same comparison the weekly summary email prints.
// The server sends only real movements (backend/notifier.py price_moves), so an empty
// list means either nothing moved or there's no week-old row yet — in both cases the
// card stays quiet rather than adding a "no change" line to an already dense row.
const TREND_TEXT = {
  price_down: d => `▼ מחיר המוצר ירד ב-₪${d} מאז השבוע שעבר`,
  price_up:   d => `▲ מחיר המוצר עלה ב-₪${d} מאז השבוע שעבר`,
  ship_down:  d => `▼ עלות המשלוח ירדה ב-₪${d} מאז השבוע שעבר`,
  ship_up:    d => `▲ עלות המשלוח עלתה ב-₪${d} מאז השבוע שעבר`,
};

function priceTrendLine(p) {
  if (!Array.isArray(p.trend) || !p.trend.length) return '';
  return p.trend.map(m => {
    const render = TREND_TEXT[m.kind];
    if (!render) return '';
    // Same colours as the email: a drop is good news, a rise is not.
    const color = m.kind.endsWith('_down') ? '#007600' : '#B12704';
    return `<span style="color:${color};font-size:12px;font-weight:bold;margin-right:4px;">${escHtml(render(m.delta))}</span>`;
  }).join('');
}

function nextCheckLabel(p) {
  if (!p.status_since || !['PAID', 'NO_SHIP'].includes(p.last_status)) return null;
  const cycle = p.last_status === 'PAID' ? 14 : 21;
  const daysSince = Math.floor((Date.now() - new Date(p.status_since)) / 86400000);
  const dayInCycle = daysSince % cycle;
  if (dayInCycle < 7) return null;
  const daysUntil = cycle - dayInCycle;
  const next = new Date();
  next.setDate(next.getDate() + daysUntil);
  return `הבדיקה הבאה: ${next.toLocaleDateString('he-IL', { day: 'numeric', month: 'numeric' })}`;
}

const STATUS_TOOLTIP = {
  FREE:      "אמזון שולח מוצר זה לישראל ללא עלות משלוח — ניתן להזמין ישירות",
  PAID:      "אמזון שולח לישראל, אך המשלוח כרוך בתשלום נוסף. כדאי לבדוק את עלות המשלוח לפני הרכישה",
  NO_SHIP:   "אמזון לא שולח מוצר זה ישירות לישראל. ניתן להיעזר בחברת שליחויות אשר מספקת כתובת אמריקאית",
  NOT_FOUND: "המוצר לא נמצא באמזון — ייתכן שהוסר, הועתק ל-ASIN אחר, או ה-ASIN שגוי",
  UNKNOWN:   "לא ניתן לקבוע סטטוס — הבדיקה הסתיימה ללא תוצאה ברורה",
  ERROR:     "שגיאה בבדיקה האחרונה (קפצ'ה או תקלה זמנית) — תנסה שוב בקרוב",
};

// ── Limit badge ───────────────────────────────────────────────────────────────

async function loadUserLimit() {
  const res = await apiFetch("/me");
  if (!res || !res.ok) return;
  const user = await res.json();
  userId = user.id ?? null;   // seeds the daily catalog draw
  if (user.effective_product_limit != null) {
    userLimit = user.effective_product_limit;
    updateLimitBadge();
  }
  // This resolves in parallel with the first product load, so the strip may already
  // have drawn with a null seed and without knowing the limit. Redraw once we know.
  renderCatalogStrip();
}

function updateLimitBadge() {
  const badge = document.getElementById("limit-badge");
  if (!badge || userLimit === null) return;
  const count = products.length;
  const pct = userLimit > 0 ? count / userLimit : 1;
  badge.textContent = `${count} / ${userLimit}`;
  badge.className = pct >= 0.9 ? 'badge-full' : pct >= 0.7 ? 'badge-warn' : 'badge-ok';
  badge.style.display = '';
}

// ── Next check time ───────────────────────────────────────────────────────────

function updateNextCheckDisplay(nextCheckAt) {
  const el = document.getElementById("next-check-display");
  const el2 = document.getElementById("add-card-next-check");
  const diff = new Date(nextCheckAt) - new Date();
  if (diff <= 0) {
    if (el) el.textContent = "בדיקה בקרוב...";
    if (el2) el2.textContent = "בדיקה בקרוב";
    return;
  }
  const mins = Math.round(diff / 60000);
  const timeStr = new Date(nextCheckAt).toLocaleTimeString("he-IL", { hour: "2-digit", minute: "2-digit" });
  if (el) el.textContent = `בדיקה הבאה בעוד ${mins} דקות (${timeStr})`;
  if (el2) el2.textContent = `בדיקה הבאה בשעה ${timeStr}`;
}

// ── Load / Render ─────────────────────────────────────────────────────────────

async function loadProducts(silent = false) {
  const list = document.getElementById("products-list");
  if (!silent) {
    list.innerHTML = ['','',''].map(() => `<div class="skeleton" style="margin-bottom:10px;"></div>`).join('');
  }

  const res = await apiFetch("/me/products");
  if (!res) return;

  if (!res.ok) {
    list.innerHTML = '<p style="color:var(--error); text-align:center;">שגיאה בטעינת המוצרים</p>';
    return;
  }

  products = await res.json();
  renderProducts();
}

function renderProducts() {
  const list = document.getElementById("products-list");
  const searchVal = (document.getElementById("search-input")?.value || "").trim().toLowerCase();

  // Show/hide filter bar
  const filterBar = document.getElementById("filter-bar");
  if (filterBar) filterBar.style.display = products.length > 0 ? "flex" : "none";

  // Counts for filter buttons
  const counts = { FREE: 0, PAID: 0, NO_SHIP: 0, NOT_FOUND: 0 };
  products.forEach(p => {
    if (p.is_paused) return;
    if (p.last_status === 'FREE')      counts.FREE++;
    if (p.last_status === 'PAID')      counts.PAID++;
    if (p.last_status === 'NO_SHIP')   counts.NO_SHIP++;
    if (p.last_status === 'NOT_FOUND') counts.NOT_FOUND++;
  });
  // Update filter button labels
  const lblMap = {
    FREE:      `✅ משלוח חינם${counts.FREE > 0 ? ` (${counts.FREE})` : ''}`,
    PAID:      `💳 משלוח בתשלום${counts.PAID > 0 ? ` (${counts.PAID})` : ''}`,
    NO_SHIP:   `🚫 לא נשלח לארץ${counts.NO_SHIP > 0 ? ` (${counts.NO_SHIP})` : ''}`,
    NOT_FOUND: `❌ מוצר לא קיים${counts.NOT_FOUND > 0 ? ` (${counts.NOT_FOUND})` : ''}`,
  };
  document.querySelectorAll('.filter-btn[onclick]').forEach(btn => {
    const m = btn.getAttribute('onclick').match(/setFilter\('(\w+)'/);
    if (m && lblMap[m[1]]) btn.textContent = lblMap[m[1]];
  });

  // Update counter
  const counterEl = document.getElementById("products-counter");
  const csvBtn = document.getElementById("csv-btn");
  if (counterEl) {
    const total = products.length;
    if (total > 0) {
      const parts = [`${total} מוצרים במעקב`];
      if (counts.FREE > 0)      parts.push(`${counts.FREE} חינם`);
      if (counts.PAID > 0)      parts.push(`${counts.PAID} בתשלום`);
      if (counts.NO_SHIP > 0)   parts.push(`${counts.NO_SHIP} לא נשלח`);
      if (counts.NOT_FOUND > 0) parts.push(`${counts.NOT_FOUND} לא קיים`);
      counterEl.textContent = parts.join(' · ');
      if (csvBtn) csvBtn.style.display = '';
    } else {
      counterEl.textContent = '';
      if (csvBtn) csvBtn.style.display = 'none';
    }
  }

  updateLimitBadge();

  // The catalog strip lives in its own container above the list and is rendered on
  // every pass, empty list or not — it is what turns a free product into a tracked one.
  renderCatalogStrip();

  if (products.length === 0) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📦</div>
        <p>עדיין לא הוספת מוצרים למעקב</p>
        <p style="font-size:0.85rem; margin-top:8px;">הדבק URL של מוצר אמזון או ASIN בתיבה למעלה</p>
      </div>`;
    return;
  }

  // Filter by status
  let filtered = [...products];
  if (currentFilter !== 'ALL') {
    filtered = filtered.filter(p => !p.is_paused && p.last_status === currentFilter);
  }

  // Filter by search
  if (searchVal) {
    filtered = filtered.filter(p => {
      const name = (p.custom_name || p.name || "").toLowerCase();
      return name.includes(searchVal) || p.asin.toLowerCase().includes(searchVal);
    });
  }

  // Sort: FREE first, paused last
  const STATUS_ORDER = { FREE: 0, PAID: 1, NO_SHIP: 1, UNKNOWN: 2, ERROR: 3 };
  filtered.sort((a, b) => {
    if (a.is_paused !== b.is_paused) return a.is_paused ? 1 : -1;
    return (STATUS_ORDER[a.last_status] ?? 4) - (STATUS_ORDER[b.last_status] ?? 4);
  });

  if (filtered.length === 0) {
    list.innerHTML = `<div class="empty-state"><p>אין מוצרים התואמים לחיפוש</p></div>`;
    return;
  }

  list.innerHTML = filtered.map(p => {
    const displayName = p.custom_name || p.name || p.asin;
    const isChecking  = checkingAsins.has(p.asin);
    const checkedStr  = isChecking
      ? '<span style="color:var(--brand-dark)">⏳ בודק עכשיו...</span>'
      : (p.last_checked ? `בדיקה אחרונה: ${formatDate(p.last_checked)}` : "טרם נבדק");
    const nextCheck   = !isChecking ? nextCheckLabel(p) : null;
    const notifiedStr = p.last_notified ? `התראה: ${formatDate(p.last_notified)}` : "";
    const aodNote     = p.found_in_aod ? '<span title="נמצא בכל אפשרויות הקנייה">⚠️</span>' : "";
    const badgeStatus = p.is_paused ? 'UNKNOWN'
      : (isChecking || p.last_status === 'UNKNOWN' || p.last_status === 'ERROR') ? 'UNKNOWN'
      : p.last_status;
    const tooltip     = STATUS_TOOLTIP[p.last_status] || "";
    const linkUrl     = p.affiliate_url || p.url;


    const isAutoPaused = p.is_paused && p.paused_reason === 'auto';
    const pausedUntilDate = p.paused_until
      ? new Date(p.paused_until).toLocaleDateString('he-IL', { day: '2-digit', month: '2-digit' })
      : null;
    const pauseBtnLabel = p.is_paused
      ? (isAutoPaused ? '▶ חדש מעקב' : (pausedUntilDate ? `▶ מושהה עד ${pausedUntilDate}` : '▶ בטל השהייה'))
      : '⏸ השהה';
    const pauseBtnTitle = p.is_paused
      ? (isAutoPaused ? 'לחץ לחידוש המעקב' : (pausedUntilDate ? `לחץ לביטול השהייה (עד ${pausedUntilDate})` : 'השהייה ללא הגבלה — לחץ לביטול'))
      : 'השהה מעקב';
    const pauseBtn = `
      <button
        class="btn-pause ${p.is_paused ? 'is-paused' : ''}"
        onclick="${p.is_paused ? `togglePause('${p.asin}', this)` : `showPauseDialog('${p.asin}', this)`}"
        title="${pauseBtnTitle}">
        ${pauseBtnLabel}
      </button>`;

    const badgeHtml = p.is_paused
      ? (isAutoPaused
          ? '<span class="status-badge badge-auto-paused">⏸ הושהה אוטומטית <button class="btn-info" onclick="showInfoPopup(\'המוצר היה חינם 5 ימים ולא לחצת על הקישור — המערכת השהתה את המעקב אוטומטית. לחץ ▶ חדש מעקב כדי לחזור לעקוב.\')">?</button></span>'
          : '<span class="status-badge badge-paused" title="המוצר בהשהייה ידנית">⏸ מושהה</span>')
      : (isChecking || p.last_status === 'UNKNOWN' || p.last_status === 'ERROR')
        ? '<span class="status-badge badge-UNKNOWN">טרם נבדק</span>'
        : `<span class="status-badge badge-${p.last_status}" title="${tooltip}">${statusLabel(p.last_status)}</span>`
    ;

    return `
      <div class="product-card status-${badgeStatus} ${p.is_paused ? 'card-paused' : ''}" id="card-${p.asin}">

        <!-- שורה 1: ✏️ ערוך שם (ימין) | שם מוצר LTR (שמאל) -->
        <div class="card-row-name" style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:5px;overflow:hidden;">
          <div class="card-name-right" style="flex-shrink:0;display:flex;align-items:center;gap:4px;">
            <div id="name-${p.asin}" class="card-name-edit-wrap">
              <button class="btn-edit-name" onclick="editName('${p.asin}')">✏️ ערוך שם</button>
            </div>
            ${aodNote}
          </div>
          <a href="${linkUrl}" target="_blank" rel="noopener" class="card-product-link" style="flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;direction:ltr;text-align:left;">${escHtml(displayName)}</a>
          ${p.image ? `<img src="${escHtml(p.image)}"
               alt="" width="60" height="60" loading="lazy"
               style="flex-shrink:0;object-fit:contain;border-radius:6px;border:1px solid #eee;"
               onerror="this.style.display='none'">` : ''}
        </div>

        <!-- שורה 2: בדיקה אחרונה (ימין) | ASIN LTR (שמאל) -->
        <div class="card-row-meta">
          <span class="card-meta-checked">${checkedStr}${notifiedStr ? ' · ' + notifiedStr : ''}</span>
          ${nextCheck ? `<span class="card-meta-next-check" style="font-size:11px;color:#e67e00;display:block;margin-top:2px;">⏸ ${nextCheck}</span>` : ''}
          <span class="card-meta-asin" dir="ltr">ASIN: ${p.asin}</span>
        </div>
        ${p.last_price && !['NO_SHIP','NOT_FOUND'].includes(p.last_status) ? `<div class="card-row-price" style="margin-top:3px;font-size:12px;">
          <span style="color:#B12704;font-weight:bold;">💰 <bdi>${escHtml(formatPrice(p.last_price))}</bdi></span>
          ${israelCostLine(p) || `<span style="color:#999;font-size:12px;margin-right:4px;">${p.last_status === 'FREE' ? '(כולל משלוח חינם — לא כולל מכס ומע"מ במידה וחל)' : '(מחיר המוצר בלבד - לא כולל משלוח, מיסים ועלויות שונות)'}</span>`}
          ${priceTrendLine(p)}
        </div>` : ''}

        <!-- שורה 3: סטטוס | השהה | בדוק | הסר -->
        ${isAutoPaused ? `<div style="font-size:0.78rem;color:#e65100;margin-bottom:4px;">הושהה אוטומטית — לא לחצת על הקישור 5 ימים</div>` : ''}
        <div class="card-row-actions">
          ${badgeHtml}
          ${pauseBtn}
          <button class="btn-remove" onclick="removeProduct('${p.asin}')">הסר</button>
        </div>

      </div>`;
  }).join("");
}

// ── Info popup ────────────────────────────────────────────────────────────────

function showInfoPopup(text) {
  document.getElementById('info-popup')?.remove();
  const pop = document.createElement('div');
  pop.id = 'info-popup';
  pop.className = 'info-popup';
  pop.innerHTML = `<span>${text}</span><button onclick="document.getElementById('info-popup').remove()">✕</button>`;
  document.body.appendChild(pop);
  setTimeout(() => {
    document.addEventListener('click', function h(e) {
      if (!pop.contains(e.target)) { pop.remove(); document.removeEventListener('click', h); }
    });
  }, 50);
}

// ── FAQ accordion ─────────────────────────────────────────────────────────────

function toggleFaq(btn) {
  const body = btn.nextElementSibling;
  const arrow = btn.querySelector('.faq-arrow');
  const open = body.style.display !== 'none';
  body.style.display = open ? 'none' : 'block';
  if (arrow) arrow.textContent = open ? '▼' : '▲';
}

// ── CSV Export ────────────────────────────────────────────────────────────────

function exportUserCSV() {
  if (!products.length) return;
  const headers = ['ASIN', 'שם', 'שם מותאם', 'סטטוס', 'בדיקה אחרונה', 'מושהה', 'קישור'];
  const rows = products.map(p => [
    p.asin,
    p.name || '',
    p.custom_name || '',
    p.last_status || '',
    p.last_checked ? new Date(p.last_checked).toLocaleString('he-IL') : '',
    p.is_paused ? 'כן' : 'לא',
    p.url,
  ]);
  _downloadCSV('my_products.csv', headers, rows);
}

// ── Filter / Search ───────────────────────────────────────────────────────────

function setFilter(filter, btn) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderProducts();
}

// ── Inline name edit ──────────────────────────────────────────────────────────

function editName(asin) {
  const p = products.find(x => x.asin === asin);
  if (!p) return;
  // Replace the product link with an inline edit input
  const linkEl = document.querySelector(`#card-${asin} .card-product-link`);
  const wrapEl = document.getElementById(`name-${asin}`);
  if (!linkEl || !wrapEl) return;
  const current = p.custom_name || p.name || p.asin;
  // Hide the link, show input in the wrap
  linkEl.style.display = 'none';
  wrapEl.innerHTML = `
    <input class="name-edit-input" id="name-input-${asin}" value="${escHtml(current)}" dir="auto">
    <button class="btn-save-name" onclick="saveName('${asin}')">שמור</button>
    <button class="btn-cancel-name" onclick="renderProducts()">ביטול</button>
  `;
  const inp = document.getElementById(`name-input-${asin}`);
  if (inp) {
    inp.focus();
    inp.select();
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') saveName(asin);
      if (e.key === 'Escape') renderProducts();
    });
  }
}

async function saveName(asin) {
  const inp = document.getElementById(`name-input-${asin}`);
  if (!inp) return;
  const newName = inp.value.trim();
  const res = await apiFetch(`/me/products/${asin}/name`, {
    method: "PATCH",
    body: JSON.stringify({ custom_name: newName }),
  });
  if (res && res.ok) {
    products = products.map(p => p.asin === asin ? { ...p, custom_name: newName || null } : p);
  }
  renderProducts();
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ── Add product (single + bulk) ───────────────────────────────────────────────

// The catalog of products that ship free right now is the only inventory we have
// that a user can adopt in one click. It used to render only for an empty list and
// vanish for good after the first ASIN — which is why, with 277 free products in the
// catalog, not one had ever been adopted. It is now permanent.

// Deterministic draw: the same six products all day, a different six tomorrow.
// Re-randomising per page load would make the dashboard look broken; tying it to the
// scanner run would rotate on an unpredictable cadence.
function _catalogSeed() {
  const day = new Date().toISOString().slice(0, 10);
  const key = `${userId ?? 0}-${day}`;
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function _seededPick(items, count, seed) {
  // Fisher-Yates over a copy, driven by mulberry32 so the order is reproducible.
  const arr = [...items];
  let s = seed;
  const rand = () => {
    s |= 0; s = (s + 0x6D2B79F5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr.slice(0, count);
}

function _escAttr(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

// "נבדק לפני X" used to sit on every card. It was misleading: scanner products
// nobody tracks are only re-checked when the Telegram/Facebook poster happens to
// draw them (~21 a day out of 277), so most of the catalog reads days old while
// the list, sorted by last_checked, shows only the fresh ones first.

// last_price arrives pre-formatted from Amazon, e.g. "ILS 253.89". Mirrors
// formatPrice() in app.js so the tracked list and the catalog on the same
// screen render the shekel identically: '₪253.89', the sign glued to the left
// of the digits. With a space ('253.89 ₪') bidi puts the sign on the right of
// the number, which is what this replaced. A USD price passes through as-is —
// the $ is a signal that the check never got an Israeli price, not a price to
// prettify.
function _fmtPrice(s) {
  const m = String(s || "").match(/^\s*(?:ILS|₪)\s*([\d.,]+)\s*$/i);
  return m ? `₪${m[1]}` : String(s || "");
}

function _atProductLimit() {
  return userLimit !== null && products.length >= userLimit;
}

function _catalogCard(p, atLimit) {
  const name = p.name_he || p.name;
  const price = p.last_price ? `<div style="font-weight:700;font-size:0.9rem;margin-bottom:4px;">${_escAttr(_fmtPrice(p.last_price))}</div>` : "";
  const cat = p.category_he ? `<div style="font-size:0.7rem;color:var(--text-muted);">${_escAttr(p.category_he)}</div>` : "";
  const track = atLimit
    ? ""
    : `<button class="btn-outline" style="flex:1;padding:6px;font-size:0.78rem;white-space:nowrap;"
         onclick="addSuggested('${p.asin}', this)">➕ עקוב</button>`;
  return `
    <div style="border:1px solid var(--border);border-radius:10px;padding:10px;text-align:center;background:var(--surface);display:flex;flex-direction:column;">
      <img src="${_escAttr(p.image)}" alt="" loading="lazy" style="width:100%;height:110px;object-fit:contain;margin-bottom:8px;">
      <div style="font-size:0.78rem;line-height:1.35;min-height:4.05em;margin-bottom:6px;display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;line-clamp:3;overflow:hidden;">${_escAttr(name)}</div>
      ${price}
      <div style="font-size:0.72rem;color:var(--success);font-weight:600;margin-bottom:2px;">משלוח חינם ✓</div>
      ${cat}
      <div style="display:flex;gap:6px;margin-top:auto;padding-top:10px;">
        <a href="/go/dash/${p.asin}" target="_blank" rel="noopener"
           class="btn-outline" style="flex:1;padding:6px;font-size:0.78rem;text-decoration:none;white-space:nowrap;">🌐 לאמזון</a>
        ${track}
      </div>
    </div>`;
}

// The catalog sits below the user's own list, so nothing above the fold hints
// that it exists. This one line in the add-product card does — that card is
// where someone stands when they want to track something and have no ASIN in
// hand, which is exactly who the catalog is for.
function _renderCatalogHint(count) {
  const hint = document.getElementById("catalog-hint");
  if (!hint) return;
  if (!count) { hint.style.display = "none"; return; }
  hint.style.display = "";
  hint.innerHTML =
    `✨ <a href="#catalog-strip" onclick="_scrollToCatalog();return false;">` +
    `אין לך מוצר מוכן? ${count} מוצרים שנשלחים חינם לישראל מחכים למטה ↓</a>`;
}

function _scrollToCatalog() {
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  document.getElementById("catalog-strip")
    ?.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
}

async function renderCatalogStrip() {
  const box = document.getElementById("catalog-strip");
  if (!box) return;

  if (!catalogItems.length) {
    try {
      const res = await fetch("/api/public/free-products");
      if (!res.ok) return;
      catalogItems = await res.json();
    } catch { return; }
  }

  const tracked = new Set(products.map(p => p.asin));
  const available = catalogItems.filter(p => !tracked.has(p.asin));
  _renderCatalogHint(available.length);
  if (!available.length) { box.innerHTML = ""; return; }

  const atLimit = _atProductLimit();

  // The count has to divide evenly into the column count or the last row is a
  // stranded card. auto-fill decided the columns for us and landed on 5 inside
  // .page (max-width 860px), which left 6 items as 5+1 — so the columns are
  // pinned below instead, to 4 and 2, and the count is 8: two full rows on
  // desktop, four on mobile. Only the tail of the catalog can go under 8.
  let n = Math.min(8, available.length);
  if (n >= 4) n -= n % 4;
  const items = _seededPick(available, n, _catalogSeed());

  // Media queries can't live in a style attribute, and the CSS files are off
  // limits here (they're cached separately from this file's ?v= bust), so the
  // grid rules ship inline with the markup they style.
  box.innerHTML = `
    <style>
      .catalog-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:12px; }
      @media (min-width:640px) { .catalog-grid { grid-template-columns:repeat(4,1fr); } }
    </style>
    <div style="margin:16px 0;">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <p style="font-weight:700;margin-bottom:4px;">✨ נשלחים חינם לישראל ממש עכשיו</p>
        <a href="#" onclick="openCatalogModal();return false;" style="font-size:0.85rem;">לכל ${available.length} המוצרים ←</a>
      </div>
      <p style="font-size:0.85rem;color:var(--text-muted);margin-bottom:12px;">
        ${atLimit ? `הגעת למגבלת ${userLimit} המוצרים — פנה לתמיכה להגדלת המגבלה`
                  : "לחיצה אחת ונתחיל לעקוב עבורך"}</p>
      <div class="catalog-grid">
        ${items.map(p => _catalogCard(p, atLimit)).join("")}
      </div>
    </div>`;
}

// ── Full catalog modal ────────────────────────────────────────────────────────
// Everything is client-side over catalogItems, which is already in memory.

function openCatalogModal() {
  let modal = document.getElementById("catalog-modal");
  if (!modal) {
    modal = document.createElement("div");
    modal.id = "catalog-modal";
    modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:1000;display:flex;align-items:center;justify-content:center;padding:16px;";
    modal.addEventListener("click", e => { if (e.target === modal) closeCatalogModal(); });
    document.body.appendChild(modal);
  }

  const cats = [...new Set(catalogItems.map(p => p.category_he).filter(Boolean))].sort();
  modal.innerHTML = `
    <div style="background:var(--bg);border-radius:12px;border-top:4px solid var(--brand);max-width:900px;width:100%;max-height:88vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:var(--shadow);">
      <div style="padding:14px 16px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
        <strong style="margin-inline-end:auto;">כל המוצרים עם משלוח חינם</strong>
        <input id="catalog-search" type="search" placeholder="חיפוש..." oninput="renderCatalogModalList()"
               style="padding:6px 10px;border:1px solid var(--border);border-radius:8px;min-width:160px;background:var(--surface);color:var(--text);">
        <select id="catalog-cat" onchange="renderCatalogModalList()"
                style="padding:6px 10px;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);">
          <option value="">כל הקטגוריות</option>
          ${cats.map(c => `<option value="${_escAttr(c)}">${_escAttr(c)}</option>`).join("")}
        </select>
        <button class="btn-outline" style="padding:6px 12px;" onclick="closeCatalogModal()">סגור</button>
      </div>
      <div id="catalog-modal-list" style="overflow-y:auto;padding:14px 16px;"></div>
    </div>`;
  renderCatalogModalList();
}

function closeCatalogModal() {
  const modal = document.getElementById("catalog-modal");
  if (modal) modal.remove();
}

function renderCatalogModalList() {
  const listEl = document.getElementById("catalog-modal-list");
  if (!listEl) return;
  const q = (document.getElementById("catalog-search")?.value || "").trim().toLowerCase();
  const cat = document.getElementById("catalog-cat")?.value || "";
  const tracked = new Set(products.map(p => p.asin));
  const atLimit = _atProductLimit();

  const rows = catalogItems.filter(p => {
    if (tracked.has(p.asin)) return false;
    if (cat && p.category_he !== cat) return false;
    if (!q) return true;
    return `${p.name_he || ""} ${p.name || ""} ${p.asin}`.toLowerCase().includes(q);
  });

  if (!rows.length) {
    listEl.innerHTML = `<p style="color:var(--text-muted);text-align:center;padding:20px;">לא נמצאו מוצרים</p>`;
    return;
  }
  listEl.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;">
      ${rows.map(p => _catalogCard(p, atLimit)).join("")}
    </div>`;
}

async function addSuggested(asin, btn) {
  btn.disabled = true;
  btn.textContent = "מוסיף...";
  const res = await apiFetch("/me/products", {
    method: "POST",
    body: JSON.stringify({ url_or_asin: asin }),
  });
  if (!res || !res.ok) {
    btn.disabled = false;
    btn.textContent = "➕ עקוב";
    const err = res ? await res.json().catch(() => ({})) : {};
    showToast(err.detail || "שגיאה בהוספת המוצר", "error");
    return;
  }
  const newProduct = await res.json();
  checkingAsins.add(newProduct.asin);
  products.unshift(newProduct);
  renderProducts();
  renderCatalogModalList();   // no-op unless the full-catalog modal is open
  showToast(`✅ מוצר ${newProduct.asin} נוסף — בודק סטטוס...`, "success");
  apiFetch("/me/products/check-new", { method: "POST" }).catch(() => {});
  _pollForChecked([newProduct.asin], document.getElementById("add-alert"));
}

async function addProduct() {
  const input   = document.getElementById("add-input");
  const btn     = document.getElementById("add-btn");
  const alertEl = document.getElementById("add-alert");
  const val     = input.value.trim();

  if (!val) return;
  hideAlert(alertEl);

  const parts = val.split(/[\n,]+/).map(s => s.trim()).filter(Boolean);
  if (parts.length > 1) {
    await addProductsBulk(parts, input, btn, alertEl);
    return;
  }

  btn.disabled = true;
  btn.textContent = "מוסיף...";

  const res = await apiFetch("/me/products", {
    method: "POST",
    body: JSON.stringify({ url_or_asin: val }),
  });

  btn.disabled = false;
  btn.textContent = "הוסף מוצר";

  if (!res) return;

  if (res.ok) {
    const newProduct = await res.json();
    input.value = "";
    checkingAsins.add(newProduct.asin);
    products.unshift(newProduct);
    renderProducts();
    showToast(`✅ מוצר ${newProduct.asin} נוסף — בודק סטטוס...`, "success");

    // Trigger immediate check for all unchecked products
    apiFetch("/me/products/check-new", { method: "POST" }).catch(() => {});

    _pollForChecked([newProduct.asin], alertEl);
  } else {
    const err = await res.json();
    showAlert(alertEl, err.detail || "שגיאה בהוספת המוצר");
  }
}

async function addProductsBulk(items, input, btn, alertEl) {
  btn.disabled = true;
  let added = 0;
  const errors = [];
  const newAsins = [];

  for (let i = 0; i < items.length; i++) {
    btn.textContent = `מוסיף ${i + 1}/${items.length}...`;
    const res = await apiFetch("/me/products", {
      method: "POST",
      body: JSON.stringify({ url_or_asin: items[i] }),
    });
    if (res && res.ok) {
      const newProduct = await res.json();
      added++;
      checkingAsins.add(newProduct.asin);
      newAsins.push(newProduct.asin);
      products.unshift(newProduct);
    } else if (res) {
      const err = await res.json().catch(() => ({}));
      errors.push(`${items[i]}: ${err.detail || 'שגיאה'}`);
    }
  }

  btn.disabled = false;
  btn.textContent = "הוסף מוצר";
  input.value = "";
  renderProducts();

  if (errors.length === 0) {
    showToast(`✅ נוספו ${added} מוצרים — בודק סטטוס...`, "success");
  } else {
    showAlert(alertEl,
      `נוספו ${added} מוצרים${errors.length ? `. שגיאות: ${errors.join(' | ')}` : ''}`,
      added === 0 ? "error" : "success"
    );
  }

  // Trigger ONE batch check after all products are added
  if (newAsins.length > 0) {
    apiFetch("/me/products/check-new", { method: "POST" }).catch(() => {});
    _pollForChecked(newAsins, alertEl);
  }
}

// ── Poll until all new products are checked ───────────────────────────────────

function _pollForChecked(asins, alertEl) {
  let attempts = 0;
  const remaining = new Set(asins);

  const poll = setInterval(async () => {
    attempts++;
    const refreshRes = await apiFetch("/me/products");
    if (refreshRes && refreshRes.ok) {
      const updated = await refreshRes.json();

      // Update each product as it finishes — don't wait for all
      for (const asin of [...remaining]) {
        const found = updated.find(p => p.asin === asin);
        if (found && found.last_checked) {
          remaining.delete(asin);
          checkingAsins.delete(asin);
        }
      }

      products = updated;
      renderProducts();

      if (remaining.size === 0) {
        hideAlert(alertEl);
        clearInterval(poll);
        return;
      }
    }

    if (attempts >= 72) { // 6 minutes max (72 × 5s)
      remaining.forEach(a => checkingAsins.delete(a));
      renderProducts();
      hideAlert(alertEl);
      clearInterval(poll);
    }
  }, 5000);
}

// ── Pause dialog ──────────────────────────────────────────────────────────────

function showPauseDialog(asin, btn) {
  // Remove any existing dialog
  document.getElementById('pause-dialog')?.remove();

  const minDate = new Date();
  minDate.setDate(minDate.getDate() + 1);
  const minStr = minDate.toISOString().split('T')[0];

  const dialog = document.createElement('div');
  dialog.id = 'pause-dialog';
  dialog.style.cssText = `
    position:fixed;inset:0;background:rgba(0,0,0,0.45);z-index:9999;
    display:flex;align-items:center;justify-content:center;`;
  dialog.innerHTML = `
    <div style="background:#fff;border-radius:12px;padding:28px 24px;max-width:320px;width:90%;
                box-shadow:0 8px 32px rgba(0,0,0,0.18);text-align:right;direction:rtl;">
      <h3 style="margin:0 0 6px;font-size:16px;">⏸ השהה מעקב</h3>
      <p style="margin:0 0 18px;font-size:13px;color:#555;">בחר תאריך חזרה או השהה ללא הגבלת זמן</p>
      <label style="font-size:13px;font-weight:600;display:block;margin-bottom:6px;">עד תאריך (אופציונלי)</label>
      <input type="date" id="pause-until-input" min="${minStr}" lang="he-IL"
        style="width:100%;padding:8px;border:1px solid #ccc;border-radius:6px;
               font-size:14px;box-sizing:border-box;margin-bottom:18px;direction:ltr;text-align:right;">
      <div style="display:flex;gap:10px;justify-content:flex-start;">
        <button id="pause-confirm-btn"
          style="background:#FF9900;color:#111;border:none;border-radius:7px;
                 padding:9px 20px;font-size:14px;font-weight:bold;cursor:pointer;">
          השהה
        </button>
        <button onclick="document.getElementById('pause-dialog').remove()"
          style="background:#f0f0f0;color:#333;border:none;border-radius:7px;
                 padding:9px 16px;font-size:14px;cursor:pointer;">
          ביטול
        </button>
      </div>
    </div>`;

  document.body.appendChild(dialog);
  dialog.addEventListener('click', e => { if (e.target === dialog) dialog.remove(); });

  document.getElementById('pause-confirm-btn').addEventListener('click', () => {
    const until = document.getElementById('pause-until-input').value || null;
    dialog.remove();
    togglePause(asin, btn, until);
  });
}

// ── Toggle pause ──────────────────────────────────────────────────────────────

async function togglePause(asin, btn, until = null) {
  const wasPaused = btn.classList.contains("is-paused");
  const card = document.getElementById(`card-${asin}`);
  if (card) {
    card.classList.toggle("card-paused");
    btn.classList.toggle("is-paused");
    const badge = card.querySelector(".status-badge");
    if (badge) {
      if (!wasPaused) {
        badge.className = "status-badge badge-paused";
        badge.textContent = "⏸ מושהה";
      } else {
        const p = products.find(x => x.asin === asin);
        if (p) {
          badge.className = `status-badge badge-${p.last_status}`;
          badge.textContent = statusLabel(p.last_status);
        }
      }
    }
  }

  const body = wasPaused ? null : (until ? { until } : {});
  const res = await apiFetch(`/me/products/${asin}/toggle-pause`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: body !== null ? JSON.stringify(body) : undefined,
  });
  if (res && res.ok) {
    const pausedUntil = (!wasPaused && until) ? until + 'T23:59:59Z' : null;
    products = products.map(p =>
      p.asin === asin ? { ...p, is_paused: !wasPaused, paused_until: pausedUntil } : p
    );
    renderProducts();
  } else {
    await loadProducts(true);
  }
}

// ── Remove ────────────────────────────────────────────────────────────────────

async function removeProduct(asin) {
  if (!confirm(`להסיר את המוצר ${asin}?`)) return;

  const card = document.getElementById(`card-${asin}`);
  if (card) card.style.opacity = "0.4";

  const res = await apiFetch(`/me/products/${asin}`, { method: "DELETE" });

  if (!res) return;

  if (res.ok) {
    products = products.filter(p => p.asin !== asin);
    renderProducts();
  } else {
    if (card) card.style.opacity = "1";
    const err = await res.json();
    alert(err.detail || "שגיאה בהסרת המוצר");
  }
}

// ── Check now ─────────────────────────────────────────────────────────────────

async function checkNow(asin) {
  checkingAsins.add(asin);
  renderProducts();

  const res = await apiFetch(`/me/products/${asin}/check-now`, { method: "POST" });
  if (!res || !res.ok) {
    checkingAsins.delete(asin);
    renderProducts();
    return;
  }

  const prevChecked = (products.find(p => p.asin === asin) || {}).last_checked;
  let attempts = 0;
  const poll = setInterval(async () => {
    attempts++;
    const refreshRes = await apiFetch("/me/products");
    if (refreshRes && refreshRes.ok) {
      const updated = await refreshRes.json();
      const found = updated.find(p => p.asin === asin);
      if (found && found.last_checked && found.last_checked !== prevChecked) {
        products = updated;
        checkingAsins.delete(asin);
        renderProducts();
        clearInterval(poll);
        return;
      }
    }
    if (attempts >= 15) {
      checkingAsins.delete(asin);
      renderProducts();
      clearInterval(poll);
    }
  }, 6000);
}

// ── Init ──────────────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("add-input").addEventListener("keydown", e => {
    if (e.key === "Enter") addProduct();
  });

  loadUserLimit();

  // Auto-refresh every 5 minutes
  setInterval(() => loadProducts(true), 5 * 60 * 1000);

  // Update next-check countdown every minute
  setInterval(() => {
    fetch("/health").then(r => r.json()).then(data => {
      if (data.next_check_at) updateNextCheckDisplay(data.next_check_at);
    }).catch(() => {});
  }, 60 * 1000);
});
