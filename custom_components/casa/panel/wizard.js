// Casa admin panel — provision wizard. One wide modal with an ESPHome-style
// stepper: method → profile → details → result. All state lives in this
// closure; the modal body is re-rendered per step with its own back arrow
// (the modal header only carries the title + X). No static sibling imports —
// payload-preview is pulled in via app.loadModule.

const METHOD_LABELS = {
  qr: "Generate Link & QR Code",
  deep_link: "Generate Setup Links",
  ble: "Broadcast Provisioning Beacon",
};

const RESULT_TITLES = {
  qr: "QR code ready",
  deep_link: "Setup links ready",
  ble: "Beacon broadcast",
};

// Mirrors CasaProvisionProfilesView._DEFAULT_FIELDS in __init__.py (and the
// profile editor's DEFAULTS) exactly — keys and types. Backs the full form
// shown when "Manual configuration" is selected on the profile step.
const FORM_DEFAULTS = {
  host_url: "",
  username: "",
  password: "",
  pin: "",
  default_dashboard: "",
  welcome_url: "",
  immersive_level: "1",
  theme_color_mode: "inherit",
  custom_color: "#000000",
  deauthenticate_existing: false,
  allow_all_pages: false,
  allowed_pages: "",
  allowed_wifi: "",
  push_notifications: "false",
  allow_wireguard: false,
  wireguard_config: "",
  wireguard_excluded_wifi: "",
  wireguard_profile_id: "",
  timeout_minutes: 5,
  password_scramble: true,
  password_scramble_in: 0,
  expiration_hours: 336,
  connect_wifi_ssid: "",
  connect_wifi_password: "",
  cache_control_hours: "",
};

const HEX_RE = /^#[0-9a-fA-F]{6}$/;

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
    profile: null, // selected profile object, or null
    manualConfig: false, // true = the pinned "Manual configuration" card was chosen
    search: "",
    expandedInfo: null, // profile id with the "More info" panel open
    qrOptionsOpen: false,
    details: {
      host_url: "",
      username: "",
      pin: "",
      bleTargets: [],
      deleteQr: true,
      qrFilename: "",
    },
    form: null, // manual-config values (FORM_DEFAULTS shape); built once on first entry
    formOpen: { connection: true, appui: false, access: false, pushvpn: false, timing: false, wifi: false },
    fieldErrors: {}, // manual-config inline errors: field key -> message
    wgProfiles: null, // lazy cache for the "Link WireGuard profile" select
    saveAsProfile: false,
    profileName: "",
    detailsError: "",
    busy: false,
    result: null,
  };
  let pendingPresetId = presetProfileId || null;
  let wgRequested = false;

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

  function ensureWgProfiles() {
    if (wgRequested) return;
    wgRequested = true;
    api
      .getWireguardProfiles()
      .then((res) => {
        state.wgProfiles = (res && res.profiles) || [];
      })
      .catch(() => {
        state.wgProfiles = [];
      })
      .then(() => rerenderFormSection("pushvpn"));
  }

  /* ---------- helpers ---------- */

  const trim = (v) => String(v ?? "").trim();
  const asBool = (v) => v === true || trim(v).toLowerCase() === "true";

  function selectProfile(profile) {
    state.manualConfig = false;
    state.profile = profile || null;
    const f = (profile && profile.fields) || {};
    state.details.host_url = trim(f.host_url) || window.location.origin;
    state.details.username = presetUsername || trim(f.username);
    state.details.pin = trim(f.pin).slice(0, 6);
    state.detailsError = "";
    state.step = "details";
    render();
  }

  function selectManualConfig() {
    state.manualConfig = true;
    state.profile = null;
    if (!state.form) {
      // Built once per wizard open — later expander/gating re-renders always
      // read back from this object, so no keystroke is ever lost.
      state.form = { ...FORM_DEFAULTS };
      state.form.host_url = window.location.origin;
      if (presetUsername) state.form.username = presetUsername;
    }
    state.detailsError = "";
    state.fieldErrors = {};
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

  function optionCard({ method, title, desc, disabled, disabledTitle }) {
    return `
      <button class="option-card" data-act="method" data-method="${esc(method)}"
        ${disabled ? `disabled title="${esc(disabledTitle || "")}"` : ""}>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          <span class="option-card__desc" style="display:block;">${esc(desc)}</span>
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

  function manualConfigCard() {
    return `
      <div class="card">
        <div class="card__body">
          <div style="font-weight:600; font-size:14px;">Manual configuration</div>
          <div class="muted" style="font-size:12px; margin-top:2px;">Configure every provisioning option by hand. Server URL and site binding are pre-filled.</div>
        </div>
        <div class="card__footer">
          <span class="spacer"></span>
          <button class="btn btn--text" data-act="select-manual" style="height:28px;">+ Select</button>
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
        ${manualConfigCard()}
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

  /* ---------- step: details (profile / compact path) ---------- */

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

  // Shared by the compact (profile) and manual-configuration details forms.
  function bleTargetsField() {
    const services = Object.keys((app.hass() && app.hass().services && app.hass().services.esphome) || {}).map(
      (s) => "esphome." + s
    );
    return `
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
  }

  function generateFooter() {
    return `
      <div style="display:flex; justify-content:flex-end; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        <button class="btn btn--primary" data-act="generate" ${state.busy ? "disabled" : ""}>
          ${state.busy ? "Working…" : esc(METHOD_LABELS[state.method] || "Generate")}
        </button>
      </div>`;
  }

  function renderDetails() {
    if (state.manualConfig) return renderManualDetails();
    const d = state.details;
    const m = state.method;
    let conditional = "";

    if (m === "ble") {
      conditional = bleTargetsField();
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
      ${generateFooter()}`;
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

  /* ---------- step: details (manual-configuration path) ---------- */

  const fval = (key) => (state.form ? state.form[key] ?? "" : "");

  function fText({ key, label, help, placeholder = "", type = "text", attrs = "" }) {
    const err = state.fieldErrors[key];
    return `
      <div class="field ${err ? "field--error" : ""}">
        <label>${esc(label)}</label>
        <input class="input" type="${esc(type)}" data-field="${esc(key)}" value="${esc(fval(key))}"
          placeholder="${esc(placeholder)}" ${attrs}>
        ${help ? `<div class="field__help">${esc(help)}</div>` : ""}
        ${err ? `<div class="field__error">${esc(err)}</div>` : ""}
      </div>`;
  }

  function fNumber({ key, label, help, min = 0, max }) {
    return fText({ key, label, help, type: "number", attrs: `min="${min}"${max != null ? ` max="${max}"` : ""}` });
  }

  function fSelect({ key, label, help, options }) {
    const current = String(fval(key));
    const opts = options
      .map(
        (o) => `<option value="${esc(o.value)}" ${String(o.value) === current ? "selected" : ""}>${esc(o.label)}</option>`
      )
      .join("");
    return `
      <div class="field">
        <label>${esc(label)}</label>
        <select class="select" data-field="${esc(key)}">${opts}</select>
        ${help ? `<div class="field__help">${esc(help)}</div>` : ""}
      </div>`;
  }

  function fToggle({ key, label, help }) {
    return `
      <div>
        <label class="toggle">
          <input type="checkbox" data-field="${esc(key)}" ${fval(key) ? "checked" : ""}>
          <span>${esc(label)}</span>
        </label>
        ${help ? `<div class="field__help" style="margin:-4px 0 12px 24px;">${esc(help)}</div>` : ""}
      </div>`;
  }

  function renderFormConnection() {
    return `
      ${fText({ key: "host_url", label: "Host URL *", placeholder: "http://192.168.1.21:8123", help: "The Home Assistant URL the device will use. Required." })}
      ${fText({ key: "username", label: "Username *", placeholder: "guest", help: "Guest account the device signs in as. Required.", attrs: 'autocapitalize="none" autocomplete="off"' })}
      ${fText({ key: "password", label: "Password", placeholder: "(auto-generated at provision)", help: "Leave blank to auto-generate a secure password at provision time.", attrs: 'autocomplete="off"' })}
      ${fText({ key: "pin", label: "PIN", placeholder: "123456", attrs: 'maxlength="6" inputmode="numeric" autocomplete="off"', help: "Optional, max 6 digits." })}
      ${fToggle({ key: "deauthenticate_existing", label: "Sign out existing sessions at provision", help: "Deauthenticates the account's existing connections when a device is provisioned." })}`;
  }

  function renderFormAppUi() {
    const colorDisabled = String(fval("theme_color_mode") || "inherit") === "inherit";
    const raw = String(fval("custom_color")).trim();
    const pickValue = HEX_RE.test(raw) ? raw : "#000000";
    return `
      ${fText({ key: "default_dashboard", label: "Default dashboard", placeholder: "/lovelace/home", help: "Path the app opens on launch." })}
      ${fText({ key: "welcome_url", label: "Welcome URL", help: "Optional URL shown after provisioning." })}
      ${fSelect({
        key: "immersive_level", label: "Immersive level",
        options: [
          { value: "1", label: "Level 1 (Standard)" },
          { value: "2", label: "Level 2 (Transparent status bar)" },
          { value: "3", label: "Level 3 (Fullscreen)" },
        ],
      })}
      ${fSelect({
        key: "theme_color_mode", label: "Theme color mode",
        options: [
          { value: "inherit", label: "Inherit from HA" },
          { value: "custom", label: "Custom color" },
          { value: "inherit_with_fallback", label: "Inherit with fallback" },
        ],
      })}
      <div class="field">
        <label>Custom color</label>
        <div class="field-row">
          <input type="color" data-ref="color-pick" value="${esc(pickValue)}" ${colorDisabled ? "disabled" : ""}
            style="flex:none; width:44px; height:36px; padding:2px; border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); background:var(--casa-card-bg); cursor:pointer;">
          <input class="input" data-field="custom_color" value="${esc(fval("custom_color"))}"
            placeholder="#03A9F4" ${colorDisabled ? "disabled" : ""}>
        </div>
        <div class="field__help">Hex color used when the theme color mode is not "Inherit from HA".</div>
      </div>`;
  }

  function renderFormAccess() {
    const allowAll = !!fval("allow_all_pages");
    return `
      ${fToggle({ key: "allow_all_pages", label: "Allow all pages", help: "When on, the device may open any page (payload sends /*)." })}
      <div class="field">
        <label>Allowed pages</label>
        <input class="input" data-field="allowed_pages" value="${esc(fval("allowed_pages"))}"
          placeholder="${allowAll ? "/*" : "/lovelace/home, /dashboard-1/*"}" ${allowAll ? "disabled" : ""}>
        <div class="field__help">Comma-separated paths the device may open. Ignored while "Allow all pages" is on.</div>
      </div>
      ${fText({ key: "allowed_wifi", label: "Allowed Wi-Fi", placeholder: "HomeSSID, OfficeSSID", help: "Comma-separated SSIDs the app may be used on. Blank = any network." })}`;
  }

  function renderFormPushVpn() {
    const linkedId = String(fval("wireguard_profile_id"));
    const wg = state.wgProfiles || [];
    const linked = linkedId ? wg.find((p) => p && p.id === linkedId) || null : null;
    const options = [{ value: "", label: "-- None / paste config below --" }].concat(
      wg.map((p) => ({ value: p.id, label: p.alias || p.id }))
    );
    if (linkedId && !linked) options.push({ value: linkedId, label: `(missing profile ${linkedId})` });

    const inlineHtml = linked
      ? `<div class="muted" style="font-size:13px; margin:2px 0 14px;">
           Config comes from profile '${esc(linked.alias || linked.id)}'.
         </div>`
      : `
        <div class="field">
          <label>WireGuard config</label>
          <textarea class="textarea" data-field="wireguard_config" placeholder="[Interface]&#10;PrivateKey = ...">${esc(fval("wireguard_config"))}</textarea>
          <div class="field__help">Full client config pushed to the device. Ignored when a WireGuard profile is linked above.</div>
        </div>
        ${fText({ key: "wireguard_excluded_wifi", label: "WireGuard excluded Wi-Fi", placeholder: "HomeSSID", help: "SSIDs on which the tunnel stays down (device is already local)." })}`;

    const summary = app.summary && app.summary();
    const siteLine = summary && summary.site_id
      ? `Site binding is automatic — site ${summary.site_id}`
      : "Site binding is automatic — the server applies the site ID at provision time";
    return `
      ${fSelect({
        key: "push_notifications", label: "Push notifications",
        options: [
          { value: "false", label: "Disabled" },
          { value: "true", label: "Enabled" },
          { value: "mandatory", label: "Mandatory" },
        ],
      })}
      <div class="muted" style="font-size:12px; margin:-6px 0 14px;">${esc(siteLine)}.</div>
      ${fToggle({ key: "allow_wireguard", label: "Allow WireGuard", help: "Lets the device bring up the VPN tunnel for remote access." })}
      ${fSelect({
        key: "wireguard_profile_id", label: "Link WireGuard profile", options,
        help: state.wgProfiles === null ? "Loading WireGuard profiles…" : "Linking a profile overrides the pasted config below.",
      })}
      ${inlineHtml}`;
  }

  function renderFormTiming() {
    const qrExtra = state.method === "qr"
      ? `
        <label class="toggle">
          <input type="checkbox" data-field="deleteQr" ${state.details.deleteQr ? "checked" : ""}>
          Delete QR image after the entry window
        </label>
        <div class="field">
          <label>QR filename</label>
          <input class="input" data-field="qrFilename" value="${esc(state.details.qrFilename)}" placeholder="Optional — e.g. casa_qr.png">
        </div>`
      : "";
    return `
      ${fNumber({ key: "timeout_minutes", label: "Timeout (minutes)", min: 0, max: 60, help: "Minutes the provisioning code can be scanned or used. 0 = code never expires." })}
      ${fNumber({ key: "expiration_hours", label: "Session expiration (hours)", min: 0, max: 87600, help: "How long the provisioned session lasts. 0 = permanent session." })}
      ${fToggle({ key: "password_scramble", label: "Scramble password after window", help: "Rotates the guest password once the provisioning window closes." })}
      ${fNumber({ key: "password_scramble_in", label: "Password scramble in (minutes)", min: 0, max: 120, help: "0 = inherit from timeout." })}
      ${fText({ key: "cache_control_hours", label: "Cache control (hours)", placeholder: "48", help: "Blank = app default (48h)." })}
      ${qrExtra}`;
  }

  function renderFormWifi() {
    return `
      ${fText({ key: "connect_wifi_ssid", label: "Connect Wi-Fi SSID", placeholder: "MyNetwork", help: "Network the device joins during provisioning. Blank = skip." })}
      <div class="field">
        <label>Connect Wi-Fi password</label>
        <div class="field-row">
          <input class="input" type="password" data-field="connect_wifi_password" value="${esc(fval("connect_wifi_password"))}" placeholder="Password" autocomplete="off">
          <button type="button" class="btn btn--icon" data-act="reveal-wifi-pw" title="Show password" style="flex:none;">
            <ha-icon icon="mdi:eye-outline"></ha-icon>
          </button>
        </div>
      </div>`;
  }

  const FORM_SECTIONS = [
    ["connection", "Connection", renderFormConnection],
    ["appui", "App UI", renderFormAppUi],
    ["access", "Access Control", renderFormAccess],
    ["pushvpn", "Push & VPN", renderFormPushVpn],
    ["timing", "Timing & Security", renderFormTiming],
    ["wifi", "Wi-Fi & Extras", renderFormWifi],
  ];

  // Re-render one section's body from state (used for gating changes and the
  // lazy WireGuard-profile load) so sibling sections keep their DOM untouched.
  function rerenderFormSection(id) {
    if (!state.manualConfig || state.step !== "details") return;
    const entry = FORM_SECTIONS.find((s) => s[0] === id);
    const body = root.querySelector(`[data-section-body="${id}"]`);
    if (entry && body) body.innerHTML = entry[2]();
  }

  function renderManualDetails() {
    const sections = FORM_SECTIONS.map(([id, label, renderFn]) => {
      const open = !!state.formOpen[id];
      return `
        <div style="border-top:1px solid var(--casa-divider);">
          <button class="btn btn--text" data-act="toggle-section" data-section="${esc(id)}" style="margin:6px 0; padding-left:0;">
            <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${open ? "180deg" : "0deg"});"></ha-icon>
            ${esc(label)}
          </button>
          <div data-section-body="${esc(id)}" ${open ? "" : "hidden"}>${renderFn()}</div>
        </div>`;
    }).join("");
    return `
      ${stepHeader("Manual configuration")}
      ${state.detailsError ? `<div class="errbar">${esc(state.detailsError)}</div>` : ""}
      ${state.method === "ble" ? bleTargetsField() : ""}
      ${sections}
      <div style="border-top:1px solid var(--casa-divider); padding-top:10px;">
        <label class="toggle">
          <input type="checkbox" data-field="saveAsProfile" ${state.saveAsProfile ? "checked" : ""}>
          Save as profile
        </label>
        <div data-save-name ${state.saveAsProfile ? "" : "hidden"}>
          <div class="field">
            <label>Profile name</label>
            <input class="input" data-field="profileName" value="${esc(state.profileName)}" placeholder="e.g. Guest tablet">
            <div class="field__help">Leave blank to auto-generate.</div>
          </div>
        </div>
      </div>
      ${generateFooter()}`;
  }

  /* ---------- generate ---------- */

  async function submitProvision(data) {
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
    if (state.method === "ble") data.esphome_service = d.bleTargets.slice();
    if (state.method === "qr") {
      data.delete_qr_after_window = !!d.deleteQr;
      const fn = d.qrFilename.trim();
      if (fn) data.qr_filename = fn;
    }

    await submitProvision(data);
  }

  async function generateManual() {
    const host = trim(state.form.host_url);
    const user = trim(state.form.username);
    state.fieldErrors = {};
    if (!host) state.fieldErrors.host_url = "Required.";
    if (!user) state.fieldErrors.username = "Required.";
    if (!host || !user) {
      state.detailsError = "Host URL and Username are required.";
      state.formOpen.connection = true;
      render();
      return;
    }
    if (state.method === "ble" && !state.details.bleTargets.length) {
      state.detailsError = "Add at least one beacon target.";
      render();
      return;
    }

    // Coerce by the defaults' types: booleans as booleans, numbers via
    // parseInt (falling back to the default), strings trimmed. The server's
    // get_field treats "" as unset, so blank optional keys are harmless.
    const fields = {};
    for (const [key, def] of Object.entries(FORM_DEFAULTS)) {
      const raw = state.form[key];
      if (typeof def === "boolean") fields[key] = !!raw;
      else if (typeof def === "number") {
        const n = parseInt(raw, 10);
        fields[key] = Number.isFinite(n) ? n : def;
      } else fields[key] = trim(raw);
    }
    fields.host_url = host;
    fields.username = user;

    if (state.saveAsProfile) {
      state.detailsError = "";
      state.busy = true;
      render();
      try {
        // Flat body shape — { name, ...fields } — matches the server view.
        await api.saveProvisionProfile({ name: trim(state.profileName), ...fields });
      } catch (err) {
        state.busy = false;
        state.detailsError = "Failed to save profile: " + ((err && err.message) || String(err));
        render();
        return;
      }
    }

    const data = { method: state.method, ...fields };
    if (state.method === "qr") {
      data.delete_qr_after_window = !!state.details.deleteQr;
      const fn = trim(state.details.qrFilename);
      if (fn) data.qr_filename = fn;
    }
    if (state.method === "ble") data.esphome_service = state.details.bleTargets.slice();

    await submitProvision(data);
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
    const pin = state.manualConfig ? trim(state.form && state.form.pin) : state.details.pin;
    return `
      <div class="muted" style="font-size:13px; margin-bottom:12px;">Bring the device near a beacon to provision it.</div>
      ${rows}
      ${r.pin_required ? `
        <div style="display:flex; gap:8px; align-items:center; margin-top:10px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
          <ha-icon icon="mdi:dialpad" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
          <span>The device will prompt for PIN <strong class="mono">${esc(pin)}</strong>.</span>
        </div>` : ""}
      ${validityChip(r.expires_at)}`;
  }

  function renderResult() {
    const r = state.result || {};
    let content;
    if (r.method === "ble") content = renderBleResult(r);
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
    if (state.manualConfig && state.step === "details" && state.formOpen.pushvpn) ensureWgProfiles();
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
      case "select-manual":
        selectManualConfig();
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
        app.navigate("/profiles/" + encodeURIComponent(el.dataset.id));
        break;
      case "toggle-qr-options":
        state.qrOptionsOpen = !state.qrOptionsOpen;
        render();
        break;
      case "toggle-section": {
        // Flip visibility in place — no re-render, so in-progress edits and
        // focus elsewhere in the form are untouched.
        const id = el.dataset.section;
        state.formOpen[id] = !state.formOpen[id];
        const body = root.querySelector(`[data-section-body="${id}"]`);
        if (body) body.hidden = !state.formOpen[id];
        const icon = el.querySelector("ha-icon");
        if (icon) icon.style.transform = `rotate(${state.formOpen[id] ? "180deg" : "0deg"})`;
        if (id === "pushvpn" && state.formOpen.pushvpn) ensureWgProfiles();
        break;
      }
      case "reveal-wifi-pw": {
        const input = root.querySelector('[data-field="connect_wifi_password"]');
        if (!input) break;
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        el.title = show ? "Hide password" : "Show password";
        el.innerHTML = `<ha-icon icon="${show ? "mdi:eye-off-outline" : "mdi:eye-outline"}"></ha-icon>`;
        break;
      }
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
        if (state.manualConfig) generateManual();
        else generate();
        break;
      case "another":
        state.result = null;
        state.detailsError = "";
        state.fieldErrors = {};
        state.step = "method";
        render();
        break;
      case "done":
        modal.close();
        app.refresh();
        break;
    }
  });

  function clearFieldError(key, inputEl) {
    if (!state.fieldErrors[key]) return;
    delete state.fieldErrors[key];
    const wrap = inputEl.closest(".field");
    if (wrap) {
      wrap.classList.remove("field--error");
      const err = wrap.querySelector(".field__error");
      if (err) err.remove();
    }
  }

  // Commit a manual-configuration form input into state.form.
  function commitFormInput(t, field) {
    let value = t.type === "checkbox" ? t.checked : t.value;
    if (field === "pin") {
      const cleaned = String(value).replace(/\D/g, "").slice(0, 6);
      if (cleaned !== t.value) t.value = cleaned;
      value = cleaned;
    }
    state.form[field] = value;
    if (field === "custom_color") {
      // Text twin drives the color swatch when it holds a valid hex.
      const trimmed = String(value).trim();
      const pick = root.querySelector('[data-ref="color-pick"]');
      if (pick && HEX_RE.test(trimmed)) pick.value = trimmed;
    }
    clearFieldError(field, t);
  }

  root.addEventListener("input", (e) => {
    const t = e.target;
    if (t.id === "wiz-search") {
      state.search = t.value;
      rerenderProfileGrid();
      return;
    }
    if (t.dataset && t.dataset.ref === "color-pick" && state.form) {
      // Color swatch drives the text twin (both map to custom_color).
      state.form.custom_color = t.value;
      const twin = root.querySelector('[data-field="custom_color"]');
      if (twin) twin.value = t.value;
      return;
    }
    const field = t.dataset && t.dataset.field;
    if (!field) return;
    if (field === "saveAsProfile") {
      state.saveAsProfile = !!t.checked;
      const nameWrap = root.querySelector("[data-save-name]");
      if (nameWrap) nameWrap.hidden = !state.saveAsProfile;
      return;
    }
    if (field === "profileName") {
      state.profileName = t.value;
      return;
    }
    if (state.manualConfig && state.step === "details" && state.form && field in state.form) {
      commitFormInput(t, field);
      return;
    }
    state.details[field] = t.type === "checkbox" ? t.checked : t.value;
  });

  // Gating fields re-render only their own section from state (nothing is
  // lost — the value was committed by the input listener / commit above).
  root.addEventListener("change", (e) => {
    const t = e.target;
    const field = t.dataset && t.dataset.field;
    if (!field || !state.manualConfig || state.step !== "details" || !state.form || !(field in state.form)) return;
    state.form[field] = t.type === "checkbox" ? t.checked : t.value;
    if (field === "theme_color_mode") rerenderFormSection("appui");
    else if (field === "allow_all_pages") rerenderFormSection("access");
    else if (field === "wireguard_profile_id") rerenderFormSection("pushvpn");
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
