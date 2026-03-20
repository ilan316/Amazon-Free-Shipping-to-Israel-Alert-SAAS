// content.js — רץ בתוך דף אמזון, שולף מידע על המוצר

function extractASIN() {
  // מ-URL
  const urlMatch = window.location.pathname.match(/\/dp\/([A-Z0-9]{10})/i)
    || window.location.pathname.match(/\/gp\/product\/([A-Z0-9]{10})/i);
  if (urlMatch) return urlMatch[1].toUpperCase();

  // מ-DOM (hidden input)
  const asinInput = document.getElementById('ASIN');
  if (asinInput?.value) return asinInput.value.toUpperCase();

  return null;
}

function extractTitle() {
  return document.getElementById('productTitle')?.innerText?.trim() || null;
}

function extractPrice() {
  const whole = document.querySelector('.a-price-whole')?.innerText?.replace(/[^0-9]/g, '');
  const fraction = document.querySelector('.a-price-fraction')?.innerText?.replace(/[^0-9]/g, '');
  if (whole) {
    return fraction ? `${whole}.${fraction}` : whole;
  }
  return null;
}

// מאזין להודעות מה-popup
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'getProductInfo') {
    sendResponse({
      asin: extractASIN(),
      title: extractTitle(),
      price: extractPrice(),
      url: window.location.href,
    });
  }
});
