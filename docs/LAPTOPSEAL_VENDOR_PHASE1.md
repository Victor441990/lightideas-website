# LaptopSeal vendor licensing: phase-one backend boundary

This is a parallel foundation. The existing `ls_licenses` customer-key flow,
owner-key flow, Paystack checkout, and desktop licensing code are unchanged.
Phase 2A now registers protected vendor routes; see `LAPTOPSEAL_VENDOR_PHASE2A.md`.

## Records and additive indexes

Call `ensure_vendor_indexes(db)` deliberately after selecting and verifying
the intended database. It creates:

| Collection | Purpose | Constraint |
| --- | --- | --- |
| `ls_vendor_identities` | Normalized, verified email identity | Unique email |
| `ls_vendor_packages` | Independent 30-day purchased packages | Unique purchase reference when payment integration is added |
| `ls_vendor_allocations` | Permanent device-slot assignment per package | Unique package/device and package/slot |
| `ls_vendor_challenges` | Five-minute QR/poll challenge and approval state | Unique hashed secrets; expiry TTL |

The TTL index is storage cleanup only. All authorization checks compare the
server time with `expires_at` directly. Package expiry starts at issuance;
future payment integration must call issuance only after server-side payment
verification, using its verified transaction timestamp. It must create a new
package for every distinct successful purchase and be idempotent by reference.

Test package issuance requires all three: `LAPTOPSEAL_TEST_MODE=1`,
`MONGO_DB_NAME=laptopseal_test`, and a selected DB named `laptopseal_test`.
Test packages are marked `environment=test` and rejected outside that mode.
There is no production package issuance path yet.

## Authentication and endpoints

`upsert_verified_vendor` and `approve_challenge` remain internal services.
Phase 2A now provides email-code authentication and derives approval identity
from a server-side vendor session. The QR and polling secrets remain separate.

## Concurrency and proof

Approval uses a MongoDB transaction, a conditional package counter update,
and unique allocation indexes. Production MongoDB must support transactions
(replica set or sharded cluster). The in-memory tests serialize a fixture to
test the algorithm; they do not prove Atlas transaction behavior.

The entitlement structure is an Ed25519-signed, device-hash-bound payload.
The private signing key must be provisioned only on the backend. A desktop
build will need a pinned public key and explicit verification before it can use
the proof. No signing key has been generated or stored by this phase. Local
clock rollback and absolute expiry are checked by the verifier, but offline
time cannot be made tamper-proof using only the desktop clock. Integrate the
existing bounded-offline policy when desktop support is approved.

Device identity is bound at challenge creation and cannot be replaced during
approval. Software alone cannot prove that an untrusted desktop supplied a
truthful hardware identity; future desktop integration must reuse the current
hardware fingerprint and treat this as a documented limit.

## Separate vendor offer

The current `laptopseal_security.PLANS['unlimited']` has a 999999-device
sentinel and the existing checkout creates an `ls_licenses` record. Vendor
packages are a separate offer with a 200-device cap in `VENDOR_PLANS`, as
confirmed for Phase 2A. No existing license or checkout rule is changed.
