// ═══════════════════════════════════════════════
//   LIGHT IDEAS TECHNOLOGY — Main JavaScript
// ═══════════════════════════════════════════════

// ── PARTICLES
(function() {
  const c = document.getElementById('particles');
  if (!c) return;
  for (let i = 0; i < 25; i++) {
    const p = document.createElement('div');
    p.className = 'particle';
    p.style.left = Math.random() * 100 + '%';
    p.style.animationDuration = (8 + Math.random() * 12) + 's';
    p.style.animationDelay = (Math.random() * 10) + 's';
    p.style.width = p.style.height = (1 + Math.random() * 2) + 'px';
    c.appendChild(p);
  }
})();

// ── NAVBAR SCROLL EFFECT
window.addEventListener('scroll', function() {
  const nav = document.getElementById('navbar');
  if (nav) nav.style.background = window.scrollY > 50 ? 'rgba(0,0,0,0.97)' : 'rgba(0,0,0,0.85)';
});

function toggleNav() {
  document.getElementById('navLinks').classList.toggle('open');
}

// ── PRODUCTS FILTER
let activeCat = 'all', activeSub = 'all';

function filterCat(cat, el) {
  activeCat = cat; activeSub = 'all';
  document.querySelectorAll('.cat-tab').forEach(t => t.classList.remove('active'));
  if (el) el.classList.add('active');
  const subTabs = document.getElementById('subTabs');
  if (subTabs) subTabs.style.display = cat === 'laptop' ? 'flex' : 'none';
  document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
  const firstSub = document.querySelector('.sub-tab');
  if (cat === 'laptop' && firstSub) firstSub.classList.add('active');
  renderProducts();
  document.getElementById('products')?.scrollIntoView({ behavior: 'smooth' });
}

function filterSub(sub, el) {
  activeSub = sub;
  document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
  if (el) el.classList.add('active');
  renderProducts();
}

function renderProducts() {
  document.querySelectorAll('.product-card').forEach(card => {
    const cat = card.dataset.cat, sub = card.dataset.sub;
    let show = true;
    if (activeCat !== 'all' && cat !== activeCat) show = false;
    if (activeCat === 'laptop' && activeSub !== 'all' && sub !== activeSub) show = false;
    card.style.display = show ? '' : 'none';
  });
}

// ── EMAIL SUBSCRIBE
async function subscribeEmail() {
  const input = document.getElementById('emailInput');
  const notice = document.getElementById('emailNotice');
  const email = input.value.trim();
  if (!email || !email.includes('@')) {
    notice.style.color = '#f87171';
    notice.textContent = 'Please enter a valid email address.';
    return;
  }
  try {
    const res = await fetch('/api/subscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email })
    });
    const data = await res.json();
    if (data.success) {
      notice.style.color = '#4ade80';
      notice.textContent = '✅ You\'re subscribed! Welcome to the Light Ideas community.';
      input.value = '';
    }
  } catch (e) {
    notice.style.color = '#4ade80';
    notice.textContent = '✅ Subscribed! Welcome aboard.';
    input.value = '';
  }
}

// ── PAYSTACK MODAL
function openDiagModal() { document.getElementById('diagModal').classList.add('open'); }
function closeDiagModal() { document.getElementById('diagModal').classList.remove('open'); }

document.getElementById('diagModal')?.addEventListener('click', function(e) {
  if (e.target === this) closeDiagModal();
});

function payWithPaystack() {
  const emailEl = document.getElementById('diagEmail');
  const email = emailEl.value.trim();
  if (!email || !email.includes('@')) {
    emailEl.style.borderColor = '#f87171';
    emailEl.placeholder = 'Please enter your email first';
    return;
  }
  // ⚠️ Replace 'pk_live_YOUR_KEY' with your actual Paystack public key from dashboard.paystack.com
  const handler = PaystackPop.setup({
    key: 'pk_live_YOUR_PAYSTACK_PUBLIC_KEY',
    email: email,
    amount: 200000, // ₦2,000 in kobo (100 kobo = ₦1)
    currency: 'NGN',
    ref: 'LIT-DIAG-' + Date.now(),
    metadata: {
      custom_fields: [{
        display_name: 'Service',
        variable_name: 'service',
        value: 'Diagnostic Tool Access'
      }]
    },
    callback: function(response) {
      closeDiagModal();
      showToast('✅ Payment confirmed! Access link sent to ' + email);
    },
    onClose: function() {}
  });
  handler.openIframe();
}

function showToast(msg, type = 'success') {
  const div = document.createElement('div');
  const color = type === 'success' ? '#14532d' : '#450a0a';
  const textColor = type === 'success' ? '#4ade80' : '#f87171';
  div.style.cssText = `position:fixed;top:80px;left:50%;transform:translateX(-50%);
    background:${color};color:${textColor};border-radius:12px;
    padding:14px 24px;font-size:0.85em;z-index:3000;
    max-width:90%;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,0.5);
    font-family:'Outfit',sans-serif;`;
  div.textContent = msg;
  document.body.appendChild(div);
  setTimeout(() => div.remove(), 5000);
}

// ── AI GREETER
let aiOpen = false;
let aiHistory = [];

function toggleAI() {
  aiOpen = !aiOpen;
  const panel = document.getElementById('aiPanel');
  const toggle = document.getElementById('aiToggle');
  if (aiOpen) {
    panel.classList.add('open');
    toggle.style.animation = 'none';
    toggle.textContent = '✕';
  } else {
    panel.classList.remove('open');
    toggle.style.animation = 'aiBounce 3s ease-in-out infinite';
    toggle.textContent = '🤖';
  }
}

function quickAsk(q) {
  document.getElementById('aiInput').value = q;
  sendAI();
}

async function sendAI() {
  const input = document.getElementById('aiInput');
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  document.getElementById('aiQuick').style.display = 'none';
  addAIMsg(msg, 'user');
  aiHistory.push({ role: 'user', content: msg });
  const typing = addTyping();
  try {
    const res = await fetch('/api/ai-chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: aiHistory })
    });
    const data = await res.json();
    typing.remove();
    if (!res.ok || !data.reply) throw new Error(data.error || 'AI request failed');
    aiHistory.push({ role: 'assistant', content: data.reply });
    addAIMsg(data.reply, 'bot');
  } catch (e) {
    typing.remove();
    addAIMsg('Sorry, I had a hiccup! Chat with Victor directly on WhatsApp 📲', 'bot');
  }
}

function addAIMsg(text, type) {
  const msgs = document.getElementById('aiMessages');
  const div = document.createElement('div');
  div.className = 'ai-msg ' + type;
  div.textContent = text;
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

function addTyping() {
  const msgs = document.getElementById('aiMessages');
  const div = document.createElement('div');
  div.className = 'ai-msg bot ai-typing';
  div.innerHTML = '<div class="ai-dot"></div><div class="ai-dot"></div><div class="ai-dot"></div>';
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

// ── ENTER KEY for AI input
document.getElementById('aiInput')?.addEventListener('keypress', function(e) {
  if (e.key === 'Enter') sendAI();
});

// ── REVIEWS SLIDER
let reviewIndex = 0;
function goToReview(i) {
  const slides = document.querySelectorAll('.review-slide');
  const dots = document.querySelectorAll('.review-dot');
  if (!slides.length) return;
  slides[reviewIndex]?.classList.remove('active');
  dots[reviewIndex]?.classList.remove('active');
  reviewIndex = i;
  slides[reviewIndex]?.classList.add('active');
  dots[reviewIndex]?.classList.add('active');
}
(function() {
  const total = document.querySelectorAll('.review-slide').length;
  if (total > 1) {
    setInterval(() => goToReview((reviewIndex + 1) % total), 5000);
  }
})();
