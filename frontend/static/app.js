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
    "FREE":    "משלוח חינם ✅",
    "PAID":    "משלוח בתשלום 💳",
    "NO_SHIP": "לא נשלח לארץ 🚫",
    "UNKNOWN": "לא ידוע ⚠️",
    "ERROR":   "שגיאה ⚠️",
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

async function logout() {
  clearToken();
  window.location = "/";
}
