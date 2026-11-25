(() => {
  'use strict';
  const form = document.getElementById('vendor-purchase-form');
  const notice = document.getElementById('purchase-notice');
  const button = document.getElementById('purchase-button');
  const say = (message, error = false) => {
    notice.textContent = message;
    notice.classList.toggle('error', error);
  };
  const post = async (path, body) => {
    const response = await fetch(path, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok || !data.success) throw new Error(data.error || 'Payment could not be confirmed.');
    return data;
  };
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (button.disabled) return;
    button.disabled = true;
    say('Preparing secure checkout…');
    try {
      if (!window.PaystackPop || !window.LaptopSealVendorPaystackPublicKey)
        throw new Error('Paystack checkout is unavailable. Please try again later.');
      const email = document.getElementById('purchase-email').value.trim();
      const planId = document.getElementById('vendor-plan').value;
      const intent = await post('/laptopseal/vendor/initialize_payment', {email, plan_id: planId});
      const handler = PaystackPop.setup({
        key: window.LaptopSealVendorPaystackPublicKey,
        email, amount: intent.amount, currency: intent.currency, ref: intent.reference,
        metadata: {product_id: 'laptopseal_vendor', plan_id: planId},
        callback: function (response) {
          say('Verifying payment directly with Paystack…');
          post('/laptopseal/vendor/verify_payment', {reference: response.reference})
            .then(result => window.location.assign(result.confirmation_path))
            .catch(error => {
              say(error.message + ' Your purchase reference is available on the confirmation page for a safe retry.', true);
              window.location.assign('/laptopseal/vendor/confirmation');
            });
        },
        onClose: () => {
          button.disabled = false;
          say('Payment window closed. If you paid, use the confirmation page to verify the same purchase.');
        }
      });
      handler.openIframe();
    } catch (error) {
      button.disabled = false;
      say(error.message || 'Could not start checkout.', true);
    }
  });
})();
