/* Order page: keep the order current without a manual refresh.
   Polls the same order endpoint the Android app uses (signed in by the
   session), or the payment sync while an online payment is awaited. When the
   status changes the page reloads to show it; while the
   order is on its way, a map shows the rider moving towards the address. */
(function () {
    var config = JSON.parse(document.getElementById("track-config").textContent);
    var wrap = document.getElementById("track-wrap");
    var box = document.getElementById("track-box");
    var etaEl = document.getElementById("eta");
    var nameEl = document.getElementById("rider-name");
    var updatedEl = document.getElementById("rider-updated");
    var map = null, rider = null, route = null;
    var timer = null;
    var FINISHED = ["delivered", "cancelled"];

    function decode(encoded) {
        // Google's encoded polyline format, as stored in route_polyline.
        var points = [], index = 0, lat = 0, lng = 0;
        while (index < encoded.length) {
            var shift = 0, result = 0, byte;
            do { byte = encoded.charCodeAt(index++) - 63; result |= (byte & 31) << shift; shift += 5; } while (byte >= 32);
            lat += result & 1 ? ~(result >> 1) : result >> 1;
            shift = 0; result = 0;
            do { byte = encoded.charCodeAt(index++) - 63; result |= (byte & 31) << shift; shift += 5; } while (byte >= 32);
            lng += result & 1 ? ~(result >> 1) : result >> 1;
            points.push([lat / 1e5, lng / 1e5]);
        }
        return points;
    }

    function ensureMap() {
        if (map) return;
        // The app swaps the status box for the map once a rider is on the road.
        wrap.hidden = false;
        box.hidden = true;
        map = L.map("track-map", { zoomControl: false }).setView(config.destination, 15);
        L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution: "&copy; OpenStreetMap contributors",
        }).addTo(map);
        L.marker(config.destination, {
            title: "Your address",
            icon: L.divIcon({ className: "pin-home", html: "", iconSize: [22, 22] }),
        }).addTo(map);
    }

    function show(order) {
        var loc = order.rider_location;
        if (order.status !== "out_for_delivery" || !loc) return;
        ensureMap();
        var here = [parseFloat(loc.latitude), parseFloat(loc.longitude)];
        if (!rider) {
            rider = L.marker(here, {
                title: "Your rider",
                icon: L.divIcon({ className: "pin-rider", html: "", iconSize: [24, 24] }),
            }).addTo(map);
            map.fitBounds([here, config.destination], { padding: [40, 40], maxZoom: 17 });
        } else {
            rider.setLatLng(here);
        }
        if (order.route_polyline) {
            var path = decode(order.route_polyline);
            if (route) route.setLatLngs(path);
            else route = L.polyline(path, { color: "#00695C", weight: 5, opacity: 0.9, lineCap: "round", lineJoin: "round" }).addTo(map);
        }
        nameEl.textContent = loc.rider_name || "Your rider";
        updatedEl.textContent = loc.updated_at ? "Updated " + since(loc.updated_at) : "";
        if (order.eta_minutes) {
            etaEl.classList.remove("is-waiting");
            etaEl.lastElementChild.textContent = "Arriving in about " + order.eta_minutes + " min";
        }
    }

    function since(iso) {
        var seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
        if (seconds < 60) return "just now";
        var minutes = Math.round(seconds / 60);
        return minutes < 60 ? minutes + " min ago" : Math.round(minutes / 60) + " h ago";
    }

    var recentre = document.getElementById("recentre");
    if (recentre) {
        recentre.addEventListener("click", function () {
            if (map && rider) map.setView(rider.getLatLng(), 16);
        });
    }

    // Awaiting an online payment: ask the gateway itself (it settles a GCash
    // payment whose webhook went missing, and every Maya payment).
    function syncPayment() {
        var csrf = document.querySelector('meta[name="csrf-token"]').content;
        fetch(config.syncUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": csrf },
            body: JSON.stringify({ order_id: config.orderId }),
        })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (result) {
                if (!result) return;
                if (result.order_status !== config.status || result.payment_status !== config.paymentStatus) {
                    window.location.reload();
                }
            })
            .catch(function () {});
    }

    function check() {
        if (config.syncUrl) {
            syncPayment();
            return;
        }
        fetch(config.orderUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (order) {
                if (!order) return;
                var leg = order.rider_location ? order.rider_location.delivery_status : null;
                // "Picked up" becomes "On the way" without the order status changing.
                if (order.status !== config.status || (leg && config.deliveryStatus && leg !== config.deliveryStatus)) {
                    window.location.reload();
                    return;
                }
                show(order);
            })
            .catch(function () {});
    }

    function start() {
        if (FINISHED.indexOf(config.status) !== -1) return;
        stop();
        check();
        timer = setInterval(check, config.status === "out_for_delivery" || config.syncUrl ? 8000 : 15000);
    }

    function stop() {
        if (timer) clearInterval(timer);
        timer = null;
    }

    // Phones pause hidden pages anyway; this also catches the moment the
    // customer comes back from paying or from a notification.
    document.addEventListener("visibilitychange", function () {
        if (document.hidden) stop(); else start();
    });
    window.addEventListener("pageshow", function (e) { if (e.persisted) start(); });
    document.addEventListener("yr:push", check);
    start();
})();
