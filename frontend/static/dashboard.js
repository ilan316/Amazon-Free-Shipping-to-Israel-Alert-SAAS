// Dashboard page logic

let products = [];

async function loadProducts() {
  const list = document.getElementById("products-list");
  list.innerHTML = '<div class="skeleton"></div><div class="skeleton" style="margin-top:10px;"></div>';

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
    const checkedStr = p.last_checked ? `בדיקה אחרונה: ${formatDate(p.last_checked)}` : "טרם נבדק";
    const notifiedStr = p.last_notified ? `התראה: ${formatDate(p.last_notified)}` : "";
    const aodNote = p.found_in_aod ? '<span title="נמצא בכל אפשרויות הקנייה">⚠️</span>' : "";
    const badgeStatus = p.is_paused ? 'UNKNOWN' : p.last_status;

    return `
      <div class="product-card status-${badgeStatus}" id="card-${p.asin}" style="${p.is_paused ? 'opacity:0.6;' : ''}">
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
        <span class="status-badge badge-${badgeStatus}">${p.is_paused ? '⏸ מושהה' : statusLabel(p.last_status)}</span>
        <button class="btn-remove" onclick="togglePause('${p.asin}')" title="${p.is_paused ? 'המשך מעקב' : 'השהה מעקב'}" style="margin-left:6px;">${p.is_paused ? '▶' : '⏸'}</button>
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
  const alert = document.getElementById("add-alert");
  const val = input.value.trim();

  if (!val) return;
  hideAlert(alert);
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
    products.unshift(newProduct);
    input.value = "";
    renderProducts();
    showAlert(alert, `מוצר ${newProduct.asin} נוסף בהצלחה`, "success");
    setTimeout(() => hideAlert(alert), 3000);
  } else {
    const err = await res.json();
    showAlert(alert, err.detail || "שגיאה בהוספת המוצר");
  }
}

async function togglePause(asin) {
  const res = await apiFetch(`/me/products/${asin}/toggle-pause`, { method: "PATCH" });
  if (res && res.ok) {
    await loadProducts();
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
  setInterval(loadProducts, 5 * 60 * 1000);
});
