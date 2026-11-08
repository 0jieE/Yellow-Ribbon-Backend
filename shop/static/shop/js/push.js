/* Push notifications for the customer web app, through Firebase Cloud
   Messaging, the same service the Android app uses.

   iOS rules this follows: notifications only work once the app is on the
   Home Screen (iOS 16.4+); permission must be asked from a tap; Firebase is
   given this app's own service worker. */
(function () {
    var cards = document.querySelectorAll("[data-push-card]");
    if (!cards.length) return;

    var cfg = JSON.parse(document.getElementById("push-config").textContent);
    var csrf = document.querySelector('meta[name="csrf-token"]').content;
    var ua = navigator.userAgent;
    var isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    var installed = window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
    var supported = "serviceWorker" in navigator && "Notification" in window && "PushManager" in window;

    function each(selector, fn) {
        cards.forEach(function (card) {
            card.querySelectorAll(selector).forEach(fn);
        });
    }
    function say(text) { each("[data-push-status]", function (el) { el.textContent = text; }); }
    function showButton(on) { each("[data-push-on]", function (el) { el.hidden = !on; }); }
    function showCards(on) { cards.forEach(function (card) { card.hidden = !on; }); }

    var messaging = null;
    function firebaseMessaging() {
        if (!messaging) {
            if (!firebase.apps.length) firebase.initializeApp(cfg.firebase);
            messaging = firebase.messaging();
        }
        return messaging;
    }

    function register() {
        return navigator.serviceWorker.register(cfg.swUrl, { scope: "/order/" })
            .then(function () { return navigator.serviceWorker.ready; })
            .then(function (registration) {
                var options = { serviceWorkerRegistration: registration };
                if (cfg.vapidKey) options.vapidKey = cfg.vapidKey;
                return firebaseMessaging().getToken(options).then(function (token) {
                    if (!token) throw new Error("no token");
                    return fetch(cfg.registerUrl, {
                        method: "POST",
                        credentials: "same-origin",
                        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
                        body: JSON.stringify({ token: token }),
                    }).then(function (r) {
                        if (!r.ok) throw new Error("not saved");
                        listen(registration);
                    });
                });
            });
    }

    // While the app is open the system does not show the push, so show it here.
    function listen(registration) {
        firebaseMessaging().onMessage(function (payload) {
            var n = payload.notification || {};
            var link = (payload.fcmOptions && payload.fcmOptions.link) || "/order/orders/";
            registration.showNotification(n.title || "Yellow Ribbon", {
                body: n.body || "",
                icon: "/order/icons/icon-192.png",
                badge: "/order/icons/badge-72.png",
                data: { link: link },
            });
            document.dispatchEvent(new CustomEvent("yr:push"));
        });
    }

    if (!supported) {
        showCards(true);
        showButton(false);
        say(isIOS && !installed
            ? "On iPhone, add this app to your Home Screen first: tap Share, then Add to Home Screen. Open it from there to turn on notifications."
            : "This browser cannot show notifications.");
        return;
    }

    if (Notification.permission === "denied") {
        showCards(true);
        showButton(false);
        say("Notifications are blocked for this app. Turn them on in your phone's Settings, under Notifications.");
        return;
    }

    if (Notification.permission === "granted") {
        // Already allowed: keep this phone's registration current, quietly.
        showButton(false);
        register().then(function () {
            say("Notifications are on for this phone.");
        }).catch(function () {
            showCards(true);
            showButton(true);
            say("");
        });
        return;
    }

    showCards(true);
    showButton(true);
    each("[data-push-on]", function (button) {
        button.addEventListener("click", function () {
            button.disabled = true;
            say("Asking your phone…");
            // First thing in the tap: iOS only shows the prompt from a user action.
            Notification.requestPermission().then(function (permission) {
                if (permission !== "granted") {
                    button.disabled = false;
                    say("Notifications stay off. You can turn them on later from this page.");
                    return;
                }
                return register().then(function () {
                    showButton(false);
                    say("Notifications are on for this phone.");
                });
            }).catch(function () {
                button.disabled = false;
                say("Could not turn notifications on. Check your connection and try again.");
            });
        });
    });
})();
