function switchTab(name, btn) {
  document.querySelectorAll(".admin-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.getElementById("tab-" + name).classList.add("active");
  btn.classList.add("active");
}

let _allUsers = [];
let _allAdminProducts = [];
let _adminFilter = 'ALL';

async function loadAdminData() {
  const meRes = await apiFetch("/me");
  if (!meRes || !meRes.ok) return;
  const me = await meRes.json();
  if (!me.is_admin) {
    document.querySelector(".admin-page").innerHTML =
      '<div style="text-align:center;padding:80px;color:var(--error);font-size:1.2rem;">⛔ גישה אסורה — אין הרשאת מנהל</div>';
    return;
  }
  await Promise.all([
    loadStats(),
    loadSchedulerStatus(),
    loadRegistrationsChart(),
    loadUsers(),
    loadProducts(),
    loadNotificationsLog(),
    loadSystemMessageAdmin(),
    loadCheckTime(),
    loadGlobalProductLimit(),
    loadCookieStatus(),
    loadInactivityDays(),
    loadChecksStatus(),
  ]);
}

async function loadCookieStatus() {
  const badge = document.getElementById("cookie-status-badge");
  if (!badge) return;
  const res = await apiFetch("/admin/cookie-status");
  if (!res || !res.ok) { badge.textContent = "לא ידוע"; return; }
  const d = await res.json();
  if (d.loaded) {
    badge.textContent = `✅ פעיל · ${d.count} cookies`;
    badge.style.background = "#e8f5e9"; badge.style.color = "#2e7d32";
  } else {
    badge.textContent = "❌ אין cookies — הזרק כדי להפעיל בדיקות";
    badge.style.background = "#fdecea"; badge.style.color = "var(--error)";
  }
}

async function loadGlobalProductLimit() {
  const res = await apiFetch("/admin/global-product-limit");
  if (!res || !res.ok) return;
  const d = await res.json();
  const inp = document.getElementById("product-limit-input");
  if (inp) inp.value = d.limit;
}

async function setGlobalProductLimit() {
  const inp = document.getElementById("product-limit-input");
  const msgEl = document.getElementById("product-limit-msg");
  const limit = parseInt(inp.value);
  if (!limit || limit < 1) return;
  const res = await apiFetch("/admin/global-product-limit", {
    method: "POST",
    body: JSON.stringify({ limit }),
  });
  if (res && res.ok) {
    const data = await res.json();
    msgEl.textContent = "✅ " + data.message;
    msgEl.style.color = "var(--success)";
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    msgEl.textContent = "❌ " + (err.detail || "שגיאה");
    msgEl.style.color = "var(--error)";
  }
  setTimeout(() => { msgEl.textContent = ""; }, 3000);
}

async function loadInactivityDays() {
  const res = await apiFetch("/admin/inactivity-days");
  if (!res || !res.ok) return;
  const d = await res.json();
  const inp = document.getElementById("inactivity-days-input");
  if (inp) inp.value = d.days;
}

async function setInactivityDays() {
  const inp = document.getElementById("inactivity-days-input");
  const msgEl = document.getElementById("inactivity-days-msg");
  const days = parseInt(inp.value);
  if (isNaN(days) || days < 0) return;
  const res = await apiFetch("/admin/inactivity-days", {
    method: "POST",
    body: JSON.stringify({ days }),
  });
  if (res && res.ok) {
    const data = await res.json();
    msgEl.textContent = "✅ " + data.message;
    msgEl.style.color = "var(--success)";
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    msgEl.textContent = "❌ " + (err.detail || "שגיאה");
    msgEl.style.color = "var(--error)";
  }
  setTimeout(() => { msgEl.textContent = ""; }, 3000);
}

async function setUserProductLimit(userId, currentLimit, globalLimit) {
  const val = prompt(
    `מגבלת מוצרים למשתמש ${userId}\nגלובלית: ${globalLimit}\nהשאר ריק לאיפוס לגלובלית:`,
    currentLimit !== null ? currentLimit : ""
  );
  if (val === null) return; // cancelled
  const res = await apiFetch(`/admin/users/${userId}/product-limit`, {
    method: "PATCH",
    body: JSON.stringify({ limit: val.trim() === "" ? null : parseInt(val) }),
  });
  if (res && res.ok) {
    await loadUsers();
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    alert(err.detail || "שגיאה");
  }
}

async function loadCheckTime() {
  const res = await apiFetch("/admin/get-check-time");
  if (!res || !res.ok) return;
  const d = await res.json();
  const input = document.getElementById("check-time-input");
  if (input && d.time) input.value = d.time;
}

async function setCheckTime() {
  const input = document.getElementById("check-time-input");
  const statusEl = document.getElementById("check-time-msg");
  const time = input.value;
  const res = await apiFetch("/admin/set-check-time", {
    method: "POST",
    body: JSON.stringify({ time }),
  });
  if (res && res.ok) {
    const data = await res.json();
    statusEl.textContent = "✅ " + data.message;
    statusEl.style.color = "var(--success)";
    await loadSchedulerStatus();
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    statusEl.textContent = "❌ " + (err.detail || "שגיאה");
    statusEl.style.color = "var(--error)";
  }
  setTimeout(() => { statusEl.textContent = ""; }, 4000);
}

async function loadStats() {
  const res = await apiFetch("/admin/stats");
  if (!res || !res.ok) return;
  const data = await res.json();
  document.getElementById("stat-users").textContent = data.total_users;
  document.getElementById("stat-admins").textContent = data.total_admins;
  document.getElementById("stat-products").textContent = data.total_products;
  document.getElementById("stat-notifs").textContent = data.notifications_24h;
}

async function loadSchedulerStatus() {
  const res = await fetch("/health");
  if (!res.ok) return;
  const data = await res.json();
  const fmt = iso => iso ? new Date(iso).toLocaleString("he-IL", { hour:"2-digit", minute:"2-digit", day:"2-digit", month:"2-digit" }) : "—";
  const el1 = document.getElementById("sched-next-check");
  const el2 = document.getElementById("sched-next-summary");
  if (el1) el1.textContent = fmt(data.next_check_at);
  if (el2) el2.textContent = fmt(data.next_summary_at);
}

let _checksPaused = false;

async function loadChecksStatus() {
  const res = await apiFetch("/admin/checks-status");
  if (!res || !res.ok) return;
  const data = await res.json();
  _checksPaused = data.paused;
  const btn = document.getElementById("btn-pause-checks");
  if (!btn) return;
  if (_checksPaused) {
    btn.textContent = "▶ הפעל בדיקות";
    btn.style.background = "var(--error, #c0392b)";
  } else {
    btn.textContent = "⏸ השהה בדיקות";
    btn.style.background = "";
  }
}

async function toggleChecks() {
  const btn = document.getElementById("btn-pause-checks");
  const msg = document.getElementById("pause-checks-msg");
  btn.disabled = true;
  const endpoint = _checksPaused ? "/admin/resume-checks" : "/admin/pause-checks";
  const res = await apiFetch(endpoint, { method: "POST" });
  btn.disabled = false;
  if (res && res.ok) {
    const data = await res.json();
    msg.textContent = "✅ " + data.message;
    msg.style.color = "var(--success)";
    await loadChecksStatus();
    await loadSchedulerStatus();
  } else {
    msg.textContent = "❌ שגיאה";
    msg.style.color = "var(--error)";
  }
  setTimeout(() => { msg.textContent = ""; }, 3000);
}

async function loadRegistrationsChart() {
  const res = await apiFetch("/admin/registrations-chart");
  if (!res || !res.ok) return;
  const data = await res.json();
  const canvas = document.getElementById("registrations-chart");
  if (!canvas || !data.length) return;
  new Chart(canvas, {
    type: "line",
    data: {
      labels: data.map(d => d.date),
      datasets: [{
        label: "משתמשים חדשים",
        data: data.map(d => d.count),
        borderColor: "#e47911",
        backgroundColor: "rgba(228,121,17,0.1)",
        tension: 0.3,
        fill: true,
        pointRadius: 4,
        pointBackgroundColor: "#e47911",
      }]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, ticks: { stepSize: 1 } },
        x: { ticks: { maxTicksLimit: 10 } }
      }
    }
  });
}

let _notifFilter = "ALL";

function setNotifFilter(val, btn) {
  _notifFilter = val;
  document.querySelectorAll("[data-notif-filter]").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  _applyNotifFilter();
}

function _applyNotifFilter() {
  const rows = document.querySelectorAll("#notifications-body tr[data-ok]");
  let count = 0;
  rows.forEach(r => {
    const ok = r.dataset.ok;
    const show = _notifFilter === "ALL" || (_notifFilter === "OK" && ok === "1") || (_notifFilter === "FAIL" && ok === "0");
    r.style.display = show ? "" : "none";
    if (show) count++;
  });
  const countEl = document.getElementById("notif-count");
  if (countEl) countEl.textContent = count + " רשומות";
}

async function loadNotificationsLog() {
  const limitEl = document.getElementById("notif-limit");
  const limit = limitEl ? limitEl.value : 100;
  const res = await apiFetch(`/admin/notifications-log?limit=${limit}`);
  if (!res || !res.ok) return;
  const logs = await res.json();
  const tbody = document.getElementById("notifications-body");
  if (!logs.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:24px;">אין התראות עדיין</td></tr>';
    const countEl = document.getElementById("notif-count");
    if (countEl) countEl.textContent = "0 רשומות";
    return;
  }
  tbody.innerHTML = logs.map(l => {
    const emailTo = l.email_to || l.user_email;
    const sameEmail = emailTo === l.user_email;
    const emailToHtml = `<span style="display:inline-block;background:#f0f4ff;border:1px solid #c5cfe8;border-radius:12px;padding:2px 8px;font-size:0.75rem;color:#2c3e7a;direction:ltr;">${emailTo}</span>`;
    const errorHtml = (!l.success && l.error_msg)
      ? `<span style="display:block;font-size:0.72rem;color:var(--error);margin-top:3px;opacity:0.85;">${l.error_msg}</span>`
      : "";
    const asinUrl = `https://www.amazon.com/dp/${l.asin}?tag=amzfreeil-20`;
    return `
    <tr data-ok="${l.success ? '1' : '0'}">
      <td class="ltr" style="white-space:nowrap;">${new Date(l.sent_at).toLocaleString("he-IL")}</td>
      <td class="ltr truncate" style="max-width:160px;">${l.user_email}</td>
      <td>${sameEmail ? '<span style="color:var(--text-muted);font-size:0.8rem;">זהה</span>' : emailToHtml}</td>
      <td class="truncate" style="max-width:200px;">${l.product_name}</td>
      <td class="ltr"><a href="${asinUrl}" target="_blank" style="color:var(--brand-dark);font-family:monospace;font-size:0.82rem;">${l.asin}</a></td>
      <td><span class="status-badge badge-${l.status}">${statusLabel(l.status)}</span></td>
      <td style="color:${l.success ? 'var(--success)' : 'var(--error)'};font-weight:600;">
        ${l.success ? '✅ נשלח' : '❌ נכשל'}${errorHtml}
      </td>
    </tr>`;
  }).join("");
  _applyNotifFilter();
}

async function loadSystemMessageAdmin() {
  const res = await fetch("/system-message");
  if (!res.ok) return;
  const data = await res.json();
  const el = document.getElementById("system-msg-input");
  if (el) el.value = data.message || "";
}

async function saveSystemMessage() {
  const msg = document.getElementById("system-msg-input").value;
  const statusEl = document.getElementById("system-msg-status");
  const res = await apiFetch("/admin/system-message", {
    method: "POST",
    body: JSON.stringify({ message: msg }),
  });
  if (res && res.ok) {
    statusEl.textContent = "✅ נשמר בהצלחה";
    statusEl.style.color = "var(--success)";
  } else {
    statusEl.textContent = "❌ שגיאה בשמירה";
    statusEl.style.color = "var(--error)";
  }
  setTimeout(() => { statusEl.textContent = ""; }, 3000);
}

function filterUsers(query) {
  const q = query.trim().toLowerCase();
  const rows = document.querySelectorAll("#users-body tr[id^='user-row-']");
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = (!q || text.includes(q)) ? "" : "none";
  });
}

async function loadUsers() {
  const [usersRes, limitRes] = await Promise.all([
    apiFetch("/admin/users"),
    apiFetch("/admin/global-product-limit"),
  ]);
  if (!usersRes || !usersRes.ok) return;
  const users = await usersRes.json();
  _allUsers = users;
  const globalLimit = (limitRes && limitRes.ok) ? (await limitRes.json()).limit : 10;
  // Keep input in sync
  const globalLimitEl = document.getElementById("product-limit-input");
  if (globalLimitEl) globalLimitEl.value = globalLimit;
  const tbody = document.getElementById("users-body");
  if (!users.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:24px;">אין משתמשים</td></tr>';
    return;
  }

  tbody.innerHTML = users.map(u => {
    const isCustom = u.max_products !== null && u.max_products !== undefined;
    const limitDisplay = isCustom
      ? `<span style="font-weight:600;color:var(--brand-dark);">${u.max_products}</span>`
      : `<span style="color:var(--text-muted);">${globalLimit}</span>`;
    return `
    <tr id="user-row-${u.id}">
      <td>${u.id}</td>
      <td class="ltr truncate">${u.email}
        ${u.is_admin ? ' <span class="tag-admin">מנהל</span>' : ''}
      </td>
      <td class="ltr truncate">${u.notify_email}</td>
      <td style="text-align:center;">${u.product_count}</td>
      <td style="text-align:center;">
        ${limitDisplay}
        <button class="btn-sm" onclick="setUserProductLimit(${u.id}, ${isCustom ? u.max_products : 'null'}, ${globalLimit})"
          style="margin-right:4px;padding:2px 6px;font-size:0.72rem;">✏️</button>
      </td>
      <td class="ltr">${u.created_at ? new Date(u.created_at).toLocaleDateString("he-IL") : "—"}</td>
      <td>
        ${u.is_active
          ? '<span style="color:var(--success);font-weight:600;">פעיל</span>'
          : '<span class="tag-inactive">מושהה</span>'}
      </td>
      <td>
        <div class="action-btns">
          <button class="btn-sm ${u.is_active ? 'active-toggle' : 'inactive-toggle'}"
            onclick="toggleActive(${u.id})">
            ${u.is_active ? 'השהה' : 'הפעל'}
          </button>
          <button class="btn-sm" onclick="toggleAdmin(${u.id})">
            ${u.is_admin ? 'הסר מנהל' : 'הפוך למנהל'}
          </button>
          <button class="btn-sm danger" onclick="deleteUser(${u.id})">מחק</button>
        </div>
      </td>
    </tr>`;
  }).join("");
}

async function loadProducts() {
  const res = await apiFetch("/admin/products");
  if (!res || !res.ok) return;
  _allAdminProducts = await res.json();
  renderAdminProducts();
}

function setAdminFilter(filter, btn) {
  _adminFilter = filter;
  document.querySelectorAll('.admin-filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  renderAdminProducts();
}

function renderAdminProducts() {
  const tbody = document.getElementById("products-body");
  const selectAll = document.getElementById("select-all-products");
  if (selectAll) selectAll.checked = false;
  updateBulkDeleteBtn();

  // Update filter button labels with counts
  const counts = { FREE: 0, PAID: 0, NO_SHIP: 0, NOT_FOUND: 0 };
  _allAdminProducts.forEach(p => {
    if (counts[p.last_status] !== undefined) counts[p.last_status]++;
  });
  const lblMap = {
    ALL:       `הכל (${_allAdminProducts.length})`,
    FREE:      `✅ משלוח חינם${counts.FREE > 0 ? ` (${counts.FREE})` : ''}`,
    PAID:      `💳 משלוח בתשלום${counts.PAID > 0 ? ` (${counts.PAID})` : ''}`,
    NO_SHIP:   `🚫 לא נשלח לארץ${counts.NO_SHIP > 0 ? ` (${counts.NO_SHIP})` : ''}`,
    NOT_FOUND: `❌ מוצר לא קיים${counts.NOT_FOUND > 0 ? ` (${counts.NOT_FOUND})` : ''}`,
  };
  document.querySelectorAll('.admin-filter-btn[data-filter]').forEach(btn => {
    const f = btn.getAttribute('data-filter');
    if (lblMap[f]) btn.textContent = lblMap[f];
  });

  const filtered = _adminFilter === 'ALL'
    ? _allAdminProducts
    : _allAdminProducts.filter(p => p.last_status === _adminFilter);

  if (!filtered.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:24px;">אין מוצרים</td></tr>';
    return;
  }
  tbody.innerHTML = filtered.map(p => `
    <tr>
      <td style="text-align:center;"><input type="checkbox" class="product-checkbox" value="${p.id}" onchange="updateBulkDeleteBtn()"></td>
      <td class="ltr"><a href="${p.url}" target="_blank">${p.asin}</a></td>
      <td class="truncate">${p.name || "—"}</td>
      <td><span class="status-badge badge-${p.last_status}">${statusLabel(p.last_status)}</span></td>
      <td style="text-align:center;">${p.watchers}</td>
      <td class="ltr">${p.last_checked ? formatDate(p.last_checked) : "—"}</td>
      <td style="text-align:center;color:${p.consecutive_errors > 0 ? 'var(--error)' : 'var(--text-muted)'}">
        ${p.consecutive_errors}
      </td>
      <td class="truncate ltr" style="max-width:200px;font-size:0.78rem;color:var(--text-muted);"
          title="${p.raw_text ? p.raw_text.replace(/"/g,'&quot;') : ''}">
        ${p.raw_text ? p.raw_text.substring(0, 60) + (p.raw_text.length > 60 ? '…' : '') : '—'}
      </td>
    </tr>
  `).join("");
}

function exportAdminCSV() {
  if (!_allAdminProducts.length) return;
  const headers = ['ASIN', 'שם', 'סטטוס', 'עוקבים', 'בדיקה אחרונה', 'שגיאות רצופות', 'קישור'];
  const rows = _allAdminProducts.map(p => [
    p.asin,
    p.name || '',
    p.last_status || '',
    p.watchers,
    p.last_checked ? new Date(p.last_checked).toLocaleString('he-IL') : '',
    p.consecutive_errors,
    p.url,
  ]);
  _downloadCSV('products_admin.csv', headers, rows);
}

function toggleSelectAllProducts(cb) {
  document.querySelectorAll(".product-checkbox").forEach(c => c.checked = cb.checked);
  updateBulkDeleteBtn();
}

function updateBulkDeleteBtn() {
  const checked = document.querySelectorAll(".product-checkbox:checked");
  const btn = document.getElementById("bulk-delete-btn");
  const countEl = document.getElementById("bulk-count");
  if (!btn) return;
  if (checked.length > 0) {
    btn.style.display = "";
    countEl.textContent = checked.length;
  } else {
    btn.style.display = "none";
  }
}

async function bulkDeleteProducts() {
  const checked = document.querySelectorAll(".product-checkbox:checked");
  if (!checked.length) return;
  if (!confirm(`למחוק ${checked.length} מוצרים לצמיתות?`)) return;
  const ids = Array.from(checked).map(c => parseInt(c.value));
  const res = await apiFetch("/admin/products/bulk", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ product_ids: ids }),
  });
  if (res && res.ok) {
    const data = await res.json();
    await loadProducts(); await loadStats();
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    alert(err.detail || "שגיאה במחיקה");
  }
}

async function toggleActive(userId) {
  await apiFetch(`/admin/users/${userId}/toggle-active`, { method: "PATCH" });
  await loadUsers(); await loadStats();
}

async function toggleAdmin(userId) {
  await apiFetch(`/admin/users/${userId}/toggle-admin`, { method: "PATCH" });
  await loadUsers(); await loadStats();
}

async function deleteUser(userId) {
  if (!confirm("למחוק משתמש זה לצמיתות?")) return;
  const res = await apiFetch(`/admin/users/${userId}`, { method: "DELETE" });
  if (!res || !res.ok) {
    const err = res ? await res.json().catch(() => ({})) : {};
    alert(err.detail || "שגיאה במחיקה");
    return;
  }
  await loadUsers(); await loadStats();
}

async function cleanOrphans() {
  if (!confirm("למחוק את כל המוצרים ללא עוקבים?")) return;
  const res = await apiFetch("/admin/products-orphans", { method: "DELETE" });
  if (res && res.ok) {
    const data = await res.json();
    alert(`נמחקו ${data.count} מוצרים`);
    await loadProducts(); await loadStats();
  }
}

async function clearCookies() {
  const msgEl = document.getElementById("inject-msg");
  const res = await apiFetch("/admin/clear-cookies", { method: "POST" });
  if (res && res.ok) {
    msgEl.textContent = "✅ Cookies נוקו";
    msgEl.style.color = "var(--success)";
    await loadCookieStatus();
  } else {
    msgEl.textContent = "❌ שגיאה בניקוי";
    msgEl.style.color = "var(--error)";
  }
  setTimeout(() => { msgEl.textContent = ""; }, 3000);
}

async function injectCookiesAndRun() {
  const raw = document.getElementById("cookies-input").value.trim();
  const msgEl = document.getElementById("inject-msg");
  if (!raw) { msgEl.textContent = "❌ הדבק cookies תחילה"; msgEl.style.color = "var(--error)"; return; }
  let cookies;
  try { cookies = JSON.parse(raw); } catch(e) { msgEl.textContent = "❌ JSON לא תקין"; msgEl.style.color = "var(--error)"; return; }
  msgEl.textContent = "שולח..."; msgEl.style.color = "var(--text-muted)";
  const res = await apiFetch("/admin/inject-cookies", {
    method: "POST",
    body: JSON.stringify({ cookies }),
  });
  if (res && res.ok) {
    const data = await res.json();
    msgEl.textContent = "✅ " + data.message;
    msgEl.style.color = "var(--success)";
    await loadCookieStatus();
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    msgEl.textContent = "❌ " + (err.detail || "שגיאה");
    msgEl.style.color = "var(--error)";
  }
}

async function triggerCheck() {
  const btn = document.getElementById("run-check-btn");
  const msg = document.getElementById("check-msg");
  btn.disabled = true; btn.textContent = "מריץ...";
  const res = await apiFetch("/admin/trigger-check", { method: "POST" });
  btn.disabled = false; btn.textContent = "▶ הרץ בדיקה עכשיו";
  if (res && res.ok) {
    msg.textContent = "✅ בדיקה הופעלה!";
    setTimeout(() => { msg.textContent = ""; }, 4000);
  }
}

async function triggerSummary() {
  const btn = document.getElementById("run-summary-btn");
  const msg = document.getElementById("check-msg");
  btn.disabled = true; btn.textContent = "שולח...";
  const res = await apiFetch("/admin/trigger-summary", { method: "POST" });
  btn.disabled = false; btn.textContent = "📧 שלח סיכום עכשיו";
  if (res && res.ok) {
    msg.textContent = "✅ סיכום יומי הופעל!";
    setTimeout(() => { msg.textContent = ""; }, 4000);
  }
}

async function changePassword() {
  const current = document.getElementById("pw-current").value;
  const newPw = document.getElementById("pw-new").value;
  const msgEl = document.getElementById("pw-msg");
  msgEl.textContent = ""; msgEl.className = "profile-msg";

  if (!current || !newPw) { msgEl.textContent = "יש למלא את כל השדות"; msgEl.className = "profile-msg err"; return; }

  const res = await apiFetch("/admin/profile/password", {
    method: "PATCH",
    body: JSON.stringify({ current_password: current, new_password: newPw }),
  });
  if (!res) return;
  const data = await res.json();
  if (res.ok) {
    msgEl.textContent = "✅ " + data.message;
    msgEl.className = "profile-msg ok";
    document.getElementById("pw-current").value = "";
    document.getElementById("pw-new").value = "";
  } else {
    msgEl.textContent = "❌ " + (data.detail || "שגיאה");
    msgEl.className = "profile-msg err";
  }
}

async function requestEmailChange() {
  const newEmail = document.getElementById("email-new").value.trim();
  const pw = document.getElementById("email-pw").value;
  const msgEl = document.getElementById("email-msg");
  msgEl.textContent = ""; msgEl.className = "profile-msg";

  if (!newEmail || !pw) { msgEl.textContent = "יש למלא את כל השדות"; msgEl.className = "profile-msg err"; return; }

  const res = await apiFetch("/admin/profile/request-email-change", {
    method: "POST",
    body: JSON.stringify({ new_email: newEmail, current_password: pw }),
  });
  if (!res) return;
  const data = await res.json();
  if (res.ok) {
    msgEl.textContent = "✅ " + data.message;
    msgEl.className = "profile-msg ok";
  } else {
    msgEl.textContent = "❌ " + (data.detail || "שגיאה");
    msgEl.className = "profile-msg err";
  }
}
