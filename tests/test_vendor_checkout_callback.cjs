// Mock Paystack v1 setup only. No Paystack network call or transaction.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'js',
  'laptopseal-vendor-purchase.js'), 'utf8');

test('v1 accepts the callback and opens only a mocked checkout', async () => {
  // The loaded Paystack v1 script uses this exact type-tag test for callbacks.
  const validV1Function = value => value &&
    Object.prototype.toString.call(value) === '[object Function]';
  assert.equal(validV1Function(async () => {}), false);

  const calls = [];
  const locations = [];
  let submit;
  let options;
  let opened = 0;
  const elements = {
    'vendor-purchase-form': {addEventListener: (event, handler) => { submit = handler; }},
    'purchase-notice': {textContent: '', classList: {toggle: () => {}}},
    'purchase-button': {disabled: false},
    'purchase-email': {value: 'synthetic@example.test'},
    'vendor-plan': {value: 'single'},
  };
  const paystack = {
    setup(value) {
      if (!validV1Function(value.callback))
        throw new Error('Attribute callback must be a valid function');
      if (!validV1Function(value.onClose))
        throw new Error('Attribute onClose must be a valid function');
      options = value;
      return {openIframe() { opened += 1; }};
    },
  };
  const context = {
    document: {getElementById: id => elements[id]},
    window: {
      PaystackPop: paystack,
      LaptopSealVendorPaystackPublicKey: 'pk_test_synthetic',
      location: {assign: value => locations.push(value)},
    },
    PaystackPop: paystack,
    fetch: async (url, request) => {
      calls.push({url, body: JSON.parse(request.body)});
      return {ok: true, json: async () => url.endsWith('/initialize_payment')
        ? {success: true, amount: 200000, currency: 'NGN', reference: 'LSV_synthetic_reference'}
        : {success: true, confirmation_path: '/laptopseal/vendor/confirmation'}};
    },
  };
  vm.runInNewContext(source, context);
  assert.equal(typeof submit, 'function');
  await submit({preventDefault() {}});
  assert.equal(opened, 1);
  assert.equal(options.amount, 200000);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/laptopseal/vendor/initialize_payment');
  options.callback({reference: 'LSV_synthetic_reference'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[1].url, '/laptopseal/vendor/verify_payment');
  assert.equal(calls[1].body.reference, 'LSV_synthetic_reference');
  assert.deepEqual(locations, ['/laptopseal/vendor/confirmation']);
});
