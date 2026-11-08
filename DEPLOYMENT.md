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

Gunicorn also reads `gunicorn.conf.py` from the repo root on its own, so the
start command stays as it is. That file switches it to threads: an APK
download from the website takes minutes on a phone and must not hold the only
worker (or hit the 30-second timeout).

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
| `MAYA_PUBLIC_KEY` / `MAYA_SECRET_KEY` | Optional: leave blank and Maya checkout answers "not configured". For the sandbox, use the shared public keys on developers.maya.ph ("Sandbox Party 1"). They are shared with every developer, so never register a webhook on them. |
| `MAYA_BASE_URL` | `https://pg-sandbox.paymaya.com` (the default). Production is `https://pg.paymaya.com`, with your own keys. |
| `FIREBASE_CREDENTIALS_JSON` | The whole service-account JSON **on one line**. Do not use `FIREBASE_CREDENTIALS_PATH` — there is no file to point at. |
| `APP_RELEASE_PUBLISH_TOKEN` | A long random secret; the same value is the `APP_RELEASE_PUBLISH_TOKEN` secret in the mobile repo's GitHub settings. Unset, GitHub cannot publish app updates (section 10). Make one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |

### Customer web app (iPhone PWA)

The customer web app is part of this server, at `/order/` (the `shop` app), so
it needs no separate hosting and no CORS entry. iPhone customers open
`https://yellow-ribbon-backend.onrender.com/order/` in Safari, tap Share, then
Add to Home Screen. Notifications work once it is opened from the Home Screen
(iOS 16.4 or later).

| Name | Value |
|---|---|
| `PWA_BASE_URL` | `https://yellow-ribbon-backend.onrender.com/order` (no trailing slash). Turns on web push (icon, and tapping opens the order) and sends a customer who paid from the web app back to it. Blank turns both off. It must be `https://`; anything else switches web push off so it cannot break Android pushes. |
| `FIREBASE_WEB_VAPID_KEY` | Optional. The "Web Push certificates" key pair from Firebase console > Project settings > Cloud Messaging. Unset, Firebase's default key is used. |

### Business rules (optional; defaults shown in `.env.example`)

`RIDER_EARNINGS_PER_DELIVERY`, `PENDING_PAYMENT_TIMEOUT_MINUTES`,
`GOOGLE_ROUTES_TRAVEL_MODE`, `CORS_ALLOWED_ORIGINS`

`DELIVERY_FEE` and `SERVICE_AREA_CENTER_LAT` / `_LNG` / `_RADIUS_KM` only give
the **first** values: they are copied into the database the first time the app
reads them, and from then on the store admin changes them in the Django admin
(**System → Store settings**) or through `/api/admin/store-settings/`.
Editing them in Render afterwards does nothing. The same page holds the
PayMongo and Maya fee rates the payments report uses; the defaults (2.5 % and
1.5 %) are placeholders, so set your real contract rates.

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

`cancel_stale_orders` cancels online orders that were never paid for. It is
not automatic. It also matters more with Maya: there is no Maya webhook, so a
customer who pays and then closes the app is only found out when the app syncs
or when this job asks Maya (it does, before cancelling any Maya order).

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

**Restore from the admin.** To put a downloaded backup back into the *running*
site, open the same page, scroll to **Restore from a backup file**, choose the
file, type `RESTORE` and press the red button. It **deletes everything currently
there** and loads the file instead, so download a fresh backup first. All
sessions end, so sign in again with an account *from the backup* (its username
and password at the time). A file that is bad changes nothing, and one with no
active admin account in it is refused, so you cannot lock yourself out. Do it
when nobody is ordering.

**Restore into a new database.** The free web service has no shell, so when the
old database is gone and the site cannot start, do this from your own computer:

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

**The APKs are not in the backup.** They are 50+ MB each. A restore from the
admin leaves them where they are, but a brand-new database starts without
them: upload them again under **System → App downloads** (step 10).

---

## 10. The public website and app downloads

`/` is the shop's website: the menu (live from the database), the delivery
area and fee, how ordering works, and a **Download the app** button. The text
under **About the shop** and the address, opening hours, phone and Facebook
link come from **Store settings → Website**; anything left blank is left off.

**Releases come from GitHub.** In the mobile repo, bump `version:` in each
app's `pubspec.yaml` (the number after `+` must go up every time) and push a
tag such as `v1.0.1`. The GitHub workflow builds the three APKs, attaches them
to a GitHub release, and announces each one to the backend at
`POST /api/app-releases/publish/`, authenticated with
`APP_RELEASE_PUBLISH_TOKEN`. From then on:

- the website's download buttons send people to the GitHub file;
- each app asks `GET /api/app-version/<customer|rider|admin>/` when it opens
  and offers the update when the published build number is higher than its
  own. Android still asks the user to confirm the install: apps installed
  outside the Play Store cannot update silently.

A published link must point at
`https://github.com/0jieE/yellow-Ribbon-Mobile/releases/download/`
(`APP_RELEASE_URL_PREFIX`), so a leaked token cannot send the apps elsewhere.
An older build number than the one published is refused.

**By hand, as a fallback**, under **System → App downloads**: either paste a
download link and version, or upload the `app-release.apk` itself
(`build/app/outputs/flutter-apk/`). An uploaded file is kept in Postgres in
1 MB pieces, because the free web service has no lasting disk and Cloudinary's
free plan refuses files over 10 MB; three 52 MB APKs take about 160 MB of the
free database's 1 GB. Publishing from GitHub drops an uploaded file.

Who can download what:

| App | Where | Who |
|---|---|---|
| Yellow Ribbon (customer) | the home page | anyone |
| Ribbon Rider | `/staff/`, after signing in | rider accounts (and store admins) |
| Ribbon Admin | `/staff/`, after signing in | store admins and superusers |

The sign-in only guards the website's button: a GitHub release asset can be
downloaded by anyone who has its link, and the repo is public. Riders still
need a rider account to use the app.

**Signing.** Every release must be signed with the same release key (kept as
a GitHub secret, with a copy backed up by you). Lose it and no update can
install over the apps again; each phone would have to uninstall first.

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
