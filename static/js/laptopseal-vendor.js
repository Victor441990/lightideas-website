(() => {
  'use strict';
  const api = '/api/laptopseal/vendor';
  const views = ['email', 'code', 'dashboard', 'scanner', 'review', 'confirmation'];
  const byId = id => document.getElementById(id);
  let csrf = null;
  let email = '';
  let packages = [];
  let challenge = null;
  let stream = null;
  let scanning = false;
  let lastScan = 0;
  const canvas = document.createElement('canvas');
  const context = canvas.getContext('2d', { willReadFrequently: true });

  async function decodeQr(source) {
    if (window.BarcodeDetector) {
      try {
        const detected = await new BarcodeDetector({ formats: ['qr_code'] }).detect(source);
        if (detected.length) return detected[0].rawValue;
      } catch (_) { /* Use bundled decoder below. */ }
    }
    if (typeof window.jsQR !== 'function' || !context) return null;
    const width = source.videoWidth || source.naturalWidth || source.width;
    const height = source.videoHeight || source.naturalHeight || source.height;
    if (!width || !height) return null;
    const scale = Math.min(1, 960 / Math.max(width, height));
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    context.drawImage(source, 0, 0, canvas.width, canvas.height);
    const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    const decoded = window.jsQR(pixels.data, canvas.width, canvas.height);
    return decoded ? decoded.data : null;
  }

  function show(name) {
    for (const view of views) byId('view-' + view).hidden = view !== name;
    byId('notice').textContent = '';
    byId('notice').classList.remove('error');
  }
  function notice(message, error = false) {
    byId('notice').textContent = message;
    byId('notice').classList.toggle('error', error);
  }
  async function call(path, method = 'GET', body = null, authenticated = false) {
    const headers = { 'Accept': 'application/json' };
    if (body !== null) headers['Content-Type'] = 'application/json';
    if (authenticated && csrf) headers['X-LaptopSeal-CSRF'] = csrf;
    const response = await fetch(api + path, {
      method, headers, credentials: 'same-origin', cache: 'no-store',
      body: body === null ? undefined : JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Request unavailable.');
    return data;
  }
  async function loadPackages() {
    const data = await call('/packages');
    packages = data.packages || [];
    const list = byId('package-list');
    list.replaceChildren();
    if (!packages.length) {
      const empty = document.createElement('p');
      empty.textContent = 'No vendor packages are available for this verified email.';
      list.append(empty);
      return;
    }
    for (const item of packages) {
      const card = document.createElement('div');
      card.className = 'package-card' + (item.status !== 'active' ? ' expired' : '');
      const title = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = item.plan_id.charAt(0).toUpperCase() + item.plan_id.slice(1);
      const detail = document.createElement('small');
      detail.textContent = `Total ${item.max_devices} · Used ${item.allocated_count} · Remaining ${item.remaining_slots} · Expires ${new Date(item.expires_at).toLocaleDateString()}`;
      title.append(name, detail);
      const state = document.createElement('span');
      state.textContent = item.status;
      card.append(title, state);
      list.append(card);
    }
  }
  async function dashboard() {
    await loadPackages();
    show('dashboard');
  }
  function stopCamera() {
    scanning = false;
    if (stream) for (const track of stream.getTracks()) track.stop();
    stream = null;
    byId('scanner-video').hidden = true;
    byId('scanner-video').srcObject = null;
  }
  async function acceptQr(raw) {
    let parsed;
    try { parsed = JSON.parse(raw); } catch (_) { throw new Error('This is not a LaptopSeal activation QR code.'); }
    if (parsed.type !== 'laptopseal-vendor-v1' || !/^[a-f0-9]{32}$/.test(parsed.challenge_id || '') ||
        !/^[A-Za-z0-9_-]{32,128}$/.test(parsed.qr_secret || '')) {
      throw new Error('This is not a valid LaptopSeal activation QR code.');
    }
    stopCamera();
    const info = await call(`/challenges/${parsed.challenge_id}/inspect`, 'POST',
                            { qr_secret: parsed.qr_secret }, true);
    challenge = { id: parsed.challenge_id, secret: parsed.qr_secret };
    byId('device-hint').textContent = info.device_hint;
    byId('challenge-expiry').textContent = new Date(info.expires_at).toLocaleTimeString();
    await loadPackages();
    const select = byId('package-choice');
    select.replaceChildren();
    for (const item of packages.filter(p => p.status === 'active')) {
      const option = document.createElement('option');
      option.value = item.id;
      option.textContent = `${item.plan_id} · ${item.remaining_slots}/${item.max_devices} slots · expires ${new Date(item.expires_at).toLocaleDateString()}`;
      select.append(option);
    }
    if (!select.options.length) throw new Error('No active package is available for this account.');
    updatePackageSummary();
    show('review');
  }
  function updatePackageSummary() {
    const item = packages.find(p => p.id === byId('package-choice').value);
    byId('package-summary').textContent = item
      ? `${item.remaining_slots} slots remain. This laptop permanently uses one slot in this package; repeat activation of the same laptop uses its existing slot.`
      : '';
  }
  async function openCamera() {
    if (!window.isSecureContext || !navigator.mediaDevices) {
      notice('Camera scanning needs HTTPS or localhost. You can choose a saved QR image below.', true);
      return;
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' }, audio: false });
      const video = byId('scanner-video');
      video.srcObject = stream;
      video.hidden = false;
      await video.play();
      scanning = true;
      const frame = async () => {
        if (!scanning) return;
        if (performance.now() - lastScan < 150) {
          requestAnimationFrame(frame);
          return;
        }
        lastScan = performance.now();
        try {
          const raw = await decodeQr(video);
          if (raw) {
            await acceptQr(raw);
            return;
          }
        } catch (error) {
          stopCamera();
          notice(error.message || 'Could not read the QR code.', true);
          return;
        }
        if (scanning) requestAnimationFrame(frame);
      };
      requestAnimationFrame(frame);
    } catch (_) {
      stopCamera();
      notice('Camera access was unavailable. Check permission or choose a saved QR image.', true);
    }
  }

  byId('email-form').addEventListener('submit', async event => {
    event.preventDefault();
    email = byId('vendor-email').value.trim();
    try {
      await call('/auth/request', 'POST', { email });
      show('code');
      notice('If this mailbox can receive mail, a one-time code was sent.');
    } catch (error) { notice(error.message, true); }
  });
  byId('code-form').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      const result = await call('/auth/verify', 'POST',
                                { email, code: byId('vendor-code').value.trim() });
      byId('vendor-code').value = '';
      csrf = result.csrf_token;
      await dashboard();
    } catch (error) { notice(error.message, true); }
  });
  byId('back-email').addEventListener('click', () => show('email'));
  byId('scan-button').addEventListener('click', () => show('scanner'));
  byId('start-camera').addEventListener('click', openCamera);
  byId('qr-file').addEventListener('change', async event => {
    const file = event.target.files[0];
    if (!file) return;
    let objectUrl;
    try {
      objectUrl = URL.createObjectURL(file);
      const image = new Image();
      await new Promise((resolve, reject) => {
        image.onload = resolve;
        image.onerror = reject;
        image.src = objectUrl;
      });
      const raw = await decodeQr(image);
      if (!raw) throw new Error('No QR code was found in that image.');
      await acceptQr(raw);
    } catch (error) { notice(error.message || 'Could not read the QR image.', true); }
    finally { if (objectUrl) URL.revokeObjectURL(objectUrl); }
    event.target.value = '';
  });
  byId('back-dashboard').addEventListener('click', () => { stopCamera(); dashboard().catch(e => notice(e.message, true)); });
  byId('back-scan').addEventListener('click', () => { challenge = null; show('scanner'); });
  byId('package-choice').addEventListener('change', updatePackageSummary);
  byId('approve').addEventListener('click', async () => {
    if (!challenge) return;
    const button = byId('approve');
    button.disabled = true;
    try {
      const selectedPackageId = byId('package-choice').value;
      await call(`/challenges/${challenge.id}/approve`, 'POST',
                 { qr_secret: challenge.secret, package_id: selectedPackageId }, true);
      challenge = null;
      show('confirmation');
      try {
        await loadPackages();
        const selected = packages.find(item => item.id === selectedPackageId);
        byId('confirmation-slots').textContent = selected
          ? `${selected.remaining_slots} of ${selected.max_devices} laptop slots remaining.` : '';
      } catch (_) {
        byId('confirmation-slots').textContent = 'View packages to refresh remaining slots.';
      }
    } catch (error) { notice(error.message, true); }
    finally { button.disabled = false; }
  });
  byId('done').addEventListener('click', () => dashboard().catch(e => notice(e.message, true)));
  byId('logout').addEventListener('click', async () => {
    try { await call('/auth/logout', 'POST', {}, true); csrf = null; challenge = null; stopCamera(); show('email'); }
    catch (error) { notice(error.message, true); }
  });

  call('/session').then(data => { csrf = data.csrf_token; return dashboard(); })
    .catch(() => show('email'));
})();
