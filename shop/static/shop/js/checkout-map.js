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
    var feeLabel = document.getElementById("fee-label");
    var totalEls = document.querySelectorAll("[data-total]");
    var form = document.getElementById("checkout-form");
    var button = document.getElementById("place");
    var subtotal = parseFloat(config.subtotal);

    var start = config.pin || config.center;
    var map = L.map("pin-map", { zoomControl: true, attributionControl: true }).setView(start, config.pin ? 17 : 15);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors",
    }).addTo(map);
    L.circle(config.center, {
        radius: config.radiusKm * 1000,
        color: "#1F1F1F",
        weight: 2,
        fillColor: "#FFC107",
        fillOpacity: 0.1,
        interactive: false,
    }).addTo(map);

    var placed = Boolean(config.pin);
    var outside = false;
    var quoteTimer = null;

    function peso(value) {
        return "₱" + Number(value).toFixed(2);
    }

    function say(text, warn) {
        statusEl.textContent = text;
        statusEl.classList.toggle("warn", Boolean(warn));
    }

    function paying() {
        var choice = form.querySelector('input[name="payment_type"]:checked');
        return choice && choice.value !== "cod";
    }

    // The same labels as the app's button.
    function label() {
        if (button.dataset.busy) return;
        button.disabled = outside;
        button.textContent = outside ? "OUTSIDE DELIVERY AREA" : paying() ? "PLACE ORDER & PAY" : "PLACE ORDER";
    }

    function quote(lat, lng) {
        clearTimeout(quoteTimer);
        quoteTimer = setTimeout(function () {
            fetch(config.quoteUrl + "?lat=" + lat + "&lng=" + lng, { credentials: "same-origin" })
                .then(function (r) { return r.ok ? r.json() : null; })
                .then(function (q) {
                    if (!q) return;
                    feeEl.textContent = peso(q.delivery_fee);
                    feeLabel.textContent = q.mode === "per_km" ? "Delivery fee (" + q.distance_km.toFixed(1) + " km)" : "Delivery fee";
                    totalEls.forEach(function (el) { el.textContent = peso(subtotal + parseFloat(q.delivery_fee)); });
                    outside = !q.within_service_area;
                    if (outside) {
                        say("That spot is " + q.distance_km.toFixed(1) + " km away, outside our delivery area (" +
                            config.radiusKm + " km). Move the pin inside the yellow circle.", true);
                    } else {
                        say("Pin placed, " + q.distance_km.toFixed(1) + " km from the shop.");
                    }
                    label();
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
    form.addEventListener("change", label);
    label();

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
        button.dataset.busy = "1";
        button.disabled = true;
        button.textContent = "PLACING YOUR ORDER…";
    });
})();
