// service_worker.js — תקשורת עם ה-API של amzfreeil.com

const API_BASE = 'https://app.amzfreeil.com';

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'login') {
    handleLogin(request.email, request.password).then(sendResponse);
    return true; // async
  }

  if (request.action === 'addProduct') {
    handleAddProduct(request.url_or_asin, request.token).then(sendResponse);
    return true; // async
  }

  if (request.action === 'logout') {
    chrome.storage.local.remove('token', () => sendResponse({ ok: true }));
    return true;
  }
});

async function handleLogin(email, password) {
  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) {
      return { ok: false, error: data.detail || 'שגיאה בהתחברות' };
    }
    await chrome.storage.local.set({ token: data.access_token });
    return { ok: true, token: data.access_token };
  } catch (e) {
    return { ok: false, error: 'בעיית חיבור לשרת' };
  }
}

async function handleAddProduct(url_or_asin, token) {
  try {
    const res = await fetch(`${API_BASE}/me/products`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${token}`,
      },
      body: JSON.stringify({ url_or_asin }),
    });
    const data = await res.json();
    if (!res.ok) {
      return { ok: false, error: data.detail || 'שגיאה בהוספת מוצר' };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: 'בעיית חיבור לשרת' };
  }
}
