"""
Firebase Cloud Messaging (spec section 9).

Firebase is used for messaging only — authentication is Django JWT.

Nothing in this module raises. A push failure is logged and swallowed: a
dead phone must never roll back an order. Every send is queued with
`transaction.on_commit`, so a notification is only ever sent about work
that actually committed.
"""

import json
import logging
import threading

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

# The notification channel all three apps create at startup (high importance)
# and name in their manifests. Without it an Android 8+ phone files the push
# under a default channel, which may not make a sound or pop up.
ANDROID_CHANNEL_ID = "yellowribbon_orders"

_init_lock = threading.Lock()
_app = None
_init_failed = False
_init_error = ""  # why initialisation failed, for the admin's test-push report


def _load_credential():
    """
    Build a firebase-admin credential, or return (None, reason).

    Two sources, in order:

    1. FIREBASE_CREDENTIALS_JSON — the service-account JSON itself, held in
       the environment. Hosted platforms hand you env vars, not a writable
       filesystem, so this is the one that works there.
    2. FIREBASE_CREDENTIALS_PATH — a path to the JSON file, for local dev
       where the file is on disk and gitignored.

    Never logs the credential: it carries a private key.
    """
    from firebase_admin import credentials

    raw = (settings.FIREBASE_CREDENTIALS_JSON or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None, (
                "FIREBASE_CREDENTIALS_JSON is not valid JSON. Store the whole "
                "service-account object, on one line."
            )

        if not isinstance(parsed, dict):
            return None, "FIREBASE_CREDENTIALS_JSON must be a JSON object."

        try:
            return credentials.Certificate(parsed), None
        except Exception as exc:
            return None, f"FIREBASE_CREDENTIALS_JSON was rejected: {exc}"

    path = (settings.FIREBASE_CREDENTIALS_PATH or "").strip()
    if path:
        if not settings.DEBUG:
            # A hosted container has no such file, and the service account
            # must not be committed to get one there. Say so loudly rather
            # than leaving push notifications mysteriously dead.
            logger.warning(
                "FIREBASE_CREDENTIALS_PATH is set with DEBUG=False. Hosted "
                "deployments have an ephemeral filesystem — set "
                "FIREBASE_CREDENTIALS_JSON instead."
            )
        try:
            return credentials.Certificate(path), None
        except Exception as exc:
            return None, f"Could not read FIREBASE_CREDENTIALS_PATH ({path}): {exc}"

    return None, (
        "Neither FIREBASE_CREDENTIALS_JSON nor FIREBASE_CREDENTIALS_PATH is "
        "set; push notifications are off."
    )


def _get_app():
    """
    Lazily initialise firebase-admin.

    Returns None when messaging is not configured, which is the normal state
    on a machine without credentials. One failed attempt is remembered so
    every push does not retry a missing file.
    """
    global _app, _init_failed, _init_error

    if _app is not None:
        return _app
    if _init_failed:
        return None

    with _init_lock:
        if _app is not None:
            return _app
        if _init_failed:
            return None

        try:
            import firebase_admin

            try:
                _app = firebase_admin.get_app()
                return _app
            except ValueError:
                pass

            credential, reason = _load_credential()
            if credential is None:
                logger.info("%s", reason)
                _init_failed = True
                _init_error = reason
                return None

            _app = firebase_admin.initialize_app(credential)
            logger.info(
                "Firebase messaging initialised from %s.",
                "FIREBASE_CREDENTIALS_JSON"
                if (settings.FIREBASE_CREDENTIALS_JSON or "").strip()
                else "FIREBASE_CREDENTIALS_PATH",
            )
            return _app
        except Exception:
            # No credential values reach the log — only the exception type
            # and message, which never echo the private key.
            logger.exception(
                "Could not initialise Firebase; push notifications are off."
            )
            _init_failed = True
            _init_error = "Firebase could not be initialised; see the server log."
            return None


def reset_app_cache():
    """Test hook — forget the memoised app and failure flag."""
    global _app, _init_failed, _init_error
    with _init_lock:
        _app = None
        _init_failed = False
        _init_error = ""


def _tokens_for(users):
    from accounts.models import DeviceToken

    user_ids = [u.pk for u in users if u is not None]
    if not user_ids:
        return []
    return list(
        DeviceToken.objects.filter(
            user_id__in=user_ids, user__is_active=True
        ).values_list("fcm_token", flat=True)
    )


def _delete_tokens(tokens):
    """Drop registrations Firebase says are dead."""
    if not tokens:
        return 0
    from accounts.models import DeviceToken

    deleted, _ = DeviceToken.objects.filter(fcm_token__in=tokens).delete()
    if deleted:
        logger.info("Removed %s unregistered device token(s).", deleted)
    return deleted


def _android_config(messaging):
    """High priority, filed under the apps' own notification channel."""
    return messaging.AndroidConfig(
        priority="high",
        notification=messaging.AndroidNotification(channel_id=ANDROID_CHANNEL_ID),
    )


def _dispatch(tokens, title, body, data):
    """The actual send. Called after commit; never raises."""
    try:
        app = _get_app()
        if app is None:
            return

        from firebase_admin import messaging

        message = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(title=title, body=body),
            # FCM data values must all be strings.
            data={k: str(v) for k, v in (data or {}).items()},
            android=_android_config(messaging),
        )

        response = messaging.send_each_for_multicast(message)

        dead = []
        for token, result in zip(tokens, response.responses):
            if result.success:
                continue
            exception = result.exception
            name = type(exception).__name__
            if isinstance(
                exception,
                (messaging.UnregisteredError, messaging.SenderIdMismatchError),
            ):
                dead.append(token)
            else:
                logger.warning("FCM send failed for one token: %s: %s", name, exception)

        _delete_tokens(dead)

        logger.info(
            "FCM '%s': %s sent, %s failed, %s pruned.",
            title,
            response.success_count,
            response.failure_count,
            len(dead),
        )

    except Exception:
        # Logged, never raised (spec section 9).
        logger.exception("Push notification failed; continuing regardless.")


def _dispatch_safely(tokens, title, body, data):
    """
    Outer guard for the post-commit callback.

    `_dispatch` already swallows its own errors, but an exception escaping an
    on_commit callback would surface as a 500 *after* the database has
    committed — the order lands and the client is told it failed. Belt and
    braces so the "never raises" guarantee does not depend on the internals
    of one function.
    """
    try:
        _dispatch(tokens, title, body, data)
    except Exception:
        logger.exception("Push dispatch raised after commit; ignoring.")


def notify_users(users, title, body, data=None):
    """
    Push to every active device token owned by `users`.

    Queued until the surrounding transaction commits. Safe to call from
    inside atomic blocks.
    """
    try:
        if not isinstance(users, (list, tuple, set)):
            users = [users]

        tokens = _tokens_for(users)
        if not tokens:
            # INFO, not DEBUG: "this person has no registered phone" is the
            # most common reason a push never arrives, and it must be visible
            # in the hosted logs.
            names = ", ".join(sorted(str(u.username) for u in users if u is not None))
            logger.info(
                "No device token for %s; nothing to push for '%s'.", names or "nobody", title
            )
            return

        transaction.on_commit(
            lambda: _dispatch_safely(tokens, title, body, data)
        )
    except Exception:
        logger.exception("Could not queue a push notification; continuing.")


def notify_admins(title, body, data=None):
    """Push to every active admin account."""
    try:
        from accounts.models import User

        admins = list(User.objects.filter(is_admin=True, is_active=True))
        if not admins:
            logger.info("No active admin accounts to notify for '%s'.", title)
            return
        notify_users(admins, title, body, data)
    except Exception:
        logger.exception("Could not queue an admin push notification; continuing.")



def send_test_push(targets):
    """
    Send a real test notification to specific device tokens and report each one.

    `targets` is a list of (label, fcm_token). Returns (error, results):
    `error` is a message when Firebase is not usable at all (and results is
    empty), otherwise None, and results is [(label, ok, detail)] where detail is
    "sent" or "ErrorClass: message".

    "sent" means Firebase accepted it; whether the phone shows it is up to the
    app. Unlike a normal push this never deletes a token Firebase calls dead:
    it is a diagnostic, so it reports the failure instead of hiding it.
    Never raises.
    """
    try:
        app = _get_app()
        if app is None:
            return _init_error or "Firebase is not configured.", []

        from firebase_admin import messaging

        results = []
        for label, token in targets:
            try:
                messaging.send(
                    messaging.Message(
                        token=token,
                        notification=messaging.Notification(
                            title="Yellow Ribbon test",
                            body="If you can read this, push notifications work.",
                        ),
                        data={"type": "test", "event": "test"},
                        android=_android_config(messaging),
                    )
                )
                results.append((label, True, "sent"))
            except Exception as exc:
                results.append((label, False, f"{type(exc).__name__}: {str(exc)[:160]}"))
        return None, results
    except Exception as exc:
        logger.exception("Test push failed unexpectedly.")
        return f"Unexpected error: {type(exc).__name__}", []
