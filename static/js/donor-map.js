/**
 * Live donor search map.
 *
 * Renders AI-ranked donors as coloured pins plus a synchronised result list.
 * Results refresh when the user changes a filter, drags the search centre, or on
 * a polling timer - which is what makes the search feel real-time without a
 * WebSocket layer.
 */
(function () {
  "use strict";

  function DonorMap(config) {
    this.config = config;
    this.mapEl = document.getElementById("map");
    if (!this.mapEl || typeof L === "undefined") return;

    this.center = config.center;
    this.markers = new Map();
    this.selected = new Set();
    this.results = [];
    this.pollTimer = null;
    this.abortController = null;

    this.listEl = document.getElementById("donor-results");
    this.countEl = document.getElementById("result-count");
    this.statusEl = document.getElementById("search-status");
    this.selectionBar = document.getElementById("selection-bar");
    this.selectedCountEl = document.getElementById("selected-count");
    this.donorIdsInput = document.getElementById("id_donor_ids");

    this.buildMap();
    this.bindFilters();
    this.search();
    this.startPolling();
  }

  DonorMap.prototype.buildMap = function () {
    this.map = L.map(this.mapEl).setView([this.center.lat, this.center.lng], this.center.zoom || 12);
    L.tileLayer(window.MAP_TILE_URL, {
      attribution: window.MAP_TILE_ATTRIBUTION,
      maxZoom: 19,
    }).addTo(this.map);

    // Draggable origin marker: moving it re-runs the search from the new point.
    this.originMarker = L.marker([this.center.lat, this.center.lng], {
      icon: window.buildMapPin("pin--request"),
      draggable: true,
      title: "Drag to search from a different point",
    })
      .addTo(this.map)
      .bindPopup("Search centre - drag to move");

    this.originMarker.on("dragend", () => {
      const point = this.originMarker.getLatLng();
      this.center = { lat: point.lat, lng: point.lng, zoom: this.map.getZoom() };
      this.search();
    });

    this.radiusCircle = L.circle([this.center.lat, this.center.lng], {
      radius: this.currentRadius() * 1000,
      color: "#e11d48",
      weight: 1.5,
      opacity: 0.65,
      fillColor: "#e11d48",
      fillOpacity: 0.06,
    }).addTo(this.map);

    this.layer = L.layerGroup().addTo(this.map);
    setTimeout(() => this.map.invalidateSize(), 200);
  };

  DonorMap.prototype.currentRadius = function () {
    const input = document.getElementById("radius-input");
    const value = input ? parseFloat(input.value) : this.config.radiusKm;
    return isNaN(value) ? 10 : value;
  };

  DonorMap.prototype.bindFilters = function () {
    const radius = document.getElementById("radius-input");
    const radiusOut = document.getElementById("radius-output");

    if (radius) {
      radius.addEventListener("input", () => {
        if (radiusOut) radiusOut.textContent = radius.value + " km";
        this.radiusCircle.setRadius(parseFloat(radius.value) * 1000);
      });
      // Only re-query when the user releases the slider, not on every pixel.
      radius.addEventListener("change", () => this.search());
    }

    ["blood-group-input", "available-input", "verified-input", "exact-input"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("change", () => this.search());
    });

    const refresh = document.getElementById("refresh-btn");
    if (refresh) {
      refresh.addEventListener("click", (event) => {
        event.preventDefault();
        this.search();
      });
    }

    const locate = document.getElementById("locate-search");
    if (locate) {
      locate.addEventListener("click", (event) => {
        event.preventDefault();
        if (!navigator.geolocation) return;
        navigator.geolocation.getCurrentPosition((position) => {
          this.center = {
            lat: position.coords.latitude,
            lng: position.coords.longitude,
            zoom: 14,
          };
          this.map.setView([this.center.lat, this.center.lng], 14);
          this.originMarker.setLatLng([this.center.lat, this.center.lng]);
          this.radiusCircle.setLatLng([this.center.lat, this.center.lng]);
          this.search();
        });
      });
    }

    const selectTop = document.getElementById("select-top");
    if (selectTop) {
      selectTop.addEventListener("click", (event) => {
        event.preventDefault();
        this.selectTop(parseInt(selectTop.dataset.count || "5", 10));
      });
    }

    const clearBtn = document.getElementById("clear-selection");
    if (clearBtn) {
      clearBtn.addEventListener("click", (event) => {
        event.preventDefault();
        this.selected.clear();
        this.renderResults();
        this.syncSelection();
      });
    }
  };

  DonorMap.prototype.buildUrl = function () {
    const params = new URLSearchParams();
    params.set("lat", this.center.lat);
    params.set("lng", this.center.lng);
    params.set("radius", this.currentRadius());

    const group = document.getElementById("blood-group-input");
    if (group && group.value) params.set("blood_group", group.value);

    const available = document.getElementById("available-input");
    params.set("available", available && !available.checked ? "0" : "1");

    const verified = document.getElementById("verified-input");
    if (verified && verified.checked) params.set("verified", "1");

    const exact = document.getElementById("exact-input");
    if (exact && exact.checked) params.set("exact", "1");

    if (this.config.requestId) params.set("request", this.config.requestId);

    return this.config.searchUrl + "?" + params.toString();
  };

  DonorMap.prototype.search = function () {
    // Cancel any in-flight request so a slow earlier response cannot overwrite
    // the results of a newer search.
    if (this.abortController) this.abortController.abort();
    this.abortController = new AbortController();

    this.setStatus("Searching...", true);
    this.radiusCircle.setLatLng([this.center.lat, this.center.lng]);
    this.radiusCircle.setRadius(this.currentRadius() * 1000);

    fetch(this.buildUrl(), {
      headers: { "X-Requested-With": "XMLHttpRequest", Accept: "application/json" },
      signal: this.abortController.signal,
    })
      .then((response) => {
        if (!response.ok) return response.json().then((body) => Promise.reject(body));
        return response.json();
      })
      .then((data) => {
        this.results = data.results || [];
        this.renderMarkers();
        this.renderResults();
        this.setStatus(
          this.results.length
            ? "Ranked by " + (data.ranked_by === "ai" ? "AI match score" : "distance") +
                " - " + (data.model || "")
            : "No donors found in this area",
          false
        );
        if (this.countEl) this.countEl.textContent = this.results.length;
      })
      .catch((error) => {
        if (error && error.name === "AbortError") return;
        this.setStatus((error && error.error) || "Search failed. Please retry.", false);
      });
  };

  DonorMap.prototype.setStatus = function (message, busy) {
    if (!this.statusEl) return;
    this.statusEl.innerHTML = busy
      ? '<span class="spinner"></span> ' + message
      : message;
  };

  DonorMap.prototype.renderMarkers = function () {
    this.layer.clearLayers();
    this.markers.clear();

    this.results.forEach((donor) => {
      if (donor.latitude == null || donor.longitude == null) return;

      const tier = donor.tier || "low";
      const label = donor.match_percent != null ? donor.match_percent + "%" : donor.blood_group;

      const marker = L.marker([donor.latitude, donor.longitude], {
        icon: L.divIcon({
          className: "",
          html:
            '<div class="pin pin--' + tier + '"><span>' + label + "</span></div>",
          iconSize: [34, 34],
          iconAnchor: [17, 34],
          popupAnchor: [0, -34],
        }),
        title: donor.name,
      });

      marker.bindPopup(this.popupHtml(donor));
      marker.on("click", () => this.highlight(donor.id));
      marker.addTo(this.layer);
      this.markers.set(donor.id, marker);
    });
  };

  DonorMap.prototype.popupHtml = function (donor) {
    // Values come from our own API and are inserted as text nodes below, so this
    // builds the shell only.
    const wrap = document.createElement("div");
    wrap.className = "map-popup";

    const name = document.createElement("div");
    name.className = "map-popup__name";
    name.textContent = donor.name;
    wrap.appendChild(name);

    const group = document.createElement("div");
    group.className = "map-popup__row";
    group.textContent =
      donor.blood_group + " - " + (donor.distance_display || donor.distance_km + " km");
    wrap.appendChild(group);

    if (donor.match_percent != null) {
      const score = document.createElement("div");
      score.className = "map-popup__row";
      score.textContent = "Match score: " + donor.match_percent + "%";
      wrap.appendChild(score);
    }

    const reliability = document.createElement("div");
    reliability.className = "map-popup__row";
    reliability.textContent = "Reliability: " + (donor.reliability_percent || 0) + "%";
    wrap.appendChild(reliability);

    if (this.donorIdsInput) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-primary btn-sm";
      button.style.marginTop = "8px";
      button.textContent = "Select this donor";
      button.addEventListener("click", () => this.toggle(donor.id));
      wrap.appendChild(button);
    }

    return wrap;
  };

  DonorMap.prototype.renderResults = function () {
    if (!this.listEl) return;
    this.listEl.textContent = "";

    if (!this.results.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.innerHTML =
        '<div class="empty__icon"><i class="fa-solid fa-magnifying-glass"></i></div>' +
        "<h3>No donors here</h3>" +
        "<p>Try widening the radius, or allowing compatible blood groups instead of an exact match.</p>";
      this.listEl.appendChild(empty);
      return;
    }

    this.results.forEach((donor) => {
      this.listEl.appendChild(this.cardFor(donor));
    });
  };

  DonorMap.prototype.cardFor = function (donor) {
    const card = document.createElement("article");
    card.className = "donor-card";
    card.dataset.donorId = donor.id;
    if (this.selected.has(donor.id)) card.classList.add("is-selected");

    const top = document.createElement("div");
    top.className = "donor-card__top";

    if (donor.match_percent != null) {
      const score = document.createElement("div");
      score.className = "score score--" + (donor.tier || "low");
      score.style.setProperty("--pct", donor.match_percent);
      const inner = document.createElement("span");
      inner.textContent = donor.match_percent + "%";
      score.appendChild(inner);
      score.title = "AI predicted willingness to donate";
      top.appendChild(score);
    }

    const identity = document.createElement("div");
    identity.className = "donor-card__identity";

    const nameRow = document.createElement("div");
    nameRow.className = "row";
    const name = document.createElement("p");
    name.className = "donor-card__name";
    name.textContent = donor.name;
    nameRow.appendChild(name);

    const group = document.createElement("span");
    group.className = "blood-badge";
    group.textContent = donor.blood_group;
    nameRow.appendChild(group);

    if (donor.is_verified) {
      const verified = document.createElement("span");
      verified.className = "badge badge--blue";
      verified.innerHTML = '<i class="fa-solid fa-circle-check"></i> Verified';
      nameRow.appendChild(verified);
    }
    identity.appendChild(nameRow);

    const meta = document.createElement("div");
    meta.className = "donor-card__meta";
    meta.textContent =
      (donor.distance_display || donor.distance_km + " km") +
      " away" +
      (donor.area ? " - " + donor.area : "");
    identity.appendChild(meta);
    top.appendChild(identity);
    card.appendChild(top);

    if (donor.reasons && donor.reasons.length) {
      const reasons = document.createElement("div");
      reasons.className = "reasons";
      donor.reasons.forEach((text) => {
        const chip = document.createElement("span");
        chip.className =
          "reason" + (text.indexOf("Caution") === 0 ? " reason--caution" : "");
        chip.textContent = text;
        reasons.appendChild(chip);
      });
      card.appendChild(reasons);
    }

    const stats = document.createElement("div");
    stats.className = "donor-card__stats";
    stats.appendChild(this.statChip("fa-droplet", donor.total_donations + " donations"));
    stats.appendChild(this.statChip("fa-shield-heart", donor.reliability_percent + "% reliable"));
    if (donor.age) stats.appendChild(this.statChip("fa-user", donor.age + " yrs"));
    card.appendChild(stats);

    const footer = document.createElement("div");
    footer.className = "donor-card__footer";

    if (this.donorIdsInput) {
      const select = document.createElement("button");
      select.type = "button";
      select.className = this.selected.has(donor.id)
        ? "btn btn-success btn-sm"
        : "btn btn-secondary btn-sm";
      select.innerHTML = this.selected.has(donor.id)
        ? '<i class="fa-solid fa-check"></i> Selected'
        : '<i class="fa-solid fa-plus"></i> Select';
      select.addEventListener("click", () => this.toggle(donor.id));
      footer.appendChild(select);
    }

    const view = document.createElement("a");
    view.className = "btn btn-ghost btn-sm";
    view.href = donor.detail_url;
    view.textContent = "View profile";
    footer.appendChild(view);

    const focus = document.createElement("button");
    focus.type = "button";
    focus.className = "btn btn-ghost btn-sm ml-auto";
    focus.innerHTML = '<i class="fa-solid fa-location-crosshairs"></i>';
    focus.title = "Show on map";
    focus.addEventListener("click", () => this.highlight(donor.id, true));
    footer.appendChild(focus);

    card.appendChild(footer);
    return card;
  };

  DonorMap.prototype.statChip = function (icon, text) {
    const el = document.createElement("span");
    el.className = "donor-card__stat";
    el.innerHTML = '<i class="fa-solid ' + icon + '"></i>';
    el.appendChild(document.createTextNode(" " + text));
    return el;
  };

  DonorMap.prototype.toggle = function (donorId) {
    if (this.selected.has(donorId)) {
      this.selected.delete(donorId);
    } else {
      this.selected.add(donorId);
    }
    this.renderResults();
    this.syncSelection();
  };

  DonorMap.prototype.selectTop = function (count) {
    this.results.slice(0, count).forEach((donor) => this.selected.add(donor.id));
    this.renderResults();
    this.syncSelection();
  };

  DonorMap.prototype.syncSelection = function () {
    const ids = Array.from(this.selected);
    if (this.donorIdsInput) this.donorIdsInput.value = ids.join(",");
    if (this.selectedCountEl) this.selectedCountEl.textContent = ids.length;
    if (this.selectionBar) this.selectionBar.hidden = ids.length === 0;
  };

  DonorMap.prototype.highlight = function (donorId, pan) {
    const marker = this.markers.get(donorId);
    if (marker) {
      if (pan) this.map.setView(marker.getLatLng(), Math.max(this.map.getZoom(), 14));
      marker.openPopup();
    }
    document.querySelectorAll(".donor-card").forEach((card) => {
      card.classList.toggle(
        "is-highlighted",
        parseInt(card.dataset.donorId, 10) === donorId
      );
    });
  };

  DonorMap.prototype.startPolling = function () {
    const seconds = this.config.pollSeconds || 30;
    if (!seconds) return;

    this.pollTimer = setInterval(() => {
      // Skip polling while the tab is hidden - no point burning queries.
      if (document.hidden) return;
      this.search();
    }, seconds * 1000);

    window.addEventListener("beforeunload", () => clearInterval(this.pollTimer));
  };

  window.DonorMap = DonorMap;
})();
