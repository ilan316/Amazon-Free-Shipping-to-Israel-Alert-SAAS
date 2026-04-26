// popup.js

const screens = {
  login: document.getElementById('screen-login'),
  product: document.getElementById('screen-product'),
  noProduct: document.getElementById('screen-no-product'),
};

function showScreen(name) {
  Object.values(screens).forEach(s => s.classList.add('hidden'));
  screens[name].classList.remove('hidden');
}

// ── Startup ──────────────────────────────────────────────────────────────────

chrome.runtime.sendMessage({ action: 'checkActivity' }, (res) => {
  if (chrome.runtime.lastError || !res || !res.valid) {
    showScreen('login');
    return;
  }
  loadProductScreen(res.token);
});

// ── Load product screen ───────────────────────────────────────────────────────

async function loadProductScreen(token) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const isAmazon = tab?.url && /amazon\.(com|co\.il)/i.test(tab.url);

  if (!isAmazon) {
    showScreen('noProduct');
    return;
  }

  // שלח הודעה ל-content.js לקבל מידע על המוצר
  chrome.tabs.sendMessage(tab.id, { action: 'getProductInfo' }, (info) => {
    if (!info?.asin) {
      showScreen('noProduct');
      return;
    }

    showScreen('product');

    const titleEl = document.getElementById('product-title');
    const priceEl = document.getElementById('product-price');

    titleEl.textContent = info.title || info.asin;
    priceEl.textContent = info.price ? `$${info.price}` : '';

    // כפתור הוספה
    const addBtn = document.getElementById('add-btn');
    addBtn.addEventListener('click', () => addProduct(info.url, token, addBtn));
  });
}

// ── Add product ───────────────────────────────────────────────────────────────

function addProduct(url, token, btn) {
  btn.disabled = true;
  btn.textContent = 'מוסיף...';

  const resultEl = document.getElementById('add-result');
  resultEl.className = 'result hidden';
  resultEl.textContent = '';

  chrome.runtime.sendMessage(
    { action: 'addProduct', url_or_asin: url, token },
    (res) => {
      btn.disabled = false;
      btn.textContent = '+ הוסף למעקב';

      if (chrome.runtime.lastError || !res) {
        resultEl.textContent = 'שגיאת חיבור — נסה שנית';
        resultEl.className = 'result error';
        resultEl.classList.remove('hidden');
        return;
      }

      resultEl.classList.remove('hidden');

      if (res.ok) {
        resultEl.textContent = '✅ המוצר נוסף למעקב!';
        resultEl.className = 'result success';
        btn.disabled = true;
        btn.textContent = '✅ במעקב';
      } else if (res.alreadyExists) {
        resultEl.textContent = '✅ המוצר כבר קיים ברשימה שלך';
        resultEl.className = 'result success';
        btn.disabled = true;
        btn.textContent = '✅ במעקב';
      } else if (res.unauthorized) {
        resultEl.textContent = 'פג תוקף החיבור — התחבר מחדש';
        resultEl.className = 'result error';
        setTimeout(() => {
          showScreen('login');
          document.getElementById('login-form').reset();
        }, 1500);
      } else {
        resultEl.textContent = res.error || 'שגיאה בהוספת מוצר';
        resultEl.className = 'result error';
      }
    }
  );
}

// ── Login form ────────────────────────────────────────────────────────────────

document.getElementById('login-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const email = document.getElementById('email').value.trim();
  const password = document.getElementById('password').value;
  const btn = document.getElementById('login-btn');
  const errorEl = document.getElementById('login-error');

  btn.disabled = true;
  btn.textContent = 'מתחבר...';
  errorEl.classList.add('hidden');

  chrome.runtime.sendMessage({ action: 'login', email, password }, (res) => {
    btn.disabled = false;
    btn.textContent = 'התחבר';

    if (chrome.runtime.lastError || !res) {
      errorEl.textContent = 'שגיאת חיבור — נסה שנית';
      errorEl.classList.remove('hidden');
      return;
    }

    if (res.ok) {
      loadProductScreen(res.token);
    } else {
      errorEl.textContent = res.error;
      errorEl.classList.remove('hidden');
    }
  });
});

// ── Google Login ──────────────────────────────────────────────────────────

document.getElementById('google-btn').addEventListener('click', () => {
  const btn = document.getElementById('google-btn');
  const errorEl = document.getElementById('login-error');

  btn.disabled = true;
  errorEl.classList.add('hidden');

  chrome.runtime.sendMessage({ action: 'googleLogin' }, (res) => {
    btn.disabled = false;

    if (chrome.runtime.lastError || !res) {
      errorEl.textContent = 'שגיאת חיבור — נסה שנית';
      errorEl.classList.remove('hidden');
      return;
    }

    if (res.ok) {
      loadProductScreen(res.token);
    } else {
      errorEl.textContent = res.error || 'שגיאה בהתחברות עם Google';
      errorEl.classList.remove('hidden');
    }
  });
});

// ── Logout ────────────────────────────────────────────────────────────────────

function logout() {
  chrome.runtime.sendMessage({ action: 'logout' }, () => {
    showScreen('login');
    document.getElementById('login-form').reset();
    document.getElementById('login-error').classList.add('hidden');
  });
}


document.getElementById('logout-btn').addEventListener('click', logout);
document.getElementById('logout-btn-2').addEventListener('click', logout);
