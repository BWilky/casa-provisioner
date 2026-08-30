// Casa admin panel — guided "new device + individual account" flow at
// /provision/guided, reached from the scenario popup on /provision. Unlike
// the classic wizard (provision.js), this flow CREATES the guest account:
// Device (name → slugged username + auto password) → Template (pick a saved
// template and tweak it) → Deliver (setup link by default, QR optional, BLE
// under Advanced) → Done (one-time credentials + links/QR).
//
// Deploy order is create_user → provision, both keyed off state so a retry
// after a failed provision never duplicates the account (createdUser guard),
// never re-saves a forked template (savedTemplateId guard) and never rotates
// the password (it is sent explicitly). The typed device name rides
// casa.provision as device_alias and is applied server-side when the device
// self-registers.

const STEPS = [
  { id: "device", label: "Device" },
  { id: "profile", label: "Template" },
  { id: "deliver", label: "Deliver" },
  { id: "result", label: "Done" },
];
const stepIndex = (id) => STEPS.findIndex((s) => s.id === id);

// Gating fields → the accordion section whose body re-renders when they flip
// (same map as provision.js; timing is not an accordion section here).
const KEY_SECTION = {
  theme_color_mode: "appui",
  allow_all_pages: "access",
  wireguard_profile_id: "pushvpn",
};

export function createView(app) {
  const { api, ui } = app;
  const esc = ui.esc;
  const unwrap = (res) => api.constructor.response(res); // CasaApi.response

  const trim = (v) => String(v ?? "").trim();
  const bleAvailable = () => !!(app.hass() && app.hass().services && app.hass().services.esphome);

  /* ---------- lazily loaded siblings (never static imports) ---------- */
  let fieldsMod = null; // views/profile-fields.js
  let previewMod = null; // payload-preview.js
  let utilsMod = null; // views/username-utils.js \u2014 slugify/USERNAME_RE, needed by step 1

  /* ---------- per-mount state ---------- */
  let mountToken = 0;
  let state = null;
  let refs = null; // { tabs, body }
  let wgRequested = false;
  let availTimer = 0;

  function freshState() {
    return {
      step: "device",
      // step 1 — device + account
      deviceName: "",
      username: "",
      usernameEdited: false, // admin typed in the username field; stop auto-slugging
      availability: null, // null | {checking:true} | {available, username_conflict, name_conflict, for}
      customPasswordOpen: false,
      password: "",
      deviceError: "",
      createdUser: null, // {name, username, password, user_id} once create_user succeeds
      // step 2 — template
      profiles: null, // null = loading (saved templates; API key stays "profiles")
      profilesError: null,
      chosen: false, // a starting point (template or defaults) was picked
      profile: null, // selected saved-template object, null = defaults
      templateSetKeys: null, // Set of fields the selected template sets
      form: null, // fieldsMod.DEFAULTS shape
      baseline: null, // collectFields snapshot at selection, for divergence diff
      formOpen: { connection: false, appui: false, access: false, pushvpn: false },
      profileError: "",
      forkChoice: null, // null | "shared" | "oneoff" — remembered across retries
      forkName: "",
      savedProfileId: null, // set once the forked template saves (retry guard)
      // step 3 — deliver
      linkChecked: true,
      qrChecked: false,
      bleChecked: false,
      bleTargets: [],
      deleteQr: true,
      qrFilename: "",
      advancedOpen: false,
      pin: "",
      scramble: true,
      scrambleIn: 0,
      timeoutMinutes: 5,
      deployError: "",
      busy: false,
      // step 4
      result: null,
      wgProfiles: null,
    };
  }

  const dirty = () =>
    !!(state && state.step !== "result" && (trim(state.deviceName) || trim(state.username) || state.chosen || state.createdUser));

  /* ---------- data ---------- */

  async function loadProfiles() {
    state.profiles = null;
    state.profilesError = null;
    if (state.step === "profile") render();
    const token = mountToken;
    try {
      const res = await api.getProvisionTemplates();
      if (token !== mountToken || !state) return;
      state.profiles = (res && res.profiles) || [];
    } catch (err) {
      if (token !== mountToken || !state) return;
      state.profiles = [];
      state.profilesError = ui.errMsg(err);
    }
    // A site with no templates shouldn't hit a dead "pick one" screen.
    if (!state.profiles.length && !state.chosen && fieldsMod) selectStartingPoint(null, { rerender: false });
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
      });
  }

  /* ---------- username availability (advisory — create_user is authoritative) ---------- */

  function availabilityHtml() {
    if (state.createdUser || !utilsMod) return "";
    return utilsMod.availabilityHintHtml(state.availability, trim(state.username), esc);
  }

  function renderAvailability() {
    const line = refs && refs.body.querySelector("#pg-availability");
    if (line) line.innerHTML = availabilityHtml();
  }

  function scheduleAvailability() {
    clearTimeout(availTimer);
    const username = trim(state.username);
    if (!username || state.createdUser || !utilsMod || !utilsMod.USERNAME_RE.test(username)) {
      state.availability = null;
      renderAvailability();
      return;
    }
    state.availability = { checking: true };
    renderAvailability();
    const token = mountToken;
    const name = trim(state.deviceName);
    availTimer = setTimeout(async () => {
      try {
        const res = await api.checkUsername(username, name);
        if (token !== mountToken || !state || trim(state.username) !== username) return;
        state.availability = { ...res, for: username };
      } catch {
        if (token !== mountToken || !state) return;
        state.availability = null; // advisory only — deploy revalidates
      }
      renderAvailability();
    }, 350);
  }

  /* ---------- step tabs / shared chrome ---------- */

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

  function stepFooter(primaryLabel, primaryAct, { back = true } = {}) {
    return `
      <div style="display:flex; justify-content:space-between; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        ${back ? `<button class="btn btn--text" data-act="back">Back</button>` : `<span></span>`}
        <button class="btn btn--primary" data-act="${esc(primaryAct)}" ${state.busy ? "disabled" : ""}>
          ${state.busy ? "Working…" : esc(primaryLabel)}
        </button>
      </div>`;
  }

  function markFieldError(key, msg) {
    const wrap = refs.body.querySelector(`[data-field="${key}"]`);
    if (!wrap || wrap.classList.contains("field--error")) return;
    wrap.classList.add("field--error");
    wrap.insertAdjacentHTML("beforeend", `<div class="field__error">${esc(msg)}</div>`);
  }

  /* ---------- step 1: device + account ---------- */

  function renderDeviceStep() {
    if (state.createdUser) {
      const u = state.createdUser;
      return `
        <div style="display:flex; gap:8px; align-items:center; margin:0 0 14px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
          <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
          <span>Account <strong class="mono">${esc(u.username)}</strong> was already created for this run — continue to retry provisioning, or leave to keep/remove it.</span>
        </div>
        <div class="field">
          <label>Device name</label>
          <input class="input" value="${esc(state.deviceName)}" disabled>
        </div>
        <div class="field">
          <label>Username</label>
          <input class="input mono" value="${esc(u.username)}" disabled>
        </div>
        ${stepFooter("Continue", "to-profile", { back: false })}`;
    }
    return `
      ${state.deviceError ? `<div class="errbar">${esc(state.deviceError)}</div>` : ""}
      <div class="muted" style="font-size:13px; margin:0 0 14px;">
        Set up a new device with its own guest account — name it, and we'll take care of the rest.
      </div>
      <div class="field" data-field="deviceName">
        <label>Device name *</label>
        <input class="input" data-field="deviceName" value="${esc(state.deviceName)}" maxlength="60"
          placeholder="e.g. Living Room Tablet" autocomplete="off">
        <div class="field__help">Shown in the device list — applied automatically when the device first connects.</div>
      </div>
      <div class="field" data-field="username">
        <label>Username *</label>
        <input class="input mono" data-field="username" value="${esc(state.username)}"
          placeholder="e.g. living-room-tablet" autocapitalize="none" autocomplete="off" spellcheck="false">
        <div id="pg-availability" style="min-height:20px; margin-top:6px;">${availabilityHtml()}</div>
        <div class="field__help">The guest account this device signs in with — suggested from the name, edit if you like.</div>
      </div>
      ${state.customPasswordOpen ? `
        <div class="field" data-field="password">
          <label>Password</label>
          <input class="input" data-field="password" type="password" value="${esc(state.password)}" placeholder="••••••••" autocomplete="new-password">
          <div class="field__help">Leave blank to auto-generate a secure password.</div>
        </div>` : `
        <div style="display:flex; gap:8px; align-items:center; margin:0 0 6px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
          <ha-icon icon="mdi:shield-key-outline" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
          <span style="flex:1;">A secure 12-character password will be generated — you'll see it once at the end.</span>
          <button class="btn btn--text" data-act="custom-password" style="flex:none; height:28px;">Set a custom password…</button>
        </div>`}
      ${stepFooter("Continue", "to-profile", { back: false })}`;
  }

  function toProfile() {
    state.deviceError = "";
    if (!state.createdUser) {
      const name = trim(state.deviceName);
      const username = trim(state.username);
      const missing = [];
      if (!name) missing.push("Device name");
      if (!username) missing.push("Username");
      if (missing.length) {
        state.deviceError = missing.join(" and ") + (missing.length === 1 ? " is" : " are") + " required.";
        render();
        missing.forEach((m) => markFieldError(m === "Device name" ? "deviceName" : "username", "Required."));
        return;
      }
      if (utilsMod && !utilsMod.USERNAME_RE.test(username)) {
        state.deviceError = "Username can only contain lowercase letters, numbers and dashes.";
        render();
        markFieldError("username", "Lowercase letters, numbers and dashes only.");
        return;
      }
      const a = state.availability;
      if (a && !a.checking && a.for === username && !a.available) {
        state.deviceError = a.username_conflict
          ? `The username '${username}' is already in use.`
          : `A user named '${name}' already exists.`;
        render();
        markFieldError(a.username_conflict ? "username" : "deviceName", "Already in use.");
        return;
      }
    }
    gotoStep("profile");
  }

  /* ---------- step 2: template ---------- */

  function selectStartingPoint(profile, { rerender = true } = {}) {
    state.chosen = true;
    state.profile = profile || null;
    const f = (profile && profile.fields) || {};
    // Templates are sparse — set fields overlay the defaults, and
    // templateSetKeys drives the "from template" / "review" badges.
    state.form = { ...fieldsMod.DEFAULTS, ...f };
    state.templateSetKeys = profile
      ? new Set(Object.keys(f).filter((k) => fieldsMod.PROFILE_KEYS.has(k)))
      : null;
    if (!trim(state.form.host_url)) state.form.host_url = window.location.origin;
    state.baseline = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
    state.timeoutMinutes = fieldsMod.DEFAULTS.timeout_minutes;
    // A new selection is a new baseline — any earlier fork decision is stale.
    state.forkChoice = null;
    state.savedProfileId = null;
    state.profileError = "";
    if (rerender) render();
  }

  function startingPointCard({ act, id, icon, title, desc, chips, active }) {
    return `
      <button class="option-card" data-act="${esc(act)}" ${id ? `data-id="${esc(id)}"` : ""}
        style="width:100%; ${active ? "border-color:var(--casa-primary); background:color-mix(in srgb, var(--casa-primary) 6%, transparent);" : ""}">
        <ha-icon icon="${esc(icon)}" style="color:${active ? "var(--casa-primary)" : "var(--casa-text-2)"};"></ha-icon>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          <span class="option-card__desc" style="display:block;">${esc(desc)}</span>
          ${chips ? `<span style="display:flex; flex-wrap:wrap; gap:4px; margin-top:6px;">${chips}</span>` : ""}
        </span>
        <ha-icon class="chevron" icon="${active ? "mdi:check-circle" : "mdi:chevron-right"}"
          style="${active ? "color:var(--casa-primary);" : ""}"></ha-icon>
      </button>`;
  }

  function profilePickerHtml() {
    if (state.profiles === null) {
      return `<div class="empty-state" style="padding:32px 16px;"><span class="muted">Loading templates…</span></div>`;
    }
    let errHtml = "";
    if (state.profilesError) {
      errHtml = `
        <div class="errbar" style="display:flex; align-items:center; gap:10px;">
          <span style="flex:1;">Failed to load templates: ${esc(state.profilesError)}</span>
          <button class="btn btn--outlined" data-act="retry-profiles" style="height:28px; flex:none;">Retry</button>
        </div>`;
    }
    const cards = state.profiles.map((p) => {
      const chips = (previewMod ? previewMod.profileChips(p) : [])
        .map((c) => `<span class="chip ${esc(c.cls || "chip--neutral")}">${esc(c.label)}</span>`)
        .join("");
      const f = p.fields || {};
      return startingPointCard({
        act: "select-profile",
        id: p.id,
        icon: "mdi:file-cog-outline",
        title: p.name || "(unnamed)",
        desc: trim(f.host_url) || "Saved provision template",
        chips,
        active: !!(state.chosen && state.profile && state.profile.id === p.id),
      });
    });
    cards.push(
      startingPointCard({
        act: "select-defaults",
        icon: "mdi:tune",
        title: "Start from defaults",
        desc: "No template — sensible defaults you can adjust below",
        active: !!(state.chosen && !state.profile),
      })
    );
    return `${errHtml}<div style="display:flex; flex-direction:column; gap:10px;">${cards.join("")}</div>`;
  }

  // Same badge semantics as the classic wizard: template-set fields are
  // "from template", unset PROFILE-scope fields are "review".
  function templateAnnotations() {
    if (!state.profile || !state.templateSetKeys) return null;
    const ann = {};
    for (const key of fieldsMod.PROFILE_KEYS) {
      ann[key] = state.templateSetKeys.has(key) ? "template" : "review";
    }
    return ann;
  }

  function tweakSectionDefs() {
    const F = fieldsMod;
    const annotations = templateAnnotations();
    const shared = (sectionId, extra = {}) =>
      F.renderSectionHtml(sectionId, state.form, { esc, heading: false, wgProfiles: state.wgProfiles || [], annotations, ...extra });
    return [
      { id: "connection", label: "Connection", render: () => shared("connection", { fields: new Set(["host_url"]) }) },
      { id: "appui", label: "App UI", render: () => shared("appui", { fields: F.LIVE_KEYS }) },
      { id: "access", label: "Access Control", render: () => shared("access", { fields: F.LIVE_KEYS }) },
      { id: "pushvpn", label: "Push & VPN", render: () => shared("pushvpn", { fields: F.LIVE_KEYS }) },
    ];
  }

  function rerenderFormSection(id) {
    if (!state || state.step !== "profile" || !fieldsMod || !state.form || !id) return;
    const def = tweakSectionDefs().find((s) => s.id === id);
    const body = refs.body.querySelector(`[data-section-body="${id}"]`);
    if (def && body) body.innerHTML = def.render();
  }

  function tweaksHtml() {
    if (!state.chosen || !state.form) return "";
    const sections = tweakSectionDefs().map(({ id, label, render: renderFn }) => {
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
    const note = state.profile
      ? `Tweak anything below — you'll be asked whether changes become a new template or stay with this device.`
      : `Adjust the device's configuration below.`;
    return `
      <h4 style="margin:18px 0 4px; font-size:14px; font-weight:600;">Settings</h4>
      <div class="muted" style="font-size:12px; margin:0 0 8px;">${esc(note)}</div>
      <div id="pg-form-sections">${sections}</div>`;
  }

  function renderProfileStep() {
    return `
      ${state.profileError ? `<div class="errbar">${esc(state.profileError)}</div>` : ""}
      <h4 style="margin:0 0 10px; font-size:14px; font-weight:600;">Start from a template</h4>
      <div id="pg-picker">${profilePickerHtml()}</div>
      ${tweaksHtml()}
      ${stepFooter("Continue", "to-deliver")}`;
  }

  function toDeliver() {
    state.profileError = "";
    if (!state.chosen || !state.form) {
      state.profileError = "Choose a starting point — a saved template, or the defaults.";
      render();
      return;
    }
    if (!trim(state.form.host_url)) {
      state.profileError = "Host URL is required.";
      state.formOpen.connection = true;
      render();
      markFieldError("host_url", "Required.");
      return;
    }
    state.deployError = "";
    gotoStep("deliver");
  }

  /* ---------- step 3: deliver ---------- */

  function methodCheckbox({ field, checked, title, desc, disabled, disabledTitle }) {
    return `
      <label class="toggle" style="align-items:flex-start; ${disabled ? "opacity:0.55;" : ""}" ${disabled ? `title="${esc(disabledTitle || "")}"` : ""}>
        <input type="checkbox" data-field="${esc(field)}" ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}>
        <span>
          <span style="display:block; font-weight:600;">${esc(title)}</span>
          <span class="muted" style="display:block; font-size:12px;">${esc(desc)}</span>
        </span>
      </label>`;
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
      <div class="field" style="margin-left:28px;">
        <label>Beacon targets *</label>
        <div class="field-row">
          <input class="input" id="pg-ble-input" list="pg-ble-list" placeholder="esphome.provision_beacon" autocomplete="off">
          <datalist id="pg-ble-list">
            ${services.map((s) => `<option value="${esc(s)}"></option>`).join("")}
          </datalist>
          <button class="btn btn--outlined" data-act="add-target" style="flex:none;">Add</button>
        </div>
        <div id="pg-ble-chips" style="display:flex; flex-wrap:wrap; gap:6px; margin-top:8px;">${bleChipsHtml()}</div>
        <div class="field__help">ESPHome services that broadcast the provisioning beacon.</div>
      </div>`;
  }

  function advancedHtml() {
    return `
      <button class="btn btn--text" data-act="toggle-advanced" style="margin:10px 0 4px; padding-left:0;">
        <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${state.advancedOpen ? "180deg" : "0deg"});"></ha-icon>
        Advanced
      </button>
      <div ${state.advancedOpen ? "" : "hidden"}>
        ${methodCheckbox({
          field: "bleChecked",
          checked: state.bleChecked,
          title: "BLE beacon broadcast",
          desc: "Also push the setup payload to ESPHome provisioning beacons",
          disabled: !bleAvailable(),
          disabledTitle: "No ESPHome services found — set up an ESPHome provisioning beacon first",
        })}
        ${state.bleChecked ? bleTargetsField() : ""}
        <div class="field" style="margin-top:10px;">
          <label>PIN</label>
          <input class="input" data-field="pin" value="${esc(state.pin)}" maxlength="6" placeholder="Optional — up to 6 digits" autocomplete="off">
          <div class="field__help">The app prompts for this PIN before provisioning completes.</div>
        </div>
        <label class="toggle">
          <input type="checkbox" data-field="scramble" ${state.scramble ? "checked" : ""}>
          Scramble password after the setup window
        </label>
        <div class="field" style="margin-top:8px;">
          <label>Scramble after (minutes)</label>
          <input class="input" data-field="scrambleIn" type="number" min="0" value="${esc(state.scrambleIn)}">
          <div class="field__help">0 inherits the setup-code window below.</div>
        </div>
      </div>`;
  }

  function renderDeliverStep() {
    const f = state.form || {};
    return `
      <div class="card" style="margin:0 0 16px;">
        <div class="card__body" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          <ha-icon icon="mdi:cellphone-check" style="color:var(--casa-text-2); flex:none;"></ha-icon>
          <span style="font-size:14px;"><strong>${esc(trim(state.deviceName))}</strong>
            <span class="muted">— ${esc(trim(state.username))} @ ${esc(trim(f.host_url))}</span></span>
          <span class="spacer"></span>
          ${state.profile
            ? `<span class="chip chip--app">Template: ${esc(state.profile.name || state.profile.id)}</span>`
            : `<span class="chip chip--neutral">Defaults</span>`}
        </div>
      </div>
      ${state.deployError ? `<div class="errbar">${esc(state.deployError)}</div>` : ""}
      <h4 style="margin:0 0 10px; font-size:14px; font-weight:600;">How should the device get its setup code?</h4>
      <div class="card"><div class="card__body" style="display:flex; flex-direction:column; gap:12px;">
        ${methodCheckbox({
          field: "linkChecked",
          checked: state.linkChecked,
          title: "Setup link",
          desc: "A tappable link you can send to the device — recommended",
        })}
        ${methodCheckbox({
          field: "qrChecked",
          checked: state.qrChecked,
          title: "QR code",
          desc: "Also generate a QR image to scan with the Casa app",
        })}
        ${state.qrChecked ? `
          <div style="margin-left:28px;">
            <label class="toggle">
              <input type="checkbox" data-field="deleteQr" ${state.deleteQr ? "checked" : ""}>
              Delete QR image after the entry window
            </label>
            <div class="field" style="margin-top:8px;">
              <label>QR filename</label>
              <input class="input" data-field="qrFilename" value="${esc(state.qrFilename)}" placeholder="Optional — e.g. casa_qr.png">
            </div>
          </div>` : ""}
      </div></div>
      ${advancedHtml()}
      <h4 style="margin:18px 0 10px; font-size:14px; font-weight:600;">Validity</h4>
      <div class="field">
        <label>Setup code expires in (minutes)</label>
        <input class="input" data-field="timeoutMinutes" type="number" min="0" max="60" value="${esc(state.timeoutMinutes)}">
        <div class="field__help">Minutes before the setup code stops working — 0 means it never expires.</div>
      </div>
      <div class="field">
        <label>Session lifetime (hours)</label>
        <input class="input" data-field="expirationHours" type="number" min="0" max="87600" value="${esc(state.form ? state.form.expiration_hours : "")}">
        <div class="field__help">How long the device stays signed in — 0 means permanent.</div>
      </div>
      ${stepFooter(state.createdUser ? "Retry provisioning" : "Create account & generate", "deploy")}`;
  }

  function addBleTarget() {
    const input = refs.body.querySelector("#pg-ble-input");
    if (!input) return;
    let value = input.value.trim();
    if (!value) return;
    if (!value.includes(".")) value = "esphome." + value;
    if (!state.bleTargets.includes(value)) state.bleTargets.push(value);
    input.value = "";
    const chips = refs.body.querySelector("#pg-ble-chips");
    if (chips) chips.innerHTML = bleChipsHtml();
    input.focus();
  }

  /* ---------- fork prompt (template diverged) ---------- */

  function changedProfileKeys(fields) {
    if (!state.profile || !state.baseline) return [];
    return Object.keys(fields).filter((k) => fields[k] !== state.baseline[k]);
  }

  // Resolves {kind:"shared", name} | {kind:"oneoff"} | null (cancelled).
  function promptFork(changedKeys) {
    return new Promise((resolve) => {
      let settled = false;
      let observer = null;
      const done = (v) => {
        if (settled) return;
        settled = true;
        observer?.disconnect();
        resolve(v);
      };
      const profileName = state.profile.name || state.profile.id;
      const chips = changedKeys
        .map((k) => `<span class="chip chip--neutral">${esc(k.replace(/_/g, " "))}</span>`)
        .join("");
      const body = document.createElement("div");
      body.innerHTML = `
        <p style="margin:0 0 10px; font-size:14px; line-height:1.5;">
          You changed ${changedKeys.length} setting${changedKeys.length === 1 ? "" : "s"} from
          <strong>${esc(profileName)}</strong>. Save them as a new reusable template, or keep them just for this device?
        </p>
        <div style="display:flex; flex-wrap:wrap; gap:4px; margin:0 0 14px;">${chips}</div>
        <div class="field" data-field="forkName">
          <label>New template name</label>
          <input class="input" id="pg-fork-name" value="${esc(profileName + " (copy)")}">
          <div class="field__help">Only used if you save as a new template.</div>
        </div>`;
      const modal = ui.openModal({
        title: "Save your changes?",
        bodyEl: body,
        buttons: [
          { label: "Cancel", variant: "text", onClick: () => done(null) },
          { label: "This device only", variant: "outlined", onClick: () => done({ kind: "oneoff" }) },
          {
            label: "Save as new template",
            variant: "primary",
            onClick: () => {
              const name = body.querySelector("#pg-fork-name").value.trim();
              if (!name) {
                const wrap = body.querySelector('[data-field="forkName"]');
                if (wrap && !wrap.classList.contains("field--error")) {
                  wrap.classList.add("field--error");
                  wrap.insertAdjacentHTML("beforeend", `<div class="field__error">Name the new template.</div>`);
                }
                return false;
              }
              done({ kind: "shared", name });
            },
          },
        ],
      });
      observer = new MutationObserver(() => {
        if (!modal.el.isConnected) done(null);
      });
      observer.observe(modal.el.parentNode, { childList: true });
    });
  }

  /* ---------- deploy: create user → provision ---------- */

  async function deploy() {
    state.deployError = "";
    if (!state.linkChecked && !state.qrChecked && !state.bleChecked) {
      state.deployError = "Pick at least one delivery method.";
      render();
      return;
    }
    if (state.bleChecked && !state.bleTargets.length) {
      state.deployError = "Add at least one beacon target, or untick the BLE broadcast.";
      render();
      return;
    }

    const fields = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
    const changedKeys = changedProfileKeys(fields);

    // 1. Fork decision — asked once; remembered so a retry doesn't re-prompt.
    if (state.profile && changedKeys.length && !state.forkChoice) {
      const choice = await promptFork(changedKeys);
      if (!choice || !state) return;
      state.forkChoice = choice.kind;
      if (choice.kind === "shared") state.forkName = choice.name;
    }

    const token = mountToken;
    state.busy = true;
    render();

    // 2. Save the forked template (skipped on retry via savedProfileId).
    // Sparse: the fork sets the original template's set keys plus whatever
    // the admin changed here — untouched defaults stay unset on the fork.
    if (state.forkChoice === "shared" && !state.savedProfileId) {
      try {
        const forkKeys = new Set([...(state.templateSetKeys || []), ...changedKeys]);
        const forkFields = {};
        for (const key of Object.keys(fields)) {
          if (forkKeys.has(key)) forkFields[key] = fields[key];
        }
        const res = await api.saveProvisionTemplate({ name: state.forkName, fields: forkFields });
        if (token !== mountToken || !state) return;
        if (res && res.id) state.savedProfileId = res.id;
        ui.toast(`Template '${state.forkName}' created.`);
      } catch (err) {
        if (token !== mountToken || !state) return;
        state.busy = false;
        state.deployError = "Failed to save template: " + (ui.errMsg(err));
        render();
        return;
      }
    }

    // 3. Create the guest account (skipped on retry via createdUser).
    if (!state.createdUser) {
      const name = trim(state.deviceName);
      const username = trim(state.username);
      const custom = trim(state.password);
      let resp;
      try {
        const res = await api.createUser({ name, username, password: custom || undefined, localOnly: true });
        if (token !== mountToken || !state) return;
        resp = unwrap(res);
      } catch (err) {
        if (token !== mountToken || !state) return;
        resp = { error: ui.errMsg(err) };
      }
      if (resp && resp.error) {
        // Almost always a name/username collision — send them back to fix it.
        state.busy = false;
        state.step = "device";
        state.deviceError = String(resp.error);
        state.availability = null;
        render();
        return;
      }
      state.createdUser = {
        name,
        username,
        password: (resp && resp.password) || custom,
        user_id: resp && resp.user_id,
      };
    }

    // 4. Provision — password sent explicitly so a retry never rotates it.
    const method = state.qrChecked ? "qr" : state.linkChecked ? "deep_link" : "ble";
    const data = {
      method,
      ...fields,
      username: state.createdUser.username,
      // Exact target: name/credential matching can't confuse accounts whose
      // display name differs from the login username (guided accounts always do).
      user_id: state.createdUser.user_id || undefined,
      password: state.createdUser.password,
      device_alias: trim(state.deviceName),
      timeout_minutes: parseInt(state.timeoutMinutes, 10) || 0,
      password_scramble: !!state.scramble,
      password_scramble_in: parseInt(state.scrambleIn, 10) || 0,
    };
    const pin = trim(state.pin);
    if (pin) data.pin = pin;
    // Lineage: the forked template, or the unchanged original. One-off
    // forks carry no template id — the fields alone describe the device.
    if (state.savedProfileId) data.profile = state.savedProfileId;
    else if (state.profile && state.forkChoice !== "oneoff") data.profile = state.profile.id;
    if (state.bleChecked) data.esphome_service = state.bleTargets.slice();
    if (method === "qr") {
      data.delete_qr_after_window = !!state.deleteQr;
      const fn = trim(state.qrFilename);
      if (fn) data.qr_filename = fn;
    }

    try {
      const res = await api.provision(data);
      if (token !== mountToken || !state) return;
      const resp = unwrap(res);
      state.busy = false;
      if (resp && resp.error) {
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
      state.deployError = ui.errMsg(err);
      render();
    }
  }

  /* ---------- step 4: result ---------- */

  function credRow(label, value) {
    return `
      <div class="field-row" style="margin-bottom:10px;">
        <span style="width:88px; flex:none; font-weight:600; font-size:13px;">${esc(label)}</span>
        <span class="mono" style="flex:1; min-width:0; word-break:break-all;">${esc(value)}</span>
        <button class="btn btn--outlined" style="height:28px; padding:0 10px; font-size:12px;" data-copy="${esc(value)}">Copy</button>
      </div>`;
  }

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

  function renderResultStep() {
    const r = state.result || {};
    const u = state.createdUser || {};
    const okSet = new Set(r.successful_targets || []);
    const bleRows = state.bleChecked
      ? state.bleTargets
          .map(
            (t) => `
          <div class="field-row" style="margin-bottom:8px;">
            <span class="mono" style="flex:1; word-break:break-all;">${esc(t)}</span>
            ${okSet.has(t)
              ? `<span class="chip chip--ok"><ha-icon icon="mdi:check-circle" style="--mdc-icon-size:14px;"></ha-icon> Broadcasting</span>`
              : `<span class="chip chip--error"><ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:14px;"></ha-icon> Failed</span>`}
          </div>`
          )
          .join("")
      : "";
    return `
      <div style="text-align:center; margin-bottom:16px;">
        <ha-icon icon="mdi:check-circle" style="--mdc-icon-size:48px; color:var(--casa-success);"></ha-icon>
        <h3 style="margin:8px 0 0; font-size:16px; font-weight:600;">${esc(trim(state.deviceName))} is ready to set up</h3>
      </div>
      <div class="card" style="margin:0 0 16px;"><div class="card__body">
        ${credRow("Name", u.name || "")}
        ${credRow("Username", u.username || "")}
        ${credRow("Password", u.password || "")}
        <div style="display:flex; gap:8px; align-items:center; margin-top:4px; padding:10px 12px; border-radius:8px; background:color-mix(in srgb, var(--casa-warning) 16%, transparent); color:var(--casa-warning); font-size:13px;">
          <ha-icon icon="mdi:alert" style="--mdc-icon-size:18px; flex:none;"></ha-icon>
          <span>This password is shown only once.</span>
        </div>
      </div></div>
      ${state.qrChecked && r.url_path ? `
        <div style="text-align:center; margin-bottom:16px;">
          <div class="muted" style="font-size:13px; margin-bottom:12px;">Scan with the Casa app, or send a setup link.</div>
          <img src="${esc(r.url_path)}" alt="Provisioning QR code"
            style="width:220px; height:220px; border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); padding:12px; background:#fff;">
        </div>` : ""}
      ${state.linkChecked || state.qrChecked ? `
        ${r.deep_link ? linkRow("Setup Deep Link", r.deep_link) : ""}
        ${r.universal_link ? linkRow("Universal Link (opens from Safari / iMessage)", r.universal_link) : ""}` : ""}
      ${bleRows}
      ${r.expires_at ? `<div style="margin-top:6px;"><span class="chip chip--warn">valid until ${esc(ui.fmtExpiry(r.expires_at))}</span></div>` : ""}
      <div style="display:flex; gap:8px; align-items:center; margin-top:14px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
        <ha-icon icon="mdi:tag-outline" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
        <span>The device will be named <strong>${esc(trim(state.deviceName))}</strong> automatically when it connects (within 30 minutes).</span>
      </div>
      <div style="display:flex; justify-content:flex-end; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        <button class="btn btn--outlined" data-act="another">Provision another</button>
        <button class="btn btn--primary" data-act="done">Done</button>
      </div>`;
  }

  /* ---------- render + events ---------- */

  function gotoStep(id) {
    state.step = id;
    render();
  }

  function goBack() {
    if (state.step === "profile") gotoStep("device");
    else if (state.step === "deliver") gotoStep("profile");
  }

  function render() {
    if (!refs || !state) return;
    renderTabs();
    if (state.step === "device") refs.body.innerHTML = renderDeviceStep();
    else if (state.step === "profile") refs.body.innerHTML = renderProfileStep();
    else if (state.step === "deliver") refs.body.innerHTML = renderDeliverStep();
    else refs.body.innerHTML = renderResultStep();
    for (const btn of refs.body.querySelectorAll("[data-copy]")) {
      ui.bindCopyButton(btn, () => btn.dataset.copy);
    }
    if (state.step === "profile" && fieldsMod && state.form) {
      const formContainer = refs.body.querySelector("#pg-form-sections");
      if (formContainer) {
        fieldsMod.bindFieldEvents(formContainer, {
          values: state.form,
          onSectionRerender: (key) => rerenderFormSection(KEY_SECTION[key]),
        });
      }
      if (state.formOpen.pushvpn) ensureWgProfiles();
    }
  }

  function onBodyClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled || !refs.body.contains(el)) return;
    switch (el.dataset.act) {
      case "goto-step":
        if (stepIndex(el.dataset.step) < stepIndex(state.step) && state.step !== "result") gotoStep(el.dataset.step);
        return;
      case "back":
        goBack();
        return;
      case "custom-password":
        state.customPasswordOpen = true;
        render();
        refs.body.querySelector('input[data-field="password"]')?.focus();
        return;
      case "to-profile":
        toProfile();
        return;
      case "retry-profiles":
        loadProfiles();
        return;
      case "select-profile": {
        const p = (state.profiles || []).find((x) => x && x.id === el.dataset.id);
        if (p && fieldsMod) selectStartingPoint(p);
        return;
      }
      case "select-defaults":
        if (fieldsMod) selectStartingPoint(null);
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
      case "to-deliver":
        toDeliver();
        return;
      case "toggle-advanced":
        state.advancedOpen = !state.advancedOpen;
        render();
        return;
      case "add-target":
        addBleTarget();
        return;
      case "remove-target": {
        state.bleTargets = state.bleTargets.filter((t) => t !== el.dataset.target);
        const chips = refs.body.querySelector("#pg-ble-chips");
        if (chips) chips.innerHTML = bleChipsHtml();
        return;
      }
      case "deploy":
        deploy();
        return;
      case "another":
        state = freshState();
        loadProfiles();
        render();
        return;
      case "done":
        app.refresh();
        app.navigate("/");
        return;
    }
  }

  // View-private inputs only (data-field). Shared-renderer fields (data-key)
  // are handled by fieldsMod.bindFieldEvents bound per-container in render().
  function onBodyInput(e) {
    const t = e.target;
    const field = t.dataset && t.dataset.field;
    if (!field) return;
    switch (field) {
      case "deviceName":
        state.deviceName = t.value;
        if (!state.usernameEdited && utilsMod) {
          state.username = utilsMod.slugify(t.value);
          const userInput = refs.body.querySelector('input[data-field="username"]');
          if (userInput) userInput.value = state.username;
        }
        scheduleAvailability();
        return;
      case "username": {
        const lower = t.value.toLowerCase();
        if (lower !== t.value) t.value = lower;
        state.username = lower;
        // Deleting everything re-couples the username to the device name.
        state.usernameEdited = !!lower;
        scheduleAvailability();
        return;
      }
      case "password":
        state.password = t.value;
        return;
      case "linkChecked":
        state.linkChecked = !!t.checked;
        render();
        return;
      case "qrChecked":
        state.qrChecked = !!t.checked;
        render();
        return;
      case "bleChecked":
        state.bleChecked = !!t.checked;
        render();
        return;
      case "deleteQr":
        state.deleteQr = !!t.checked;
        return;
      case "qrFilename":
        state.qrFilename = t.value;
        return;
      case "pin":
        state.pin = t.value;
        return;
      case "scramble":
        state.scramble = !!t.checked;
        return;
      case "scrambleIn":
        state.scrambleIn = t.value;
        return;
      case "timeoutMinutes":
        state.timeoutMinutes = t.value;
        return;
      case "expirationHours":
        if (state.form) state.form.expiration_hours = t.value;
        return;
    }
  }

  function onBodyKeydown(e) {
    if (e.key === "Enter" && e.target.id === "pg-ble-input") {
      e.preventDefault();
      addBleTarget();
    }
  }

  /* ---------- unload guard ---------- */

  function onBeforeUnload(e) {
    if (!dirty()) return;
    e.preventDefault();
    e.returnValue = "";
  }

  /* ---------- view ---------- */

  return {
    id: "provision-guided",
    header: () => ({ title: "Provision new device", back: "/provision" }),
    polling: "paused",

    async mount(el) {
      const token = ++mountToken;
      state = freshState();
      wgRequested = false;

      el.innerHTML = `
        <div class="page">
          <div class="tabs tabs--steps" id="pg-tabs"></div>
          <div id="pg-body"></div>
        </div>`;
      refs = {
        tabs: el.querySelector("#pg-tabs"),
        body: el.querySelector("#pg-body"),
      };
      refs.body.addEventListener("click", onBodyClick);
      refs.body.addEventListener("input", onBodyInput);
      refs.body.addEventListener("keydown", onBodyKeydown);
      window.addEventListener("beforeunload", onBeforeUnload);
      // username-utils is needed by step 1's inputs (name→username slug), so
      // load it before the first render; the guards in onBodyInput make a
      // failed load degrade to manual username entry rather than breaking.
      try {
        utilsMod = utilsMod || (await app.loadModule("views/username-utils.js"));
      } catch {
        utilsMod = null;
      }
      if (token !== mountToken) return;
      render(); // step 1 needs no other async data — usable immediately

      try {
        const [fields, preview] = await Promise.all([
          fieldsMod || app.loadModule("views/profile-fields.js"),
          previewMod || app.loadModule("payload-preview.js"),
        ]);
        if (token !== mountToken) return;
        fieldsMod = fields;
        previewMod = preview;
      } catch (err) {
        if (token !== mountToken) return;
        refs.body.innerHTML = `<div class="errbar">Failed to load: ${esc(ui.errMsg(err))}</div>`;
        return;
      }

      loadProfiles();
    },

    unmount() {
      mountToken++;
      clearTimeout(availTimer);
      window.removeEventListener("beforeunload", onBeforeUnload);
      refs = null;
      state = null;
    },

    // Same openModal-based dialog as provision.js (ui.showConfirm has no
    // cancel callback); adds a third choice when an account already exists
    // for this run so leaving can't silently orphan it.
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
        const created = state.createdUser;
        const buttons = created
          ? [
              { label: "Keep going", variant: "text", onClick: () => done(false) },
              { label: "Leave — keep account", variant: "outlined", onClick: () => done(true) },
              {
                label: "Delete account & leave",
                variant: "danger",
                onClick: async (btn) => {
                  btn.disabled = true;
                  btn.textContent = "Deleting…";
                  try {
                    await api.removeUser(created.username);
                  } catch (err) {
                    ui.toast("Failed to remove account: " + ui.errMsg(err), { error: true });
                  }
                  done(true);
                },
              },
            ]
          : [
              { label: "Keep going", variant: "text", onClick: () => done(false) },
              { label: "Discard", variant: "danger", onClick: () => done(true) },
            ];
        const modal = ui.openModal({
          title: "Leave provisioning?",
          bodyHtml: created
            ? `<p style="margin:0; font-size:14px; line-height:1.5;">The account <strong class="mono">${esc(created.username)}</strong> was created but the device hasn't been provisioned. You can keep the account and re-provision it later from Accounts, or delete it.</p>`
            : '<p style="margin:0; font-size:14px; line-height:1.5;">This device hasn\'t been provisioned yet. Discard this setup and leave?</p>',
          buttons,
        });
        observer = new MutationObserver(() => {
          if (!modal.el.isConnected) done(false);
        });
        observer.observe(modal.el.parentNode, { childList: true });
      });
    },
  };
}
