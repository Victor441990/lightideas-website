# Phase 3A deployment checklist

This branch is local-only. Do not push or deploy it until every prerequisite
below has been completed in Render.

## Required Render environment values

Configure the variable names listed in `.env.example`. Existing values must be
copied into Render through its normal dashboard; do not put values in source
control or share them in chat.

Before deployment, add `ADMIN_PASSWORD_HASH`. Generate it locally from the
administrator password using a one-time Python command in an isolated shell:

```powershell
.\.venv\Scripts\python.exe -c "from werkzeug.security import generate_password_hash; import getpass; print(generate_password_hash(getpass.getpass('Administrator password: ')))"
```

Paste only the generated hash into Render. Do not commit it. Keep the current
`ADMIN_PASSWORD` variable temporarily during the migration if other deployment
notes require it, but this branch does not use it for authentication. Remove it
only after a verified production login with `ADMIN_PASSWORD_HASH`.

Set `LAPTOPSEAL_MASTER_KEY` in Render before deployment if it is not already
present. The value must remain consistent with the desktop application; changing
it would disrupt classification of LaptopSeal check-ins.

For Render, set `SESSION_COOKIE_SECURE=true`, `RENDER=true`, and
`TRUST_PROXY_HEADERS=true`. The default administrator session lifetime is 60
minutes and can be changed through `ADMIN_SESSION_MINUTES`.

## Safe migration sequence

1. Record the currently deployed commit and confirm the Render service is green.
2. Add the new environment variables in Render without removing current ones.
3. Push this branch only after code review and explicit approval.
4. Verify administrator sign-in, dashboard loading, product updates, image
   upload, email action, and LaptopSeal administration in a controlled browser.
5. Confirm browser cookies are `HttpOnly`, `SameSite=Lax`, and `Secure`.
6. Only after verification, remove the obsolete plaintext administrator
   environment variable and rotate source-embedded private credentials.

## Rollback

The pre-change production baseline is commit
`78286861b99a390db63d43f8a8684f3077f16e66` on `main`.

If a deployment fails, redeploy that exact commit from Render or revert the
security branch merge. Keep the newly added environment variables in place:
they are harmless to the baseline code and allow a later controlled retry.
Do not delete MongoDB records, Cloudinary assets, subscriber data, or payment
records as part of a rollback.

## Scaling note

The login limiter is process-local, which is appropriate for the current
single-process `gunicorn app:app` start command. If the Render service is
scaled to multiple workers or instances, replace it with a shared Redis-backed
limiter before treating it as a distributed brute-force control.

## Phase 3B Biztrack integration

The Light Ideas administrator dashboard communicates with the independent
Biztrack landing service over a server-to-server HTTP connection. Add these
values only when preparing a future private deployment:

- In the Light Ideas service: `BIZTRACK_SERVICE_URL` and
  `BIZTRACK_INTEGRATION_TOKEN`.
- In the Biztrack landing service:
  `BIZTRACK_LIGHTIDEAS_INTEGRATION_TOKEN` with the exact same token value.

Use a new high-entropy token generated locally. Never put it in JavaScript,
HTML, a repository, or browser requests. Place the Biztrack service on a
private network or otherwise restrict inbound access in addition to token
authentication. The public landing page remains independent and does not need
the integration token.
