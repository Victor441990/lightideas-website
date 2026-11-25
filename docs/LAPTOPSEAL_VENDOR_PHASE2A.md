# LaptopSeal vendor authentication and QR API

The routes in `laptopseal_vendor_api.py` register on the existing Flask app.
They do not issue paid packages. Existing checkout, owner-key activation, and
customer-key activation are unchanged.

## Route contract

| Method and route prefix `/api/laptopseal/vendor` | Purpose |
| --- | --- |
| `POST /auth/request` | Request eight-digit email code; generic 202 response |
| `POST /auth/verify` | Consume code and create server-side vendor session |
| `GET /session` | Check session and obtain fresh CSRF token |
| `POST /auth/logout` | Revoke session; requires CSRF |
| `GET /packages` | List only authenticated vendor's packages |
| `GET /packages/<id>` | Package status and masked allocation history |
| `POST /challenges` | Desktop creates five-minute challenge for its 32-hex fingerprint |
| `POST /challenges/<id>/inspect` | Authenticated vendor inspects QR; requires CSRF |
| `POST /challenges/<id>/approve` | Authenticated vendor approves selected owned package; requires CSRF |
| `POST /challenges/<id>/poll` | Desktop polls with separate secret; receives signed proof only after approval |
| `POST /challenges/<id>/cancel` | Desktop cancels a pending QR using its polling secret |
| `POST /entitlements/check` | Recheck a signed device allocation online without consuming another slot |

The desktop receives the QR payload and a separate polling secret. Only the QR
payload should be shown to a scanner. The polling secret must stay with the
desktop process. Do not put secrets in query strings or analytics. The mobile
approval response does not contain the entitlement.

## Authentication and deployment configuration

Vendor email codes use the existing Brevo transactional sender. An isolated
test can inject a code-capture delivery fixture; production HTTP responses
never include codes. The code digest is domain-separated from
`LAPTOPSEAL_LICENSE_PEPPER`; that server-side pepper must be configured.
Codes expire after 10 minutes, permit five attempts, and are single-use.
Requests have a 60-second per-email cooldown plus hourly per-email and per-IP
limits. Successful verification creates an opaque eight-hour session token;
only its keyed hash is stored. Login rotates an existing session. Logout
revokes it. Cookies are HttpOnly, SameSite=Lax, and Secure except on an
explicit local loopback test request. Cookie-authenticated mutations require
a CSRF token.

To return an entitlement, configure
`LAPTOPSEAL_VENDOR_SIGNING_PRIVATE_KEY_PEM` on the server only, as an Ed25519
private key in PEM format. No key is bundled in the desktop or repository.
Provision and pin the matching public key in a later desktop release. Until
the server key is configured, approved polls fail closed with HTTP 503.

## Isolated index migration

`tools/prepare_laptopseal_vendor_test_indexes.py` requires
`LAPTOPSEAL_TEST_MODE=1`, `MONGO_DB_NAME=laptopseal_test`, and `MONGO_URI`.
It pings the selected test DB, verifies its name, then creates only additive
vendor and authentication indexes. It is idempotent. It must never be run
against `lightideas` or with production credentials. MongoDB transactions
require a replica set or sharded deployment; approval fails closed if sessions
are unavailable. Existing license collections are not migrated or changed.

## Known integration limits

The existing desktop app does not yet request or verify vendor entitlements.
The vendor mobile scanner UI is not built. Production vendor package issuance
must later be connected to server-verified Paystack fulfilment; client payment
claims must never create packages. The API validates the desktop's existing
32-hex fingerprint format and binds approval to its hash, but software cannot
prove a malicious desktop supplied an honest hardware ID. QR and polling
endpoints must be reached over HTTPS in production; the local HTTP allowance
exists only for isolated loopback development.
