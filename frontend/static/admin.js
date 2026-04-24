function switchTab(name, btn) {
  document.querySelectorAll(".admin-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.getElementById("tab-" + name).classList.add("active");
  btn.classList.add("active");
}

let _allUsers = [];
let _allAdminProducts = [];
let _adminFilter = 'ALL';
let _userFilter = 'ALL';

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
    loadClickStats(),
    loadTemplates(),
    loadSendLogs(),
  ]);
}

async function loadClickStats() {
  const days = document.getElementById("clicks-days")?.value || 7;
  const res = await apiFetch(`/admin/clicks?days=${days}`);
  const summary = document.getElementById("clicks-summary");
  const byAsin = document.getElementById("clicks-by-asin");
  if (!res || !res.ok) { if (summary) summary.textContent = "שגיאה בטעינה"; return; }
  const data = await res.json();
  if (summary) summary.innerHTML = `סה"כ <strong>${data.total}</strong> לחיצות ב-${data.days} הימים האחרונים`;
  if (byAsin) {
    if (!data.recent || !data.recent.length) {
      byAsin.innerHTML = '<span style="color:var(--text-muted)">אין לחיצות עדיין</span>';
    } else {
      byAsin.innerHTML = `
        <table style="width:100%;border-collapse:collapse;font-size:0.82rem;margin-top:8px;">
          <thead>
            <tr style="border-bottom:2px solid var(--border);color:var(--text-muted);">
              <th style="text-align:right;padding:6px 8px;">משתמש</th>
              <th style="text-align:right;padding:6px 8px;">מקור</th>
              <th style="text-align:right;padding:6px 8px;">מתי</th>
              <th style="text-align:right;padding:6px 8px;">IP</th>
            </tr>
          </thead>
          <tbody>
            ${data.recent.map(r => `
              <tr style="border-bottom:1px solid var(--border);" id="click-row-${r.id}">
                <td style="padding:6px 8px;">${r.user_email}</td>
                <td style="padding:6px 8px;">${({"automation_activation":"x","automation_reminder":"x","automation_expansion":"x","cta":"x"})[r.asin] ? `<span style="color:var(--brand-dark);font-size:0.8rem;">${({"automation_activation":"הפעלה 📨","automation_reminder":"תזכורת 🔔","automation_expansion":"הרחבה 📦","cta":"📧 מייל אוטומציה"})[r.asin]}</span>` : `<a href="https://www.amazon.com/dp/${r.asin}" target="_blank" style="color:var(--brand-dark);font-family:monospace;">${r.asin}</a>`}</td>
                <td style="padding:6px 8px;white-space:nowrap;">${r.clicked_at}</td>
                <td style="padding:6px 8px;font-family:monospace;color:var(--text-muted);">${r.ip}</td>
                <td style="padding:6px 8px;"><button onclick="deleteClick(${r.id})" style="background:none;border:none;cursor:pointer;color:var(--error);font-size:1rem;" title="מחק">🗑</button></td>
              </tr>`).join("")}
          </tbody>
        </table>`;
    }
  }
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
  if (!canvas) return;
  if (!data.length) {
    canvas.style.display = "none";
    const msg = document.createElement("p");
    msg.textContent = "אין נתונים עדיין";
    msg.style.cssText = "text-align:center;color:var(--text-muted);padding:24px 0;margin:0;";
    canvas.parentNode.appendChild(msg);
    return;
  }
  // Destroy existing chart instance if present (prevents "canvas already in use" error)
  Chart.getChart(canvas)?.destroy();
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

function setUserFilter(filter, btn) {
  _userFilter = filter;
  document.querySelectorAll('.user-filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  filterUsers();
}

function filterUsers() {
  const q = (document.getElementById('users-search')?.value || '').trim().toLowerCase();
  _allUsers.forEach(u => {
    const row = document.getElementById(`user-row-${u.id}`);
    if (!row) return;
    const matchText = !q || u.email.toLowerCase().includes(q) || (u.notify_email || '').toLowerCase().includes(q);
    const matchFilter =
      _userFilter === 'ALL' ||
      (_userFilter === 'ACTIVE'   &&  u.is_active && !u.vacation_mode) ||
      (_userFilter === 'VACATION' &&  u.is_active &&  u.vacation_mode) ||
      (_userFilter === 'INACTIVE' && !u.is_active);
    row.style.display = (matchText && matchFilter) ? '' : 'none';
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
        ${!u.is_active
          ? '<span class="tag-inactive">מושהה</span>'
          : u.vacation_mode
            ? '<span style="color:var(--warning,#f59e0b);font-weight:600;">🏖 חופשה</span>'
            : '<span style="color:var(--success);font-weight:600;">פעיל</span>'}
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
  filterUsers();
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

function exportUsersCSV() {
  if (!_allUsers.length) return;
  const headers = ['#', 'אימייל', 'אימייל התראה', 'מוצרים', 'סטטוס', 'נרשם'];
  const rows = _allUsers.map(u => [
    u.id,
    u.email,
    u.notify_email || '',
    u.product_count,
    !u.is_active ? 'מושהה' : u.vacation_mode ? 'חופשה' : 'פעיל',
    u.created_at ? new Date(u.created_at).toLocaleDateString('he-IL') : '',
  ]);
  _downloadCSV('users.csv', headers, rows);
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
  const msg = document.getElementById("test-msg");
  const to = document.getElementById("test-target-email")?.value.trim();
  btn.disabled = true; btn.textContent = "שולח...";
  const url = to ? `/admin/trigger-summary?to=${encodeURIComponent(to)}` : "/admin/trigger-summary";
  const res = await apiFetch(url, { method: "POST" });
  btn.disabled = false; btn.textContent = "📧 שלח סיכום יומי";
  if (res) {
    const data = await res.json().catch(() => ({}));
    msg.textContent = data.message || (res.ok ? "✅ נשלח!" : "❌ שגיאה");
    setTimeout(() => { msg.textContent = ""; }, 5000);
  }
}

async function triggerAutomation() {
  const btn = document.getElementById("run-automation-btn");
  const msg = document.getElementById("test-msg");
  btn.disabled = true; btn.textContent = "מריץ...";
  const res = await apiFetch("/admin/trigger-automation", { method: "POST" });
  btn.disabled = false; btn.textContent = "⚡ הרץ אוטומציה עכשיו";
  if (res && res.ok) {
    msg.textContent = "✅ אוטומציה הופעלה!";
    setTimeout(() => { msg.textContent = ""; }, 4000);
  }
}

async function deleteClick(id) {
  const res = await apiFetch(`/admin/clicks/${id}`, { method: "DELETE" });
  if (res && res.ok) {
    document.getElementById(`click-row-${id}`)?.remove();
  }
}

async function sendTestClickEmail() {
  const btn = document.getElementById("run-test-click-btn");
  const msg = document.getElementById("test-msg");
  const to = document.getElementById("test-target-email")?.value.trim();
  btn.disabled = true; btn.textContent = "שולח...";
  const url = to ? `/admin/send-test-click-email?to=${encodeURIComponent(to)}` : "/admin/send-test-click-email";
  const res = await apiFetch(url, { method: "POST" });
  btn.disabled = false; btn.textContent = "🧪 שלח מייל בדיקת לחיצה";
  if (res && res.ok) {
    const data = await res.json().catch(() => ({}));
    msg.textContent = `✅ מייל בדיקה נשלח ל-${data.to || ""}!`;
    setTimeout(() => { msg.textContent = ""; }, 6000);
  } else {
    msg.textContent = "❌ שגיאה בשליחה";
    setTimeout(() => { msg.textContent = ""; }, 4000);
  }
}

// ── Email Templates ──────────────────────────────────────────────────────────

let _editingTemplateId = null;

async function loadTemplates() {
  const res = await apiFetch("/admin/email-templates");
  const tbody = document.getElementById("tpl-list-body");
  if (!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--error);padding:20px;">שגיאה בטעינה</td></tr>'; return; }
  const templates = await res.json();
  if (!templates.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted);padding:24px;">אין תבניות עדיין — לחץ "+ תבנית חדשה"</td></tr>';
    return;
  }
  tbody.innerHTML = templates.map(t => `
    <tr id="tpl-row-${t.id}" class="${_editingTemplateId === t.id ? 'selected' : ''}">
      <td style="font-weight:600;">${t.name}</td>
      <td style="color:var(--text-muted);font-size:0.85rem;">${t.subject}</td>
      <td class="ltr" style="font-size:0.82rem;color:var(--text-muted);white-space:nowrap;">${t.created_at ? new Date(t.created_at).toLocaleDateString("he-IL") : "—"}</td>
      <td>
        <div class="action-btns">
          <button class="btn-sm" onclick="editTemplate(${t.id})">✏️ ערוך</button>
          <button class="btn-sm active-toggle" onclick="editTemplate(${t.id}, true)">📤 שלח</button>
          <button class="btn-sm danger" onclick="deleteTemplate(${t.id})">מחק</button>
        </div>
      </td>
    </tr>`).join("");
}

function newTemplate() {
  _editingTemplateId = null;
  document.getElementById("tpl-editing-id").value = "";
  document.getElementById("tpl-editor-title").textContent = "✏️ תבנית חדשה";
  document.getElementById("tpl-name").value = "";
  document.getElementById("tpl-subject").value = "";
  document.getElementById("tpl-body").value = "";
  document.getElementById("tpl-save-msg").textContent = "";
  document.getElementById("tpl-send-panel").style.display = "none";
  document.getElementById("tpl-open-send-btn").style.display = "none";
  document.getElementById("tpl-editor-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function editTemplate(id, openSend = false) {
  const res = await apiFetch("/admin/email-templates");
  if (!res || !res.ok) return;
  const templates = await res.json();
  const t = templates.find(x => x.id === id);
  if (!t) return;
  _editingTemplateId = id;
  document.getElementById("tpl-editing-id").value = id;
  document.getElementById("tpl-editor-title").textContent = `✏️ עריכה: ${t.name}`;
  document.getElementById("tpl-name").value = t.name;
  document.getElementById("tpl-subject").value = t.subject;
  document.getElementById("tpl-body").value = t.body;
  document.getElementById("tpl-save-msg").textContent = "";
  document.getElementById("tpl-open-send-btn").style.display = "";
  document.querySelectorAll("#tpl-list-body tr").forEach(el => el.classList.remove("selected"));
  document.getElementById(`tpl-row-${id}`)?.classList.add("selected");
  if (openSend) {
    document.getElementById("tpl-send-panel").style.display = "";
    document.getElementById("tpl-audience").value = "all";
    document.getElementById("tpl-single-user-id").style.display = "none";
    document.getElementById("tpl-send-msg").textContent = "";
  } else {
    document.getElementById("tpl-send-panel").style.display = "none";
  }
  document.getElementById("tpl-editor-wrap").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function saveTemplate() {
  const id = document.getElementById("tpl-editing-id").value;
  const name = document.getElementById("tpl-name").value.trim();
  const subject = document.getElementById("tpl-subject").value.trim();
  const body = document.getElementById("tpl-body").value;
  const msgEl = document.getElementById("tpl-save-msg");
  if (!name || !subject || !body) { msgEl.textContent = "❌ יש למלא שם, נושא ותוכן"; msgEl.style.color = "var(--error)"; return; }

  const method = id ? "PUT" : "POST";
  const url = id ? `/admin/email-templates/${id}` : "/admin/email-templates";
  const res = await apiFetch(url, { method, body: JSON.stringify({ name, subject, body }) });
  if (res && res.ok) {
    const data = await res.json();
    if (!id) {
      _editingTemplateId = data.id;
      document.getElementById("tpl-editing-id").value = data.id;
      document.getElementById("tpl-editor-title").textContent = `✏️ עריכה: ${name}`;
    }
    document.getElementById("tpl-open-send-btn").style.display = "";
    msgEl.textContent = "✅ נשמר בהצלחה";
    msgEl.style.color = "var(--success)";
    await loadTemplates();
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    msgEl.textContent = "❌ " + (err.detail || "שגיאה");
    msgEl.style.color = "var(--error)";
  }
  setTimeout(() => { msgEl.textContent = ""; }, 3000);
}

async function deleteTemplate(id) {
  if (!confirm("למחוק תבנית זו לצמיתות?")) return;
  const res = await apiFetch(`/admin/email-templates/${id}`, { method: "DELETE" });
  if (res && res.ok) {
    if (_editingTemplateId === id) newTemplate();
    await loadTemplates();
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    alert(err.detail || "שגיאה במחיקה");
  }
}

function previewTemplate() {
  const html = document.getElementById("tpl-body").value;
  const frame = document.getElementById("tpl-preview-frame");
  if (!html || !frame) return;
  const doc = frame.contentDocument || frame.contentWindow.document;
  doc.open(); doc.write(html); doc.close();
}

function openSendPanel(id) {
  if (id) {
    editTemplate(id, true);
  } else {
    document.getElementById("tpl-send-panel").style.display = "";
    document.getElementById("tpl-audience").value = "all";
    document.getElementById("tpl-single-user-id").style.display = "none";
    document.getElementById("tpl-send-msg").textContent = "";
  }
}

function toggleSingleUser() {
  const v = document.getElementById("tpl-audience").value;
  document.getElementById("tpl-single-user-id").style.display = v === "single" ? "" : "none";
  document.getElementById("tpl-custom-emails-wrap").style.display = v === "custom" ? "" : "none";
}

function _parseCustomEmails() {
  const raw = document.getElementById("tpl-custom-emails").value;
  const emails = raw.split(/[\n,]+/).map(e => e.trim().toLowerCase()).filter(e => e.includes("@"));
  const countEl = document.getElementById("tpl-custom-count");
  if (countEl) countEl.textContent = emails.length ? `${emails.length} כתובות` : "";
  return emails;
}

async function sendTemplate() {
  const id = document.getElementById("tpl-editing-id").value;
  if (!id) { alert("יש לשמור את התבנית תחילה"); return; }
  const audience = document.getElementById("tpl-audience").value;
  const user_id = audience === "single" ? parseInt(document.getElementById("tpl-single-user-id").value) : null;
  if (audience === "single" && !user_id) { alert("יש להזין User ID"); return; }

  let custom_emails = null;
  if (audience === "custom") {
    custom_emails = _parseCustomEmails();
    if (!custom_emails.length) { alert("יש להזין לפחות כתובת מייל אחת"); return; }
  }

  const minVal = document.getElementById("tpl-products-min").value;
  const maxVal = document.getElementById("tpl-products-max").value;
  const products_min = minVal !== "" ? parseInt(minVal) : null;
  const products_max = maxVal !== "" ? parseInt(maxVal) : null;

  const filterDesc = [];
  if (products_min !== null) filterDesc.push(`מינ' ${products_min} מוצרים`);
  if (products_max !== null) filterDesc.push(`מקס' ${products_max} מוצרים`);
  const label = { all: "כל המשתמשים", active: "הפעילים", vacation: "במצב חופשה", inactive: "המושהים", single: `משתמש #${user_id}`, custom: `${custom_emails?.length} כתובות מותאמות` }[audience] || audience;
  const filterStr = filterDesc.length ? ` (${filterDesc.join(", ")})` : "";
  if (!confirm(`לשלוח מייל זה ל${label}${filterStr}?`)) return;

  const btn = document.getElementById("tpl-send-btn");
  const msgEl = document.getElementById("tpl-send-msg");
  const progressEl = document.getElementById("tpl-send-progress");
  btn.disabled = true; btn.textContent = "מתחיל...";
  msgEl.textContent = "";

  const res = await apiFetch(`/admin/email-templates/${id}/send`, {
    method: "POST",
    body: JSON.stringify({ audience, user_id, products_min, products_max, custom_emails }),
  });

  if (!res || !res.ok) {
    btn.disabled = false; btn.textContent = "📤 שלח עכשיו";
    const err = res ? await res.json().catch(() => ({})) : {};
    msgEl.textContent = "❌ " + (err.detail || "שגיאה בשליחה");
    msgEl.style.color = "var(--error)";
    return;
  }

  const data = await res.json();
  if (!data.job_id) {
    btn.disabled = false; btn.textContent = "📤 שלח עכשיו";
    msgEl.textContent = "ℹ️ " + (data.message || "אין משתמשים");
    msgEl.style.color = "var(--text-muted)";
    return;
  }

  // Show progress panel
  progressEl.style.display = "";
  document.getElementById("prog-total").textContent = data.total;
  document.getElementById("prog-sent").textContent = "0";
  document.getElementById("prog-failed").textContent = "0";
  document.getElementById("prog-remaining").textContent = data.total;
  document.getElementById("prog-bar").style.width = "0%";
  document.getElementById("prog-eta").textContent = `~${Math.round(data.total * 0.55)} שניות`;

  // Poll progress
  const pollInterval = setInterval(async () => {
    const pRes = await apiFetch(`/admin/send-progress/${data.job_id}`);
    if (!pRes || !pRes.ok) return;
    const p = await pRes.json();

    const done = p.sent + p.failed;
    const pct = data.total > 0 ? Math.round((done / data.total) * 100) : 0;
    const etaSec = Math.round(p.remaining * 0.55);

    document.getElementById("prog-bar").style.width = pct + "%";
    document.getElementById("prog-sent").textContent = p.sent;
    document.getElementById("prog-failed").textContent = p.failed;
    document.getElementById("prog-remaining").textContent = p.remaining;
    document.getElementById("prog-eta").textContent = p.done ? "✅ הושלם" : `~${etaSec} שניות`;

    if (p.done) {
      clearInterval(pollInterval);
      btn.disabled = false; btn.textContent = "📤 שלח עכשיו";
      msgEl.textContent = "✅ " + p.message;
      msgEl.style.color = "var(--success)";
      document.getElementById("prog-bar").style.background = p.failed > 0 ? "var(--warning,#f59e0b)" : "var(--success)";
      await Promise.all([loadTemplates(), loadSendLogs()]);
      setTimeout(() => { progressEl.style.display = "none"; msgEl.textContent = ""; }, 6000);
    }
  }, 500);
}

async function loadSendLogs() {
  const tbody = document.getElementById("send-log-body");
  if (!tbody) return;
  const days = document.getElementById("send-log-days")?.value || 30;
  const res = await apiFetch(`/admin/email-send-logs?days=${days}`);
  if (!res || !res.ok) { tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--error);padding:16px;">שגיאה בטעינה</td></tr>'; return; }
  const logs = await res.json();
  if (!logs.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:20px;">לא נשלחו מיילים עדיין</td></tr>';
    return;
  }
  const audienceLabel = { all: "כל המשתמשים", active: "פעילים", vacation: "חופשה", inactive: "מושהים", single: "בודד", self: "בדיקה" };
  tbody.innerHTML = logs.map(l => {
    return `
    <tr style="cursor:pointer;" onclick="toggleSendLogDetail(${l.id}, ${l.template_id}, '${new Date(l.sent_at).toISOString()}', this)">
      <td class="ltr" style="white-space:nowrap;font-size:0.82rem;">
        <span style="font-size:0.75rem;color:var(--text-muted);margin-left:4px;">▶</span>
        ${new Date(l.sent_at).toLocaleString("he-IL", { day:"2-digit", month:"2-digit", year:"2-digit", hour:"2-digit", minute:"2-digit" })}
      </td>
      <td style="font-weight:600;">${l.template_name}</td>
      <td style="font-size:0.82rem;color:var(--text-muted);">${audienceLabel[l.audience] || l.audience}</td>
      <td style="text-align:center;">
        <span style="font-weight:600;">${l.sent_count + l.failed_count}</span>
        <span style="font-size:0.78rem;color:var(--text-muted);"> ניסיונות</span><br>
        <span style="font-size:0.78rem;color:var(--success);">✅ ${l.sent_count}</span>
        ${l.failed_count > 0 ? `<span style="font-size:0.78rem;color:var(--error);margin-right:6px;">❌ ${l.failed_count}</span>` : ""}
      </td>
      <td style="text-align:center;font-weight:600;">${l.clicks > 0 ? `🖱 ${l.clicks}` : "—"}</td>
      <td style="text-align:center;" onclick="event.stopPropagation()">
        <button onclick="deleteSendLog(${l.id})" style="background:none;border:none;cursor:pointer;color:var(--error);font-size:1rem;padding:2px 6px;" title="מחק">🗑</button>
      </td>
    </tr>
    <tr id="send-log-detail-${l.id}" style="display:none;">
      <td colspan="6" style="padding:0;background:var(--bg);">
        <div id="send-log-detail-inner-${l.id}" style="padding:12px 20px;font-size:0.82rem;"></div>
      </td>
    </tr>`;
  }).join("");
}

async function deleteSendLog(logId) {
  if (!confirm("למחוק רשומה זו?")) return;
  const res = await apiFetch(`/admin/email-send-logs/${logId}`, { method: "DELETE" });
  if (res && res.ok) {
    document.getElementById(`send-log-detail-${logId}`)?.remove();
    document.querySelector(`tr[onclick*="toggleSendLogDetail(${logId},"]`)?.remove();
  }
}

async function toggleSendLogDetail(logId, templateId, sentAt, clickedRow) {
  const detailRow = document.getElementById(`send-log-detail-${logId}`);
  const inner = document.getElementById(`send-log-detail-inner-${logId}`);
  const arrow = clickedRow.querySelector("span");

  if (detailRow.style.display !== "none") {
    detailRow.style.display = "none";
    if (arrow) arrow.textContent = "▶";
    return;
  }
  detailRow.style.display = "";
  if (arrow) arrow.textContent = "▼";
  inner.textContent = "טוען...";

  const res = await apiFetch(`/admin/email-send-logs/${logId}/recipients`);
  if (!res || !res.ok) { inner.textContent = "שגיאה בטעינה"; return; }
  const recipients = await res.json();

  if (!recipients.length) {
    inner.innerHTML = '<span style="color:var(--text-muted);">אין נתוני נמענים לשליחה זו (נשלחה לפני שמירת נמענים)</span>';
    return;
  }

  const failed = recipients.filter(r => !r.success);

  const rowBg = r => {
    if (!r.success) return "#fef2f2";
    if (r.clicked) return "#f0fdf4";
    return "";
  };

  inner.innerHTML = `
    <div style="display:flex;justify-content:center;">
    <table style="border-collapse:collapse;font-size:0.82rem;margin-bottom:8px;min-width:320px;max-width:520px;width:auto;">
      <thead>
        <tr style="border-bottom:2px solid var(--border);color:var(--text-muted);font-size:0.75rem;">
          <th style="text-align:right;padding:4px 10px;font-weight:600;">מייל</th>
          <th style="text-align:center;padding:4px 10px;font-weight:600;width:56px;">נשלח</th>
          <th style="text-align:center;padding:4px 10px;font-weight:600;width:56px;">לחץ</th>
        </tr>
      </thead>
      <tbody>
        ${recipients.map(r => `
          <tr style="border-bottom:1px solid var(--border);background:${rowBg(r)};">
            <td style="padding:4px 10px;direction:ltr;text-align:left;">${r.email}</td>
            <td style="text-align:center;padding:4px 10px;">${r.success ? "✅" : "❌"}</td>
            <td style="text-align:center;padding:4px 10px;">${r.clicked ? "✅" : "—"}</td>
          </tr>`).join("")}
      </tbody>
    </table>
    </div>
    ${failed.length ? `
      <button class="btn-run-check" id="resend-btn-${logId}" onclick="resendFailed(${logId})"
        style="font-size:0.82rem;padding:7px 16px;">
        🔄 שלח מחדש לנכשלים (${failed.length})
      </button>
      <span id="resend-msg-${logId}" style="font-size:0.82rem;display:block;margin-top:6px;"></span>
    ` : ""}`;
}

async function resendFailed(logId) {
  const btn = document.getElementById(`resend-btn-${logId}`);
  const msg = document.getElementById(`resend-msg-${logId}`);
  btn.disabled = true; btn.textContent = "שולח...";
  const res = await apiFetch(`/admin/email-send-logs/${logId}/resend-failed`, { method: "POST" });
  btn.disabled = false; btn.textContent = `🔄 שלח מחדש`;
  if (res && res.ok) {
    const data = await res.json();
    msg.textContent = "✅ " + data.message;
    msg.style.color = "var(--success)";
    await loadSendLogs();
  } else {
    const err = res ? await res.json().catch(() => ({})) : {};
    msg.textContent = "❌ " + (err.detail || "שגיאה");
    msg.style.color = "var(--error)";
  }
}

async function loadTemplateOpens(templateId) {
  const cell = document.getElementById(`tpl-opens-cell-${templateId}`);
  if (!cell) return;
  cell.textContent = "טוען...";
  const res = await apiFetch(`/admin/email-templates/${templateId}/opens`);
  if (!res || !res.ok) { cell.textContent = "שגיאה"; return; }
  const data = await res.json();
  if (!data.total_opens) {
    cell.innerHTML = '<span style="color:var(--text-muted);">0 פתיחות</span>';
    return;
  }
  const list = data.opens.slice(0, 5).map(o =>
    `<div style="font-size:0.75rem;color:var(--text-muted);">${o.email} · ${new Date(o.opened_at).toLocaleString("he-IL")}</div>`
  ).join("");
  const more = data.total_opens > 5 ? `<div style="font-size:0.72rem;color:var(--text-muted);">+${data.total_opens - 5} נוספים</div>` : "";
  cell.innerHTML = `
    <div style="font-weight:600;font-size:0.85rem;">👁 ${data.total_opens} פתיחות · ${data.unique_openers} ייחודיות</div>
    ${list}${more}`;
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
