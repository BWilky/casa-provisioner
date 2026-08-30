// Casa admin panel — Location Zones editor. Embedded (not routed) inside the
// Settings view: settings.js lazy-loads this module and calls createView(app)
// exactly like a routed view, but only mount(el)/unmount() are used (no
// header/polling — the Settings shell owns chrome).
//
// Renders a Leaflet map with one draggable marker + a stack of L.circle rings
// per anchor, plus a plain-HTML form for names/rings/stale_after_minutes.
// Client-side validation mirrors validate_zone_config() in location.py
// exactly (see MAX_TOTAL_RINGS / RESERVED_LABELS below) so Save only ever
// fails on a version-skewed backend, never a rules mismatch.

const MAX_TOTAL_RINGS = 18;
const RESERVED_LABELS = new Set(["away", "unknown"]);

const VENDOR_JS = "/casa_static/vendor/leaflet.js";
const VENDOR_CSS = "/casa_static/vendor/leaflet.css";
const TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
const TILE_ATTRIBUTION = "&copy; OpenStreetMap contributors";

// Leaflet is a classic script exposing global `L`. Load it once per document
// (module-scope promise) regardless of how many times this view mounts —
// re-appending the <script> would re-run the "already loaded" guard inside
// leaflet.js and throw.
let leafletPromise = null;
function ensureLeaflet() {
  if (leafletPromise) return leafletPromise;
  leafletPromise = new Promise((resolve, reject) => {
    if (window.L) {
      resolve(window.L);
      return;
    }
    const existing = document.head.querySelector('script[data-casa-leaflet="1"]');
    if (existing) {
      existing.addEventListener("load", () => resolve(window.L));
      existing.addEventListener("error", () => reject(new Error("Failed to load Leaflet")));
      return;
    }
    const script = document.createElement("script");
    script.src = VENDOR_JS;
    script.dataset.casaLeaflet = "1";
    script.addEventListener("load", () => resolve(window.L));
    script.addEventListener("error", () => reject(new Error("Failed to load Leaflet")));
    document.head.appendChild(script);
  });
  return leafletPromise;
}

// The <link> lives in the panel's shadow root (styles.js only adopts the
// panel's own stylesheet there) so Leaflet's CSS actually applies to the
// map — inject it once, idempotently, per shadow root.
function ensureLeafletCss(shadowRoot) {
  if (!shadowRoot || shadowRoot.getElementById("casa-leaflet-css")) return;
  const link = document.createElement("link");
  link.id = "casa-leaflet-css";
  link.rel = "stylesheet";
  link.href = VENDOR_CSS;
  shadowRoot.appendChild(link);
}

export function createView(app) {
  const { api, ui } = app;
  const esc = ui.esc;

  let el = null;
  let mounted = false;
  let loading = true;
  let loadError = null;
  let saving = false;
  // Guards against a load() started by one mount() call rendering into a
  // later mount() call's DOM after a rapid tab unmount/remount (settings.js
  // reuses the same host element across tab switches).
  let loadToken = 0;

  // Local editable state, hydrated from GET on mount.
  let configVersion = "";
  let staleAfterMinutes = 30;
  let anchors = []; // [{id, name, latitude, longitude, rings:[{label, radius_m}]}]

  // Leaflet objects, keyed by anchor id — rebuilt in full on every structural
  // change (add/remove anchor or ring); live-updated in place on drag / radius
  // input so typing/dragging never triggers a form re-render.
  let map = null;
  let layers = new Map(); // id -> { marker, circles: L.circle[] }
  // L.Icon.Default's path auto-detection probes document.body CSS / a
  // light-DOM <link href$="leaflet.css">; both miss because our leaflet.css
  // lives in the shadow root, so the default marker PNGs 404 unless pinned
  // explicitly. Done once, right after ensureLeaflet() resolves.
  let iconDefaultsConfigured = false;

  let refs = null; // DOM refs into the static shell (map div persists across renders)

  /* ---------- validation (mirrors location.py validate_zone_config) ---------- */

  function validate() {
    const errors = [];
    if (!Number.isInteger(staleAfterMinutes) || staleAfterMinutes < 0) {
      errors.push("Stale-after must be a non-negative whole number of minutes.");
    }
    const names = new Set();
    let totalRings = 0;
    anchors.forEach((a, i) => {
      const where = `Anchor ${i + 1}`;
      const name = String(a.name || "").trim();
      if (!name) errors.push(`${where}: name is required.`);
      else if (names.has(name.toLowerCase())) errors.push(`${where}: anchor names must be unique ('${name}').`);
      else names.add(name.toLowerCase());

      const lat = a.latitude, lon = a.longitude;
      if (typeof lat !== "number" || !Number.isFinite(lat) || lat < -90 || lat > 90) {
        errors.push(`${where}: latitude must be a number in [-90, 90].`);
      }
      if (typeof lon !== "number" || !Number.isFinite(lon) || lon < -180 || lon > 180) {
        errors.push(`${where}: longitude must be a number in [-180, 180].`);
      }

      if (!Array.isArray(a.rings) || !a.rings.length) {
        errors.push(`${where}: at least one ring is required.`);
        return;
      }
      totalRings += a.rings.length;
      const labels = new Set();
      let prevRadius = 0;
      a.rings.forEach((r, j) => {
        const rw = `${where} ring ${j + 1}`;
        const label = String(r.label || "").trim();
        if (!label) errors.push(`${rw}: label is required.`);
        else if (RESERVED_LABELS.has(label.toLowerCase())) errors.push(`${rw}: '${label}' is a reserved label.`);
        else if (labels.has(label.toLowerCase())) errors.push(`${rw}: labels must be unique within an anchor.`);
        else labels.add(label.toLowerCase());

        const radius = r.radius_m;
        if (typeof radius !== "number" || !Number.isFinite(radius) || radius <= 0) {
          errors.push(`${rw}: radius must be a positive number.`);
        } else if (radius <= prevRadius) {
          errors.push(`${rw}: radii must be strictly ascending.`);
        } else {
          prevRadius = radius;
        }
      });
    });
    if (totalRings > MAX_TOTAL_RINGS) {
      errors.push(`Total rings across anchors is ${totalRings}; iOS geofencing caps this at ${MAX_TOTAL_RINGS}.`);
    }
    return errors;
  }

  function updateValidation() {
    if (!refs) return [];
    const errors = validate();
    refs.err.innerHTML = errors.length
      ? `<div class="errbar"><ul style="margin:0; padding-left:18px;">${errors
          .map((e) => `<li>${esc(e)}</li>`)
          .join("")}</ul></div>`
      : "";
    refs.save.disabled = errors.length > 0 || saving || loading;
    return errors;
  }

  /* ---------- map sync ---------- */

  async function ensureMap() {
    if (map || !refs) return;
    const L = await ensureLeaflet();
    if (!iconDefaultsConfigured) {
      L.Icon.Default.mergeOptions({
        iconUrl: "/casa_static/vendor/images/marker-icon.png",
        iconRetinaUrl: "/casa_static/vendor/images/marker-icon-2x.png",
        shadowUrl: "/casa_static/vendor/images/marker-shadow.png",
      });
      iconDefaultsConfigured = true;
    }
    ensureLeafletCss(ui._shadowRoot);
    if (!refs || !mounted) return; // unmounted while awaiting the script load

    map = L.map(refs.mapEl, { center: [39.5, -98.35], zoom: 4 });
    L.tileLayer(TILE_URL, { attribution: TILE_ATTRIBUTION, maxZoom: 19 }).addTo(map);
    // Leaflet-in-shadow-DOM: the container has no real size until after first
    // layout; force a recalculation once it settles.
    requestAnimationFrame(() => map && map.invalidateSize());
    syncMapLayers();
    fitMapToAnchors();
  }

  function anchorColor(index) {
    const palette = ["#03a9f4", "#e91e63", "#4caf50", "#ff9800", "#9c27b0", "#795548", "#009688", "#f44336"];
    return palette[index % palette.length];
  }

  // Full rebuild of markers/circles from `anchors`. Called after structural
  // changes (add/remove anchor or ring) and on initial load — cheap since
  // anchor counts are small (rings cap at 18 total).
  function syncMapLayers() {
    if (!map) return;
    const L = window.L;
    const seen = new Set();
    anchors.forEach((a, i) => {
      seen.add(a.id);
      let entry = layers.get(a.id);
      const latlng = [num(a.latitude, 0), num(a.longitude, 0)];
      if (!entry) {
        const marker = L.marker(latlng, { draggable: true });
        marker.addTo(map);
        marker.on("drag", (e) => onMarkerMove(a.id, e.target.getLatLng()));
        marker.on("dragend", (e) => onMarkerMove(a.id, e.target.getLatLng()));
        entry = { marker, circles: [] };
        layers.set(a.id, entry);
      } else {
        entry.marker.setLatLng(latlng);
      }
      // bindTooltip is safe to call repeatedly — it replaces any existing
      // binding. Leaflet assigns tooltip content via innerHTML without
      // escaping, so anchor names (user input) must be pre-escaped.
      entry.marker.bindTooltip(esc(a.name || `Anchor ${i + 1}`));

      // Rebuild this anchor's circles to match its current ring count.
      for (const c of entry.circles) map.removeLayer(c);
      entry.circles = a.rings.map((r) =>
        L.circle(latlng, {
          radius: Math.max(0, num(r.radius_m, 0)),
          color: anchorColor(i),
          weight: 2,
          fillOpacity: 0.08,
        }).addTo(map)
      );
    });
    // Drop layers for anchors that no longer exist.
    for (const [id, entry] of layers) {
      if (!seen.has(id)) {
        map.removeLayer(entry.marker);
        for (const c of entry.circles) map.removeLayer(c);
        layers.delete(id);
      }
    }
  }

  function fitMapToAnchors() {
    if (!map || !anchors.length) return;
    const L = window.L;
    const pts = anchors.map((a) => [num(a.latitude, 0), num(a.longitude, 0)]);
    if (pts.length === 1) map.setView(pts[0], 14);
    else map.fitBounds(L.latLngBounds(pts), { padding: [40, 40] });
  }

  // Live drag: move every circle for this anchor with its marker, and reflect
  // the new coordinates into the (non-focused) lat/lon inputs without
  // touching anything else in the DOM.
  function onMarkerMove(anchorId, latlng) {
    const a = anchors.find((x) => x.id === anchorId);
    if (!a) return;
    a.latitude = round6(latlng.lat);
    a.longitude = round6(latlng.lng);
    const entry = layers.get(anchorId);
    if (entry) for (const c of entry.circles) c.setLatLng(latlng);
    const latInput = refs.anchors.querySelector(`[data-anchor-id="${cssEsc(anchorId)}"][data-field="latitude"]`);
    const lonInput = refs.anchors.querySelector(`[data-anchor-id="${cssEsc(anchorId)}"][data-field="longitude"]`);
    if (latInput) latInput.value = a.latitude;
    if (lonInput) lonInput.value = a.longitude;
    updateValidation();
  }

  function num(v, fallback) {
    const n = typeof v === "number" ? v : parseFloat(v);
    return Number.isFinite(n) ? n : fallback;
  }
  function round6(n) {
    return Math.round(n * 1e6) / 1e6;
  }
  function cssEsc(s) {
    return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");
  }

  /* ---------- form rendering ---------- */

  function ringRow(anchor, ring, ringIdx, ringCount) {
    return `
      <div class="field-row" data-ring-idx="${ringIdx}" style="margin-bottom:8px;">
        <input class="input" type="text" data-anchor-id="${esc(anchor.id)}" data-ring-idx="${ringIdx}"
          data-field="label" placeholder="Ring label (e.g. home_close)" value="${esc(ring.label || "")}" style="flex:2;">
        <input class="input" type="number" min="1" step="1" data-anchor-id="${esc(anchor.id)}" data-ring-idx="${ringIdx}"
          data-field="radius_m" placeholder="Radius (m)" value="${esc(ring.radius_m != null ? ring.radius_m : "")}" style="flex:1;">
        <button class="btn btn--icon" data-act="ring-up" data-anchor-id="${esc(anchor.id)}" data-ring-idx="${ringIdx}"
          title="Move ring up" ${ringIdx === 0 ? "disabled" : ""}><ha-icon icon="mdi:arrow-up"></ha-icon></button>
        <button class="btn btn--icon" data-act="ring-down" data-anchor-id="${esc(anchor.id)}" data-ring-idx="${ringIdx}"
          title="Move ring down" ${ringIdx === ringCount - 1 ? "disabled" : ""}><ha-icon icon="mdi:arrow-down"></ha-icon></button>
        <button class="btn btn--icon danger" data-act="ring-remove" data-anchor-id="${esc(anchor.id)}" data-ring-idx="${ringIdx}"
          title="Remove ring"><ha-icon icon="mdi:delete"></ha-icon></button>
      </div>`;
  }

  function anchorCard(anchor, index) {
    const rings = anchor.rings || [];
    return `
      <div class="card section-card" data-anchor-id="${esc(anchor.id)}" style="border-left:4px solid ${anchorColor(index)};">
        <div class="card__body">
          <div class="field-row" style="margin-bottom:10px;">
            <span class="chip" style="background:${anchorColor(index)}; color:#fff; flex:none;">${index + 1}</span>
            <input class="input" type="text" data-anchor-id="${esc(anchor.id)}" data-field="name"
              placeholder="Anchor name (e.g. Home)" value="${esc(anchor.name || "")}" style="flex:2;">
            <input class="input" type="number" step="any" data-anchor-id="${esc(anchor.id)}" data-field="latitude"
              placeholder="Latitude" value="${esc(anchor.latitude != null ? anchor.latitude : "")}" style="flex:1;">
            <input class="input" type="number" step="any" data-anchor-id="${esc(anchor.id)}" data-field="longitude"
              placeholder="Longitude" value="${esc(anchor.longitude != null ? anchor.longitude : "")}" style="flex:1;">
            <button class="btn btn--icon danger" data-act="anchor-remove" data-anchor-id="${esc(anchor.id)}"
              title="Remove anchor"><ha-icon icon="mdi:delete"></ha-icon></button>
          </div>
          <div class="field__help" style="margin:0 0 8px;">Rings (ascending radius, meters):</div>
          <div data-rings-for="${esc(anchor.id)}">
            ${rings.map((r, j) => ringRow(anchor, r, j, rings.length)).join("")}
          </div>
          <button class="btn btn--text" data-act="ring-add" data-anchor-id="${esc(anchor.id)}">+ Add ring</button>
        </div>
      </div>`;
  }

  function renderAnchors() {
    if (!refs) return;
    if (!anchors.length) {
      refs.anchors.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:map-marker-outline"></ha-icon>
        <div>No anchors yet</div>
        <button class="btn btn--primary" data-act="anchor-add">+ Add anchor</button>
      </div>`;
    } else {
      refs.anchors.innerHTML = anchors.map(anchorCard).join("");
    }
    updateValidation();
  }

  /* ---------- mutations ---------- */

  function addAnchor(seed) {
    const anchor = {
      id: crypto.randomUUID(),
      name: "",
      latitude: seed && Number.isFinite(seed.latitude) ? round6(seed.latitude) : 0,
      longitude: seed && Number.isFinite(seed.longitude) ? round6(seed.longitude) : 0,
      rings: [{ label: "", radius_m: 100 }],
    };
    anchors.push(anchor);
    renderAnchors();
    syncMapLayers();
    fitMapToAnchors();
    return anchor;
  }

  function removeAnchor(id) {
    anchors = anchors.filter((a) => a.id !== id);
    renderAnchors();
    syncMapLayers();
  }

  function addRing(anchorId) {
    const a = anchors.find((x) => x.id === anchorId);
    if (!a) return;
    const lastRadius = a.rings.length ? num(a.rings[a.rings.length - 1].radius_m, 0) : 0;
    a.rings.push({ label: "", radius_m: lastRadius + 50 });
    renderAnchors();
    syncMapLayers();
  }

  function removeRing(anchorId, idx) {
    const a = anchors.find((x) => x.id === anchorId);
    if (!a) return;
    a.rings.splice(idx, 1);
    renderAnchors();
    syncMapLayers();
  }

  function moveRing(anchorId, idx, delta) {
    const a = anchors.find((x) => x.id === anchorId);
    if (!a) return;
    const j = idx + delta;
    if (j < 0 || j >= a.rings.length) return;
    const tmp = a.rings[idx];
    a.rings[idx] = a.rings[j];
    a.rings[j] = tmp;
    renderAnchors();
    syncMapLayers();
  }

  function useHaHomeLocation() {
    const hass = app.hass && app.hass();
    const lat = hass && hass.config && hass.config.latitude;
    const lon = hass && hass.config && hass.config.longitude;
    if (typeof lat !== "number" || typeof lon !== "number") {
      ui.toast("Home Assistant home location is not set.", { error: true });
      return;
    }
    if (!anchors.length) {
      addAnchor({ latitude: lat, longitude: lon });
      return;
    }
    const a = anchors[0];
    a.latitude = round6(lat);
    a.longitude = round6(lon);
    renderAnchors();
    syncMapLayers();
    fitMapToAnchors();
  }

  /* ---------- delegated events ---------- */

  function onAnchorsInput(e) {
    const t = e.target;
    const anchorId = t.dataset.anchorId;
    const field = t.dataset.field;
    if (!anchorId || !field) return;
    const a = anchors.find((x) => x.id === anchorId);
    if (!a) return;

    if (t.dataset.ringIdx != null) {
      const idx = parseInt(t.dataset.ringIdx, 10);
      const ring = a.rings[idx];
      if (!ring) return;
      if (field === "label") {
        ring.label = t.value;
      } else if (field === "radius_m") {
        ring.radius_m = t.value === "" ? null : parseFloat(t.value);
        const entry = layers.get(anchorId);
        if (entry && entry.circles[idx] && Number.isFinite(ring.radius_m) && ring.radius_m > 0) {
          entry.circles[idx].setRadius(ring.radius_m);
        }
      }
    } else if (field === "name") {
      a.name = t.value;
      const entry = layers.get(anchorId);
      // Tooltip content is raw innerHTML in Leaflet — escape user input.
      if (entry) entry.marker.bindTooltip(esc(a.name || ""));
    } else if (field === "latitude" || field === "longitude") {
      const v = t.value === "" ? null : parseFloat(t.value);
      a[field] = v;
      if (Number.isFinite(v)) {
        const entry = layers.get(anchorId);
        if (entry) {
          const latlng = [num(a.latitude, 0), num(a.longitude, 0)];
          entry.marker.setLatLng(latlng);
          for (const c of entry.circles) c.setLatLng(latlng);
        }
      }
    }
    updateValidation();
  }

  function onAnchorsClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    const anchorId = el.dataset.anchorId;
    const ringIdx = el.dataset.ringIdx != null ? parseInt(el.dataset.ringIdx, 10) : null;
    switch (el.dataset.act) {
      case "anchor-add":
        addAnchor();
        break;
      case "anchor-remove":
        removeAnchor(anchorId);
        break;
      case "ring-add":
        addRing(anchorId);
        break;
      case "ring-remove":
        removeRing(anchorId, ringIdx);
        break;
      case "ring-up":
        moveRing(anchorId, ringIdx, -1);
        break;
      case "ring-down":
        moveRing(anchorId, ringIdx, 1);
        break;
    }
  }

  function onStaleInput(e) {
    const v = parseInt(e.target.value, 10);
    staleAfterMinutes = Number.isFinite(v) ? v : e.target.value;
    updateValidation();
  }

  async function onSave() {
    const errors = updateValidation();
    if (errors.length || saving) return;
    saving = true;
    refs.save.disabled = true;
    refs.save.textContent = "Saving…";
    try {
      const config = {
        config_version: configVersion,
        stale_after_minutes: staleAfterMinutes,
        anchors: anchors.map((a) => ({
          id: a.id,
          name: String(a.name || "").trim(),
          latitude: a.latitude,
          longitude: a.longitude,
          rings: a.rings.map((r) => ({ label: String(r.label || "").trim(), radius_m: r.radius_m })),
        })),
      };
      const res = await api.saveLocationZones(config);
      configVersion = (res && res.config_version) || configVersion;
      const queued = (res && res.queued) || 0;
      ui.toast(`Saved — pushing to ${queued} device${queued === 1 ? "" : "s"}`);
    } catch (err) {
      ui.toast("Save failed: " + ui.errMsg(err), { error: true });
    } finally {
      saving = false;
      refs.save.textContent = "Save";
      updateValidation();
    }
  }

  /* ---------- load ---------- */

  async function load() {
    const token = ++loadToken;
    loading = true;
    loadError = null;
    renderShellLoading();
    try {
      const res = await api.getLocationZones();
      if (token !== loadToken) return; // superseded by a newer mount()
      configVersion = (res && res.config_version) || "";
      staleAfterMinutes = Number.isFinite(res && res.stale_after_minutes) ? res.stale_after_minutes : 30;
      anchors = ((res && res.anchors) || []).map((a) => ({
        id: a.id || crypto.randomUUID(),
        name: a.name || "",
        latitude: typeof a.latitude === "number" ? a.latitude : num(a.latitude, 0),
        longitude: typeof a.longitude === "number" ? a.longitude : num(a.longitude, 0),
        rings: Array.isArray(a.rings) ? a.rings.map((r) => ({ label: r.label || "", radius_m: r.radius_m })) : [],
      }));
    } catch (err) {
      if (token !== loadToken) return;
      loadError = err;
      anchors = [];
    }
    if (token !== loadToken) return;
    loading = false;
    if (!mounted) return;
    renderShellLoaded();
    await ensureMap();
  }

  /* ---------- shell ---------- */

  function renderShellLoading() {
    if (!el) return;
    el.innerHTML = `<div class="empty-state" style="padding:48px 16px;"><span class="muted">Loading location zones…</span></div>`;
  }

  function renderShellLoaded() {
    if (!el) return;
    if (loadError) {
      el.innerHTML = `<div class="errbar">Failed to load location zones: ${esc(ui.errMsg(loadError))}
        <div style="margin-top:8px;"><button class="btn btn--outlined" data-act="retry">Retry</button></div></div>`;
      el.querySelector("[data-act=retry]")?.addEventListener("click", load);
      return;
    }

    el.innerHTML = `
      <h2>Location Zones</h2>
      <p class="editor__form-desc">
        Define named anchors (e.g. Home, Office) with nested proximity rings. Devices report
        which ring they're in and push it to iOS geofencing; this list is capped at
        ${MAX_TOTAL_RINGS} rings total across every anchor.
      </p>
      <div class="card section-card">
        <div class="card__body">
          <h5>Staleness</h5>
          <div class="field-row">
            <input type="number" class="input" id="lz-stale" min="0" step="1" value="${esc(staleAfterMinutes)}" style="max-width:140px; flex:none;">
            <span class="muted">minutes without a report before a device is treated as "unknown"</span>
          </div>
        </div>
      </div>
      <div class="card section-card">
        <div class="card__body">
          <div class="field-row" style="margin-bottom:10px;">
            <h5 style="margin:0; flex:1;">Map</h5>
            <button class="btn btn--outlined" id="lz-use-home">Use HA home location</button>
          </div>
          <div id="lz-map" style="height:360px; border-radius:var(--casa-radius-sm); overflow:hidden;"></div>
          <div class="field__help">Drag a pin to reposition its anchor; rings track the radius fields below live.</div>
        </div>
      </div>
      <div id="lz-err"></div>
      <div id="lz-anchors"></div>
      <div class="list-toolbar">
        <button class="btn btn--outlined" data-act="anchor-add" id="lz-add-anchor">+ Add anchor</button>
        <span class="spacer"></span>
        <button class="btn btn--primary" id="lz-save">Save</button>
      </div>`;

    refs = {
      stale: el.querySelector("#lz-stale"),
      mapEl: el.querySelector("#lz-map"),
      useHome: el.querySelector("#lz-use-home"),
      err: el.querySelector("#lz-err"),
      anchors: el.querySelector("#lz-anchors"),
      addAnchor: el.querySelector("#lz-add-anchor"),
      save: el.querySelector("#lz-save"),
    };

    refs.stale.addEventListener("input", onStaleInput);
    refs.useHome.addEventListener("click", useHaHomeLocation);
    refs.addAnchor.addEventListener("click", () => addAnchor());
    refs.anchors.addEventListener("input", onAnchorsInput);
    refs.anchors.addEventListener("click", onAnchorsClick);
    refs.save.addEventListener("click", onSave);

    renderAnchors();
  }

  /* ---------- view ---------- */

  return {
    id: "location-zones",

    mount(hostEl) {
      el = hostEl;
      mounted = true;
      load();
    },

    unmount() {
      mounted = false;
      loadToken++; // invalidate any in-flight load() from this mount cycle
      if (map) {
        map.remove();
        map = null;
      }
      layers = new Map();
      refs = null;
      el = null;
    },
  };
}
