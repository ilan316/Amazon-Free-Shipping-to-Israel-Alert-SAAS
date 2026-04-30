// Dashboard page logic

let products = [];
let checkingAsins = new Set();
let currentFilter = 'ALL';
let userLimit = null;

const STATUS_TOOLTIP = {
  FREE:      "משלוח חינם לישראל זמין",
  PAID:      "משלוח לישראל בתשלום",
  NO_SHIP:   "לא נשלח לארץ",
  NOT_FOUND: "המוצר לא קיים באמזון",
  UNKNOWN:   "לא ניתן לקבוע סטטוס — ראה פרטים",
  ERROR:     "שגיאה בבדיקה (קפצ'ה או תקלה)",
};

// ── Limit badge ───────────────────────────────────────────────────────────────

async function loadUserLimit() {
  const res = await apiFetch("/me");
  if (!res || !res.ok) return;
  const user = await res.json();
  if (user.effective_product_limit != null) {
    userLimit = user.effective_product_limit;
    updateLimitBadge();
  }
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
    const notifiedStr = p.last_notified ? `התראה: ${formatDate(p.last_notified)}` : "";
    const aodNote     = p.found_in_aod ? '<span title="נמצא בכל אפשרויות הקנייה">⚠️</span>' : "";
    const badgeStatus = p.is_paused ? 'UNKNOWN'
      : (isChecking || p.last_status === 'UNKNOWN' || p.last_status === 'ERROR') ? 'UNKNOWN'
      : p.last_status;
    const tooltip     = STATUS_TOOLTIP[p.last_status] || "";
    const linkUrl     = p.affiliate_url || p.url;


    const pausedUntilStr = p.paused_until
      ? ` עד ${new Date(p.paused_until).toLocaleDateString('he-IL')}`
      : '';
    const pauseBtn = `
      <button
        class="btn-pause ${p.is_paused ? 'is-paused' : ''}"
        onclick="${p.is_paused ? `togglePause('${p.asin}', this)` : `showPauseDialog('${p.asin}', this)`}"
        title="${p.is_paused ? `המשך מעקב${pausedUntilStr}` : 'השהה מעקב'}">
        ${p.is_paused ? `▶ המשך${pausedUntilStr}` : '⏸ השהה'}
      </button>`;

    const badgeHtml = p.is_paused
      ? '<span class="status-badge badge-paused">⏸ מושהה</span>'
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
        </div>

        <!-- שורה 2: בדיקה אחרונה (ימין) | ASIN LTR (שמאל) -->
        <div class="card-row-meta">
          <span class="card-meta-checked">${checkedStr}${notifiedStr ? ' · ' + notifiedStr : ''}</span>
          <span class="card-meta-asin" dir="ltr">ASIN: ${p.asin}</span>
        </div>

        <!-- שורה 3: סטטוס | השהה | בדוק | הסר -->
        <div class="card-row-actions">
          ${badgeHtml}
          ${pauseBtn}
          <button class="btn-remove" onclick="removeProduct('${p.asin}')">הסר</button>
        </div>

      </div>`;
  }).join("");
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
