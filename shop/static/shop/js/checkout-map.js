/* Checkout: place the delivery pin by moving the map under a fixed pin.
   The pin's position fills the hidden latitude/longitude fields, and the
   delivery fee is fetched for it from the same endpoint the Android app uses.
   The server checks the service area and works out the real fee again when
   the order is placed. */
(function () {
    var config = JSON.parse(document.getElementById("map-config").textContent);
    var latInput = document.getElementById("id_delivery_latitude");
    var lngInput = document.getElementById("id_delivery_longitude");
    var statusEl = document.getElementById("map-status");
    var feeEl = document.getElementById("fee");
    var totalEl = document.getElementById("total");
    var form = document.getElementById("checkout-form");
    var subtotal = parseFloat(config.subtotal);

    var start = config.pin || config.center;
    var map = L.map("pin-map", { zoomControl: true, attributionControl: true }).setView(start, config.pin ? 17 : 15);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);
    L.circle(config.center, {
        radius: config.radiusKm * 1000,
        color: "#3A1A0C",
        weight: 2,
        fillColor: "#F4B708",
        fillOpacity: 0.08,
        interactive: false,
    }).addTo(map);

    var placed = Boolean(config.pin);
    var quoteTimer = null;

    function peso(value) {
        return "₱" + Number(value).toFixed(2);
    }

    function say(text, warn) {
        statusEl.textContent = text;
        statusEl.classList.toggle("warn", Boolean(warn));
    }

    function quote(lat, lng) {
        clearTimeout(quoteTimer);
        quoteTimer = setTimeout(function () {
            fetch(config.quoteUrl + "?lat=" + lat + "&lng=" + lng, { credentials: "same-origin" })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (q) {
                    if (!q) return;
                    feeEl.textContent = peso(q.delivery_fee);
                    totalEl.textContent = peso(subtotal + parseFloat(q.delivery_fee));
                    if (q.within_service_area) {
                        say("Pin placed, " + q.distance_km.toFixed(1) + " km from the shop.");
                    } else {
                        say("That spot is " + q.distance_km.toFixed(1) + " km away, outside our delivery area (" +
                            config.radiusKm + " km). Move the pin inside the yellow circle.", true);
                    }
                })
                .catch(function () {});
        }, 350);
    }

    function record() {
        var c = map.getCenter();
        var lat = c.lat.toFixed(6);
        var lng = c.lng.toFixed(6);
        latInput.value = lat;
        lngInput.value = lng;
        placed = true;
        quote(lat, lng);
    }

    if (placed) {
        record();
    } else {
        say("Move the map until the pin is on your door.");
    }

    map.on("dragend zoomend", record);

    document.getElementById("locate").addEventListener("click", function () {
        if (!navigator.geolocation) {
            say("This phone cannot share its location. Move the map instead.", true);
            return;
        }
        say("Finding you…");
        navigator.geolocation.getCurrentPosition(
            function (pos) {
                map.setView([pos.coords.latitude, pos.coords.longitude], 18);
                record();
            },
            function () {
                say("Location is off for this app. Move the map to your door instead.", true);
            },
            { enableHighAccuracy: true, timeout: 15000 }
        );
    });

    form.addEventListener("submit", function (event) {
        if (!placed) {
            event.preventDefault();
            say("Move the map so the pin is on your door first.", true);
            statusEl.scrollIntoView({ behavior: "smooth", block: "center" });
            return;
        }
        var button = document.getElementById("place");
        button.disabled = true;
        button.textContent = "Placing your order…";
    });
})();
