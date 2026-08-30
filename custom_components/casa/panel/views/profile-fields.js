// Shared provisioning-field schema + renderer, used by the provision-template
// editor (template-editor.js), a single device's "Provisioning" section
// (device-editor.js), and the provision flow (views/provision.js). Same
// fields, same markup, same coercion rules — the callers differ in which
// field *scope* they render and what happens after: saving a named template,
// force-pushing a per-device override, or generating a one-time provision
// profile (the install payload).
//
// This module owns rendering + event wiring for a `values` object (plain
// field-key -> value map, no `name`/`id` — those are template-editor-only
// concepts). Callers own the surrounding state/mount lifecycle.
//
// Two rendering modes:
// - total (default): every rendered field is a concrete value (device editor,
//   provision wizard).
// - sparse (pass `setKeys`, a Set of field keys): templates store only the
//   fields their author explicitly set. `values` stays *effective* (defaults
//   backfilled) so gating logic and previews keep working; `setKeys` drives
//   the set/unset styling, the per-field "reset to default" button, and
//   sparse collection via collectFields(..., { setKeys }).

// Mirrors the field-scope constants in const.py (LIVE/PROFILE_ONLY/
// PROCESS_PROVISIONING_FIELDS) exactly — keys, types, and scopes.
export const DEFAULTS = {
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
  require_alias: false,
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

// live    — ongoing device state: templates, per-device editing, self-reports.
// profile — reusable template policy, resolved at provision time; not
//           device-editable (expires_at_override covers post-hoc changes).
// process — one-time provisioning-ceremony inputs / server-side actions;
//           wizard + casa.provision service_data only, never persisted.
export const FIELD_SCOPES = {
  host_url: "live",
  username: "process",
  password: "process",
  pin: "process",
  default_dashboard: "live",
  welcome_url: "live",
  immersive_level: "live",
  theme_color_mode: "live",
  custom_color: "live",
  deauthenticate_existing: "process",
  allow_all_pages: "live",
  allowed_pages: "live",
  allowed_wifi: "live",
  require_alias: "live",
  push_notifications: "live",
  allow_wireguard: "live",
  wireguard_config: "live",
  wireguard_excluded_wifi: "live",
  wireguard_profile_id: "live",
  timeout_minutes: "process",
  password_scramble: "process",
  password_scramble_in: "process",
  expiration_hours: "profile",
  connect_wifi_ssid: "process",
  connect_wifi_password: "process",
  cache_control_hours: "live",
};

const scopeKeys = (pred) => new Set(Object.keys(FIELD_SCOPES).filter((k) => pred(FIELD_SCOPES[k])));
export const LIVE_KEYS = scopeKeys((s) => s === "live");
export const PROFILE_KEYS = scopeKeys((s) => s !== "process");
export const PROCESS_KEYS = scopeKeys((s) => s === "process");

export const SECTIONS = [
  { id: "connection", label: "Connection", icon: "mdi:server-network" },
  { id: "appui", label: "App UI", icon: "mdi:palette-outline" },
  { id: "access", label: "Access Control", icon: "mdi:shield-lock-outline" },
  { id: "pushvpn", label: "Push & VPN", icon: "mdi:bell-badge-outline" },
  { id: "timing", label: "Timing & Security", icon: "mdi:timer-lock-outline" },
  { id: "wifi", label: "Wi-Fi & Extras", icon: "mdi:wifi-cog" },
];

// Which fields each section's renderer emits — must match the render*
// functions below so sectionsFor() can drop sections that would be empty
// under a given include-set.
export const SECTION_FIELDS = {
  connection: ["host_url", "username", "password", "pin", "deauthenticate_existing"],
  appui: ["default_dashboard", "welcome_url", "immersive_level", "theme_color_mode", "custom_color"],
  access: ["allow_all_pages", "allowed_pages", "allowed_wifi", "require_alias"],
  pushvpn: ["push_notifications", "allow_wireguard", "wireguard_profile_id", "wireguard_config", "wireguard_excluded_wifi"],
  timing: ["timeout_minutes", "expiration_hours", "password_scramble", "password_scramble_in", "cache_control_hours"],
  wifi: ["connect_wifi_ssid", "connect_wifi_password"],
};

export function sectionsFor(fieldsSet) {
  if (!fieldsSet) return SECTIONS;
  return SECTIONS.filter((s) => (SECTION_FIELDS[s.id] || []).some((k) => fieldsSet.has(k)));
}

const HEX_RE = /^#[0-9a-fA-F]{6}$/;

// `esc` is passed in by the caller (all hosts already have `ui.esc`) so this
// module doesn't need its own copy or a dependency on `ui`.
// `fields` (a Set of keys, e.g. LIVE_KEYS) limits which fields render —
// omitted means render everything. `heading: false` suppresses the section
// heading for hosts with their own headers (the wizard accordion).
// `setKeys` (Set|null) turns on sparse mode: unset fields render dimmed with
// the default value, set fields get a "reset to default" button.
// `annotations` ({key: "template"|"review"}|null) badges fields the wizard
// pre-filled from a template vs blanks the admin should review.
export function clearButtonHtml(key, esc) {
  return `<button type="button" class="field__clear" data-clear="${esc(key)}" title="Reset to default"><ha-icon icon="mdi:close-circle-outline"></ha-icon></button>`;
}

export function renderSectionHtml(sectionId, values, { readOnly = false, wgProfiles = [], esc, fields = null, heading = true, setKeys = null, annotations = null } = {}) {
  const val = (key) => values[key] ?? "";
  const dis = (extra = false) => (readOnly || extra ? "disabled" : "");
  const inc = (key) => !fields || fields.has(key);
  const stateCls = (key) => (setKeys ? (setKeys.has(key) ? " field--set" : " field--unset") : "");
  const clearBtn = (key) => (setKeys && setKeys.has(key) && !readOnly ? clearButtonHtml(key, esc) : "");
  const chip = (key) => {
    const kind = annotations ? annotations[key] : null;
    if (kind === "template") return `<span class="chip chip--app field__chip">from template</span>`;
    if (kind === "review") return `<span class="chip chip--warn field__chip">review</span>`;
    return "";
  };
  const labelRow = (key, labelHtml) => `<div class="field__labelrow">${labelHtml}${chip(key)}${clearBtn(key)}</div>`;

  function textField({ key, label, help, placeholder = "", type = "text", attrs = "", disabled = false }) {
    if (!inc(key)) return "";
    return `
      <div class="field${stateCls(key)}" data-field="${esc(key)}">
        ${labelRow(key, `<label>${esc(label)}</label>`)}
        <input class="input" type="${esc(type)}" data-key="${esc(key)}" value="${esc(val(key))}"
          placeholder="${esc(placeholder)}" ${dis(disabled)} ${attrs}>
        ${help ? `<div class="field__help">${esc(help)}</div>` : ""}
      </div>`;
  }

  function numberField({ key, label, help, min = 0, max, disabled = false }) {
    return textField({
      key, label, help, type: "number", disabled,
      attrs: `min="${min}"${max != null ? ` max="${max}"` : ""}`,
    });
  }

  function selectField({ key, label, help, options, disabled = false }) {
    if (!inc(key)) return "";
    const current = String(val(key));
    const opts = options
      .map(
        (o) => `<option value="${esc(o.value)}" ${String(o.value) === current ? "selected" : ""}>${esc(o.label)}</option>`
      )
      .join("");
    return `
      <div class="field${stateCls(key)}" data-field="${esc(key)}">
        ${labelRow(key, `<label>${esc(label)}</label>`)}
        <select class="select" data-key="${esc(key)}" ${dis(disabled)}>${opts}</select>
        ${help ? `<div class="field__help">${esc(help)}</div>` : ""}
      </div>`;
  }

  function toggleField({ key, label, help }) {
    if (!inc(key)) return "";
    return `
      <div class="field--toggle${stateCls(key)}" data-field="${esc(key)}">
        <div class="field__labelrow">
          <label class="toggle">
            <input type="checkbox" data-key="${esc(key)}" ${val(key) ? "checked" : ""} ${dis()}>
            <span>${esc(label)}</span>
          </label>
          ${chip(key)}${clearBtn(key)}
        </div>
        ${help ? `<div class="field__help" style="margin:-4px 0 12px 24px;">${esc(help)}</div>` : ""}
      </div>`;
  }

  function sectionHeading(title, desc) {
    if (!heading) return "";
    return `<h2>${esc(title)}</h2><p class="editor__form-desc">${esc(desc)}</p>`;
  }

  function renderConnection() {
    return `
      ${sectionHeading("Connection", "Where the device connects and, at provision time, which guest account it signs in as.")}
      ${textField({ key: "host_url", label: "Host URL *", placeholder: "http://192.168.1.21:8123", help: "The Home Assistant URL the device will use. Required." })}
      ${textField({ key: "username", label: "Username *", placeholder: "guest", help: "Guest account the device signs in as. Required." })}
      ${textField({ key: "password", label: "Password", placeholder: "(auto-generated at provision)", help: "Leave blank to auto-generate a secure password at provision time." })}
      ${textField({ key: "pin", label: "PIN", placeholder: "123456", attrs: 'maxlength="6" inputmode="numeric" autocomplete="off"', help: "Optional, max 6 digits." })}
      ${toggleField({ key: "deauthenticate_existing", label: "Sign out existing sessions at provision", help: "Deauthenticates the account's existing connections when a device is provisioned." })}`;
  }

  function renderAppUi() {
    const mode = String(val("theme_color_mode") || "inherit");
    const colorDisabled = mode === "inherit";
    const raw = String(val("custom_color")).trim();
    const pickValue = HEX_RE.test(raw) ? raw : "#000000";
    return `
      ${sectionHeading("App UI", "Dashboard, chrome, and theming for the provisioned app.")}
      ${textField({ key: "default_dashboard", label: "Default dashboard", placeholder: "/lovelace/home", help: "Path the app opens on launch." })}
      ${textField({ key: "welcome_url", label: "Welcome URL", help: "Optional URL shown after provisioning." })}
      ${selectField({
        key: "immersive_level", label: "Immersive level",
        options: [
          { value: "1", label: "Level 1 (Standard)" },
          { value: "2", label: "Level 2 (Transparent status bar)" },
          { value: "3", label: "Level 3 (Fullscreen)" },
        ],
      })}
      ${selectField({
        key: "theme_color_mode", label: "Theme color mode",
        options: [
          { value: "inherit", label: "Inherit from HA" },
          { value: "custom", label: "Custom color" },
          { value: "inherit_with_fallback", label: "Inherit with fallback" },
        ],
      })}
      ${inc("custom_color") ? `
      <div class="field${stateCls("custom_color")}" data-field="custom_color">
        ${labelRow("custom_color", `<label>Custom color</label>`)}
        <div class="field-row">
          <input type="color" data-ref="color-pick" value="${esc(pickValue)}" ${dis(colorDisabled)}
            style="flex:none; width:44px; height:36px; padding:2px; border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); background:var(--casa-card-bg); cursor:pointer;">
          <input class="input" data-key="custom_color" value="${esc(val("custom_color"))}"
            placeholder="#03A9F4" ${dis(colorDisabled)}>
        </div>
        <div class="field__help">Hex color used when the theme color mode is not "Inherit from HA".</div>
      </div>` : ""}`;
  }

  function renderAccess() {
    const allowAll = !!val("allow_all_pages");
    return `
      ${sectionHeading("Access Control", "Restrict which pages and networks the device may use.")}
      ${toggleField({ key: "allow_all_pages", label: "Allow all pages", help: "When on, the device may open any page (payload sends /*)." })}
      ${inc("allowed_pages") ? `
      <div class="field${stateCls("allowed_pages")}" data-field="allowed_pages">
        ${labelRow("allowed_pages", `<label>Allowed pages</label>`)}
        <input class="input" data-key="allowed_pages" value="${esc(val("allowed_pages"))}"
          placeholder="${allowAll ? "/*" : "/lovelace/home, /dashboard-1/*"}" ${dis(allowAll)}>
        <div class="field__help">Comma-separated paths the device may open. Ignored while "Allow all pages" is on.</div>
      </div>` : ""}
      ${textField({ key: "allowed_wifi", label: "Allowed Wi-Fi", placeholder: "HomeSSID, OfficeSSID", help: "Comma-separated SSIDs the app may be used on. Blank = any network." })}
      ${toggleField({ key: "require_alias", label: "Require device alias", help: "The app blocks the user with a name prompt until the device has an alias. Admin-set aliases satisfy it; also enforceable site-wide in Settings." })}`;
  }

  function renderPushVpn() {
    const linkedId = String(val("wireguard_profile_id"));
    const linked = linkedId ? wgProfiles.find((p) => p && p.id === linkedId) || null : null;
    const options = [{ value: "", label: "-- None / paste config below --" }].concat(
      wgProfiles.map((p) => ({ value: p.id, label: p.alias || p.id }))
    );
    // A saved link to a since-deleted WG profile still needs a visible option
    // so the select doesn't silently snap back to "None".
    if (linkedId && !linked) options.push({ value: linkedId, label: `(missing profile ${linkedId})` });

    const inlineHtml = !inc("wireguard_config")
      ? textField({ key: "wireguard_excluded_wifi", label: "WireGuard excluded Wi-Fi", placeholder: "HomeSSID", help: "SSIDs on which the tunnel stays down (device is already local)." })
      : linked
      ? `<div class="muted" style="font-size:13px; margin:2px 0 14px;">
           Config comes from profile '${esc(linked.alias || linked.id)}'.
         </div>`
      : `
        <div class="field${stateCls("wireguard_config")}" data-field="wireguard_config">
          ${labelRow("wireguard_config", `<label>WireGuard config</label>`)}
          <textarea class="textarea" data-key="wireguard_config" placeholder="[Interface]&#10;PrivateKey = ..." ${dis()}>${esc(val("wireguard_config"))}</textarea>
          <div class="field__help">Full client config pushed to the device. Ignored when a WireGuard profile is linked above.</div>
        </div>
        ${textField({ key: "wireguard_excluded_wifi", label: "WireGuard excluded Wi-Fi", placeholder: "HomeSSID", help: "SSIDs on which the tunnel stays down (device is already local)." })}`;

    return `
      ${sectionHeading("Push & VPN", "Push notification policy and the WireGuard tunnel for remote access.")}
      ${selectField({
        key: "push_notifications", label: "Push notifications",
        options: [
          { value: "false", label: "Disabled" },
          { value: "true", label: "Enabled" },
          { value: "mandatory", label: "Mandatory" },
        ],
      })}
      ${toggleField({ key: "allow_wireguard", label: "Allow WireGuard", help: "Lets the device bring up the VPN tunnel for remote access." })}
      ${selectField({ key: "wireguard_profile_id", label: "Link WireGuard profile", options, help: "Linking a profile overrides the pasted config below." })}
      ${inlineHtml}`;
  }

  function renderTiming() {
    return `
      ${sectionHeading("Timing & Security", "Session lifetime, cache behavior, and provisioning-window timing.")}
      ${numberField({ key: "timeout_minutes", label: "Timeout (minutes)", min: 0, max: 60, help: "Minutes before the provisioning code expires. 0 = code never expires." })}
      ${numberField({ key: "expiration_hours", label: "Session expiration (hours)", min: 0, max: 87600, help: "How long the provisioned session lasts. 0 = permanent session." })}
      ${toggleField({ key: "password_scramble", label: "Scramble password after window", help: "Rotates the guest password once the provisioning window closes." })}
      ${numberField({ key: "password_scramble_in", label: "Password scramble in (minutes)", min: 0, max: 120, help: "0 = inherit from timeout." })}
      ${textField({ key: "cache_control_hours", label: "Cache control (hours)", placeholder: "48", help: "Blank = app default (48h)." })}`;
  }

  function renderWifi() {
    return `
      ${sectionHeading("Wi-Fi & Extras", "Optionally join the device to a Wi-Fi network during provisioning.")}
      ${textField({ key: "connect_wifi_ssid", label: "Connect Wi-Fi SSID", placeholder: "MyNetwork", help: "Network the device joins during provisioning. Blank = skip." })}
      ${inc("connect_wifi_password") ? `
      <div class="field${stateCls("connect_wifi_password")}" data-field="connect_wifi_password">
        ${labelRow("connect_wifi_password", `<label>Connect Wi-Fi password</label>`)}
        <div class="field-row">
          <input class="input" type="password" data-key="connect_wifi_password" value="${esc(val("connect_wifi_password"))}" placeholder="Password" autocomplete="off" ${dis()}>
          <button type="button" class="btn btn--icon" data-ref="reveal-pw" title="Show password" style="flex:none;" ${readOnly ? "disabled" : ""}>
            <ha-icon icon="mdi:eye-outline"></ha-icon>
          </button>
        </div>
      </div>` : ""}`;
  }

  const RENDERERS = {
    connection: renderConnection,
    appui: renderAppUi,
    access: renderAccess,
    pushvpn: renderPushVpn,
    timing: renderTiming,
    wifi: renderWifi,
  };

  return (RENDERERS[sectionId] || renderConnection)();
}

// Delegated input/change/click handling for a form element rendered by
// renderSectionHtml. Mutates `values` in place and calls `onChange()` after
// each commit. `onSectionRerender()` is invoked when a field that gates
// sibling visibility changes (theme_color_mode, allow_all_pages,
// wireguard_profile_id) — the caller re-renders the current section from
// `values` (nothing is lost; values already hold the committed change).
// Sparse mode: pass the same `setKeys` given to renderSectionHtml. Editing a
// field marks it set (touched = set, flipped in place so typing isn't
// interrupted); the per-field [data-clear] button un-sets it, restoring
// `defaults[key]` and re-rendering the section (clearing a gating field must
// re-evaluate siblings).
export function bindFieldEvents(formEl, { values, readOnly = false, onChange = () => {}, onSectionRerender = () => {}, setKeys = null, defaults = DEFAULTS, esc = null } = {}) {
  if (readOnly) return () => {}; // nothing interactive to wire up

  function commit(el) {
    const key = el.dataset.key;
    if (!key) return;
    values[key] = el.type === "checkbox" ? el.checked : el.value;
    markSet(key);
  }

  function markSet(key) {
    if (!setKeys || setKeys.has(key)) return;
    setKeys.add(key);
    const wrap = formEl.querySelector(`[data-field="${key}"]`);
    if (!wrap) return;
    wrap.classList.remove("field--unset");
    wrap.classList.add("field--set");
    const row = wrap.querySelector(".field__labelrow");
    if (row && esc && !row.querySelector("[data-clear]")) {
      row.insertAdjacentHTML("beforeend", clearButtonHtml(key, esc));
    }
  }

  function clearFieldError(el) {
    const wrap = el.closest(".field");
    if (!wrap) return;
    wrap.classList.remove("field--error");
    wrap.querySelector(".field__error")?.remove();
  }

  function onInput(e) {
    const t = e.target;
    if (t.dataset.ref === "color-pick") {
      values.custom_color = t.value;
      markSet("custom_color");
      const twin = formEl.querySelector('[data-key="custom_color"]');
      if (twin) twin.value = t.value;
      onChange();
      return;
    }
    const key = t.dataset.key;
    if (!key) return;
    if (key === "pin") {
      const cleaned = t.value.replace(/\D/g, "").slice(0, 6);
      if (cleaned !== t.value) t.value = cleaned;
    }
    commit(t);
    if (key === "custom_color") {
      const trimmed = t.value.trim();
      const pick = formEl.querySelector('[data-ref="color-pick"]');
      if (pick && HEX_RE.test(trimmed)) pick.value = trimmed;
    }
    clearFieldError(t);
    onChange();
  }

  function onChangeEvt(e) {
    const t = e.target;
    if (!t.dataset.key) return;
    commit(t);
    if (["theme_color_mode", "allow_all_pages", "wireguard_profile_id"].includes(t.dataset.key)) {
      // The key is passed so multi-section hosts (wizard accordion) can
      // re-render just the section containing it; single-section hosts
      // ignore the argument.
      onSectionRerender(t.dataset.key);
    }
    onChange();
  }

  function onClick(e) {
    const clear = e.target.closest("[data-clear]");
    if (clear && setKeys) {
      const key = clear.dataset.clear;
      setKeys.delete(key);
      values[key] = defaults[key];
      onSectionRerender(key);
      onChange();
      return;
    }
    const btn = e.target.closest('[data-ref="reveal-pw"]');
    if (!btn) return;
    const input = formEl.querySelector('[data-key="connect_wifi_password"]');
    if (!input) return;
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.title = show ? "Hide password" : "Show password";
    btn.innerHTML = `<ha-icon icon="${show ? "mdi:eye-off-outline" : "mdi:eye-outline"}"></ha-icon>`;
  }

  formEl.addEventListener("input", onInput);
  formEl.addEventListener("change", onChangeEvt);
  formEl.addEventListener("click", onClick);

  return () => {
    formEl.removeEventListener("input", onInput);
    formEl.removeEventListener("change", onChangeEvt);
    formEl.removeEventListener("click", onClick);
  };
}

// Coerce a `values` object (raw form values) into the payload shape the
// server expects — bool/int/string per DEFAULTS' types. `fieldsSet` (e.g.
// PROFILE_KEYS) limits which keys are emitted — omitted means all. In sparse
// mode pass `setKeys` to emit only explicitly-set fields (the server also
// drops values equal to the defaults, so set-to-default round-trips as
// unset).
export function collectFields(values, fieldsSet = null, { setKeys = null } = {}) {
  const out = {};
  for (const [key, def] of Object.entries(DEFAULTS)) {
    if (fieldsSet && !fieldsSet.has(key)) continue;
    if (setKeys && !setKeys.has(key)) continue;
    const raw = values[key];
    if (typeof def === "boolean") out[key] = !!raw;
    else if (typeof def === "number") out[key] = parseInt(raw, 10) || 0;
    else out[key] = String(raw ?? "");
  }
  return out;
}
