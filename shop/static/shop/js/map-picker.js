/* The delivery pin, picked on a full-screen map like the Android app's "Pin
   delivery location" screen. Drag the pin or tap the map, check the banner
   says it is inside the delivery area, then confirm. Used by checkout and by
   Edit Profile (the saved location); only checkout has a fee and a button.

   Google Maps when the page has a browser key, OpenStreetMap otherwise, behind
   one small adapter so the rest does not care which. The delivery fee is then
   fetched for the pin from the same endpoint the Android app uses; the server
   checks the service area and prices the order again when it is placed. */
(function () {
    var config = JSON.parse(document.getElementById("map-config").textContent);
    var form = document.getElementById("checkout-form");
    var checkout = Boolean(form);
    var latInput = document.getElementById(checkout ? "id_delivery_latitude" : "id_default_latitude");
    var lngInput = document.getElementById(checkout ? "id_delivery_longitude" : "id_default_longitude");
    var feeEl = document.getElementById("fee");
    var feeLabel = document.getElementById("fee-label");
    var totalEls = document.querySelectorAll("[data-total]");
    var button = document.getElementById("place");
    var subtotal = parseFloat(config.subtotal);

    var emptyCard = document.getElementById("pin-empty");
    var setCard = document.getElementById("pin-set");
    var setTitle = document.getElementById("pin-title");
    var setCoords = document.getElementById("pin-coords");
    var setIcon = document.getElementById("pin-icon");
    var pinError = document.getElementById("pin-error");

    var picker = document.getElementById("picker");
    var zone = document.getElementById("zone");
    var zoneIcon = zone.querySelector(".ms");
    var zoneText = zone.querySelector("p");
    var pickerCoords = document.getElementById("picker-coords");
    var locateBtn = document.getElementById("locate");

    var ZOOM = 17;
    var pin = config.pin;            // [lat, lng] the order will use, or null
    var draft = null;                // the pin while the picker is open
    var outside = false;
    var map = null;

    function peso(value) { return "₱" + Number(value).toFixed(2); }
    function fixed(n) { return Number(n).toFixed(6); }

    // Straight-line distance, as the app's banner and the server's geofence use.
    function distanceKm(a, b) {
        var rad = Math.PI / 180, r = 6371;
        var dLat = (b[0] - a[0]) * rad, dLng = (b[1] - a[1]) * rad;
        var h = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
            Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
        return 2 * r * Math.asin(Math.sqrt(h));
    }

    function toast(text) {
        var list = document.querySelector(".snacks");
        if (!list) {
            list = document.createElement("ul");
            list.className = "snacks";
            list.setAttribute("role", "status");
            document.body.appendChild(list);
        }
        var item = document.createElement("li");
        item.className = "snack";
        item.textContent = text;
        list.appendChild(item);
        setTimeout(function () { item.remove(); }, 4600);
    }

    // -- the two map libraries behind one interface --------------------------

    function googleMap(el, start, onMove) {
        var g = window.google.maps;
        var m = new g.Map(el, {
            center: { lat: start[0], lng: start[1] },
            zoom: ZOOM,
            disableDefaultUI: true,
            zoomControl: true,
            // Clear of the area banner at the top and the confirm card below.
            zoomControlOptions: { position: g.ControlPosition.RIGHT_CENTER },
            clickableIcons: false,
            gestureHandling: "greedy",
        });
        new g.Circle({
            map: m,
            center: { lat: config.center[0], lng: config.center[1] },
            radius: config.radiusKm * 1000,
            fillColor: "#FFC107",
            fillOpacity: 0.12,
            strokeColor: "#FFC107",
            strokeWeight: 2,
            clickable: false,
        });
        var marker = new g.Marker({ map: m, position: { lat: start[0], lng: start[1] }, draggable: true, title: "Deliver here" });
        marker.addListener("dragend", function (e) { onMove([e.latLng.lat(), e.latLng.lng()]); });
        m.addListener("click", function (e) {
            marker.setPosition(e.latLng);
            onMove([e.latLng.lat(), e.latLng.lng()]);
        });
        return {
            moveTo: function (p, zoom) {
                marker.setPosition({ lat: p[0], lng: p[1] });
                m.panTo({ lat: p[0], lng: p[1] });
                if (zoom) m.setZoom(zoom);
            },
            refresh: function () {},
        };
    }

    function leafletMap(el, start, onMove) {
        var m = L.map(el, { zoomControl: false }).setView(start, ZOOM);
        // Clear of the area banner at the top and the confirm card below.
        L.control.zoom({ position: "topright" }).addTo(m);
        L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
            maxZoom: 19,
            attribution: "&copy; OpenStreetMap contributors",
        }).addTo(m);
        L.circle(config.center, {
            radius: config.radiusKm * 1000,
            color: "#FFC107",
            weight: 2,
            fillColor: "#FFC107",
            fillOpacity: 0.12,
            interactive: false,
        }).addTo(m);
        var marker = L.marker(start, { draggable: true, title: "Deliver here" }).addTo(m);
        marker.on("dragend", function () { var p = marker.getLatLng(); onMove([p.lat, p.lng]); });
        m.on("click", function (e) { marker.setLatLng(e.latlng); onMove([e.latlng.lat, e.latlng.lng]); });
        return {
            moveTo: function (p, zoom) { marker.setLatLng(p); m.setView(p, zoom || m.getZoom()); },
            refresh: function () { m.invalidateSize(); },
        };
    }

    // -- the picker ------------------------------------------------------------

    function showDraft() {
        var km = distanceKm(config.center, draft);
        var inside = km <= config.radiusKm;
        zone.classList.toggle("is-out", !inside);
        zoneIcon.textContent = inside ? "check_circle" : "error";
        zoneText.textContent = inside
            ? "Inside the delivery area (" + km.toFixed(1) + " km from the centre)."
            : "Outside the delivery area. We deliver within " + Math.round(config.radiusKm) + " km of " +
              config.city + " — this pin is " + km.toFixed(1) + " km away.";
        pickerCoords.textContent = "Pinned: " + fixed(draft[0]) + ", " + fixed(draft[1]);
    }

    function moveDraft(p) {
        draft = p;
        showDraft();
    }

    function open() {
        draft = (pin || config.center).slice();
        picker.hidden = false;
        document.body.classList.add("picker-open");
        if (!map) {
            var el = document.getElementById("picker-map");
            map = config.googleKey && window.google && window.google.maps
                ? googleMap(el, draft, moveDraft)
                : leafletMap(el, draft, moveDraft);
        } else {
            map.moveTo(draft, ZOOM);
        }
        map.refresh();
        showDraft();
        // The phone's back gesture closes the picker instead of leaving checkout.
        history.pushState({ picker: true }, "");
    }

    function close(fromHistory) {
        if (picker.hidden) return;
        picker.hidden = true;
        document.body.classList.remove("picker-open");
        if (!fromHistory && history.state && history.state.picker) history.back();
    }

    window.addEventListener("popstate", function () { close(true); });
    document.querySelectorAll("[data-picker-open]").forEach(function (el) { el.addEventListener("click", open); });
    document.querySelectorAll("[data-picker-close]").forEach(function (el) { el.addEventListener("click", function () { close(false); }); });

    document.getElementById("picker-confirm").addEventListener("click", function () {
        // Confirming outside the area is allowed, as in the app: the checkout
        // button and the server then say what to do.
        setPin(draft, checkout ? "Pinned on the map" : "Location saved");
        close(false);
    });

    locateBtn.addEventListener("click", function () {
        if (!navigator.geolocation) {
            toast("This phone cannot share its location. Drag the pin instead.");
            return;
        }
        locateBtn.disabled = true;
        navigator.geolocation.getCurrentPosition(
            function (pos) {
                locateBtn.disabled = false;
                var here = [pos.coords.latitude, pos.coords.longitude];
                map.moveTo(here, ZOOM);
                moveDraft(here);
                toast("Centred on your location. Drag the pin to set the drop-off.");
            },
            function () {
                locateBtn.disabled = false;
                toast("Could not get your location. Allow it in Settings, or just drag the pin instead.");
            },
            { enableHighAccuracy: true, timeout: 15000 }
        );
    });

    // -- checkout ----------------------------------------------------------------

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

    function showPin(title) {
        emptyCard.hidden = Boolean(pin);
        setCard.hidden = !pin;
        if (!pin) return;
        setCard.classList.toggle("is-out", outside);
        setIcon.textContent = outside ? "wrong_location" : "place";
        if (title) setTitle.textContent = title;
        setCoords.textContent = fixed(pin[0]) + ", " + fixed(pin[1]);
    }

    function quote() {
        fetch(config.quoteUrl + "?lat=" + fixed(pin[0]) + "&lng=" + fixed(pin[1]), { credentials: "same-origin" })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (q) {
                if (!q) return;
                feeEl.textContent = peso(q.delivery_fee);
                feeLabel.textContent = q.mode === "per_km" ? "Delivery fee (" + q.distance_km.toFixed(1) + " km)" : "Delivery fee";
                totalEls.forEach(function (el) { el.textContent = peso(subtotal + parseFloat(q.delivery_fee)); });
                outside = !q.within_service_area;
                showPin();
                label();
            })
            .catch(function () {});
    }

    function setPin(p, title) {
        pin = [p[0], p[1]];
        latInput.value = fixed(pin[0]);
        lngInput.value = fixed(pin[1]);
        outside = distanceKm(config.center, pin) > config.radiusKm;
        showPin(title);
        if (!checkout) return;
        pinError.hidden = true;
        label();
        quote();
    }

    if (pin) setPin(pin, checkout ? "Using your saved address" : "Location saved");
    else showPin();

    if (!checkout) return;

    form.addEventListener("change", label);
    label();

    form.addEventListener("submit", function (event) {
        if (!pin) {
            event.preventDefault();
            pinError.hidden = false;
            emptyCard.scrollIntoView({ behavior: "smooth", block: "center" });
            return;
        }
        button.dataset.busy = "1";
        button.disabled = true;
        button.textContent = "PLACING YOUR ORDER…";
    });
})();
