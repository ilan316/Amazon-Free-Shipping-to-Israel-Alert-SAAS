function switchTab(name, btn) {
  document.querySelectorAll(".admin-tab").forEach(t => t.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
  document.getElementById("tab-" + name).classList.add("active");
  btn.classList.add("active");
}

async function loadAdminData() {
  const meRes = await apiFetch("/me");
  if (!meRes || !meRes.ok) return;
  const me = await meRes.json();
  if (!me.is_admin) {
    document.querySelector(".admin-page").innerHTML =
      '<div style="text-align:center;padding:80px;color:var(--error);font-size:1.2rem;">⛔ גישה אסורה — אין הרשאת מנהל</div>';
    return;
  }
  await Promise.all([loadStats(), loadUsers(), loadProducts()]);
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

async function loadUsers() {
  const res = await apiFetch("/admin/users");
  if (!res || !res.ok) return;
  const users = await res.json();
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
  if (!products.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted);padding:24px;">אין מוצרים</td></tr>';
    return;
  }
  tbody.innerHTML = products.map(p => `
    <tr>
      <td class="ltr"><a href="${p.url}" target="_blank">${p.asin}</a></td>
      <td class="truncate">${p.name || "—"}</td>
      <td><span class="status-badge badge-${p.last_status}">${statusLabel(p.last_status)}</span></td>
      <td style="text-align:center;">${p.watchers}</td>
      <td class="ltr">${p.last_checked ? formatDate(p.last_checked) : "—"}</td>
      <td style="text-align:center;color:${p.consecutive_errors > 0 ? 'var(--error)' : 'var(--text-muted)'}">
        ${p.consecutive_errors}
      </td>
    </tr>
  `).join("");
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
    const err = await res.json().catch(() => ({}));
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
