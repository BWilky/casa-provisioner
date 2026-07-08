// Casa admin panel — provision wizard. One wide modal with an ESPHome-style
// stepper: method → profile → details → result. All state lives in this
// closure; the modal body is re-rendered per step with its own back arrow
// (the modal header only carries the title + X). No static sibling imports —
// payload-preview is pulled in via app.loadModule.

const METHOD_LABELS = {
  qr: "Generate Link & QR Code",
  deep_link: "Generate Setup Links",
  manual: "Generate Manual Entry Values",
  ble: "Broadcast Provisioning Beacon",
};

const RESULT_TITLES = {
  qr: "QR code ready",
  deep_link: "Setup links ready",
  manual: "Manual entry values",
  ble: "Beacon broadcast",
};

export function openProvisionWizard(app, { presetUsername, presetProfileId } = {}) {
  const { api, ui } = app;
  const esc = ui.esc;
  const unwrap = (res) => api.constructor.response(res); // CasaApi.response

  const state = {
    step: "method", // "method" | "profile" | "details" | "result"
    method: "qr",
    advancedOpen: false,
    profiles: null, // null = loading
    profilesError: null,
    profile: null, // selected profile object, or null = ad-hoc
    search: "",
    expandedInfo: null, // profile id with the "More info" panel open
    qrOptionsOpen: false,
    details: {
      host_url: "",
      username: "",
      pin: "",
      timeout_minutes: 30,
      bleTargets: [],
      deleteQr: true,
      qrFilename: "",
    },
    detailsError: "",
    busy: false,
    result: null,
  };
  let pendingPresetId = presetProfileId || null;

  // profileChips from payload-preview.js (lazy; cards render without chips
  // until it lands, which in practice is before the profile step shows).
  let profileChips = null;
  app
    .loadModule("payload-preview.js")
    .then((mod) => {
      profileChips = mod.profileChips;
      if (state.step === "profile") render();
    })
    .catch(() => {});

  const modal = ui.openModal({
    title: "Provision device",
    wide: true,
    buttons: [],
    bodyEl: document.createElement("div"),
  });
  const root = modal.body;

  /* ---------- data ---------- */

  async function loadProfiles() {
    state.profiles = null;
    state.profilesError = null;
    if (state.step === "profile") render();
    try {
      const res = await api.getProvisionProfiles();
      state.profiles = (res && res.profiles) || [];
    } catch (err) {
      state.profiles = [];
      state.profilesError = (err && err.message) || String(err);
    }
    if (pendingPresetId && state.step === "profile" && maybeApplyPreset()) return;
    if (state.step === "profile") render();
  }
  loadProfiles(); // fresh list every wizard open

  function maybeApplyPreset() {
    if (!pendingPresetId || !Array.isArray(state.profiles)) return false;
    const p = state.profiles.find((x) => x && x.id === pendingPresetId);
    pendingPresetId = null; // consume either way
    if (p) {
      selectProfile(p);
      return true;
    }
    return false;
  }

  /* ---------- helpers ---------- */

  const trim = (v) => String(v ?? "").trim();
  const asBool = (v) => v === true || trim(v).toLowerCase() === "true";
  const pushOn = (v) => {
    const s = v === true ? "true" : trim(v).toLowerCase();
    return s === "true" || s === "mandatory";
  };
  // Settings a manual provision silently drops if this profile is used.
  const manualLoses = (fields) => {
    const f = fields || {};
    return !!(trim(f.pin) || pushOn(f.push_notifications) || asBool(f.allow_wireguard));
  };

  function selectProfile(profile) {
    state.profile = profile || null;
    const f = (profile && profile.fields) || {};
    state.details.host_url = trim(f.host_url) || window.location.origin;
    state.details.username = presetUsername || trim(f.username);
    state.details.pin = trim(f.pin).slice(0, 6);
    state.detailsError = "";
    state.step = "details";
    render();
  }

  function goBack() {
    if (state.step === "profile") state.step = "method";
    else if (state.step === "details") state.step = "profile";
    else if (state.step === "result") state.step = "details";
    render();
  }

  function stepHeader(title) {
    return `
      <div style="display:flex; align-items:center; gap:8px; margin-bottom:16px;">
        <button class="btn btn--icon" data-act="back" title="Back"><ha-icon icon="mdi:arrow-left"></ha-icon></button>
        <h3 style="margin:0; font-size:16px; font-weight:600;">${esc(title)}</h3>
      </div>`;
  }

  function optionCard({ method, title, desc, note, disabled, disabledTitle }) {
    return `
      <button class="option-card" data-act="method" data-method="${esc(method)}"
        ${disabled ? `disabled title="${esc(disabledTitle || "")}"` : ""}>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          <span class="option-card__desc" style="display:block;">${esc(desc)}</span>
          ${note ? `<span style="display:block; font-size:12px; color:var(--casa-warning); margin-top:4px;">${esc(note)}</span>` : ""}
        </span>
        <ha-icon class="chevron" icon="mdi:chevron-right"></ha-icon>
      </button>`;
  }

  /* ---------- step: method ---------- */

  function renderMethod() {
    const bleAvailable = !!(app.hass() && app.hass().services && app.hass().services.esphome);
    return `
      <div style="display:flex; align-items:center; gap:10px; margin-bottom:16px;">
        <ha-icon icon="mdi:cog" style="--mdc-icon-size:22px; color:var(--casa-text-2);"></ha-icon>
        <h3 style="margin:0; font-size:16px; font-weight:600;">How would you like to provision this device?</h3>
      </div>
      ${optionCard({
        method: "qr",
        title: "Guided provisioning",
        desc: "QR code plus setup links — recommended",
      })}
      <button class="btn btn--text" data-act="toggle-advanced" style="margin:2px 0 10px;">
        <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${state.advancedOpen ? "180deg" : "0deg"});"></ha-icon>
        Advanced set up options
      </button>
      <div ${state.advancedOpen ? "" : "hidden"}>
        ${optionCard({
          method: "deep_link",
          title: "Deep link only",
          desc: "Send a setup link; no QR image is written",
        })}
        ${optionCard({
          method: "manual",
          title: "Manual entry values",
          desc: "Plaintext values to read into the app's manual sheet",
          note: "cannot carry PIN, push, or VPN settings",
        })}
        ${optionCard({
          method: "ble",
          title: "BLE beacon",
          desc: "Broadcast via ESPHome provisioning beacons",
          disabled: !bleAvailable,
          disabledTitle: "No ESPHome services found — set up an ESPHome provisioning beacon first",
        })}
      </div>`;
  }

  /* ---------- step: profile ---------- */

  function profileMeta(f) {
    const bits = [trim(f.username), trim(f.host_url)].filter(Boolean);
    return bits.join(" · ");
  }

  function profileInfoPanel(p) {
    const f = p.fields || {};
    const hours = f.expiration_hours === "" || f.expiration_hours == null ? 336 : Number(f.expiration_hours);
    const timeout = f.timeout_minutes === "" || f.timeout_minutes == null ? 5 : Number(f.timeout_minutes);
    const dt = (label, value) => `
      <dt class="muted" style="margin:0;">${esc(label)}</dt>
      <dd style="margin:0; word-break:break-all;">${esc(value)}</dd>`;
    return `
      <div class="card__body" style="border-top:1px solid var(--casa-divider); font-size:12px;">
        <dl style="display:grid; grid-template-columns:auto 1fr; gap:4px 12px; margin:0 0 8px;">
          ${dt("Dashboard", trim(f.default_dashboard) || "—")}
          ${dt("Welcome URL", trim(f.welcome_url) || "—")}
          ${dt("Immersive level", trim(f.immersive_level) || "1")}
          ${dt("Expiry hours", Number.isFinite(hours) && hours === 0 ? "Never" : String(hours))}
          ${dt("Timeout", `${Number.isFinite(timeout) ? timeout : 5} min`)}
          ${dt("Push", trim(f.push_notifications) || "false")}
          ${dt("VPN", asBool(f.allow_wireguard) ? "Allowed" : "Off")}
        </dl>
        <button class="btn btn--text" data-act="edit-profile" data-id="${esc(p.id)}" style="height:28px;">Edit profile</button>
      </div>`;
  }

  function profileCard(p) {
    const f = p.fields || {};
    const chips = profileChips ? profileChips(p) : [];
    if (state.method === "manual" && manualLoses(f)) {
      chips.push({ label: "settings lost with manual entry", cls: "chip--warn" });
    }
    const chipsHtml = chips
      .map((c) => `<span class="chip ${esc(c.cls || "chip--neutral")}">${esc(c.label)}</span>`)
      .join("");
    return `
      <div class="card">
        <div class="card__body">
          <div style="font-weight:600; font-size:14px;">${esc(p.name || p.id)}</div>
          <div class="muted" style="font-size:12px; margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(profileMeta(f))}</div>
          ${chipsHtml ? `<div style="display:flex; flex-wrap:wrap; gap:4px; margin-top:8px;">${chipsHtml}</div>` : ""}
        </div>
        ${state.expandedInfo === p.id ? profileInfoPanel(p) : ""}
        <div class="card__footer">
          <button class="btn btn--text" data-act="more-info" data-id="${esc(p.id)}" style="height:28px;">More info</button>
          <span class="spacer"></span>
          <button class="btn btn--text" data-act="select-profile" data-id="${esc(p.id)}" style="height:28px;">+ Select</button>
        </div>
      </div>`;
  }

  function adhocCard() {
    return `
      <div class="card">
        <div class="card__body">
          <div style="font-weight:600; font-size:14px;">Ad-hoc (no profile)</div>
          <div class="muted" style="font-size:12px; margin-top:2px;">Enter connection details only; server defaults for the rest</div>
        </div>
        <div class="card__footer">
          <span class="spacer"></span>
          <button class="btn btn--text" data-act="select-adhoc" style="height:28px;">+ Select</button>
        </div>
      </div>`;
  }

  function profileGridHtml() {
    if (state.profiles === null) {
      return `<div class="empty-state" style="padding:32px 16px;"><span class="muted">Loading profiles…</span></div>`;
    }
    let errHtml = "";
    if (state.profilesError) {
      errHtml = `
        <div class="errbar" style="display:flex; align-items:center; gap:10px;">
          <span style="flex:1;">Failed to load profiles: ${esc(state.profilesError)}</span>
          <button class="btn btn--outlined" data-act="retry-profiles" style="height:28px; flex:none;">Retry</button>
        </div>`;
    }
    const q = state.search.trim().toLowerCase();
    const matches = (state.profiles || []).filter((p) => {
      if (!q) return true;
      const f = p.fields || {};
      return [p.name, f.username, f.host_url].some((v) => String(v || "").toLowerCase().includes(q));
    });
    return `
      ${errHtml}
      <div class="grid-cards">
        ${adhocCard()}
        ${matches.map(profileCard).join("")}
      </div>
      ${q && !matches.length ? `<div class="muted" style="margin-top:10px; font-size:13px;">No profiles match "${esc(state.search.trim())}".</div>` : ""}`;
  }

  function renderProfile() {
    return `
      ${stepHeader("Select a provisioning profile")}
      <div class="list-toolbar">
        <div class="search-field">
          <ha-icon icon="mdi:magnify"></ha-icon>
          <input class="input" id="wiz-search" type="search" placeholder="Search profiles…" value="${esc(state.search)}">
        </div>
      </div>
      <div id="wiz-grid">${profileGridHtml()}</div>`;
  }

  function rerenderProfileGrid() {
    const grid = root.querySelector("#wiz-grid");
    if (grid) grid.innerHTML = profileGridHtml();
  }

  /* ---------- step: details ---------- */

  function bleChipsHtml() {
    if (!state.details.bleTargets.length) {
      return `<span class="muted" style="font-size:12px;">No targets added yet — at least one is required.</span>`;
    }
    return state.details.bleTargets
      .map(
        (t) => `
        <span class="chip">
          ${esc(t)}
          <button data-act="remove-target" data-target="${esc(t)}" title="Remove"
            style="border:none; background:none; cursor:pointer; padding:0; display:inline-flex; color:inherit;">
            <ha-icon icon="mdi:close" style="--mdc-icon-size:14px;"></ha-icon>
          </button>
        </span>`
      )
      .join("");
  }

  function renderDetails() {
    const d = state.details;
    const m = state.method;
    let conditional = "";

    if (m === "manual") {
      conditional = `
        <div class="field">
          <label>Entry window (minutes)</label>
          <input class="input" type="number" min="1" data-field="timeout_minutes" value="${esc(d.timeout_minutes)}">
          <div class="field__help">the generated password is scrambled after this window</div>
        </div>`;
    } else if (m === "ble") {
      const services = Object.keys((app.hass() && app.hass().services && app.hass().services.esphome) || {}).map(
        (s) => "esphome." + s
      );
      conditional = `
        <div class="field">
          <label>Beacon targets *</label>
          <div class="field-row">
            <input class="input" id="wiz-ble-input" list="wiz-ble-list" placeholder="esphome.provision_beacon" autocomplete="off">
            <datalist id="wiz-ble-list">
              ${services.map((s) => `<option value="${esc(s)}"></option>`).join("")}
            </datalist>
            <button class="btn btn--outlined" data-act="add-target" style="flex:none;">Add</button>
          </div>
          <div id="wiz-ble-chips" style="display:flex; flex-wrap:wrap; gap:6px; margin-top:8px;">${bleChipsHtml()}</div>
          <div class="field__help">ESPHome services that broadcast the provisioning beacon.</div>
        </div>`;
    } else if (m === "qr") {
      conditional = `
        <button class="btn btn--text" data-act="toggle-qr-options" style="margin:2px 0 8px; padding-left:0;">
          <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${state.qrOptionsOpen ? "180deg" : "0deg"});"></ha-icon>
          QR options
        </button>
        <div ${state.qrOptionsOpen ? "" : "hidden"}>
          <label class="toggle">
            <input type="checkbox" data-field="deleteQr" ${d.deleteQr ? "checked" : ""}>
            Delete QR image after the entry window
          </label>
          <div class="field">
            <label>QR filename</label>
            <input class="input" data-field="qrFilename" value="${esc(d.qrFilename)}" placeholder="Optional — e.g. casa_qr.png">
          </div>
        </div>`;
    }

    return `
      ${stepHeader("Connection details")}
      ${state.detailsError ? `<div class="errbar">${esc(state.detailsError)}</div>` : ""}
      <div class="field">
        <label>Host URL *</label>
        <input class="input" data-field="host_url" value="${esc(d.host_url)}" placeholder="https://ha.example.com">
      </div>
      <div class="field">
        <label>Username *</label>
        <input class="input" data-field="username" value="${esc(d.username)}" placeholder="e.g. guest-john" autocapitalize="none" autocomplete="off">
      </div>
      <div class="field">
        <label>PIN</label>
        <input class="input" data-field="pin" value="${esc(d.pin)}" maxlength="6" placeholder="Optional — up to 6 digits" autocomplete="off">
      </div>
      ${conditional}
      <div style="display:flex; justify-content:flex-end; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        <button class="btn btn--primary" data-act="generate" ${state.busy ? "disabled" : ""}>
          ${state.busy ? "Working…" : esc(METHOD_LABELS[m] || "Generate")}
        </button>
      </div>`;
  }

  function addBleTarget() {
    const input = root.querySelector("#wiz-ble-input");
    if (!input) return;
    let value = input.value.trim();
    if (!value) return;
    if (!value.includes(".")) value = "esphome." + value;
    if (!state.details.bleTargets.includes(value)) state.details.bleTargets.push(value);
    input.value = "";
    const chips = root.querySelector("#wiz-ble-chips");
    if (chips) chips.innerHTML = bleChipsHtml();
    input.focus();
  }

  /* ---------- generate ---------- */

  async function generate() {
    const d = state.details;
    const host = d.host_url.trim();
    const user = d.username.trim();
    if (!host || !user) {
      state.detailsError = "Host URL and Username are required.";
      render();
      return;
    }
    if (state.method === "ble" && !d.bleTargets.length) {
      state.detailsError = "Add at least one beacon target.";
      render();
      return;
    }

    const data = { method: state.method, host_url: host, username: user };
    const pin = d.pin.trim();
    if (pin) data.pin = pin;
    if (state.profile && state.profile.id) data.profile = state.profile.id;
    if (state.method === "manual") {
      const mins = Number(d.timeout_minutes);
      data.timeout_minutes = Number.isFinite(mins) && mins > 0 ? mins : 30;
    }
    if (state.method === "ble") data.esphome_service = d.bleTargets.slice();
    if (state.method === "qr") {
      data.delete_qr_after_window = !!d.deleteQr;
      const fn = d.qrFilename.trim();
      if (fn) data.qr_filename = fn;
    }

    state.detailsError = "";
    state.busy = true;
    render();
    try {
      const res = await api.provision(data);
      const resp = unwrap(res);
      state.busy = false;
      if (resp && resp.error) {
        // User-level failure — server reports these in-band, not as a throw.
        state.detailsError = String(resp.error);
        render();
        return;
      }
      state.result = resp;
      state.step = "result";
      render();
    } catch (err) {
      state.busy = false;
      state.detailsError = (err && err.message) || String(err);
      render();
    }
  }

  /* ---------- step: result ---------- */

  function linkRow(label, value) {
    return `
      <div class="field">
        <label>${esc(label)}</label>
        <div class="field-row">
          <input class="input mono" readonly value="${esc(value)}">
          <button class="btn btn--outlined" data-copy="${esc(value)}" style="flex:none;">Copy</button>
        </div>
      </div>`;
  }

  function validityChip(expiresAt) {
    if (!expiresAt) return "";
    return `<div style="margin-top:6px;"><span class="chip chip--warn">valid until ${esc(ui.fmtExpiry(expiresAt))}</span></div>`;
  }

  function renderQrResult(r) {
    return `
      <div style="text-align:center; margin-bottom:16px;">
        <div class="muted" style="font-size:13px; margin-bottom:12px;">Scan with the Casa app, or send a setup link.</div>
        <img src="${esc(r.url_path)}" alt="Provisioning QR code"
          style="width:220px; height:220px; border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); padding:12px; background:#fff;">
      </div>
      ${linkRow("Setup Deep Link", r.deep_link)}
      ${r.universal_link ? linkRow("Universal Link (opens from Safari / iMessage)", r.universal_link) : ""}
      ${validityChip(r.expires_at)}`;
  }

  function renderDeepLinkResult(r) {
    return `
      <div class="muted" style="font-size:13px; margin-bottom:12px;">Send a setup link to the device.</div>
      ${linkRow("Setup Deep Link", r.deep_link)}
      ${r.universal_link ? linkRow("Universal Link (opens from Safari / iMessage)", r.universal_link) : ""}
      ${validityChip(r.expires_at)}`;
  }

  function renderManualResult(r) {
    const f = r.fields || {};
    const u = r.unsupported || {};
    const val = (v) =>
      v === undefined || v === null || String(v) === ""
        ? `<em class="muted">(leave blank)</em>`
        : `<code class="mono" style="word-break:break-all;">${esc(String(v))}</code>`;
    const row = (label, v, copyValue) => `
      <div style="font-weight:600;">${esc(label)}</div>
      <div style="display:flex; align-items:center; gap:8px; min-width:0;">
        ${val(v)}
        ${copyValue ? `<button class="btn btn--outlined" data-copy="${esc(String(copyValue))}" style="height:24px; padding:0 8px; font-size:11px; flex:none;">Copy</button>` : ""}
      </div>`;
    const section = (label) => `
      <div style="grid-column:1 / -1; margin-top:8px; font-weight:600; border-bottom:1px solid var(--casa-divider); padding-bottom:2px;">${esc(label)}</div>`;

    const sessionText =
      Number(f.session_expiration) === 0
        ? 'Never — toggle "Session Never Expires" ON'
        : ui.fmtExpiry(f.session_expiration);

    const lost = [];
    if (u.pin) lost.push("provisioning PIN");
    if (u.push_notifications && u.push_notifications !== "false") lost.push("push notifications");
    if (u.wireguard) lost.push("WireGuard VPN");

    return `
      <p class="muted" style="font-size:12px; margin:0 0 10px;">
        Enter these in the Casa app's manual provisioning sheet, field for field.
      </p>
      ${r.expires_at ? `
        <div style="display:flex; gap:8px; align-items:center; margin-bottom:12px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:color-mix(in srgb, var(--casa-warning) 16%, transparent); color:var(--casa-warning); font-size:13px;">
          <ha-icon icon="mdi:clock-alert-outline" style="--mdc-icon-size:18px; flex:none;"></ha-icon>
          <span>Password valid until <strong>${esc(ui.fmtExpiry(r.expires_at))}</strong> — it is scrambled after that window.</span>
        </div>` : ""}
      <div style="display:grid; grid-template-columns:auto 1fr; gap:6px 16px; font-size:13px; align-items:center;">
        ${section("Server Configuration")}
        ${row("Server URL", f.server_url, f.server_url)}
        ${row("Username", f.username, f.username)}
        ${row("Password", f.password, f.password)}
        ${section("Access Control & Network")}
        ${row("Allowed Paths (comma-separated)", f.allowed_paths)}
        ${row("Allowed Wi-Fi SSIDs (comma-separated)", f.allowed_wifi)}
        ${section("Client Customization")}
        ${row("Default Dashboard Path", f.default_dashboard)}
        ${row("Immersive Level", f.immersive_level)}
        ${row("Immersive Color Mode", f.theme_color_mode)}
        ${row("Custom Hex Color", f.custom_color)}
        ${section("Session & Caching")}
        ${row("Session Expiration", sessionText)}
        ${row("Cache Control Hours", f.cache_control_hours)}
        ${section("Onboarding Extras")}
        ${row("Welcome Screen URL", f.welcome_url)}
        ${row("Auto-Join Wi-Fi SSID", f.connect_wifi_ssid)}
        ${row("Auto-Join Wi-Fi Password", f.connect_wifi_password)}
      </div>
      ${lost.length ? `<div class="errbar" style="margin:12px 0 0;">This configuration includes settings manual entry cannot carry over: <strong>${esc(lost.join(", "))}</strong>.</div>` : ""}
      <p class="muted" style="font-size:12px; margin:10px 0 0;">
        Manual entry cannot configure a PIN, push notifications (site binding), or WireGuard VPN.
        A manually provisioned device receives no pushes, remote updates, or remote deprovision;
        session expiration changes still apply on its heartbeat.
      </p>`;
  }

  function renderBleResult(r) {
    const okSet = new Set(r.successful_targets || []);
    const submitted = state.details.bleTargets.length ? state.details.bleTargets : r.successful_targets || [];
    const rows = submitted
      .map(
        (t) => `
        <div class="field-row" style="margin-bottom:8px;">
          <span class="mono" style="flex:1; word-break:break-all;">${esc(t)}</span>
          ${okSet.has(t)
            ? `<span class="chip chip--ok"><ha-icon icon="mdi:check-circle" style="--mdc-icon-size:14px;"></ha-icon> Broadcasting</span>`
            : `<span class="chip chip--error"><ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:14px;"></ha-icon> Failed</span>`}
        </div>`
      )
      .join("");
    return `
      <div class="muted" style="font-size:13px; margin-bottom:12px;">Bring the device near a beacon to provision it.</div>
      ${rows}
      ${r.pin_required ? `
        <div style="display:flex; gap:8px; align-items:center; margin-top:10px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
          <ha-icon icon="mdi:dialpad" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
          <span>The device will prompt for PIN <strong class="mono">${esc(state.details.pin)}</strong>.</span>
        </div>` : ""}
      ${validityChip(r.expires_at)}`;
  }

  function renderResult() {
    const r = state.result || {};
    let content;
    if (r.method === "manual") content = renderManualResult(r);
    else if (r.method === "ble") content = renderBleResult(r);
    else if (r.method === "deep_link") content = renderDeepLinkResult(r);
    else content = renderQrResult(r);
    return `
      ${stepHeader(RESULT_TITLES[r.method] || "Provisioning result")}
      ${content}
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        <button class="btn btn--outlined" data-act="another">Provision another</button>
        <button class="btn btn--primary" data-act="done">Done</button>
      </div>`;
  }

  /* ---------- render + events ---------- */

  function render() {
    if (state.step === "method") root.innerHTML = renderMethod();
    else if (state.step === "profile") root.innerHTML = renderProfile();
    else if (state.step === "details") root.innerHTML = renderDetails();
    else root.innerHTML = renderResult();
    for (const btn of root.querySelectorAll("[data-copy]")) {
      ui.bindCopyButton(btn, () => btn.dataset.copy);
    }
  }

  root.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled || !root.contains(el)) return;
    switch (el.dataset.act) {
      case "back":
        goBack();
        break;
      case "method":
        state.method = el.dataset.method;
        state.step = "profile";
        if (!maybeApplyPreset()) render();
        break;
      case "toggle-advanced":
        state.advancedOpen = !state.advancedOpen;
        render();
        break;
      case "retry-profiles":
        loadProfiles();
        break;
      case "select-adhoc":
        selectProfile(null);
        break;
      case "select-profile": {
        const p = (state.profiles || []).find((x) => x && x.id === el.dataset.id);
        if (p) selectProfile(p);
        break;
      }
      case "more-info":
        state.expandedInfo = state.expandedInfo === el.dataset.id ? null : el.dataset.id;
        rerenderProfileGrid();
        break;
      case "edit-profile":
        modal.close();
        app.navigate("/next/profiles/" + encodeURIComponent(el.dataset.id));
        break;
      case "toggle-qr-options":
        state.qrOptionsOpen = !state.qrOptionsOpen;
        render();
        break;
      case "add-target":
        addBleTarget();
        break;
      case "remove-target": {
        state.details.bleTargets = state.details.bleTargets.filter((t) => t !== el.dataset.target);
        const chips = root.querySelector("#wiz-ble-chips");
        if (chips) chips.innerHTML = bleChipsHtml();
        break;
      }
      case "generate":
        generate();
        break;
      case "another":
        state.result = null;
        state.detailsError = "";
        state.step = "method";
        render();
        break;
      case "done":
        modal.close();
        app.refresh();
        break;
    }
  });

  root.addEventListener("input", (e) => {
    const t = e.target;
    if (t.id === "wiz-search") {
      state.search = t.value;
      rerenderProfileGrid();
      return;
    }
    const field = t.dataset && t.dataset.field;
    if (field) state.details[field] = t.type === "checkbox" ? t.checked : t.value;
  });

  root.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.id === "wiz-ble-input") {
      e.preventDefault();
      addBleTarget();
    }
  });

  render();
  return modal;
}
