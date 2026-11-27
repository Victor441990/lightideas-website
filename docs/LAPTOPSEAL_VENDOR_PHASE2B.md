# LaptopSeal vendor QR development test

Phase 2B adds a mobile-first vendor portal and an isolated desktop QR path.
The existing customer key, owner key, Paystack fulfilment, diagnostics, and
installed application are unchanged. Vendor packages remain a separate offer.

## Development signing and storage

`tools/prepare_vendor_test_keys.py` created a development Ed25519 pair. The
private key is only in the Git-ignored `.env.laptopseal.test`; the matching
public key is in the ignored `.vendor-test/public.pem`. The desktop test runner
points to that public file. No production key was created or configured.
The backend fails closed without its private key. The desktop fails closed
without its explicit test-mode public key and does not accept a key override in
a frozen production build.

The desktop keeps the QR polling secret in its Flask process, never in its
browser page or URL. The QR image encodes the short-lived, single-use challenge
ID and QR approval secret. Signed entitlements are stored separately from
`license.json` in the selected `LaptopSealTest` profile. The desktop verifies
the Ed25519 signature, test environment, device fingerprint, package identity,
and absolute expiry. It checks the backend for revocation when online. Offline
access is bounded to 24 hours from the last signed server timestamp and never
past package expiry. A local clock rollback or missing state fails closed.
Once an online rejection is seen, a local revocation marker prevents a later
outage from restoring access. Device clocks and writable local storage cannot
provide perfect resistance to deliberate offline tampering.
Refreshing or cancelling a pending QR first asks the backend to cancel the old
challenge using the desktop-only polling secret. If cancellation cannot be
confirmed, the desktop keeps the old challenge and asks the user to retry or
wait for its five-minute expiry.

## Local manual workflow

1. Leave the installed LaptopSeal on port 5000 untouched. As of the Phase 2B
   check, an older development backend still occupies port 5050. Stop that
   known backend from its own terminal with Ctrl+C; do not terminate an
   unidentified process. Both vendor runners refuse to share occupied ports.
   In the website project,
   run `powershell -ExecutionPolicy Bypass -File .\tools\Start-LaptopSealVendorTest.ps1`.
   This binds only to `127.0.0.1:5050`, requires `laptopseal_test`, captures
   test email codes locally, and forces checkout disabled.
2. In `C:\Users\user\Desktop\LightIdeas_Diagnostic`, run
   `powershell -ExecutionPolicy Bypass -File .\tools\Start-LaptopSealVendorTest.ps1`.
   It uses the separate `vendor-qr-20261121` profile and the matching test
   public key. Open `http://127.0.0.1:5001/unlock?manage=1` and choose
   **Activate with QR Code**.
3. On the same laptop, open `http://127.0.0.1:5050/laptopseal/vendor` in a
   browser. Enter the synthetic test vendor address
   `vendor.laptopseal.test@example.test`. Open the ignored local file
   `.vendor-test/email-code.txt` privately to read the one-time test code,
   then verify it in the portal. No real email is sent by this runner.
4. For a same-computer test, right-click the laptop QR image and save it as a
   PNG (or capture it with Windows Snipping Tool). Use
   **Choose a saved QR image** in the portal. Select the synthetic Single
   package, check its device hint and remaining slot, then approve. This
   consumes its one test slot. The desktop should poll, verify the signature,
   save the entitlement, and open paid diagnostics.
5. Restart the desktop with the same runner/profile to check persistence.
   Test expiration, wrong-device rejection, clock rollback, and revocation in
   the automated isolated fixtures; do not alter this purchased test fixture
   or the owner's production activation to simulate them.

A real phone cannot reach a `127.0.0.1` server on the laptop. Camera scanning
on a phone needs an approved HTTPS-accessible test endpoint or a controlled
local HTTPS/LAN setup. No tunnel, public exposure, or deployment was created.
The portal includes a bundled jsQR 1.4.0 decoder so camera and saved-image
scanning do not depend solely on the limited-availability BarcodeDetector API.

## Release boundaries

This is development-only. Production vendor signing/public-key provisioning,
desktop packaging, phone-camera testing, and payment-backed vendor package
issuance remain separate approval stages. The in-memory concurrency tests do
not prove real Atlas transaction behavior. The backend requires MongoDB
transactions and fails closed if they are unavailable. Hardware fingerprints
are bound at challenge creation, but software cannot attest that a malicious
desktop supplied an honest hardware identity.
