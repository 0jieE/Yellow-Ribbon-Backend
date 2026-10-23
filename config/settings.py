"""
Django settings for the Yellow Ribbon Pizza backend.

All environment-specific values come from `.env` (see `.env.example`).
Nothing secret is hardcoded here.
"""

import sys
from datetime import timedelta
from pathlib import Path

import dj_database_url
import environ

from .unfold_settings import UNFOLD  # noqa: F401  (read by django-unfold)

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, []),
    DELIVERY_FEE=(str, "30.00"),
    RIDER_EARNINGS_PER_DELIVERY=(str, "30.00"),
    PENDING_PAYMENT_TIMEOUT_MINUTES=(int, 30),
    SERVICE_AREA_CENTER_LAT=(float, 7.0083),
    SERVICE_AREA_CENTER_LNG=(float, 125.0894),
    SERVICE_AREA_RADIUS_KM=(float, 8.0),
    GOOGLE_MAPS_SERVER_KEY=(str, ""),
    GOOGLE_ROUTES_TIMEOUT_SECONDS=(float, 5.0),
    GOOGLE_ROUTES_TRAVEL_MODE=(str, "TWO_WHEELER"),
    MAYA_PUBLIC_KEY=(str, ""),
    MAYA_SECRET_KEY=(str, ""),
    MAYA_BASE_URL=(str, "https://pg-sandbox.paymaya.com"),
    MAYA_TIMEOUT_SECONDS=(float, 15.0),
    PAYMONGO_PUBLIC_KEY=(str, ""),
    PAYMONGO_SECRET_KEY=(str, ""),
    PAYMONGO_WEBHOOK_SECRET=(str, ""),
    PAYMONGO_TIMEOUT_SECONDS=(float, 15.0),
    PAYMONGO_WEBHOOK_TOLERANCE_SECONDS=(int, 0),
    PAYMONGO_SUCCESS_REDIRECT_URL=(str, ""),
    PAYMONGO_FAILED_REDIRECT_URL=(str, ""),
    FIREBASE_CREDENTIALS_PATH=(str, ""),
    FIREBASE_CREDENTIALS_JSON=(str, ""),
    CSRF_TRUSTED_ORIGINS=(list, []),
    # Cloudinary. Absent locally, which keeps uploads on the filesystem.
    CLOUDINARY_CLOUD_NAME=(str, ""),
    CLOUDINARY_API_KEY=(str, ""),
    CLOUDINARY_API_SECRET=(str, ""),
    # Render sets RENDER=true in every service. Used only to decide whether
    # the database connection should insist on TLS.
    RENDER=(bool, False),
    DATABASE_SSL_REQUIRE=(bool, None),
    SECURE_SSL_REDIRECT=(bool, None),
)

environ.Env.read_env(BASE_DIR / ".env")


# ---------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------

SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")


INSTALLED_APPS = [
    # Unfold has to come before django.contrib.admin: it overrides the admin
    # templates, and Django takes the first match it finds.
    "unfold",
    "unfold.contrib.filters",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    # Local
    "core",
    "accounts",
    "menu",
    "orders",
    "delivery",
    "payments",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Immediately after SecurityMiddleware and above everything else, as
    # WhiteNoise requires — it must answer for a static file before the
    # session, auth and CSRF layers do any work.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Project-level overrides; templates/admin/index.html is the dashboard.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------
# Database — PostgreSQL, from DATABASE_URL
# ---------------------------------------------------------------------

# Managed Postgres terminates TLS, so require it anywhere that is not a
# local box. DATABASE_SSL_REQUIRE overrides the guess either way.
_ssl_require = env("DATABASE_SSL_REQUIRE")
if _ssl_require is None:
    _ssl_require = env("RENDER") or not DEBUG

DATABASES = {
    "default": dj_database_url.config(
        default=env("DATABASE_URL"),
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=_ssl_require,
    )
}


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------

AUTH_USER_MODEL = "accounts.User"

# Unfold's login form has no hidden `next` field, so signing in from a bare
# /admin/login/ would fall through to Django's default, /accounts/profile/,
# which does not exist. Only the admin uses session login; the apps use JWT.
LOGIN_REDIRECT_URL = "admin:index"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------
# Django REST Framework + SimpleJWT
# ---------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",  # for browsable API
    ),
    # Fail closed. Every view still declares its own permission_classes.
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    # Uniform {"error": "message"} responses (spec section 13).
    "EXCEPTION_HANDLER": "core.exceptions.api_exception_handler",
    "DEFAULT_PARSER_CLASSES": (
        "rest_framework.parsers.JSONParser",
        "rest_framework.parsers.MultiPartParser",
        "rest_framework.parsers.FormParser",
    ),
}

if DEBUG:
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] += (
        "rest_framework.renderers.BrowsableAPIRenderer",
    )

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "AUTH_HEADER_TYPES": ("Bearer",),
    "UPDATE_LAST_LOGIN": True,
}


# ---------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------

# Native Flutter clients do not enforce CORS; this is for Flutter web
# during development and for the browsable API.
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")

# Needed for the Django admin login form once the site is on HTTPS behind a
# proxy. Set to the full origin, scheme included: https://your.onrender.com
CSRF_TRUSTED_ORIGINS = env("CSRF_TRUSTED_ORIGINS")


# ---------------------------------------------------------------------
# Production hardening — only takes effect with DEBUG=False
# ---------------------------------------------------------------------

# Render terminates TLS at its edge and forwards over plain HTTP with this
# header. Without it Django thinks every request is insecure, and an HTTPS
# redirect would loop forever.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

# On by default whenever DEBUG is off. The override exists so the stack can
# be exercised with DEBUG=False over plain http locally — without it, every
# request 301s to an https port nothing is listening on.
_ssl_redirect = env("SECURE_SSL_REDIRECT")
SECURE_SSL_REDIRECT = (not DEBUG) if _ssl_redirect is None else _ssl_redirect

if not DEBUG:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    X_FRAME_OPTIONS = "DENY"
    # Start short. Raise it once the domain is settled — HSTS is hard to
    # walk back, since browsers cache the instruction.
    SECURE_HSTS_SECONDS = 3600
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True


# ---------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------
# File storage
# ---------------------------------------------------------------------

# Render's disk is ephemeral: every deploy replaces the container and takes
# uploaded files with it. Menu photos could be re-uploaded, but a proof of
# delivery is evidence that a delivery happened, so it has to outlive the
# container. Cloudinary holds media when configured; without those three
# vars — the normal state on a development machine — uploads stay on the
# local filesystem and nothing about local work changes.
CLOUDINARY_CLOUD_NAME = env("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = env("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = env("CLOUDINARY_API_SECRET")

USE_CLOUDINARY = bool(
    CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET
)

if USE_CLOUDINARY:
    CLOUDINARY_STORAGE = {
        "CLOUD_NAME": CLOUDINARY_CLOUD_NAME,
        "API_KEY": CLOUDINARY_API_KEY,
        "API_SECRET": CLOUDINARY_API_SECRET,
        # Uploaded files are public — the rider app and admin app fetch
        # proof photos and menu images directly by URL.
        "SECURE": True,
    }
    INSTALLED_APPS = INSTALLED_APPS + ["cloudinary", "cloudinary_storage"]
    _media_backend = "cloudinary_storage.storage.MediaCloudinaryStorage"
else:
    _media_backend = "django.core.files.storage.FileSystemStorage"

STORAGES = {
    "default": {"BACKEND": _media_backend},
    "staticfiles": {
        # Hashes filenames and gzip/brotli-compresses them, so WhiteNoise can
        # serve the admin CSS with a far-future cache header. Manifest storage
        # raises at collectstatic time if a referenced file is missing, which
        # is a build failure rather than an unstyled admin in production.
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# The test runner forces DEBUG off, which makes every {% static %} look the
# file up in a manifest that only `collectstatic` writes. A fresh clone has no
# manifest, so every admin page would 500 in the tests. Plain storage needs
# none; production and development keep the manifest storage above.
if sys.argv[1:2] == ["test"]:
    STORAGES["staticfiles"] = {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    }


# ---------------------------------------------------------------------
# Business rules
# ---------------------------------------------------------------------

# DELIVERY_FEE and SERVICE_AREA_* are only the *initial* values: core.StoreSettings
# copies them into the database the first time it is read, and from then on
# the admin edits them there (Django admin, or /api/admin/store-settings/).
# Changing them in the environment afterwards has no effect.
# Kept as strings so the app layer can build exact Decimals from them.
DELIVERY_FEE = env("DELIVERY_FEE")

# Snapshotted onto Delivery.rider_earnings at assignment. The spec fixes the
# snapshot rule but not the rate; this default pays the rider the whole
# delivery fee. Change it here, not in code.
RIDER_EARNINGS_PER_DELIVERY = env("RIDER_EARNINGS_PER_DELIVERY")

# How long a GCash order may sit in pending_payment before
# `manage.py cancel_stale_orders` reaps it.
PENDING_PAYMENT_TIMEOUT_MINUTES = env("PENDING_PAYMENT_TIMEOUT_MINUTES")

SERVICE_AREA_CENTER_LAT = env("SERVICE_AREA_CENTER_LAT")
SERVICE_AREA_CENTER_LNG = env("SERVICE_AREA_CENTER_LNG")
SERVICE_AREA_RADIUS_KM = env("SERVICE_AREA_RADIUS_KM")


# ---------------------------------------------------------------------
# Third-party integrations (wired up in later build steps)
# ---------------------------------------------------------------------

GOOGLE_MAPS_SERVER_KEY = env("GOOGLE_MAPS_SERVER_KEY")

# Kept short so a slow Maps call never stalls rider assignment.
GOOGLE_ROUTES_TIMEOUT_SECONDS = env("GOOGLE_ROUTES_TIMEOUT_SECONDS")

# TWO_WHEELER suits motorcycle delivery and is supported in the Philippines.
# Switch to DRIVE if the Routes API ever refuses it for this region.
GOOGLE_ROUTES_TRAVEL_MODE = env("GOOGLE_ROUTES_TRAVEL_MODE")

# Maya Checkout. The sandbox keys are public and shared by every developer, so
# there is no webhook: payments are verified by looking them up (payments/maya.py).
# Production is https://pg.paymaya.com with the merchant's own keys.
MAYA_PUBLIC_KEY = env("MAYA_PUBLIC_KEY")
MAYA_SECRET_KEY = env("MAYA_SECRET_KEY")
MAYA_BASE_URL = env("MAYA_BASE_URL")
MAYA_TIMEOUT_SECONDS = env("MAYA_TIMEOUT_SECONDS")

PAYMONGO_PUBLIC_KEY = env("PAYMONGO_PUBLIC_KEY")
PAYMONGO_SECRET_KEY = env("PAYMONGO_SECRET_KEY")
PAYMONGO_WEBHOOK_SECRET = env("PAYMONGO_WEBHOOK_SECRET")
PAYMONGO_TIMEOUT_SECONDS = env("PAYMONGO_TIMEOUT_SECONDS")

# Where PayMongo sends the customer after the GCash page. PayMongo only
# accepts http(s) URLs — a custom app scheme is rejected outright — so these
# point at web pages that deep-link into customer_app. Leave blank and the
# checkout view derives them from the incoming request, which is what you
# want behind a tunnel.
PAYMONGO_SUCCESS_REDIRECT_URL = env("PAYMONGO_SUCCESS_REDIRECT_URL")
PAYMONGO_FAILED_REDIRECT_URL = env("PAYMONGO_FAILED_REDIRECT_URL")

# Replay window for Paymongo-Signature, in seconds. 0 disables the age check.
# Off by default: settlement is already idempotent, so a replayed event is
# harmless, whereas a strict window risks rejecting a legitimate PayMongo
# retry that carries its original timestamp. Set it once you have confirmed
# how PayMongo stamps retries.
PAYMONGO_WEBHOOK_TOLERANCE_SECONDS = env("PAYMONGO_WEBHOOK_TOLERANCE_SECONDS")

# Two ways to supply the Firebase service account, checked in this order:
#   FIREBASE_CREDENTIALS_JSON — the JSON itself, for hosts that give you env
#                               vars rather than a filesystem
#   FIREBASE_CREDENTIALS_PATH — a path to the gitignored JSON file, local dev
FIREBASE_CREDENTIALS_JSON = env("FIREBASE_CREDENTIALS_JSON")
FIREBASE_CREDENTIALS_PATH = env("FIREBASE_CREDENTIALS_PATH")


# ---------------------------------------------------------------------
# Logging — integration failures are logged, never raised
# ---------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {asctime} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
