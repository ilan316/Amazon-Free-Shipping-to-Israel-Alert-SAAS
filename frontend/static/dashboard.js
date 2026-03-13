// Dashboard page logic

let products = [];
let checkingAsins = new Set();
let currentFilter = 'ALL';

const STATUS_TOOLTIP = {
  FREE:    "משלוח חינם לישראל זמין",
  PAID:    "משלוח לישראל בתשלום",
  NO_SHIP: "לא ניתן לשלוח לישראל",
  UNKNOWN: "ASIN שגוי או לינק לא תקין",
  ERROR:   "שגיאה בבדיקה (קפצ'ה או תקלה)",
};

async function loadProducts(silent = false) {
  const list = document.getElementById("products-list");
  if (!silent) {
    list.innerHTML = '<div class="skeleton"></div><div class="skeleton" style="margin-top:10px;"></div>';
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

  // Show/hide filter bar
  const filterBar = document.getElementById("filter-bar");
  if (filterBar) filterBar.style.display = products.length > 0 ? "flex" : "none";

  // Update counter
  const counterEl = document.getElementById("products-counter");
  if (counterEl) {
    const total = products.length;
    const free    = products.filter(p => !p.is_paused && p.last_status === 'FREE').length;
    const noShip  = products.filter(p => !p.is_paused && p.last_status === 'NO_SHIP').length;
    const unknown = products.filter(p => !p.is_paused && (p.last_status === 'UNKNOWN' || p.last_status === 'ERROR')).length;
    if (total > 0) {
      const parts = [`${total} מוצרים במעקב`];
      if (free > 0)    parts.push(`${free} חינם`);
      if (noShip > 0)  parts.push(`${noShip} לא נשלח`);
      if (unknown > 0) parts.push(`${unknown} לא ידוע`);
      counterEl.textContent = parts.join(' · ');
    } else {
      counterEl.textContent = '';
    }
  }

  if (products.length === 0) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📦</div>
        <p>עדיין לא הוספת מוצרים לעקיבה</p>
        <p style="font-size:0.85rem; margin-top:8px;">הדבק URL של מוצר אמזון או ASIN בתיבה למעלה</p>
      </div>`;
    return;
  }

  // Filter
  let filtered = [...products];
  if (currentFilter !== 'ALL') {
    filtered = filtered.filter(p => !p.is_paused && p.last_status === currentFilter);
  }

  // Sort: FREE first, paused last
  const STATUS_ORDER = { FREE: 0, PAID: 1, NO_SHIP: 2, UNKNOWN: 3, ERROR: 4 };
  filtered.sort((a, b) => {
    if (a.is_paused !== b.is_paused) return a.is_paused ? 1 : -1;
    return (STATUS_ORDER[a.last_status] ?? 5) - (STATUS_ORDER[b.last_status] ?? 5);
  });

  if (filtered.length === 0) {
    list.innerHTML = `<div class="empty-state"><p>אין מוצרים בסטטוס זה</p></div>`;
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
    const badgeStatus = p.is_paused ? 'UNKNOWN' : (isChecking ? 'UNKNOWN' : p.last_status);
    const tooltip     = STATUS_TOOLTIP[p.last_status] || "";

    const pauseBtn = `
      <button
        class="btn-pause ${p.is_paused ? 'is-paused' : ''}"
        onclick="togglePause('${p.asin}', this)"
        title="${p.is_paused ? 'המשך מעקב' : 'השהה מעקב'}">
        ${p.is_paused ? '▶ המשך' : '⏸ השהה'}
      </button>`;

    return `
      <div class="product-card status-${badgeStatus} ${p.is_paused ? 'card-paused' : ''}" id="card-${p.asin}">
        <div class="product-info">
          <div class="product-name" id="name-${p.asin}">
            <a href="${p.url}" target="_blank" rel="noopener">${escHtml(displayName)}</a>
            ${aodNote}
            <button class="btn-edit-name" onclick="editName('${p.asin}')" title="ערוך שם">✏️</button>
          </div>
          <div class="product-meta">
            <span>ASIN: ${p.asin}</span>
            <span>${checkedStr}</span>
            ${notifiedStr ? `<span>${notifiedStr}</span>` : ""}
          </div>
        </div>
        ${p.is_paused
          ? '<span class="status-badge badge-paused">⏸ מושהה</span>'
          : isChecking
            ? '<span class="status-badge badge-UNKNOWN">⏳ בודק...</span>'
            : `<span class="status-badge badge-${p.last_status}" title="${tooltip}">${statusLabel(p.last_status)}</span>`
        }
        ${pauseBtn}
        ${!p.is_paused && !isChecking
          ? `<button class="btn-check-now" onclick="checkNow('${p.asin}')" title="בדוק סטטוס עכשיו">🔄 בדוק</button>`
          : ''}
        <button class="btn-remove" onclick="removeProduct('${p.asin}')">הסר</button>
      </div>`;
  }).join("");
}

function setFilter(filter, btn) {
  currentFilter = filter;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderProducts();
}

function editName(asin) {
  const p = products.find(x => x.asin === asin);
  if (!p) return;
  const nameEl = document.getElementById(`name-${asin}`);
  if (!nameEl) return;
  const current = p.custom_name || p.name || p.asin;
  nameEl.innerHTML = `
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

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function addProduct() {
  const input   = document.getElementById("add-input");
  const btn     = document.getElementById("add-btn");
  const alertEl = document.getElementById("add-alert");
  const val     = input.value.trim();

  if (!val) return;
  hideAlert(alertEl);

  // Bulk detect: commas or newlines
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
    showAlert(alertEl, `✅ מוצר ${newProduct.asin} נוסף — בודק סטטוס...`, "success");

    let attempts = 0;
    const poll = setInterval(async () => {
      attempts++;
      const refreshRes = await apiFetch("/me/products");
      if (refreshRes && refreshRes.ok) {
        const updated = await refreshRes.json();
        const found = updated.find(p => p.asin === newProduct.asin);
        if (found && found.last_checked) {
          products = updated;
          checkingAsins.delete(newProduct.asin);
          renderProducts();
          hideAlert(alertEl);
          clearInterval(poll);
          return;
        }
      }
      if (attempts >= 5) {
        checkingAsins.delete(newProduct.asin);
        products = products.map(p => p.asin === newProduct.asin ? { ...p } : p);
        renderProducts();
        hideAlert(alertEl);
        clearInterval(poll);
      }
    }, 6000);

  } else {
    const err = await res.json();
    showAlert(alertEl, err.detail || "שגיאה בהוספת המוצר");
  }
}

async function addProductsBulk(items, input, btn, alertEl) {
  btn.disabled = true;
  let added = 0;
  const errors = [];

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
    showAlert(alertEl, `✅ נוספו ${added} מוצרים בהצלחה`, "success");
  } else {
    const detail = errors.join(' | ');
    showAlert(alertEl,
      `נוספו ${added} מוצרים${errors.length ? `. שגיאות: ${detail}` : ''}`,
      added === 0 ? "error" : "success"
    );
  }
}

async function togglePause(asin, btn) {
  const wasPaused = btn.classList.contains("is-paused");
  const card = document.getElementById(`card-${asin}`);
  if (card) {
    card.classList.toggle("card-paused");
    btn.classList.toggle("is-paused");
    btn.textContent = wasPaused ? '⏸ השהה' : '▶ המשך';
    btn.title = wasPaused ? 'השהה מעקב' : 'המשך מעקב';
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

  const res = await apiFetch(`/me/products/${asin}/toggle-pause`, { method: "PATCH" });
  if (res && res.ok) {
    products = products.map(p => p.asin === asin ? { ...p, is_paused: !wasPaused } : p);
  } else {
    await loadProducts(true);
  }
}

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

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("add-input").addEventListener("keydown", e => {
    if (e.key === "Enter") addProduct();
  });

  // Auto-refresh every 5 minutes
  setInterval(() => loadProducts(true), 5 * 60 * 1000);
});
