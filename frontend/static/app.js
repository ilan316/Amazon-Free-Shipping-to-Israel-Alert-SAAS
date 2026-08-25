// ── Shared utilities (loaded on every page) ───────────────────────────────────

const API = "";  // Same origin

function getToken() { return localStorage.getItem("jwt"); }
function setToken(t) { localStorage.setItem("jwt", t); }
function clearToken() { localStorage.removeItem("jwt"); }

async function apiFetch(path, options = {}) {
  const { skipAuthRedirect, rawServerErrors, ...fetchOptions } = options;
  const token = getToken();
  const headers = {
    "Content-Type": "application/json",
    ...(token ? { "Authorization": `Bearer ${token}` } : {}),
    ...((fetchOptions.headers) || {}),
  };
  const res = await fetch(API + path, { ...fetchOptions, headers });
  if (res.status === 401 && !skipAuthRedirect) {
    clearToken();
    window.location = "/";
    return null;
  }
  if (res.status === 429) {
    showToast("יותר מדי ניסיונות — נסה שוב עוד דקה", "error");
    return null;
  }
  // rawServerErrors: the caller wants to read the 5xx body itself (e.g. to show
  // the real `detail`, or to check whether the work actually completed server-side).
  if (res.status >= 500 && !rawServerErrors) {
    showToast("שגיאת שרת — נסה שוב מאוחר יותר", "error");
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

// המחיר נשמר כפי שאמזון מציגה אותו — 'ILS 159.76'. בתצוגה מציגים שקלים: '159.76 ₪'.
// מחיר דולרי נשאר עם $ במכוון: הוא סימן שהבדיקה לא קיבלה מחיר ישראלי, לא מחיר להצגה.
function formatPrice(raw) {
  const s = String(raw ?? "").trim();
  if (!s) return "";
  const m = s.match(/^(?:ILS|₪)\s*([\d,]+(?:\.\d+)?)$/i);
  return m ? `${m[1]} ₪` : s;
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
