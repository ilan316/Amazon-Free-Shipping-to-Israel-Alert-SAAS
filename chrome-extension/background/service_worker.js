// service_worker.js — תקשורת עם ה-API של amzfreeil.com

const API_BASE = 'https://app.amzfreeil.com';
const INACTIVITY_DAYS = 30;

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  if (request.action === 'login') {
    handleLogin(request.email, request.password).then(sendResponse);
    return true;
  }

  if (request.action === 'addProduct') {
    handleAddProduct(request.url_or_asin, request.token).then(sendResponse);
    return true;
  }

  if (request.action === 'logout') {
    chrome.storage.local.remove(['token', 'lastActivity'], () => sendResponse({ ok: true }));
    return true;
  }

  if (request.action === 'googleLogin') {
    handleGoogleLogin().then(sendResponse);
    return true;
  }

  if (request.action === 'updateActivity') {
    chrome.storage.local.set({ lastActivity: Date.now() }, () => sendResponse({ ok: true }));
    return true;
  }

  if (request.action === 'checkActivity') {
    chrome.storage.local.get(['token', 'lastActivity'], (data) => {
      if (!data.token) { sendResponse({ valid: false }); return; }
      const last = data.lastActivity || 0;
      const expired = (Date.now() - last) > INACTIVITY_DAYS * 24 * 60 * 60 * 1000;
      if (expired) {
        chrome.storage.local.remove(['token', 'lastActivity'], () => sendResponse({ valid: false }));
      } else {
        chrome.storage.local.set({ lastActivity: Date.now() }, () => sendResponse({ valid: true, token: data.token }));
      }
    });
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
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      return { ok: false, error: data.detail || 'שגיאה בהתחברות' };
    }
    await chrome.storage.local.set({ token: data.access_token, lastActivity: Date.now() });
    return { ok: true, token: data.access_token };
  } catch (e) {
    return { ok: false, error: 'בעיית חיבור לשרת' };
  }
}

async function handleGoogleLogin() {
  try {
    const configRes = await fetch(`${API_BASE}/auth/google-config`);
    if (!configRes.ok) return { ok: false, error: 'Google login not available' };
    const { client_id } = await configRes.json();

    const redirectUrl = chrome.identity.getRedirectURL();
    const authUrl =
      `https://accounts.google.com/o/oauth2/v2/auth` +
      `?client_id=${encodeURIComponent(client_id)}` +
      `&response_type=token` +
      `&redirect_uri=${encodeURIComponent(redirectUrl)}` +
      `&scope=email%20profile`;

    return new Promise((resolve) => {
      chrome.identity.launchWebAuthFlow({ url: authUrl, interactive: true }, async (redirectedTo) => {
        if (chrome.runtime.lastError || !redirectedTo) {
          resolve({ ok: false, error: chrome.runtime.lastError?.message || 'ביטול התחברות' });
          return;
        }
        const hash = new URL(redirectedTo).hash.substring(1);
        const params = new URLSearchParams(hash);
        const accessToken = params.get('access_token');
        if (!accessToken) {
          resolve({ ok: false, error: 'שגיאה בקבלת טוקן מ-Google' });
          return;
        }
        try {
          const res = await fetch(`${API_BASE}/auth/google-extension`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ access_token: accessToken }),
          });
          let data = {};
          try { data = await res.json(); } catch (_) {}
          if (!res.ok) {
            resolve({ ok: false, error: data.detail || 'שגיאה בהתחברות' });
            return;
          }
          await chrome.storage.local.set({ token: data.access_token, lastActivity: Date.now() });
          resolve({ ok: true, token: data.access_token });
        } catch (e) {
          resolve({ ok: false, error: 'בעיית חיבור לשרת' });
        }
      });
    });
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
    let data = {};
    try { data = await res.json(); } catch (_) {}
    if (!res.ok) {
      if (res.status === 409) {
        return { ok: false, alreadyExists: true, error: 'המוצר כבר קיים ברשימה שלך' };
      }
      if (res.status === 401) {
        chrome.storage.local.remove(['token', 'lastActivity']);
        return { ok: false, unauthorized: true, error: 'פג תוקף החיבור — התחבר מחדש' };
      }
      return { ok: false, error: data.detail || `שגיאה בהוספת מוצר (${res.status})` };
    }
    fetch(`${API_BASE}/me/products/check-new`, {
      method: 'POST',
      headers: { 'Authorization': `Bearer ${token}` },
    }).catch(() => {});
    return { ok: true };
  } catch (e) {
    return { ok: false, error: 'בעיית חיבור לשרת' };
  }
}
