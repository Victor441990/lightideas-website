# Light Ideas Technology — Website

Flask site at **lightideastechnology.com.ng**, hosted on Render (free plan), auto-deployed from
this GitHub repo. Sells laptops and the **LaptopSeal** diagnostic app.

Owner: Victor. He calls Claude "Light". Sign technical messages with 🔵.

## Ground rules (from Victor)

- **Always tell the truth, no matter how hard it is.** Flag risks early, before he ships.
- Give **complete file replacements**, not partial edits.
- **Simple numbered steps**, one at a time, wait for confirmation.
- **Never break a working page.** Only add.

## Three separate projects — do not mix them up

| Project | Path | Deploys via |
|---|---|---|
| **Website (this one)** | `C:\Users\user\Downloads\lightideas-website\lightideas-website` | `git push` → Render |
| **Desktop app** | `C:\Users\user\Desktop\LightIdeas_Diagnostic` | PyInstaller → NSIS → GitHub release |
| **Android app** | `C:\Users\user\Desktop\Light ideas catalouge-app` | EAS build → APK / Play Store |

The website and the desktop app both contain a file called `app.py`. They are
**completely different files**.

The Android app is **read-only** against this site — it only calls
`GET /api/catalog` over the internet. It never touches this code or the
database. See "Contract with the Android app" in
[`WEBSITE-WORK-2026-08-04.md`](WEBSITE-WORK-2026-08-04.md) before changing that
endpoint's shape.

## Recent work

[`WEBSITE-WORK-2026-08-04.md`](WEBSITE-WORK-2026-08-04.md) — the product detail
popup on `/` and `/catalog` was rebuilt to match the Android app, and a
malformed-tag bug in `static/js/main.js` was fixed. **Read it before touching
either popup**; it records one CSS ordering constraint that will silently revert
the spec list if broken.

## Deploying

```bash
git add <the specific files you changed>   # NOT `git add .`
git commit -m "what changed"
git push
# Render redeploys in 1-2 minutes
```

## Stack

- Flask + Jinja templates
- MongoDB Atlas (`lightideas` db)
- Cloudinary (product images)
- Brevo (bulk email)
- Paystack (payments)

## Key routes

| Route | What it does |
|---|---|
| `/` , `/catalog` | laptop storefront |
| `/laptopseal` | LaptopSeal sales page — 5 pricing tiers |
| `/laptopseal/verify_payment` | Paystack callback → creates a license, assigns tier by amount |
| `/laptopseal/bind_hardware` | registers a laptop against a key, enforces the laptop limit |
| `/laptopseal/check_license` | desktop app calls this to validate a key |
| `/laptopseal/version.json` | drives the desktop auto-updater |
| `/laptopseal/apps.json` | curated App Store list — **fix a dead download link here once and every laptop gets it** |
| `/terms` | Terms & Conditions page |
| `/guide` , `/laptopseal/userguide` | User Guide page |
| `/victor-admin` | admin login |
| `/victor-admin/dashboard` | products / subscribers / email |
| `/victor-admin/laptopseal` | LaptopSeal licenses, revenue, key-reveal-on-click |

## Licensing logic

`ls_licenses` collection. Tier is assigned from the amount paid (in kobo):

| Amount | Tier | max_devices |
|---|---|---|
| ≥ 6,000,000 (₦60k) | unlimited | 999999 |
| ≥ 3,000,000 (₦30k) | medium | 20 |
| ≥ 800,000 (₦8k) | small | 5 |
| ≥ 200,000 (₦2k) | single | 1 |

Each license has a `devices` **list**. `bind_hardware` appends a laptop until `max_devices`
is reached, then refuses. Old single-laptop licenses (with `hardware_id` but no `devices`)
are migrated on the fly — **keep that backward compatibility**.

All licenses last 30 days from purchase, on **one shared clock** — every laptop on a key
locks together at expiry.

## Releasing a new desktop version

1. Build the installer in the desktop project
2. Publish a GitHub release **first** (tag `vX.Y.Z`, attach `LaptopSeal_Setup.exe`, mark **Latest**)
3. **Then** bump `LAPTOPSEAL_LATEST_VERSION` + `LAPTOPSEAL_SETUP_URL` + `notes` here and push

Doing it in the other order points every laptop at a download that doesn't exist yet.

## ⚠️ SECURITY — the source is clean, the history is not

*Checked 4 Aug 2026. An earlier version of this file said the secrets were
hardcoded in `app.py`. That is no longer true and led to a false warning — do
not repeat it without re-reading `app.py` first.*

**Current source is clean.** `MONGO_URI`, `ADMIN_PASSWORD`, `FLASK_SECRET_KEY`,
the Cloudinary `api_secret` and both Paystack keys are all read from
`os.environ`. Only the Cloudinary `cloud_name` and public `api_key` are literals,
and neither is a secret.

**The real, unfixed problem is git history.** Verified by asking GitHub
anonymously:

```
repo visibility : PUBLIC
MongoDB connection string with password : present in 40 of 56 commits
Flask secret key                        : present in 44 of 56 commits
```

Anyone can open an old commit and read both. The Mongo password gives full
database access. The Flask secret key allows forging a session cookie and
logging into `/victor-admin` **without knowing the password**.

Making the repo private does **not** undo this — assume both are already out.

**Fix — rotate, so the leaked values become worthless:**

1. MongoDB Atlas → Database Access → change the user's password
2. Render → Environment → update `MONGO_URI`
3. Render → Environment → set a new random `FLASK_SECRET_KEY`
4. GitHub → Settings → make the repository **Private**

**Still outstanding as of 4 Aug 2026.**

## Pending work

- No real payment has ever been tested end-to-end. Buy a ₦2,000 license from the live site
  and confirm: key issued → activates the desktop app → license appears on the dashboard →
  money lands in Paystack/UBA.
- Paystack takes ~1.5% + ₦100 (₦100 waived under ₦2,500). Currently absorbed by Victor.
  Revisit once orders start coming in.

## LaptopCare (rebuilt 20 Aug 2026 — ₦5,000/month) — needs Victor's action before real customers use it

- **Paystack Plan mismatch risk.** The subscribe page charges ₦5,000 for the *first*
  payment, but recurring monthly amount is whatever `PAYSTACK_LAPTOPCARE_PLAN_CODE`
  (a Render env var) points to in the Paystack dashboard. If that Plan is still the old
  ₦2,000/month one (or unset), customers will be charged ₦5,000 once and then ₦2,000/month
  after — or not billed automatically at all. **Before promoting this: in Paystack →
  Plans, create/confirm a ₦5,000/month Plan, then set `PAYSTACK_LAPTOPCARE_PLAN_CODE`
  on Render to its plan code.**
- **Cancellation doesn't yet stop Paystack billing.** "Cancel my membership" on the member
  dashboard marks the subscriber cancelled on our side immediately, but actually disabling
  the Paystack subscription needs an `email_token` Paystack only hands over via webhook —
  not built yet. **Until a webhook is added, check the Paystack dashboard yourself whenever
  someone cancels, and disable their subscription there too**, or they'll keep being charged.
- **Included services are a placeholder**, not something Victor specified exactly: 1 free
  health check + 2 free cleanups per month, admin-marked as used from the dashboard's
  LaptopCare tab, resetting every 30 days. Confirm or change the list/limits in
  `LAPTOPCARE_SERVICES` in `app.py`.
- Member sign-in: Paystack subscribers set their own password at checkout; subscribers
  Victor adds manually (bank transfer) get a random 6-digit PIN shown once in the admin
  dashboard — copy it to the customer over WhatsApp.
