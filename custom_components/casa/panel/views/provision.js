// Casa admin panel — provision device flow. Full-page view at /provision
// with a step-tab flow: Profile (pick a saved profile, create a new one, or
// one-off config) → Configure → Deploy (QR / deep link / BLE) → Done. Tabs
// are progress indicators — earlier tabs are clickable and preserve state,
// forward movement only happens through the footer buttons so validation
// can't be skipped. "Create new profile" saves via the profiles API at
// deploy time (no orphan profiles if the flow is abandoned); retries reuse
// the created id so a failed provision never duplicates the profile.
//
// The field schema/renderer is shared with the profile editor and device
// editor via profile-fields.js — this view renders the same sections
// filtered by scope: template (LIVE + expiration_hours) fields alongside
// process-only fields (username/password/pin, deauth, timeout, scramble,
// Wi-Fi join), which are legitimate here because this flow IS the
// provisioning action. Process values ride casa.provision service_data and
// are never persisted to a profile.

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

// Gating fields → the accordion section whose body re-renders when they flip.
const KEY_SECTION = {
  theme_color_mode: "appui",
  allow_all_pages: "access",
  wireguard_profile_id: "pushvpn",
};

// Full-form Timing section shows only template-scope timing fields; the
// process-scope timing/wifi fields live in the "Provisioning options" section.
const TEMPLATE_TIMING_SET = new Set(["expiration_hours", "cache_control_hours"]);
const FORM_PROCESS_SET = new Set([
  "timeout_minutes", "password_scramble", "password_scramble_in",
  "connect_wifi_ssid", "connect_wifi_password",
]);

const STEPS = [
  { id: "profile", label: "Profile" },
  { id: "configure", label: "Configure" },
  { id: "deploy", label: "Deploy" },
  { id: "result", label: "Done" },
];
const stepIndex = (id) => STEPS.findIndex((s) => s.id === id);

export function createView(app) {
  const { api, ui } = app;
  const esc = ui.esc;
  const unwrap = (res) => api.constructor.response(res); // CasaApi.response

  const trim = (v) => String(v ?? "").trim();
  const bleAvailable = () => !!(app.hass() && app.hass().services && app.hass().services.esphome);

  /* ---------- lazily loaded siblings (never static imports) ---------- */
  let fieldsMod = null; // views/profile-fields.js
  let previewMod = null; // payload-preview.js
  let renderJsonCode = null; // editor-shell.js named export

  /* ---------- per-mount state ---------- */
  let mountToken = 0;
  let state = null;
  let refs = null; // { tabs, body }
  let wgRequested = false;
  let mountedWithPresetPath = false;
  let scenarioModal = null; // scenario-picker handle, closed on unmount

  function freshState() {
    return {
      step: "profile", // "profile" | "configure" | "deploy" | "result"
      entry: null, // "profile" | "new" | "oneoff"
      // step 1
      profiles: null, // null = loading
      profilesError: null,
      search: "",
      profile: null, // selected saved-profile object
      presetUsername: "",
      // step 2 — profile path (compact)
      details: { host_url: "", username: "", pin: "" },
      procOpts: null, // process-setting overrides; seeded per selected profile
      procOptionsOpen: false,
      previewOpen: false,
      overrideFields: false, // compact path escaped to the full form
      // step 2 — new / one-off / override path (full form)
      form: null, // fieldsMod.DEFAULTS shape; built once per run
      formOpen: { connection: true, appui: false, access: false, pushvpn: false, timing: false, process: false },
      profileName: "", // entry === "new" only
      savedProfileId: null, // set once saveProvisionProfile succeeds (retry guard)
      configError: "",
      // step 3
      method: "qr",
      bleTargets: [],
      deleteQr: true,
      qrFilename: "",
      qrOptionsOpen: false,
      deployError: "",
      busy: false,
      // step 4
      result: null,
      wgProfiles: null, // lazy cache for the "Link WireGuard profile" select
    };
  }

  const isCompact = () => state.entry === "profile" && !state.overrideFields;
  const dirty = () => !!(state && state.entry !== null && state.step !== "result");

  /* ---------- data ---------- */

  async function loadProfiles() {
    state.profiles = null;
    state.profilesError = null;
    if (state.step === "profile") render();
    const token = mountToken;
    try {
      const res = await api.getProvisionProfiles();
      if (token !== mountToken || !state) return;
      state.profiles = (res && res.profiles) || [];
    } catch (err) {
      if (token !== mountToken || !state) return;
      state.profiles = [];
      state.profilesError = (err && err.message) || String(err);
    }
    if (state.step === "profile") render();
  }

  function ensureWgProfiles() {
    if (wgRequested) return;
    wgRequested = true;
    const token = mountToken;
    api
      .getWireguardProfiles()
      .then((res) => {
        if (token !== mountToken || !state) return;
        state.wgProfiles = (res && res.profiles) || [];
      })
      .catch(() => {
        if (token !== mountToken || !state) return;
        state.wgProfiles = [];
      })
      .then(() => {
        if (token !== mountToken || !state) return;
        rerenderFormSection("pushvpn");
        updatePreview();
      });
  }

  /* ---------- step transitions ---------- */

  function gotoStep(id) {
    state.step = id;
    render();
  }

  function selectProfile(profile) {
    if (!fieldsMod) return; // sibling modules still loading (sub-second window)
    state.entry = "profile";
    state.overrideFields = false;
    state.profile = profile || null;
    const f = (profile && profile.fields) || {};
    state.details.host_url = trim(f.host_url) || window.location.origin;
    state.details.username = state.presetUsername || trim(f.username);
    state.details.pin = trim(f.pin).slice(0, 6);
    // Process settings for this generation: prefilled from the profile's
    // LEGACY stored process values when present (older profiles saved them),
    // else defaults — what's shown is exactly what gets sent. username/pin
    // stay compact top-level inputs.
    state.procOpts = {};
    for (const key of fieldsMod.PROCESS_KEYS) {
      if (key === "username" || key === "pin") continue;
      state.procOpts[key] = f[key] !== undefined ? f[key] : fieldsMod.DEFAULTS[key];
    }
    state.procOptionsOpen = false;
    state.configError = "";
    gotoStep("configure");
  }

  function selectEntry(kind) {
    if (!fieldsMod) return; // sibling modules still loading (sub-second window)
    state.entry = kind; // "new" | "oneoff"
    state.overrideFields = false;
    state.profile = null;
    if (!state.form) {
      // Built once per run — later expander/gating re-renders always read
      // back from this object, so no keystroke is ever lost.
      state.form = { ...fieldsMod.DEFAULTS };
      state.form.host_url = window.location.origin;
      if (state.presetUsername) state.form.username = state.presetUsername;
    }
    state.configError = "";
    gotoStep("configure");
  }

  // "Override fields" escape hatch from the compact profile-path configure
  // screen: seeds the full form from the profile's fields plus whatever the
  // admin already typed in the compact host/username/pin inputs and the
  // Provisioning-options panel, then routes to the same full-form renderer.
  // One-way — no path back to the compact view once triggered.
  function overrideFieldsFromProfile() {
    if (!state.profile) return;
    const f = (state.profile && state.profile.fields) || {};
    state.form = { ...fieldsMod.DEFAULTS, ...f, ...(state.procOpts || {}) };
    state.form.host_url = trim(state.details.host_url) || state.form.host_url;
    state.form.username = trim(state.details.username) || state.form.username;
    state.form.pin = trim(state.details.pin) || state.form.pin;
    state.overrideFields = true;
    state.configError = "";
    render();
  }

  function goBack() {
    if (state.step === "configure") gotoStep("profile");
    else if (state.step === "deploy") gotoStep("configure");
  }

  /* ---------- step tabs ---------- */

  function renderTabs() {
    const cur = stepIndex(state.step);
    const done = state.step === "result";
    refs.tabs.innerHTML = STEPS.map((s, i) => `
      <button class="tab ${i === cur ? "tab--active" : ""} ${i < cur && !done ? "tab--done" : ""}"
        data-act="goto-step" data-step="${esc(s.id)}"
        ${i >= cur || done ? "disabled" : ""}>
        <span class="step-dot">${i < cur ? "✓" : i + 1}</span>${esc(s.label)}
      </button>`).join("");
  }

  /* ---------- step 1: profile ---------- */

  function entryCardsHtml() {
    return `
      <div style="display:flex; gap:12px; flex-wrap:wrap;">
        <button class="option-card" data-act="entry-new" style="flex:1; min-width:260px;">
          <ha-icon icon="mdi:file-plus-outline" style="color:var(--casa-text-2);"></ha-icon>
          <span class="option-card__text">
            <span class="option-card__title" style="display:block;">Create new profile</span>
            <span class="option-card__desc" style="display:block;">Build a reusable profile as you go — saved when you deploy</span>
          </span>
          <ha-icon class="chevron" icon="mdi:chevron-right"></ha-icon>
        </button>
        <button class="option-card" data-act="entry-oneoff" style="flex:1; min-width:260px;">
          <ha-icon icon="mdi:tune" style="color:var(--casa-text-2);"></ha-icon>
          <span class="option-card__text">
            <span class="option-card__title" style="display:block;">One-off configuration</span>
            <span class="option-card__desc" style="display:block;">Device-specific setup, nothing saved</span>
          </span>
          <ha-icon class="chevron" icon="mdi:chevron-right"></ha-icon>
        </button>
      </div>`;
  }

  function profileTableHtml() {
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
    if (!state.profiles.length) {
      return `${errHtml}
        <div class="empty-state">
          <ha-icon icon="mdi:file-cog-outline"></ha-icon>
          <div>No provisioning profiles yet</div>
          <div class="muted" style="font-size:13px;">Create one as you provision, or configure this device one-off.</div>
          <button class="btn btn--primary" data-act="entry-new">+ Create new profile</button>
        </div>`;
    }
    const q = state.search.trim().toLowerCase();
    const matches = state.profiles.filter((p) => {
      if (!q) return true;
      const f = p.fields || {};
      return [p.name, f.username, f.host_url].some((v) => String(v || "").toLowerCase().includes(q));
    });
    if (!matches.length) {
      return `${errHtml}
        <div class="empty-state">
          <ha-icon icon="mdi:magnify-close"></ha-icon>
          <div>No profiles match "${esc(state.search.trim())}"</div>
          <button class="btn btn--text" data-act="clear-search">Clear search</button>
        </div>`;
    }
    const trs = matches
      .map((p) => {
        const f = p.fields || {};
        const chips = (previewMod ? previewMod.profileChips(p) : [])
          .map((c) => `<span class="chip ${esc(c.cls || "chip--neutral")}">${esc(c.label)}</span>`)
          .join("");
        return `<tr class="row--clickable" data-id="${esc(p.id)}">
          <td>
            <strong>${esc(p.name || "(unnamed)")}</strong>
            ${chips ? `<div style="display:flex; flex-wrap:wrap; gap:4px; margin-top:4px;">${chips}</div>` : ""}
          </td>
          <td class="mono">${esc(f.host_url || "—")}${f.username ? ` <span class="muted">(${esc(f.username)})</span>` : ""}</td>
          <td>${esc(ui.fmtTime(p.updated_at))}</td>
          <td class="col-actions">
            <button class="btn btn--icon" data-act="edit-profile" data-id="${esc(p.id)}" title="Edit profile"><ha-icon icon="mdi:pencil"></ha-icon></button>
            <button class="btn btn--text" data-act="select-profile" data-id="${esc(p.id)}" style="height:28px;">Select</button>
          </td>
        </tr>`;
      })
      .join("");
    return `${errHtml}
      <div class="card" style="overflow-x:auto;">
        <table class="table">
          <thead><tr><th>Name</th><th>Host</th><th>Updated</th><th></th></tr></thead>
          <tbody>${trs}</tbody>
        </table>
      </div>`;
  }

  function renderProfileStep() {
    return `
      ${entryCardsHtml()}
      <div style="border-top:1px solid var(--casa-divider); margin:8px 0 14px;"></div>
      <h4 style="margin:0 0 10px; font-size:14px; font-weight:600;">Or start from a saved profile</h4>
      <div class="list-toolbar">
        <div class="search-field">
          <ha-icon icon="mdi:magnify"></ha-icon>
          <input class="input" id="pw-search" type="search" placeholder="Search profiles…" value="${esc(state.search)}">
        </div>
      </div>
      <div id="pw-table">${profileTableHtml()}</div>`;
  }

  function rerenderProfileTable() {
    const table = refs && refs.body.querySelector("#pw-table");
    if (table) table.innerHTML = profileTableHtml();
  }

  /* ---------- step 2: configure ---------- */

  function stepFooter(primaryLabel, primaryAct) {
    return `
      <div style="display:flex; justify-content:space-between; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        <button class="btn btn--text" data-act="back">Back</button>
        <button class="btn btn--primary" data-act="${esc(primaryAct)}" ${state.busy ? "disabled" : ""}>
          ${state.busy ? "Working…" : esc(primaryLabel)}
        </button>
      </div>`;
  }

  function originChipHtml() {
    if (state.entry === "profile") {
      return `<span class="chip chip--app">Profile: ${esc(state.profile ? state.profile.name || state.profile.id : "")}</span>`;
    }
    if (state.entry === "new") {
      return `<span class="chip chip--app">New profile: ${esc(trim(state.profileName) || "(unnamed)")}</span>`;
    }
    return `<span class="chip chip--neutral">One-off configuration</span>`;
  }

  // Compact-path process settings (everything PROCESS-scope except the
  // username/pin compact inputs): password override, deauth, provisioning
  // window, scramble, Wi-Fi join. Prefilled from the selected profile's
  // legacy stored values where present; always sent explicitly.
  function procOptionsHtml() {
    if (!fieldsMod || !state.procOpts) return "";
    const procSet = new Set(Object.keys(state.procOpts));
    const opts = { esc, heading: false, fields: procSet };
    return `
      <button class="btn btn--text" data-act="toggle-proc-options" style="margin:2px 0 8px; padding-left:0;">
        <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${state.procOptionsOpen ? "180deg" : "0deg"});"></ha-icon>
        Provisioning options
      </button>
      <div ${state.procOptionsOpen ? "" : "hidden"}>
        <div class="muted" style="font-size:12px; margin:0 0 10px;">
          One-time settings for this provisioning run — prefilled from this
          profile's saved settings where present, never stored on the device.
        </div>
        <div id="pw-proc-fields">
          ${fieldsMod.renderSectionHtml("connection", state.procOpts, opts)}
          ${fieldsMod.renderSectionHtml("timing", state.procOpts, opts)}
          ${fieldsMod.renderSectionHtml("wifi", state.procOpts, opts)}
        </div>
      </div>`;
  }

  function previewAndOverrideHtml() {
    return `
      <div style="border-top:1px solid var(--casa-divider); margin-top:14px; padding-top:10px; display:flex; align-items:center; justify-content:space-between; gap:8px;">
        <button class="btn btn--text" data-act="toggle-preview" style="padding-left:0;">
          <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${state.previewOpen ? "180deg" : "0deg"});"></ha-icon>
          Preview full configuration
        </button>
        <button class="btn btn--text" data-act="override-fields">Override fields</button>
      </div>
      <div ${state.previewOpen ? "" : "hidden"}>
        <div id="pw-preview-body" style="border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); max-height:320px; overflow:auto; margin-top:4px; background:var(--casa-bg-2);"></div>
      </div>`;
  }

  function renderCompactConfigure() {
    const d = state.details;
    return `
      <div style="margin:0 0 14px;">${originChipHtml()}</div>
      ${state.configError ? `<div class="errbar">${esc(state.configError)}</div>` : ""}
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
      ${procOptionsHtml()}
      ${previewAndOverrideHtml()}
      ${stepFooter("Continue", "to-deploy")}`;
  }

  // Rebuilds the read-only payload preview from the profile's stored fields
  // merged with the compact form's live host/username/pin edits. No-op unless
  // the disclosure is open, so it's safe to call speculatively.
  function updatePreview() {
    if (!state || !isCompact() || !state.profile) return;
    if (state.step !== "configure" || !state.previewOpen || !renderJsonCode || !previewMod) return;
    const body = refs.body.querySelector("#pw-preview-body");
    if (!body) return;
    const f = {
      ...(state.profile.fields || {}),
      ...(state.procOpts || {}),
      host_url: state.details.host_url,
      username: state.details.username,
      pin: state.details.pin,
    };
    renderJsonCode(body, previewMod.buildV2PayloadPreview(f, { siteId: app.summary && app.summary()?.site_id, wgProfiles: state.wgProfiles || [] }));
  }

  /* ---------- step 2: configure (full-form paths) ---------- */

  // Post-render required-error insertion into the shared renderer's
  // [data-field] wrappers — same pattern profile-editor.js uses.
  function markFieldError(key, msg) {
    const wrap = refs.body.querySelector(`[data-field="${key}"]`);
    if (!wrap || wrap.classList.contains("field--error")) return;
    wrap.classList.add("field--error");
    wrap.insertAdjacentHTML("beforeend", `<div class="field__error">${esc(msg)}</div>`);
  }

  // Accordion sections, each rendered by the shared profile-fields.js
  // renderer filtered to its scope. Connection intentionally includes the
  // process-scope account fields (username/password/pin/deauth) — this flow
  // IS the provisioning action, so they're first-class here even though the
  // profile editor no longer shows them. Template-scope timing fields stay
  // under "Timing & Security"; the remaining process fields (provisioning
  // window, scramble, Wi-Fi join) get their own "Provisioning options"
  // section. QR/BLE delivery options live on the Deploy step.
  function formSectionDefs() {
    const F = fieldsMod;
    const shared = (sectionId, extra = {}) =>
      F.renderSectionHtml(sectionId, state.form, { esc, heading: false, wgProfiles: state.wgProfiles || [], ...extra });
    const siteNote = () => {
      const summary = app.summary && app.summary();
      const line = summary && summary.site_id
        ? `Site binding is automatic — site ${summary.site_id}`
        : "Site binding is automatic — the server applies the site ID at provision time";
      return `<div class="muted" style="font-size:12px; margin:2px 0 14px;">${esc(line)}.</div>`;
    };
    return [
      { id: "connection", label: "Connection", render: () => shared("connection") },
      { id: "appui", label: "App UI", render: () => shared("appui", { fields: F.LIVE_KEYS }) },
      { id: "access", label: "Access Control", render: () => shared("access", { fields: F.LIVE_KEYS }) },
      { id: "pushvpn", label: "Push & VPN", render: () => shared("pushvpn", { fields: F.LIVE_KEYS }) + siteNote() },
      { id: "timing", label: "Timing & Security", render: () => shared("timing", { fields: TEMPLATE_TIMING_SET }) },
      {
        id: "process",
        label: "Provisioning options",
        render: () => shared("timing", { fields: FORM_PROCESS_SET }) + shared("wifi", { fields: FORM_PROCESS_SET }),
      },
    ];
  }

  // Re-render one section's body from state (used for gating changes and the
  // lazy WireGuard-profile load) so sibling sections keep their DOM untouched.
  function rerenderFormSection(id) {
    if (!state || isCompact() || state.step !== "configure" || !fieldsMod || !state.form) return;
    const def = formSectionDefs().find((s) => s.id === id);
    const body = refs.body.querySelector(`[data-section-body="${id}"]`);
    if (def && body) body.innerHTML = def.render();
  }

  function renderFullConfigure() {
    const sections = formSectionDefs().map(({ id, label, render: renderFn }) => {
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
    const nameFieldHtml = state.entry === "new" ? `
      <div class="field" data-field="profileName">
        <label>Profile name *</label>
        <input class="input" data-field="profileName" value="${esc(state.profileName)}" placeholder="e.g. Guest tablet">
        <div class="field__help">Saved as a provisioning profile when you deploy.</div>
      </div>` : "";
    return `
      <div style="margin:0 0 14px;">${originChipHtml()}</div>
      ${state.configError ? `<div class="errbar">${esc(state.configError)}</div>` : ""}
      ${nameFieldHtml}
      <div id="pw-form-sections">${sections}</div>
      ${stepFooter("Continue", "to-deploy")}`;
  }

  function renderConfigureStep() {
    return isCompact() ? renderCompactConfigure() : renderFullConfigure();
  }

  function toDeploy() {
    state.configError = "";
    if (isCompact()) {
      if (!trim(state.details.host_url) || !trim(state.details.username)) {
        state.configError = "Host URL and Username are required.";
        render();
        return;
      }
    } else {
      const host = trim(state.form.host_url);
      const user = trim(state.form.username);
      const needName = state.entry === "new" && !trim(state.profileName);
      if (!host || !user || needName) {
        const missing = [];
        if (needName) missing.push("Profile name");
        if (!host) missing.push("Host URL");
        if (!user) missing.push("Username");
        state.configError = missing.join(", ") + (missing.length === 1 ? " is" : " are") + " required.";
        if (!host || !user) state.formOpen.connection = true;
        render();
        if (needName) markFieldError("profileName", "Name this profile — it's created when you deploy.");
        if (!host) markFieldError("host_url", "Required.");
        if (!user) markFieldError("username", "Required.");
        return;
      }
    }
    if (state.method === "ble" && !bleAvailable()) state.method = "qr";
    state.deployError = "";
    gotoStep("deploy");
  }

  /* ---------- step 3: deploy ---------- */

  function optionCard({ method, title, desc, disabled, disabledTitle, active }) {
    return `
      <button class="option-card" data-act="method" data-method="${esc(method)}"
        style="${active ? "border-color:var(--casa-primary); background:color-mix(in srgb, var(--casa-primary) 6%, transparent);" : ""}"
        ${disabled ? `disabled title="${esc(disabledTitle || "")}"` : ""}>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          <span class="option-card__desc" style="display:block;">${esc(desc)}</span>
        </span>
        <ha-icon class="chevron" icon="${active ? "mdi:check-circle" : "mdi:chevron-right"}"
          style="${active ? "color:var(--casa-primary);" : ""}"></ha-icon>
      </button>`;
  }

  function bleChipsHtml() {
    if (!state.bleTargets.length) {
      return `<span class="muted" style="font-size:12px;">No targets added yet — at least one is required.</span>`;
    }
    return state.bleTargets
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

  function bleTargetsField() {
    const services = Object.keys((app.hass() && app.hass().services && app.hass().services.esphome) || {}).map(
      (s) => "esphome." + s
    );
    return `
      <div class="field">
        <label>Beacon targets *</label>
        <div class="field-row">
          <input class="input" id="pw-ble-input" list="pw-ble-list" placeholder="esphome.provision_beacon" autocomplete="off">
          <datalist id="pw-ble-list">
            ${services.map((s) => `<option value="${esc(s)}"></option>`).join("")}
          </datalist>
          <button class="btn btn--outlined" data-act="add-target" style="flex:none;">Add</button>
        </div>
        <div id="pw-ble-chips" style="display:flex; flex-wrap:wrap; gap:6px; margin-top:8px;">${bleChipsHtml()}</div>
        <div class="field__help">ESPHome services that broadcast the provisioning beacon.</div>
      </div>`;
  }

  function qrOptionsFields() {
    return `
      <label class="toggle">
        <input type="checkbox" data-field="deleteQr" ${state.deleteQr ? "checked" : ""}>
        Delete QR image after the entry window
      </label>
      <div class="field">
        <label>QR filename</label>
        <input class="input" data-field="qrFilename" value="${esc(state.qrFilename)}" placeholder="Optional — e.g. casa_qr.png">
      </div>`;
  }

  function methodOptionsHtml() {
    if (state.method === "ble") return bleTargetsField();
    if (state.method === "qr") {
      return `
        <button class="btn btn--text" data-act="toggle-qr-options" style="margin:2px 0 8px; padding-left:0;">
          <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${state.qrOptionsOpen ? "180deg" : "0deg"});"></ha-icon>
          QR options
        </button>
        <div ${state.qrOptionsOpen ? "" : "hidden"}>
          ${qrOptionsFields()}
        </div>`;
    }
    return "";
  }

  function renderDeployStep() {
    const src = isCompact() ? state.details : state.form;
    return `
      <div class="card" style="margin:0 0 16px;">
        <div class="card__body" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <ha-icon icon="mdi:account-arrow-right-outline" style="color:var(--casa-text-2); flex:none;"></ha-icon>
          <span style="font-size:14px;"><strong>${esc(trim(src.username))}</strong> <span class="muted">@ ${esc(trim(src.host_url))}</span></span>
          <span class="spacer"></span>
          ${originChipHtml()}
        </div>
      </div>
      ${state.deployError ? `<div class="errbar">${esc(state.deployError)}</div>` : ""}
      <h4 style="margin:0 0 10px; font-size:14px; font-weight:600;">Choose a delivery method</h4>
      ${optionCard({
        method: "qr",
        title: "Guided provisioning",
        desc: "QR code plus setup links — recommended",
        active: state.method === "qr",
      })}
      ${optionCard({
        method: "deep_link",
        title: "Deep link only",
        desc: "Send a setup link; no QR image is written",
        active: state.method === "deep_link",
      })}
      ${optionCard({
        method: "ble",
        title: "BLE beacon",
        desc: "Broadcast via ESPHome provisioning beacons",
        disabled: !bleAvailable(),
        disabledTitle: "No ESPHome services found — set up an ESPHome provisioning beacon first",
        active: state.method === "ble",
      })}
      ${methodOptionsHtml()}
      ${stepFooter(METHOD_LABELS[state.method] || "Generate", "deploy")}`;
  }

  function addBleTarget() {
    const input = refs.body.querySelector("#pw-ble-input");
    if (!input) return;
    let value = input.value.trim();
    if (!value) return;
    if (!value.includes(".")) value = "esphome." + value;
    if (!state.bleTargets.includes(value)) state.bleTargets.push(value);
    input.value = "";
    const chips = refs.body.querySelector("#pw-ble-chips");
    if (chips) chips.innerHTML = bleChipsHtml();
    input.focus();
  }

  /* ---------- deploy (save-then-provision) ---------- */

  async function submitProvision(data) {
    state.deployError = "";
    state.busy = true;
    render();
    const token = mountToken;
    try {
      const res = await api.provision(data);
      if (token !== mountToken || !state) return;
      const resp = unwrap(res);
      state.busy = false;
      if (resp && resp.error) {
        // User-level failure — server reports these in-band, not as a throw.
        state.deployError = String(resp.error);
        render();
        return;
      }
      state.result = resp;
      state.step = "result";
      render();
    } catch (err) {
      if (token !== mountToken || !state) return;
      state.busy = false;
      state.deployError = (err && err.message) || String(err);
      render();
    }
  }

  async function deploy() {
    if (state.method === "ble" && !state.bleTargets.length) {
      state.deployError = "Add at least one beacon target.";
      render();
      return;
    }

    let data;
    if (isCompact()) {
      data = {
        method: state.method,
        host_url: trim(state.details.host_url),
        username: trim(state.details.username),
      };
      const pin = trim(state.details.pin);
      if (pin) data.pin = pin;
      if (state.profile && state.profile.id) data.profile = state.profile.id;
      // Process settings from the Provisioning-options panel ride service_data
      // explicitly (bools/numbers verbatim — an explicit false/0 wins over a
      // legacy profile value server-side; strings only when non-empty, since
      // get_field treats "" as unset and would fall back anyway).
      if (fieldsMod && state.procOpts) {
        for (const [key, raw] of Object.entries(state.procOpts)) {
          const def = fieldsMod.DEFAULTS[key];
          if (typeof def === "boolean") data[key] = !!raw;
          else if (typeof def === "number") {
            const n = parseInt(raw, 10);
            data[key] = Number.isFinite(n) ? n : def;
          } else {
            const s = trim(raw);
            if (s) data[key] = s;
          }
        }
      }
    } else {
      // Coerce by the defaults' types: booleans as booleans, numbers via
      // parseInt (falling back to the default), strings trimmed. The server's
      // get_field treats "" as unset, so blank optional keys are harmless.
      const fields = {};
      for (const [key, def] of Object.entries(fieldsMod.DEFAULTS)) {
        const raw = state.form[key];
        if (typeof def === "boolean") fields[key] = !!raw;
        else if (typeof def === "number") {
          const n = parseInt(raw, 10);
          fields[key] = Number.isFinite(n) ? n : def;
        } else fields[key] = trim(raw);
      }

      if (state.entry === "new") {
        // Save-then-provision: the profile is created here, at deploy time.
        // Flat body shape — { name, ...fields } — matches the server view;
        // only profile-scope keys are saved (process inputs are for this
        // generation only). A retry after a failed provision carries the id
        // from the first save, so it updates instead of duplicating.
        state.deployError = "";
        state.busy = true;
        render();
        const token = mountToken;
        try {
          const profileFields = {};
          for (const key of fieldsMod.PROFILE_KEYS) profileFields[key] = fields[key];
          const body = { name: trim(state.profileName), ...profileFields };
          if (state.savedProfileId) body.id = state.savedProfileId;
          const res = await api.saveProvisionProfile(body);
          if (token !== mountToken || !state) return;
          if (!state.savedProfileId) {
            if (res && res.id) state.savedProfileId = res.id;
            ui.toast(`Profile '${trim(state.profileName)}' created.`);
          }
        } catch (err) {
          if (token !== mountToken || !state) return;
          state.busy = false;
          state.deployError = "Failed to save profile: " + ((err && err.message) || String(err));
          render();
          return;
        }
      }

      data = { method: state.method, ...fields };
      // Lineage only — explicit fields already win server-side per-key
      // (_provision_internal's get_field), so this doesn't change resolved
      // values, it just correlates the device back to its originating profile.
      if (state.entry === "new" && state.savedProfileId) data.profile = state.savedProfileId;
      else if (state.overrideFields && state.profile && state.profile.id) data.profile = state.profile.id;
    }

    if (state.method === "ble") data.esphome_service = state.bleTargets.slice();
    if (state.method === "qr") {
      data.delete_qr_after_window = !!state.deleteQr;
      const fn = trim(state.qrFilename);
      if (fn) data.qr_filename = fn;
    }

    await submitProvision(data);
  }

  /* ---------- step 4: result ---------- */

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
    const submitted = state.bleTargets.length ? state.bleTargets : r.successful_targets || [];
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
    const pin = isCompact() ? state.details.pin : trim(state.form && state.form.pin);
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

  function renderResultStep() {
    const r = state.result || {};
    let content;
    if (r.method === "ble") content = renderBleResult(r);
    else if (r.method === "deep_link") content = renderDeepLinkResult(r);
    else content = renderQrResult(r);
    return `
      <h3 style="margin:0 0 16px; font-size:16px; font-weight:600;">${esc(RESULT_TITLES[r.method] || "Provisioning result")}</h3>
      ${content}
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        <button class="btn btn--outlined" data-act="another">Provision another</button>
        <button class="btn btn--primary" data-act="done">Done</button>
      </div>`;
  }

  /* ---------- render + events ---------- */

  function render() {
    if (!refs || !state) return;
    renderTabs();
    if (state.step === "profile") refs.body.innerHTML = renderProfileStep();
    else if (state.step === "configure") refs.body.innerHTML = renderConfigureStep();
    else if (state.step === "deploy") refs.body.innerHTML = renderDeployStep();
    else refs.body.innerHTML = renderResultStep();
    for (const btn of refs.body.querySelectorAll("[data-copy]")) {
      ui.bindCopyButton(btn, () => btn.dataset.copy);
    }
    // Shared-renderer fields (data-key inputs) get their delegated handling
    // from bindFieldEvents on their container; view-private inputs
    // (data-field) keep the body listeners below. Old listeners die with the
    // replaced DOM, so re-binding per render leaks nothing.
    if (state.step === "configure" && fieldsMod) {
      const formContainer = refs.body.querySelector("#pw-form-sections");
      if (formContainer && state.form) {
        fieldsMod.bindFieldEvents(formContainer, {
          values: state.form,
          onSectionRerender: (key) => rerenderFormSection(KEY_SECTION[key]),
        });
      }
      const procContainer = refs.body.querySelector("#pw-proc-fields");
      if (procContainer && state.procOpts) {
        fieldsMod.bindFieldEvents(procContainer, { values: state.procOpts });
      }
      if (!isCompact() && state.formOpen.pushvpn) ensureWgProfiles();
      if (isCompact() && state.previewOpen) {
        ensureWgProfiles();
        updatePreview();
      }
    }
  }

  function onBodyClick(e) {
    const el = e.target.closest("[data-act]");
    if (el && !el.disabled && refs.body.contains(el)) {
      switch (el.dataset.act) {
        case "goto-step":
          // Backward only — forward tabs are disabled in the markup, but
          // guard anyway so a stray click can never skip validation.
          if (stepIndex(el.dataset.step) < stepIndex(state.step) && state.step !== "result") gotoStep(el.dataset.step);
          return;
        case "back":
          goBack();
          return;
        case "entry-new":
          selectEntry("new");
          return;
        case "entry-oneoff":
          selectEntry("oneoff");
          return;
        case "retry-profiles":
          loadProfiles();
          return;
        case "clear-search": {
          state.search = "";
          const input = refs.body.querySelector("#pw-search");
          if (input) input.value = "";
          rerenderProfileTable();
          return;
        }
        case "select-profile": {
          const p = (state.profiles || []).find((x) => x && x.id === el.dataset.id);
          if (p) selectProfile(p);
          return;
        }
        case "edit-profile":
          app.navigate("/profiles/" + encodeURIComponent(el.dataset.id));
          return;
        case "to-deploy":
          toDeploy();
          return;
        case "method":
          state.method = el.dataset.method;
          render();
          return;
        case "toggle-qr-options":
          state.qrOptionsOpen = !state.qrOptionsOpen;
          render();
          return;
        case "toggle-preview":
          state.previewOpen = !state.previewOpen;
          render();
          return;
        case "toggle-proc-options":
          state.procOptionsOpen = !state.procOptionsOpen;
          render();
          return;
        case "override-fields":
          overrideFieldsFromProfile();
          return;
        case "toggle-section": {
          // Flip visibility in place — no re-render, so in-progress edits and
          // focus elsewhere in the form are untouched.
          const id = el.dataset.section;
          state.formOpen[id] = !state.formOpen[id];
          const body = refs.body.querySelector(`[data-section-body="${id}"]`);
          if (body) body.hidden = !state.formOpen[id];
          const icon = el.querySelector("ha-icon");
          if (icon) icon.style.transform = `rotate(${state.formOpen[id] ? "180deg" : "0deg"})`;
          if (id === "pushvpn" && state.formOpen.pushvpn) ensureWgProfiles();
          return;
        }
        case "add-target":
          addBleTarget();
          return;
        case "remove-target": {
          state.bleTargets = state.bleTargets.filter((t) => t !== el.dataset.target);
          const chips = refs.body.querySelector("#pw-ble-chips");
          if (chips) chips.innerHTML = bleChipsHtml();
          return;
        }
        case "deploy":
          deploy();
          return;
        case "another": {
          // Fresh run: rebuild state locally (works even if the router skips
          // a same-path remount) and strip any preset segment from the URL.
          state = freshState();
          wgRequested = false;
          loadProfiles();
          render();
          if (mountedWithPresetPath) app.navigate("/provision", { replace: true });
          return;
        }
        case "done":
          app.refresh();
          app.navigate("/");
          return;
      }
      return;
    }
    // Row click (no data-act target) selects the profile.
    const row = e.target.closest("tr[data-id]");
    if (row && state.step === "profile") {
      const p = (state.profiles || []).find((x) => x && x.id === row.dataset.id);
      if (p) selectProfile(p);
    }
  }

  // View-private inputs only (data-field). Shared-renderer fields (data-key,
  // including the color picker and reveal-password buttons) are handled by
  // fieldsMod.bindFieldEvents bound per-container in render().
  function onBodyInput(e) {
    const t = e.target;
    if (t.id === "pw-search") {
      state.search = t.value;
      rerenderProfileTable();
      return;
    }
    const field = t.dataset && t.dataset.field;
    if (!field) return;
    if (field === "profileName") {
      state.profileName = t.value;
      return;
    }
    if (field === "deleteQr") {
      state.deleteQr = !!t.checked;
      return;
    }
    if (field === "qrFilename") {
      state.qrFilename = t.value;
      return;
    }
    state.details[field] = t.type === "checkbox" ? t.checked : t.value;
    if (isCompact() && state.previewOpen && (field === "host_url" || field === "username" || field === "pin")) {
      updatePreview();
    }
  }

  function onBodyKeydown(e) {
    if (e.key === "Enter" && e.target.id === "pw-ble-input") {
      e.preventDefault();
      addBleTarget();
    }
  }

  /* ---------- scenario picker ---------- */

  // Shown on plain /provision visits only — preset paths (/provision/user/…,
  // /provision/profile/…) already declare their intent. Dismissing lands on
  // the classic wizard, which is fully rendered underneath.
  function openScenarioModal() {
    const card = ({ act, icon, title, desc }) => `
      <button class="option-card" data-scenario="${esc(act)}" style="width:100%;">
        <ha-icon icon="${esc(icon)}" style="color:var(--casa-text-2);"></ha-icon>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          <span class="option-card__desc" style="display:block;">${esc(desc)}</span>
        </span>
        <ha-icon class="chevron" icon="mdi:chevron-right"></ha-icon>
      </button>`;
    const body = document.createElement("div");
    body.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:10px;">
        ${card({
          act: "guided",
          icon: "mdi:account-plus-outline",
          title: "New device with its own account",
          desc: "Guided — name the device, we create the account, pick a profile, get a link or QR",
        })}
        ${card({
          act: "reprovision",
          icon: "mdi:cellphone-arrow-down",
          title: "Re-provision an existing device",
          desc: "Use the classic wizard with an existing guest account",
        })}
        ${card({
          act: "advanced",
          icon: "mdi:tune",
          title: "Advanced / manual setup",
          desc: "Full control — profiles, one-off config, BLE beacons",
        })}
      </div>`;
    body.addEventListener("click", (e) => {
      const el = e.target.closest("[data-scenario]");
      if (!el) return;
      const scenario = el.dataset.scenario;
      scenarioModal?.close();
      scenarioModal = null;
      if (scenario === "guided") {
        app.navigate("/provision/guided");
      } else if (scenario === "reprovision") {
        ui.toast("Tip: you can also re-provision straight from a device's menu in the device list.");
      }
      // "reprovision" and "advanced" both land on the classic wizard below.
    });
    scenarioModal = ui.openModal({
      title: "How do you want to provision?",
      bodyEl: body,
      dismissable: true,
    });
  }

  /* ---------- unload guard ---------- */

  function onBeforeUnload(e) {
    if (!dirty()) return;
    e.preventDefault();
    e.returnValue = "";
  }

  /* ---------- view ---------- */

  return {
    id: "provision",
    header: () => ({ title: "Provision device", back: "/" }),
    polling: "paused",

    async mount(el, params) {
      const token = ++mountToken;
      state = freshState();
      state.presetUsername = (params && params.username) || "";
      const presetProfileId = (params && params.profileId) || null;
      mountedWithPresetPath = !!(state.presetUsername || presetProfileId);
      wgRequested = false;

      el.innerHTML = `
        <div class="page">
          <div class="tabs tabs--steps" id="pw-tabs"></div>
          <div id="pw-body"></div>
        </div>`;
      refs = {
        tabs: el.querySelector("#pw-tabs"),
        body: el.querySelector("#pw-body"),
      };
      refs.body.addEventListener("click", onBodyClick);
      refs.body.addEventListener("input", onBodyInput);
      refs.body.addEventListener("keydown", onBodyKeydown);
      window.addEventListener("beforeunload", onBeforeUnload);
      render(); // loading state while modules + profiles land

      try {
        const [fields, preview, shell] = await Promise.all([
          fieldsMod || app.loadModule("views/profile-fields.js"),
          previewMod || app.loadModule("payload-preview.js"),
          renderJsonCode ? null : app.loadModule("editor-shell.js"),
        ]);
        if (token !== mountToken) return;
        fieldsMod = fields;
        previewMod = preview;
        if (shell) renderJsonCode = shell.renderJsonCode;
      } catch (err) {
        if (token !== mountToken) return;
        refs.body.innerHTML = `<div class="errbar">Failed to load: ${esc(String((err && err.message) || err))}</div>`;
        return;
      }

      await loadProfiles();
      if (token !== mountToken || !state) return;
      if (presetProfileId) {
        const p = (state.profiles || []).find((x) => x && x.id === presetProfileId);
        if (p) {
          selectProfile(p);
          return;
        }
        ui.toast("That provisioning profile no longer exists.", { error: true });
      }
      render();
      if (!mountedWithPresetPath) openScenarioModal();
    },

    unmount() {
      mountToken++;
      window.removeEventListener("beforeunload", onBeforeUnload);
      scenarioModal?.close();
      scenarioModal = null;
      refs = null;
      state = null;
    },

    // ui.showConfirm has no cancel callback, so build the dialog with openModal
    // and resolve false on any dismissal (X, overlay click, Escape) by watching
    // for the overlay leaving the DOM.
    confirmLeave() {
      if (!dirty()) return true;
      return new Promise((resolve) => {
        let settled = false;
        let observer = null;
        const done = (v) => {
          if (settled) return;
          settled = true;
          observer?.disconnect();
          resolve(v);
        };
        const modal = ui.openModal({
          title: "Leave provisioning?",
          bodyHtml:
            '<p style="margin:0; font-size:14px; line-height:1.5;">This device hasn\'t been provisioned yet. Discard this setup and leave?</p>',
          buttons: [
            { label: "Keep going", variant: "text", onClick: () => done(false) },
            { label: "Discard", variant: "danger", onClick: () => done(true) },
          ],
        });
        observer = new MutationObserver(() => {
          if (!modal.el.isConnected) done(false);
        });
        observer.observe(modal.el.parentNode, { childList: true });
      });
    },
  };
}
