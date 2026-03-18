function switchTab(name, btn) {
  document.querySelectorAll(".admin-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.getElementById("tab-" + name).classList.add("active");
  btn.classList.add("active");
}

let _allUsers = [];

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
    loadCheckInterval(),
    loadCookieStatus(),
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

async function loadCheckInterval() {
  const res = await fetch("/health");
  if (!res.ok) return;
  const data = await res.json();
  // Read current interval from SystemSetting via dedicated endpoint
  const res2 = await apiFetch("/admin/get-check-interval");
  if (!res2 || !res2.ok) return;
  const d = await res2.json();
  const sel = document.getElementById("interval-select");
  if (sel && d.minutes) sel.value = String(d.minutes);
}

async function setCheckInterval() {
  const sel = document.getElementById("interval-select");
  const statusEl = document.getElementById("interval-msg");
  const minutes = parseInt(sel.value);
  const res = await apiFetch("/admin/set-check-interval", {
    method: "POST",
    body: JSON.stringify({ minutes }),
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

async function loadNotificationsLog() {
  const res = await apiFetch("/admin/notifications-log");
  if (!res || !res.ok) return;
  const logs = await res.json();
  const tbody = document.getElementById("notifications-body");
  if (!logs.length) {
    tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:24px;">אין התראות עדיין</td></tr>';
    return;
  }
  tbody.innerHTML = logs.map(l => `
    <tr>
      <td class="ltr">${new Date(l.sent_at).toLocaleString("he-IL")}</td>
      <td class="ltr truncate">${l.user_email}</td>
      <td class="truncate">${l.product_name}</td>
      <td><span class="status-badge badge-${l.status}">${statusLabel(l.status)}</span></td>
      <td style="color:${l.success ? 'var(--success)' : 'var(--error)'}">
        ${l.success ? '✅ נשלח' : '❌ נכשל'}
      </td>
    </tr>
  `).join("");
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
  const res = await apiFetch("/admin/users");
  if (!res || !res.ok) return;
  const users = await res.json();
  _allUsers = users;
  const tbody = document.getElementById("users-body");
  if (!users.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--text-muted);padding:24px;">אין משתמשים</td></tr>';
    return;
  }
  tbody.innerHTML = users.map(u => `
    <tr id="user-row-${u.id}">
      <td>${u.id}</td>
      <td class="ltr truncate">${u.email}
        ${u.is_admin ? ' <span class="tag-admin">מנהל</span>' : ''}
      </td>
      <td class="ltr truncate">${u.notify_email}</td>
      <td style="text-align:center;">${u.product_count}</td>
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
    </tr>
  `).join("");
}

async function loadProducts() {
  const res = await apiFetch("/admin/products");
  if (!res || !res.ok) return;
  const products = await res.json();
  const tbody = document.getElementById("products-body");
  const selectAll = document.getElementById("select-all-products");
  if (selectAll) selectAll.checked = false;
  updateBulkDeleteBtn();
  if (!products.length) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:24px;">אין מוצרים</td></tr>';
    return;
  }
  tbody.innerHTML = products.map(p => `
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
