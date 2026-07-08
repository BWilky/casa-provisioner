// Casa admin panel — provisioning-profile editor view. Three-pane editor
// (editor-shell.js) mounted at /profiles/new and /profiles/{profileId}:
// left navigator sections, center form bound to `state.form` (the profile's
// `fields` object, same keys as CasaProvisionProfilesView._DEFAULT_FIELDS in
// __init__.py), right live v2 payload preview (payload-preview.js). Saves the
// legacy flat body shape — { id?, name, ...fields } — because the server view
// reads every field key from the top level of the JSON body.

// Mirrors CasaProvisionProfilesView._DEFAULT_FIELDS exactly (keys and types).
const DEFAULTS = {
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

const SECTIONS = [
  { id: "connection", label: "Connection", icon: "mdi:server-network" },
  { id: "appui", label: "App UI", icon: "mdi:palette-outline" },
  { id: "access", label: "Access Control", icon: "mdi:shield-lock-outline" },
  { id: "pushvpn", label: "Push & VPN", icon: "mdi:bell-badge-outline" },
  { id: "timing", label: "Timing & Security", icon: "mdi:timer-lock-outline" },
  { id: "wifi", label: "Wi-Fi & Extras", icon: "mdi:wifi-cog" },
];

const HEX_RE = /^#[0-9a-fA-F]{6}$/;

export function createView(app) {
  const { api, ui, store } = app;
  const esc = ui.esc;

  /* ---------- lazily loaded siblings (never static imports) ---------- */
  let shellMod = null; // editor-shell.js
  let previewMod = null; // payload-preview.js

  /* ---------- per-mount state ---------- */
  let mountToken = 0;
  let loaded = false;
  let handle = null; // editor-shell handle
  let saveBtn = null;
  let discardBtn = null;

  let profileId = null; // null → new profile (POST); set → existing (PUT)
  let state = { name: "", form: { ...DEFAULTS } };
  let pristineState = null; // deep copy for "Discard changes"
  let pristine = ""; // normalized snapshot for the dirty getter
  let wgProfiles = []; // [{id, alias, excluded_wifi}] for the link select + preview
  let activeSection = "connection";
  let previewTimer = null;

  /* ---------- dirty tracking ---------- */

  // Inputs hand back strings, so normalize every non-bool field to a string
  // before comparing — otherwise 5 vs "5" would read as dirty.
  function snapshot(s) {
    const norm = {};
    for (const key of Object.keys(DEFAULTS)) {
      norm[key] = typeof DEFAULTS[key] === "boolean" ? !!s.form[key] : String(s.form[key] ?? "");
    }
    return JSON.stringify({ name: String(s.name ?? ""), fields: norm });
  }
  const dirty = () => loaded && snapshot(state) !== pristine;
  const deepCopy = (obj) => JSON.parse(JSON.stringify(obj));

  const titleText = () => `Editing ${state.name || "New profile"}`;

  /* ---------- form section rendering ---------- */

  const val = (key) => state.form[key] ?? "";

  function textField({ key, label, help, placeholder = "", type = "text", attrs = "" }) {
    return `
      <div class="field" data-field="${esc(key)}">
        <label>${esc(label)}</label>
        <input class="input" type="${esc(type)}" data-key="${esc(key)}" value="${esc(val(key))}"
          placeholder="${esc(placeholder)}" ${attrs}>
        ${help ? `<div class="field__help">${esc(help)}</div>` : ""}
      </div>`;
  }

  function numberField({ key, label, help, min = 0, max }) {
    return textField({
      key, label, help, type: "number",
      attrs: `min="${min}"${max != null ? ` max="${max}"` : ""}`,
    });
  }

  function selectField({ key, label, help, options, disabled = false }) {
    const current = String(val(key));
    const opts = options
      .map(
        (o) => `<option value="${esc(o.value)}" ${String(o.value) === current ? "selected" : ""}>${esc(o.label)}</option>`
      )
      .join("");
    return `
      <div class="field" data-field="${esc(key)}">
        <label>${esc(label)}</label>
        <select class="select" data-key="${esc(key)}" ${disabled ? "disabled" : ""}>${opts}</select>
        ${help ? `<div class="field__help">${esc(help)}</div>` : ""}
      </div>`;
  }

  function toggleField({ key, label, help }) {
    return `
      <div data-field="${esc(key)}">
        <label class="toggle">
          <input type="checkbox" data-key="${esc(key)}" ${val(key) ? "checked" : ""}>
          <span>${esc(label)}</span>
        </label>
        ${help ? `<div class="field__help" style="margin:-4px 0 12px 24px;">${esc(help)}</div>` : ""}
      </div>`;
  }

  function sectionHeading(title, desc) {
    return `<h2>${esc(title)}</h2><p class="editor__form-desc">${esc(desc)}</p>`;
  }

  function renderConnection() {
    return `
      ${sectionHeading("Connection", "Where the device connects and which guest account it signs in as.")}
      <div class="field" data-field="name">
        <label>Profile name</label>
        <input class="input" data-key="name" value="${esc(state.name)}" placeholder="e.g. Guest tablet">
        <div class="field__help">Auto-generated if blank.</div>
      </div>
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
      <div class="field" data-field="custom_color">
        <label>Custom color</label>
        <div class="field-row">
          <input type="color" data-ref="color-pick" value="${esc(pickValue)}" ${colorDisabled ? "disabled" : ""}
            style="flex:none; width:44px; height:36px; padding:2px; border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); background:var(--casa-card-bg); cursor:pointer;">
          <input class="input" data-key="custom_color" value="${esc(val("custom_color"))}"
            placeholder="#03A9F4" ${colorDisabled ? "disabled" : ""}>
        </div>
        <div class="field__help">Hex color used when the theme color mode is not "Inherit from HA".</div>
      </div>`;
  }

  function renderAccess() {
    const allowAll = !!val("allow_all_pages");
    return `
      ${sectionHeading("Access Control", "Restrict which pages and networks the device may use.")}
      ${toggleField({ key: "allow_all_pages", label: "Allow all pages", help: "When on, the device may open any page (payload sends /*)." })}
      <div class="field" data-field="allowed_pages">
        <label>Allowed pages</label>
        <input class="input" data-key="allowed_pages" value="${esc(val("allowed_pages"))}"
          placeholder="${allowAll ? "/*" : "/lovelace/home, /dashboard-1/*"}" ${allowAll ? "disabled" : ""}>
        <div class="field__help">Comma-separated paths the device may open. Ignored while "Allow all pages" is on.</div>
      </div>
      ${textField({ key: "allowed_wifi", label: "Allowed Wi-Fi", placeholder: "HomeSSID, OfficeSSID", help: "Comma-separated SSIDs the app may be used on. Blank = any network." })}`;
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

    const inlineHtml = linked
      ? `<div class="muted" style="font-size:13px; margin:2px 0 14px;">
           Config comes from profile '${esc(linked.alias || linked.id)}'.
         </div>`
      : `
        <div class="field" data-field="wireguard_config">
          <label>WireGuard config</label>
          <textarea class="textarea" data-key="wireguard_config" placeholder="[Interface]&#10;PrivateKey = ...">${esc(val("wireguard_config"))}</textarea>
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
      ${sectionHeading("Timing & Security", "Provisioning-code lifetime, session expiration, and password hygiene.")}
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
      <div class="field" data-field="connect_wifi_password">
        <label>Connect Wi-Fi password</label>
        <div class="field-row">
          <input class="input" type="password" data-key="connect_wifi_password" value="${esc(val("connect_wifi_password"))}" placeholder="Password" autocomplete="off">
          <button type="button" class="btn btn--icon" data-ref="reveal-pw" title="Show password" style="flex:none;">
            <ha-icon icon="mdi:eye-outline"></ha-icon>
          </button>
        </div>
      </div>`;
  }

  const SECTION_RENDERERS = {
    connection: renderConnection,
    appui: renderAppUi,
    access: renderAccess,
    pushvpn: renderPushVpn,
    timing: renderTiming,
    wifi: renderWifi,
  };

  function renderSection(id) {
    activeSection = SECTION_RENDERERS[id] ? id : "connection";
    if (!handle) return;
    handle.formEl.innerHTML = SECTION_RENDERERS[activeSection]();
    handle.formEl.scrollTop = 0;
  }

  /* ---------- form events (delegated; formEl persists across sections) ---------- */

  function commit(el) {
    const key = el.dataset.key;
    if (!key) return;
    const value = el.type === "checkbox" ? el.checked : el.value;
    if (key === "name") state.name = value;
    else state.form[key] = value;
  }

  function clearFieldError(el) {
    const wrap = el.closest(".field");
    if (!wrap) return;
    wrap.classList.remove("field--error");
    wrap.querySelector(".field__error")?.remove();
  }

  function afterChange() {
    updateDirtyUi();
    schedulePreview();
  }

  function onFormInput(e) {
    const t = e.target;
    if (t.dataset.ref === "color-pick") {
      // Color swatch drives the text twin (both map to custom_color).
      state.form.custom_color = t.value;
      const twin = handle.formEl.querySelector('[data-key="custom_color"]');
      if (twin) twin.value = t.value;
      afterChange();
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
      const pick = handle.formEl.querySelector('[data-ref="color-pick"]');
      if (pick && HEX_RE.test(trimmed)) pick.value = trimmed;
    }
    clearFieldError(t);
    afterChange();
  }

  function onFormChange(e) {
    const t = e.target;
    if (!t.dataset.key) return;
    commit(t);
    // These fields gate the visibility/enabled state of siblings, so re-render
    // the section from state (nothing is lost — state is committed above).
    if (["theme_color_mode", "allow_all_pages", "wireguard_profile_id"].includes(t.dataset.key)) {
      renderSection(activeSection);
    }
    afterChange();
  }

  function onFormClick(e) {
    const btn = e.target.closest('[data-ref="reveal-pw"]');
    if (!btn || !handle) return;
    const input = handle.formEl.querySelector('[data-key="connect_wifi_password"]');
    if (!input) return;
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.title = show ? "Hide password" : "Show password";
    btn.innerHTML = `<ha-icon icon="${show ? "mdi:eye-off-outline" : "mdi:eye-outline"}"></ha-icon>`;
  }

  /* ---------- live payload preview ---------- */

  const PREVIEW_NOTES_HTML = `
    <div class="muted" style="padding:12px 16px 2px; font-size:12px; line-height:1.5;">
      Parenthesized values are placeholders the server resolves at provision time.
    </div>
    <div class="muted" style="padding:6px 16px 14px; font-size:12px; line-height:1.5;">
      Not part of the payload: <span class="mono">deauthenticate_existing</span>,
      <span class="mono">password_scramble</span> / <span class="mono">password_scramble_in</span>,
      and timeout enforcement — the server applies these at provision time.
    </div>`;

  function currentPreview() {
    return previewMod.buildV2PayloadPreview(state.form, {
      siteId: app.summary()?.site_id,
      wgProfiles,
    });
  }

  function renderPreview() {
    if (!handle || !previewMod) return;
    shellMod.renderJsonCode(handle.codeEl, currentPreview());
    handle.codeEl.insertAdjacentHTML("beforeend", PREVIEW_NOTES_HTML);
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      previewTimer = null;
      renderPreview();
    }, 150);
  }

  /* ---------- dirty chrome ---------- */

  function updateDirtyUi() {
    if (!handle) return;
    const d = dirty();
    handle.setTitle(titleText(), d ? '<span class="chip chip--warn">unsaved</span>' : "");
    if (saveBtn) saveBtn.disabled = !d;
    if (discardBtn) discardBtn.hidden = !d;
  }

  function discard() {
    state = deepCopy(pristineState);
    renderSection(activeSection);
    updateDirtyUi();
    schedulePreview();
  }

  /* ---------- save ---------- */

  function autoName(username, hostUrl) {
    let host = hostUrl;
    try {
      host = new URL(hostUrl).host || hostUrl;
    } catch (_e) {
      /* not a parsable URL — use as-is */
    }
    return `${username} @ ${host}`;
  }

  function markRequiredError(key) {
    const wrap = handle.formEl.querySelector(`[data-field="${key}"]`);
    if (!wrap || wrap.classList.contains("field--error")) return;
    wrap.classList.add("field--error");
    wrap.insertAdjacentHTML("beforeend", '<div class="field__error">Required.</div>');
  }

  async function save() {
    if (!handle) return;
    const hostUrl = String(state.form.host_url ?? "").trim();
    const username = String(state.form.username ?? "").trim();
    if (!hostUrl || !username) {
      handle.setActive("connection");
      renderSection("connection");
      if (!hostUrl) markRequiredError("host_url");
      if (!username) markRequiredError("username");
      ui.toast("Host URL and Username are required.", { error: true });
      return;
    }

    // Legacy/server body shape: flat — name (+ id for updates) alongside every
    // field key at the top level; ints coerced like the legacy editor.
    const payload = { name: String(state.name ?? "").trim() || autoName(username, hostUrl) };
    for (const [key, def] of Object.entries(DEFAULTS)) {
      const raw = state.form[key];
      if (typeof def === "boolean") payload[key] = !!raw;
      else if (typeof def === "number") payload[key] = parseInt(raw, 10) || 0;
      else payload[key] = String(raw ?? "");
    }
    payload.host_url = hostUrl;
    payload.username = username;
    if (profileId) payload.id = profileId;

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      const res = await api.saveProvisionProfile(payload); // PUT if id else POST
      state.name = payload.name;
      pristineState = deepCopy(state);
      pristine = snapshot(state);
      ui.toast("Profile saved");
      if (!profileId && res && res.id) {
        // POST returns the created profile; adopt its id and fix the URL. The
        // router remounts this view for the new path (state is clean now).
        profileId = res.id;
        app.navigate("/profiles/" + encodeURIComponent(res.id), { replace: true });
        return;
      }
      app.setHeader({ title: titleText() });
      updateDirtyUi();
    } catch (err) {
      ui.toast("Save failed: " + ((err && err.message) || err), { error: true });
    } finally {
      // The success path may have navigated away and unmounted us.
      if (saveBtn) {
        saveBtn.textContent = "Save";
        saveBtn.disabled = !dirty();
      }
    }
  }

  /* ---------- unload guard ---------- */

  function onBeforeUnload(e) {
    if (!dirty()) return;
    e.preventDefault();
    e.returnValue = "";
  }

  /* ---------- shell assembly ---------- */

  function buildShell(hostEl) {
    handle = shellMod.renderEditorShell(hostEl, {
      ui,
      store,
      storeKey: "editor.profile",
      title: titleText(),
      titleChips: "",
      navHint: "Configure the provisioning profile. The preview shows the payload a device will receive.",
      sections: SECTIONS,
      activeSection,
      onSelectSection: (id) => renderSection(id),
      codeTitle: "payload.json — live preview (v2)",
      onCopyCode: () => JSON.stringify(currentPreview(), null, 2),
      footerHtml: `
        <span class="muted" style="font-size:12px;">Saved profiles appear in the provisioning wizard.</span>
        <span class="spacer"></span>
        <button class="btn btn--text" data-act="discard" hidden>Discard changes</button>
        <button class="btn btn--primary" data-act="save" disabled>Save</button>`,
    });
    saveBtn = handle.footerEl.querySelector('[data-act="save"]');
    discardBtn = handle.footerEl.querySelector('[data-act="discard"]');
    saveBtn.addEventListener("click", save);
    discardBtn.addEventListener("click", discard);
    handle.formEl.addEventListener("input", onFormInput);
    handle.formEl.addEventListener("change", onFormChange);
    handle.formEl.addEventListener("click", onFormClick);
    renderSection(activeSection);
    renderPreview();
  }

  /* ---------- view ---------- */

  return {
    id: "profile-editor",
    header: (params) => ({
      title: loaded ? titleText() : params && params.profileId ? "Editing profile…" : "Editing New profile",
      back: "/",
    }),
    polling: "paused",

    async mount(el, params) {
      const token = ++mountToken;
      profileId = (params && params.profileId) || null;
      loaded = false;
      activeSection = "connection";

      const hostEl = document.createElement("div");
      hostEl.className = "page--flush";
      hostEl.innerHTML = '<div class="empty-state"><span class="muted">Loading profile…</span></div>';
      el.appendChild(hostEl);

      try {
        const [shell, preview, ppRes, wgRes] = await Promise.all([
          shellMod || app.loadModule("editor-shell.js"),
          previewMod || app.loadModule("payload-preview.js"),
          profileId ? api.getProvisionProfiles() : null,
          api.getWireguardProfiles().catch(() => ({ profiles: [] })),
        ]);
        if (token !== mountToken) return;
        shellMod = shell;
        previewMod = preview;
        wgProfiles = (wgRes && wgRes.profiles) || [];

        if (profileId) {
          const profile = ((ppRes && ppRes.profiles) || []).find((p) => p && p.id === profileId);
          if (!profile) {
            ui.showInfo({ title: "Profile not found", message: "That provisioning profile no longer exists." });
            app.navigate("/", { replace: true });
            return;
          }
          state = { name: profile.name || "", form: { ...DEFAULTS, ...(profile.fields || {}) } };
        } else {
          state = { name: "", form: { ...DEFAULTS } };
        }
      } catch (err) {
        if (token !== mountToken) return;
        hostEl.innerHTML = `<div class="page"><div class="errbar">Failed to load profile: ${esc(String((err && err.message) || err))}</div></div>`;
        return;
      }

      pristineState = deepCopy(state);
      pristine = snapshot(state);
      loaded = true;

      buildShell(hostEl);
      app.setHeader({ title: titleText() });
      window.addEventListener("beforeunload", onBeforeUnload);
    },

    unmount() {
      mountToken++;
      clearTimeout(previewTimer);
      previewTimer = null;
      window.removeEventListener("beforeunload", onBeforeUnload);
      handle = null;
      saveBtn = null;
      discardBtn = null;
      loaded = false;
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
          title: "Discard changes?",
          bodyHtml:
            '<p style="margin:0; font-size:14px; line-height:1.5;">This profile has unsaved changes. Discard them and leave?</p>',
          buttons: [
            { label: "Keep editing", variant: "text", onClick: () => done(false) },
            { label: "Discard", variant: "danger", onClick: () => done(true) },
          ],
        });
        observer = new MutationObserver(() => {
          if (!modal.el.isConnected) done(false);
        });
        observer.observe(modal.el.parentNode, { childList: true });
      });
    },

    onSummary() {
      // site_id may arrive after mount; refresh the preview's placeholder.
      if (loaded && handle) schedulePreview();
    },
  };
}
