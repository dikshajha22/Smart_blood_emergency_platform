/**
 * Map location picker.
 *
 * Binds a Leaflet map to a pair of hidden latitude/longitude inputs so a user can
 * pin their exact location. Supports click-to-place, marker dragging, browser
 * geolocation and free-text search via the public Nominatim endpoint.
 *
 * The hidden inputs remain the single source of truth: Django validates them
 * server-side, so a user with JS disabled or a tampered payload cannot store a
 * broken coordinate.
 */
(function () {
  "use strict";

  const TILE_URL = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
  const TILE_ATTRIBUTION =
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

  function buildPin(modifier) {
    return L.divIcon({
      className: "",
      html: '<div class="pin ' + (modifier || "") + '"><span></span></div>',
      iconSize: [34, 34],
      iconAnchor: [17, 34],
      popupAnchor: [0, -34],
    });
  }

  function LocationPicker(options) {
    this.mapEl = document.getElementById(options.mapId || "location-map");
    if (!this.mapEl || typeof L === "undefined") {
      return;
    }

    this.latInput = document.querySelector(options.latSelector || "#id_latitude");
    this.lngInput = document.querySelector(options.lngSelector || "#id_longitude");
    this.labelInput = document.querySelector(options.labelSelector || "#id_location_label");
    this.readout = document.querySelector(options.readoutSelector || "#location-readout");
    this.config = options.config || { lat: 0, lng: 0, zoom: 12, hasPin: false };

    this.map = L.map(this.mapEl, { scrollWheelZoom: true }).setView(
      [this.config.lat, this.config.lng],
      this.config.zoom
    );
    L.tileLayer(TILE_URL, { attribution: TILE_ATTRIBUTION, maxZoom: 19 }).addTo(this.map);

    this.marker = null;
    if (this.config.hasPin) {
      this.place(this.config.lat, this.config.lng, false);
    }

    this.map.on("click", (event) => {
      this.place(event.latlng.lat, event.latlng.lng, true);
    });

    this.bindControls(options);

    // Leaflet mis-measures its container when the map starts hidden (e.g. inside
    // a collapsed section); recalculating after layout settles fixes grey tiles.
    setTimeout(() => this.map.invalidateSize(), 200);
  }

  LocationPicker.prototype.place = function (lat, lng, updateInputs) {
    const position = [lat, lng];

    if (this.marker) {
      this.marker.setLatLng(position);
    } else {
      this.marker = L.marker(position, {
        icon: buildPin("pin--self"),
        draggable: true,
        keyboard: true,
        title: "Drag to fine-tune your location",
      }).addTo(this.map);

      this.marker.on("dragend", () => {
        const point = this.marker.getLatLng();
        this.write(point.lat, point.lng);
      });
    }

    if (updateInputs !== false) {
      this.write(lat, lng);
    } else {
      this.render(lat, lng);
    }
  };

  LocationPicker.prototype.write = function (lat, lng) {
    // Six decimals is ~0.1 m precision: plenty, and keeps the payload small.
    if (this.latInput) this.latInput.value = Number(lat).toFixed(6);
    if (this.lngInput) this.lngInput.value = Number(lng).toFixed(6);
    this.render(lat, lng);
    this.mapEl.dispatchEvent(
      new CustomEvent("location:changed", { detail: { lat: lat, lng: lng }, bubbles: true })
    );
  };

  LocationPicker.prototype.render = function (lat, lng) {
    if (this.readout) {
      this.readout.textContent =
        Number(lat).toFixed(5) + ", " + Number(lng).toFixed(5);
    }
  };

  LocationPicker.prototype.bindControls = function (options) {
    const locateBtn = document.querySelector(options.locateSelector || "#locate-me");
    if (locateBtn) {
      locateBtn.addEventListener("click", (event) => {
        event.preventDefault();
        this.locate(locateBtn);
      });
    }

    const searchBtn = document.querySelector(options.searchSelector || "#geocode-btn");
    const searchInput = document.querySelector(options.searchInputSelector || "#geocode-input");
    if (searchBtn && searchInput) {
      const run = (event) => {
        event.preventDefault();
        this.geocode(searchInput.value, searchBtn);
      };
      searchBtn.addEventListener("click", run);
      searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") run(event);
      });
    }
  };

  LocationPicker.prototype.locate = function (button) {
    if (!navigator.geolocation) {
      this.notify("This browser does not support location detection. Click the map instead.");
      return;
    }
    const original = button ? button.innerHTML : "";
    if (button) {
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span> Locating...';
    }

    navigator.geolocation.getCurrentPosition(
      (position) => {
        const lat = position.coords.latitude;
        const lng = position.coords.longitude;
        this.map.setView([lat, lng], 16);
        this.place(lat, lng, true);
        if (button) {
          button.disabled = false;
          button.innerHTML = original;
        }
      },
      () => {
        // Permission denied or unavailable: manual pinning still works.
        this.notify("Could not detect your location. Please click your spot on the map.");
        if (button) {
          button.disabled = false;
          button.innerHTML = original;
        }
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 60000 }
    );
  };

  LocationPicker.prototype.geocode = function (query, button) {
    const text = (query || "").trim();
    if (text.length < 3) {
      this.notify("Type at least three characters to search for a place.");
      return;
    }

    const url =
      "https://nominatim.openstreetmap.org/search?format=json&limit=1&q=" +
      encodeURIComponent(text);

    const original = button ? button.innerHTML : "";
    if (button) {
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span>';
    }

    fetch(url, { headers: { Accept: "application/json" } })
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then((results) => {
        if (!results || !results.length) {
          this.notify("No place matched that search. Try a different name.");
          return;
        }
        const lat = parseFloat(results[0].lat);
        const lng = parseFloat(results[0].lon);
        this.map.setView([lat, lng], 16);
        this.place(lat, lng, true);
        if (this.labelInput && results[0].display_name) {
          this.labelInput.value = results[0].display_name.slice(0, 255);
        }
      })
      .catch(() => this.notify("Place search is unavailable. Click the map to pin instead."))
      .finally(() => {
        if (button) {
          button.disabled = false;
          button.innerHTML = original;
        }
      });
  };

  LocationPicker.prototype.notify = function (message) {
    const target = document.getElementById("map-message");
    if (target) {
      target.textContent = message;
      target.hidden = false;
    } else {
      window.alert(message);
    }
  };

  window.LocationPicker = LocationPicker;
  window.buildMapPin = buildPin;
  window.MAP_TILE_URL = TILE_URL;
  window.MAP_TILE_ATTRIBUTION = TILE_ATTRIBUTION;
})();
