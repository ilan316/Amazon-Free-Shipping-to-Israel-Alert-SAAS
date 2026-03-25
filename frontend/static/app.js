// ── Shared utilities (loaded on every page) ───────────────────────────────────

const API = "";  // Same origin

function getToken() { return localStorage.getItem("jwt"); }
function setToken(t) { localStorage.setItem("jwt", t); }
function clearToken() { localStorage.removeItem("jwt"); }

async function apiFetch(path, options = {}) {
  const token = getToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { "Authorization": `Bearer ${token}` } : {}),
    ...((options.headers) || {}),
  };
  const res = await fetch(API + path, { ...options, headers });
  if (res.status === 401) {
    clearToken();
    window.location = "/";
    return null;
  }
  return res;
}

function requireAuth() {
  if (!getToken()) {
    window.location = "/";
  }
}

function statusLabel(status) {
  const map = {
    "FREE":      "משלוח חינם ✅",
    "PAID":      "משלוח בתשלום 💳",
    "NO_SHIP":   "לא נשלח לארץ 🚫",
    "NOT_FOUND": "מוצר לא קיים ❌",
    "UNKNOWN":   "לא ידוע ⚠️",
    "ERROR":     "שגיאה ⚠️",
  };
  return map[status] || status;
}

function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("he-IL", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function showAlert(el, msg, type = "error") {
  el.textContent = msg;
  el.className = `alert alert-${type} visible`;
}

function hideAlert(el) {
  el.className = "alert";
}

function showToast(msg, type = "info", duration = 3500) {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.className = "toast-container";
    document.body.appendChild(container);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.textContent = msg;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transition = "opacity 0.3s";
    setTimeout(() => toast.remove(), 300);
  }, duration);
}

async function logout() {
  clearToken();
  window.location = "/";
}

function _downloadCSV(filename, headers, rows) {
  const bom = '\uFEFF'; // UTF-8 BOM for Excel compatibility
  const lines = [headers, ...rows].map(row =>
    row.map(cell => `"${String(cell == null ? '' : cell).replace(/"/g, '""')}"`).join(',')
  );
  const blob = new Blob([bom + lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}
