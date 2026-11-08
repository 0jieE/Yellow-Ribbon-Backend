/* Order page: keep the order current without a manual refresh.
   Polls the same order endpoint the Android app uses (signed in by the
   session). When the status changes the page reloads to show it; while the
   order is on its way, a map shows the rider moving towards the address. */
(function () {
    var config = JSON.parse(document.getElementById("track-config").textContent);
    var wrap = document.getElementById("track-wrap");
    var etaEl = document.getElementById("eta");
    var noteEl = document.getElementById("track-note");
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
        wrap.hidden = false;
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
                icon: L.divIcon({ className: "pin-rider", html: "", iconSize: [28, 28] }),
            }).addTo(map);
            map.fitBounds([here, config.destination], { padding: [40, 40], maxZoom: 17 });
        } else {
            rider.setLatLng(here);
        }
        if (order.route_polyline) {
            var path = decode(order.route_polyline);
            if (route) route.setLatLngs(path);
            else route = L.polyline(path, { color: "#C8302A", weight: 5, opacity: 0.85 }).addTo(map);
        }
        if (order.eta_minutes) {
            etaEl.hidden = false;
            etaEl.textContent = "Arriving in about " + order.eta_minutes + " min";
        }
        noteEl.textContent = (loc.rider_name ? loc.rider_name + " is" : "Your rider is") + " on the way. The map updates by itself.";
    }

    function check() {
        fetch(config.orderUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (order) {
                if (!order) return;
                if (order.status !== config.status) {
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
        timer = setInterval(check, config.status === "out_for_delivery" ? 8000 : 15000);
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
