// Dashboard page logic

let products = [];
let checkingAsins = new Set(); // ASINs currently being checked

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

  if (products.length === 0) {
    list.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">📦</div>
        <p>עדיין לא הוספת מוצרים לעקיבה</p>
        <p style="font-size:0.85rem; margin-top:8px;">הדבק URL של מוצר אמזון או ASIN בתיבה למעלה</p>
      </div>`;
    return;
  }

  list.innerHTML = products.map(p => {
    const displayName = p.custom_name || p.name || p.asin;
    const isChecking = checkingAsins.has(p.asin);
    const checkedStr = isChecking
      ? '<span style="color:var(--brand-dark)">⏳ בודק עכשיו...</span>'
      : (p.last_checked ? `בדיקה אחרונה: ${formatDate(p.last_checked)}` : "טרם נבדק");
    const notifiedStr = p.last_notified ? `התראה: ${formatDate(p.last_notified)}` : "";
    const aodNote = p.found_in_aod ? '<span title="נמצא בכל אפשרויות הקנייה">⚠️</span>' : "";
    const badgeStatus = p.is_paused ? 'UNKNOWN' : (isChecking ? 'UNKNOWN' : p.last_status);

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
          <div class="product-name">
            <a href="${p.url}" target="_blank" rel="noopener">${escHtml(displayName)}</a>
            ${aodNote}
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
            : `<span class="status-badge badge-${p.last_status}">${statusLabel(p.last_status)}</span>`
        }
        ${pauseBtn}
        <button class="btn-remove" onclick="removeProduct('${p.asin}')">הסר</button>
      </div>`;
  }).join("");
}

function escHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function addProduct() {
  const input = document.getElementById("add-input");
  const btn = document.getElementById("add-btn");
  const alertEl = document.getElementById("add-alert");
  const val = input.value.trim();

  if (!val) return;
  hideAlert(alertEl);
  btn.disabled = true;
  btn.textContent = "מוסיף...";

  const res = await apiFetch("/me/products", {
    method: "POST",
    body: JSON.stringify({ url_or_asin: val }),
  });

  btn.disabled = false;
  btn.textContent = "הוסף";

  if (!res) return;

  if (res.ok) {
    const newProduct = await res.json();
    input.value = "";
    // Mark as checking and add to list
    checkingAsins.add(newProduct.asin);
    products.unshift(newProduct);
    renderProducts();
    showAlert(alertEl, `✅ מוצר ${newProduct.asin} נוסף — בודק סטטוס...`, "success");

    // Poll for updated status every 6 seconds, up to 5 times (30s)
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

async function togglePause(asin, btn) {
  const wasPaused = btn.classList.contains("is-paused");
  // Optimistic UI update
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
    // Update local state silently
    products = products.map(p => p.asin === asin ? { ...p, is_paused: !wasPaused } : p);
  } else {
    // Revert on error
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

// Enter key in add input
document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("add-input").addEventListener("keydown", e => {
    if (e.key === "Enter") addProduct();
  });

  // Auto-refresh every 5 minutes
  setInterval(() => loadProducts(true), 5 * 60 * 1000);
});
