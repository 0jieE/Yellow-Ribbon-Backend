# Deploying to Render

Reference for the Yellow Ribbon Pizza backend. Nothing here is automated —
follow it in order.

---

## 0. Before you push anything

The repository is not yet under version control. When you run `git init`,
confirm `.env` is ignored **before** the first commit:

```bash
git init
git add -A
git status --short | grep -E "^A.*\.env$"     # must print nothing
git status --short | grep -E "\.env\.example" # this one SHOULD appear
```

If `.env` ever shows up as staged, `git rm --cached .env` and fix
`.gitignore` before committing. A secret committed once lives in the
history forever, even after a later deletion.

---

## 1. Sign-ups needed

| Service | Why | Free tier? |
|---|---|---|
| **Render** | Hosts the web service and Postgres | Yes |
| **Cloudinary** | Stores menu photos and proof-of-delivery images | Yes |

Render's disk is **ephemeral** — every deploy replaces the container and
deletes anything written to it. Menu photos could be re-uploaded, but a
proof of delivery is the evidence that a delivery happened. That is why
media goes to Cloudinary.

After signing up to Cloudinary, the dashboard shows a **Cloud Name**,
**API Key** and **API Secret**. Those are the three values you need.

---

## 2. Create the database first

Render dashboard → **New** → **Postgres**.

- Name: `yellowribbon-db`
- Region: **Singapore** (closest to Kidapawan; keep the web service in the
  same region or every query pays a round trip across the ocean)
- Plan: Free

When it finishes provisioning, copy the **Internal Database URL**. Use the
internal one, not the external — it is faster and does not leave Render's
network.

---

## 3. Create the web service

Render dashboard → **New** → **Web Service** → connect the GitHub repo.

| Field | Value |
|---|---|
| Region | **Same as the database** |
| Runtime | Python 3 |
| Build command | `bash ./build.sh` |
| Start command | `gunicorn config.wsgi:application` |
| Plan | Free |

`bash ./build.sh` rather than `./build.sh` — the repo is authored on
Windows, which does not track the executable bit, so invoking it through
`bash` avoids a "permission denied" build failure.

---

## 4. Environment variables

Set these on the **web service** (Environment tab). Values are yours to
supply; only the names are listed.

### Required

| Name | Notes |
|---|---|
| `SECRET_KEY` | Generate a **new** one — do not reuse the development key. Render's "Generate" button is fine. |
| `DEBUG` | `False` |
| `ALLOWED_HOSTS` | `yellowribbon-backend.onrender.com` — your actual Render hostname, no scheme |
| `CSRF_TRUSTED_ORIGINS` | `https://yellowribbon-backend.onrender.com` — **with** the scheme |
| `DATABASE_URL` | The Internal Database URL from step 2 |
| `PYTHON_VERSION` | `3.13.14` — matches local, avoids a surprise interpreter |

### Media storage

| Name |
|---|
| `CLOUDINARY_CLOUD_NAME` |
| `CLOUDINARY_API_KEY` |
| `CLOUDINARY_API_SECRET` |

Leave all three unset and uploads silently fall back to the container's
disk, where they will not survive a deploy. Set all three.

### Integrations

| Name | Notes |
|---|---|
| `GOOGLE_MAPS_SERVER_KEY` | Restrict it by IP or API in Google Cloud Console. It must **not** be the key shipped inside the Flutter apps. |
| `PAYMONGO_PUBLIC_KEY` | |
| `PAYMONGO_SECRET_KEY` | |
| `PAYMONGO_WEBHOOK_SECRET` | Regenerate after step 6 — the webhook URL changes |
| `FIREBASE_CREDENTIALS_JSON` | The whole service-account JSON **on one line**. Do not use `FIREBASE_CREDENTIALS_PATH` — there is no file to point at. |

### Business rules (optional; defaults shown in `.env.example`)

`DELIVERY_FEE`, `RIDER_EARNINGS_PER_DELIVERY`,
`PENDING_PAYMENT_TIMEOUT_MINUTES`, `SERVICE_AREA_CENTER_LAT`,
`SERVICE_AREA_CENTER_LNG`, `SERVICE_AREA_RADIUS_KM`,
`GOOGLE_ROUTES_TRAVEL_MODE`, `CORS_ALLOWED_ORIGINS`

### Deliberately left unset

`SECURE_SSL_REDIRECT` and `DATABASE_SSL_REQUIRE` both infer the right value
from `DEBUG`. Only set them to override.

---

## 5. First deploy

Trigger it and watch the build log. Expect, in order:

```
--> Installing dependencies
--> Collecting static files
--> Applying migrations
--> Build complete
```

Then create your admin account from the Render **Shell** tab:

```bash
python manage.py createsuperuser
python manage.py seed_demo        # optional: sample menu, one rider, one customer
```

Check `https://<your-service>.onrender.com/api/health/` returns
`{"status": "ok"}`, and that `/admin/` loads **with styling**. Unstyled
admin means static files did not collect.

---

## 6. Point PayMongo at the new URL

PayMongo dashboard → Developers → Webhooks.

- URL: `https://<your-service>.onrender.com/api/payments/webhook/`
- Events: `source.chargeable`, `payment.paid`, `payment.failed`

Copy the new signing secret into `PAYMONGO_WEBHOOK_SECRET` and redeploy.
The old `whsk_` from the tunnel will not validate against the new endpoint.

---

## 7. Point the Flutter apps at the new base URL

Replace the `trycloudflare` URL with
`https://<your-service>.onrender.com/api/` in all three apps.

---

## 8. Schedule the stale-order reaper

`cancel_stale_orders` cancels GCash orders that were never paid for. It is
not automatic.

Render → **New** → **Cron Job**, same repo and region:

- Schedule: `*/15 * * * *`
- Command: `python manage.py cancel_stale_orders`

A cron job is a separate paid service on Render. If you would rather not,
run it manually from the Shell tab before a demo.

---

## 9. Back up, and move to a new database

Render's free Postgres is deleted after 30 days. Take a backup before then.

**Back up.** Sign in to `/admin/` as a superuser and press **Back up now** on
the dashboard (or **System → Database backup** in the sidebar), then
**Download backup**. You get `yellowribbon-backup-<date>.json.gz`: every table
in one file. It does not include uploaded photos (Cloudinary keeps those). It
**does** include customer addresses, phone numbers and password hashes, so keep
it private. `.gitignore` already stops it being committed.

**Restore into the new database.** The free web service has no shell, so do
this from your own computer:

1. Create the new Render Postgres and copy its **External Database URL**.
2. In your local `.env`, set `DATABASE_URL` to it, with `?sslmode=require` on the end.
3. `python manage.py migrate`
4. `python manage.py restore_backup path/to/yellowribbon-backup-<date>.json.gz`

   If you already ran `createsuperuser` on the new database, add `--flush`: it
   wipes the database first, and the admin comes back from the backup.
5. Set the web service's `DATABASE_URL` to the new database and redeploy.
6. Put your local `.env` back.

Order and invoice numbers carry on from where they stopped (`restore_backup`
resets their Postgres sequences). Restore with the same version of the code
that made the backup.

---

## Known gotchas

**Free tier sleeps.** After 15 minutes idle the service spins down, and the
next request takes ~50 seconds. Before the defence, hit the URL once to
wake it. A PayMongo webhook arriving cold may time out — PayMongo retries,
and settlement is idempotent, so it recovers, but the order will not flip
to `pending` instantly.

**Free Postgres expires** after 30 days. Note the date and download a backup
from the admin before it lapses (section 9).

**Static files are collected at build time.** Adding one later requires a
redeploy, not just a file upload.

**`migrate` runs on every deploy.** A migration that fails takes the build
down with it — which is the intended behaviour, since a half-migrated
database serving traffic is worse.
