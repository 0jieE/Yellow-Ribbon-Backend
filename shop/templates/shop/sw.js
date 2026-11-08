/* Yellow Ribbon customer web app: service worker (served at /order/sw.js).
   Its only job is push notifications. Every push from the backend carries a
   visible notification (iOS requires that), which Firebase's SDK shows on its
   own; tapping it opens the order, from the link in the push. */

importScripts("https://www.gstatic.com/firebasejs/{{ firebase_version }}/firebase-app-compat.js");
importScripts("https://www.gstatic.com/firebasejs/{{ firebase_version }}/firebase-messaging-compat.js");

firebase.initializeApp({{ firebase_config|safe }});
firebase.messaging();

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

/* Notifications this app shows itself (a push that arrives while the app is
   open, see push.js) carry their link in data.link. */
self.addEventListener("notificationclick", (event) => {
    const link = event.notification.data && event.notification.data.link;
    if (!link) return;
    event.notification.close();
    event.waitUntil((async () => {
        const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
        for (const win of windows) {
            if ("focus" in win) {
                await win.navigate(link);
                return win.focus();
            }
        }
        return self.clients.openWindow(link);
    })());
});
