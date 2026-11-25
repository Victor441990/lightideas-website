# LaptopSeal vendor payment development handoff

Phase 3A reuses the website's Paystack inline checkout, direct transaction
verification, signed `/webhooks/paystack/laptopseal` endpoint, payment intent
collection, and Brevo sender. The vendor offer remains separate from legacy
customer activation keys. It uses `ls_vendor_packages` and the existing
email-code vendor portal. No vendor backup activation key exists.

## Purchase and fulfilment

- `/laptopseal/vendor/purchase` lets a buyer select Single, Basic, Business,
  or Unlimited and enter an email. Amounts come from `VENDOR_PLANS`, not the
  browser. The server stores one intent in `ls_payment_intents` and generates
  its reference. The existing Paystack inline checkout uses that reference.
- `/laptopseal/vendor/verify_payment` is tied to the initiating browser
  session. Both it and the existing signed webhook retrieve the transaction
  directly from Paystack's verification API before issuing a package.
- Verification requires the exact reference, success status, NGN currency,
  server price, customer email, and vendor product/plan metadata. Paystack's
  verified `paid_at` timestamp starts the fixed 30-day period.
- `ls_vendor_packages.purchase_reference` is unique. An interrupted retry
  finds the same package and repairs the intent state; it cannot insert a
  second one. Each new purchase creates a separate package for the normalized
  email identity. Email-code login reveals only that identity's packages.
- The confirmation page requires the initiating session and shows package,
  capacity, reference, expiry, and the portal link. It can retry verification
  of the same reference without starting another charge.
- Production confirmation mail uses the existing Brevo sender. An email
  failure leaves the package available in the vendor portal. Test mode
  suppresses real confirmation mail; the existing test-only code capture
  supports local portal login.

## Release gates

The new `LAPTOPSEAL_VENDOR_PAYMENTS_ENABLED` flag defaults off. The existing
test runner also forces legacy `LAPTOPSEAL_PAYMENTS_ENABLED=0`. The vendor
runner's `-EnableVendorPayments` switch requires both Paystack test key
prefixes and exactly `laptopseal_test`. The application checks that the
payment-intent, vendor-email, and vendor-purchase unique indexes exist before
starting checkout. The three indexes were found in `laptopseal_test` during
Phase 3A. Production index setup and credentials are a separate release step.

## Controlled test purchase, when separately authorized

1. Stop only the known isolated backend on port 5050 from its terminal. Leave
   the desktop on 5001 and installed app on 5000 untouched.
2. In this website project run
   `powershell -ExecutionPolicy Bypass -File .\tools\Start-LaptopSealVendorTest.ps1 -EnableVendorPayments`.
   The switch refuses live Paystack keys or a database other than
   `laptopseal_test`; legacy checkout stays disabled.
3. Open `http://127.0.0.1:5050/laptopseal/vendor/purchase`, choose a plan,
   use an approved test address, and complete only a Paystack test-mode
   transaction. The browser callback verifies the same reference directly
   with Paystack. Open the session-bound confirmation page and check its
   package and expiry.
4. Open `http://127.0.0.1:5050/laptopseal/vendor`, request a login code for
   that same address, read the ignored `.vendor-test/email-code.txt` locally,
   and verify the email. Confirm that the new package appears separately from
   the existing synthetic package.
5. Check `laptopseal_test` for exactly one package and fulfilled intent for
   the payment reference. Repeat verification of the same reference to confirm
   the package count stays one. No real confirmation email is sent in test
   mode.

The local backend is not publicly exposed, so Paystack cannot deliver an
external webhook to `127.0.0.1`. A public test endpoint or tunnel requires
separate approval. The callback route is sufficient for a local test payment;
webhook behavior is covered by isolated signed-event tests.
