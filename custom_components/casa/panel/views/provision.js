// Casa admin panel — provision a new device. Full-page wizard at /provision:
// ① Account — create a new account named after the device (username
//   suggested as casa-<slug>, live availability; a taken username that is a
//   Casa account offers "use this account instead"), or pick an existing
//   Casa account; the device name is required either way.
// ② Template — a saved template or "Configure manually". Per-device tweaks
//   live behind "Customize for this device" (with an optional "Also save as
//   new template"); PIN / Wi-Fi join / sign-out-others behind "Advanced".
// ③ Done — QR + setup links.
// Re-provisioning an existing device is a device action
// (views/reprovision.js), not part of this flow.
//
// Generate order is create_user → (save template) → casa.provision, each
// guarded by state so a retry never duplicates the account (createdUser) or
// the template (savedTemplateId). A new account's generated password rides
// casa.provision explicitly so a retry never rotates it; an existing account
// sends none and the server rotates it. Passwords are never shown. Decision
// logic lives in provision-logic.js (node-tested).

const STEPS = [
  { id: "account", label: "Account" },
  { id: "template", label: "Template" },
  { id: "result", label: "Done" },
];
const stepIndex = (id) => STEPS.findIndex((s) => s.id === id);

// Gating fields → the Customize section whose body re-renders when they flip.
const KEY_SECTION = { theme_color_mode: "appui", allow_all_pages: "access", wireguard_profile_id: "pushvpn" };
const CONNECTION_SET = new Set(["host_url"]);
const TIMING_SET = new Set(["expiration_hours", "cache_control_hours"]);
const ADV_CONNECTION_SET = new Set(["pin", "deauthenticate_existing"]);
const ADV_WIFI_SET = new Set(["connect_wifi_ssid", "connect_wifi_password"]);
const SEARCH_THRESHOLD = 8;

export function createView(app) {
  const { api, ui } = app;
  const esc = ui.esc;
  const unwrap = (res) => api.constructor.response(res); // CasaApi.response
  const trim = (v) => String(v ?? "").trim();

  /* ---------- lazily loaded siblings (never static imports) ---------- */
  let fieldsMod = null; // views/profile-fields.js
  let previewMod = null; // payload-preview.js
  let utilsMod = null; // views/username-utils.js
  let logicMod = null; // views/provision-logic.js
  let resultMod = null; // views/provision-result.js

  /* ---------- per-mount state ---------- */
  let mountToken = 0;
  let state = null;
  let refs = null; // { tabs, body }
  let wgRequested = false;
  let availTimer = 0;
  let mountedWithPresetPath = false;

  function freshState() {
    return {
      step: "account",
      // ① account
      accountMode: "new", // "new" | "existing"
      deviceName: "",
      username: "",
      usernameEdited: false, // admin typed in the username field; stop auto-suggesting
      availability: null, // null | {checking:true} | {available, username_conflict, name_conflict, for}
      existingUsername: "",
      accountError: "",
      createdUser: null, // {name, username, password, user_id} — retry guard
      // ② template
      templates: null, // null = loading
      templatesError: null,
      search: "",
      choice: null, // template id | "manual" | null
      form: null, // full DEFAULTS-shaped values (profile + process keys)
      baseline: null, // collectFields(form, PROFILE_KEYS) at seed time
      customizeOpen: false,
      advancedOpen: false,
      saveAsTemplate: false,
      newTemplateName: "",
      savedTemplateId: null, // retry guard
      wgProfiles: null,
      deployError: "",
      busy: false,
      // ③
      result: null,
    };
  }

  const dirty = () =>
    !!(state && state.step !== "result" && (trim(state.deviceName) || state.choice || state.createdUser));

  const casaAccounts = () =>
    ((app.summary() && app.summary().accounts) || [])
      .slice()
      .sort((a, b) => String(a.name || a.username).localeCompare(String(b.name || b.username)));

  const selectedTemplate = () =>
    state.choice && state.choice !== "manual"
      ? (state.templates || []).find((t) => t && t.id === state.choice) || null
      : null;

  /* ---------- data ---------- */

  async function loadTemplates() {
    state.templates = null;
    state.templatesError = null;
    if (state.step === "template") render();
    const token = mountToken;
    try {
      const res = await api.getProvisionTemplates();
      if (token !== mountToken || !state) return;
      state.templates = (res && res.profiles) || [];
    } catch (err) {
      if (token !== mountToken || !state) return;
      state.templates = [];
      state.templatesError = ui.errMsg(err);
    }
    if (state.step === "template") render();
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
        rerenderCustomizeSection("pushvpn");
      });
  }

  /* ---------- username availability (advisory — create_user is authoritative) ---------- */

  function conflictAccount() {
    const a = state.availability;
    const u = trim(state.username);
    if (!a || a.checking || a.for !== u || a.available || !a.username_conflict) return null;
    return casaAccounts().find((x) => x.username === u) || null;
  }

  function availabilityHtml() {
    const acct = conflictAccount();
    if (acct) {
      return `
        <span class="chip chip--error"><ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:14px;"></ha-icon> ${esc(acct.username)} already exists</span>
        <button class="btn btn--text" data-act="use-existing" data-username="${esc(acct.username)}" style="height:24px;">Use this account instead?</button>`;
    }
    return utilsMod ? utilsMod.availabilityHintHtml(state.availability, trim(state.username), esc) : "";
  }

  function renderAvailability() {
    const el = refs && refs.body.querySelector("#pv-availability");
    if (el) el.innerHTML = availabilityHtml();
  }

  function scheduleAvailability() {
    clearTimeout(availTimer);
    const username = trim(state.username);
    if (!username || !utilsMod || !utilsMod.USERNAME_RE.test(username)) {
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
        state.availability = null;
      }
      renderAvailability();
    }, 350);
  }

  /* ---------- tabs / shared chrome ---------- */

  function gotoStep(id) {
    state.step = id;
    render();
  }

  function renderTabs() {
    const cur = stepIndex(state.step);
    const done = state.step === "result";
    refs.tabs.innerHTML = STEPS.map((s, i) => `
      <button class="tab ${i === cur ? "tab--active" : ""} ${i < cur && !done ? "tab--done" : ""}"
        data-act="goto-step" data-step="${esc(s.id)}"
        ${i >= cur || done || state.busy ? "disabled" : ""}>
        <span class="step-dot">${i < cur ? "✓" : i + 1}</span>${esc(s.label)}
      </button>`).join("");
  }

  function footer(primaryLabel, primaryAct, { back = true } = {}) {
    return `
      <div style="display:flex; justify-content:space-between; gap:8px; margin-top:18px; padding-top:12px; border-top:1px solid var(--casa-divider);">
        ${back ? `<button class="btn btn--text" data-act="back" ${state.busy ? "disabled" : ""}>Back</button>` : "<span></span>"}
        <button class="btn btn--primary" data-act="${esc(primaryAct)}" ${state.busy ? "disabled" : ""}>
          ${state.busy ? "Working…" : esc(primaryLabel)}
        </button>
      </div>`;
  }

  function markFieldError(field, msg) {
    const wrap = refs.body.querySelector(`[data-pv-field="${field}"]`);
    if (!wrap || wrap.classList.contains("field--error")) return;
    wrap.classList.add("field--error");
    wrap.insertAdjacentHTML("beforeend", `<div class="field__error">${esc(msg)}</div>`);
  }

  /* ---------- step 1: account ---------- */

  function modeCard(mode, title, desc) {
    const active = state.accountMode === mode;
    return `
      <button class="option-card" data-act="mode" data-mode="${esc(mode)}" style="flex:1; min-width:240px;
        ${active ? "border-color:var(--casa-primary); background:color-mix(in srgb, var(--casa-primary) 6%, transparent);" : ""}">
        <ha-icon icon="${active ? "mdi:radiobox-marked" : "mdi:radiobox-blank"}" style="color:${active ? "var(--casa-primary)" : "var(--casa-text-2)"};"></ha-icon>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          <span class="option-card__desc" style="display:block;">${esc(desc)}</span>
        </span>
      </button>`;
  }

  function deviceNameField() {
    return `
      <div class="field" data-pv-field="deviceName">
        <label>Device name *</label>
        <input class="input" data-pv="deviceName" value="${esc(state.deviceName)}" maxlength="60"
          placeholder="e.g. Kitchen iPad" autocomplete="off">
        <div class="field__help">${state.accountMode === "new"
          ? "Shown in the device list, and used as the account's name."
          : "Shown in the device list — applied automatically when the device first connects."}</div>
      </div>`;
  }

  function renderAccountStep() {
    if (state.createdUser) {
      const u = state.createdUser;
      return `
        <div style="display:flex; gap:8px; align-items:center; margin:0 0 14px; padding:10px 12px; border-radius:var(--casa-radius-sm); background:var(--casa-bg-2); font-size:13px;">
          <ha-icon icon="mdi:information-outline" style="--mdc-icon-size:18px; flex:none; color:var(--casa-text-2);"></ha-icon>
          <span>Account <strong class="mono">${esc(u.username)}</strong> was already created for this run — continue to retry, or leave to keep it (remove it from Accounts if unwanted).</span>
        </div>
        <div class="field"><label>Device name</label><input class="input" value="${esc(state.deviceName)}" disabled></div>
        ${footer("Continue", "to-template", { back: false })}`;
    }
    const accounts = casaAccounts();
    const newFields = `
      ${deviceNameField()}
      <div class="field" data-pv-field="username">
        <label>Username *</label>
        <input class="input mono" data-pv="username" value="${esc(state.username)}"
          placeholder="e.g. casa-kitchen-ipad" autocapitalize="none" autocomplete="off" spellcheck="false">
        <div id="pv-availability" style="min-height:20px; margin-top:6px;">${availabilityHtml()}</div>
        <div class="field__help">The account this device signs in with — suggested from the name, edit if you like.</div>
      </div>`;
    const existingFields = accounts.length
      ? `
        <div class="field" data-pv-field="existingUsername">
          <label>Account *</label>
          <select class="select" data-pv="existingUsername" style="width:100%;">
            <option value="" ${state.existingUsername ? "" : "selected"} disabled>Choose an account…</option>
            ${accounts.map((a) => `<option value="${esc(a.username)}" ${a.username === state.existingUsername ? "selected" : ""}>${esc(a.name || a.username)} (${esc(a.username)}) · ${Number(a.device_count) || 0} device${Number(a.device_count) === 1 ? "" : "s"}</option>`).join("")}
          </select>
          <div class="field__help">Several devices can share an account; other devices on it stay signed in.</div>
        </div>
        ${deviceNameField()}`
      : `<div class="empty-state" style="padding:24px 16px;"><div>No Casa accounts yet</div>
           <button class="btn btn--text" data-act="mode" data-mode="new">Create one instead</button></div>`;
    return `
      ${state.accountError ? `<div class="errbar">${esc(state.accountError)}</div>` : ""}
      <div style="display:flex; gap:12px; flex-wrap:wrap; margin-bottom:14px;">
        ${modeCard("new", "Create new account", "A fresh account for this device")}
        ${modeCard("existing", "Use existing account", "Sign this device in as an account you already have")}
      </div>
      ${state.accountMode === "new" ? newFields : existingFields}
      ${footer("Continue", "to-template", { back: false })}`;
  }

  function toTemplateStep() {
    state.accountError = "";
    if (state.createdUser) return gotoStep("template");
    const name = trim(state.deviceName);
    const errs = [];
    if (state.accountMode === "new") {
      const username = trim(state.username);
      if (!name) errs.push(["deviceName", "Required."]);
      if (!username) errs.push(["username", "Required."]);
      else if (!utilsMod.USERNAME_RE.test(username)) errs.push(["username", "Lowercase letters, numbers and dashes only."]);
      const a = state.availability;
      if (username && a && !a.checking && a.for === username && !a.available) {
        errs.push(a.username_conflict ? ["username", "Already in use."] : ["deviceName", `A user named '${name}' already exists.`]);
      }
    } else {
      if (!casaAccounts().some((x) => x.username === state.existingUsername)) errs.push(["existingUsername", "Choose an account."]);
      if (!name) errs.push(["deviceName", "Required."]);
    }
    if (errs.length) {
      state.accountError = "Fix the highlighted fields to continue.";
      render();
      for (const [field, msg] of errs) markFieldError(field, msg);
      return;
    }
    gotoStep("template");
  }

  /* ---------- step 2: template ---------- */

  function seedForm(template) {
    const F = fieldsMod;
    const form = { ...F.DEFAULTS };
    const f = (template && template.fields) || {};
    for (const key of Object.keys(f)) {
      if (F.PROFILE_KEYS.has(key)) form[key] = f[key];
    }
    if (!trim(form.host_url)) form.host_url = window.location.origin;
    // Advanced (process) inputs survive a template switch.
    if (state.form) for (const key of F.PROCESS_KEYS) form[key] = state.form[key];
    return form;
  }

  function applyChoice(choice) {
    state.choice = choice;
    state.form = seedForm(selectedTemplate());
    state.baseline = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
    state.customizeOpen = choice === "manual" || state.customizeOpen;
    state.deployError = "";
    render();
  }

  function isCustomized() {
    if (!state.form || !state.baseline) return false;
    const fields = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
    return logicMod.changedKeys(fields, state.baseline).length > 0;
  }

  function requestChoice(choice) {
    if (choice === state.choice) return;
    if (!isCustomized()) return applyChoice(choice);
    ui.showConfirm({
      title: "Discard customizations?",
      message: "Switching discards the changes you made under 'Customize for this device'.",
      confirmLabel: "Switch",
      confirmDanger: false,
      onConfirm: () => applyChoice(choice),
    });
  }

  function choiceRow({ choice, title, chipsHtml, desc }) {
    const active = state.choice === choice;
    return `
      <button class="option-card" data-act="choose" data-choice="${esc(choice)}" style="width:100%; margin-bottom:8px;
        ${active ? "border-color:var(--casa-primary); background:color-mix(in srgb, var(--casa-primary) 6%, transparent);" : ""}">
        <ha-icon icon="${active ? "mdi:radiobox-marked" : "mdi:radiobox-blank"}" style="color:${active ? "var(--casa-primary)" : "var(--casa-text-2)"};"></ha-icon>
        <span class="option-card__text">
          <span class="option-card__title" style="display:block;">${esc(title)}</span>
          ${chipsHtml ? `<span style="display:flex; flex-wrap:wrap; gap:4px; margin-top:4px;">${chipsHtml}</span>` : ""}
          ${desc ? `<span class="option-card__desc" style="display:block;">${esc(desc)}</span>` : ""}
        </span>
      </button>`;
  }

  function templateListHtml() {
    if (state.templates === null) {
      return `<div class="empty-state" style="padding:24px 16px;"><span class="muted">Loading templates…</span></div>`;
    }
    const errHtml = state.templatesError ? `
      <div class="errbar" style="display:flex; align-items:center; gap:10px;">
        <span style="flex:1;">Failed to load templates: ${esc(state.templatesError)}</span>
        <button class="btn btn--outlined" data-act="retry-templates" style="height:28px; flex:none;">Retry</button>
      </div>` : "";
    const q = state.search.trim().toLowerCase();
    const rows = state.templates
      .filter((p) => !q || String(p.name || "").toLowerCase().includes(q))
      .map((p) => {
        const chips = (previewMod ? previewMod.profileChips(p) : [])
          .map((c) => `<span class="chip ${esc(c.cls || "chip--neutral")}">${esc(c.label)}</span>`)
          .join("");
        return choiceRow({ choice: p.id, title: p.name || "(unnamed)", chipsHtml: chips });
      })
      .join("");
    const noMatch = q && !rows && state.templates.length
      ? `<div class="muted" style="margin:0 0 8px; font-size:13px;">No templates match "${esc(state.search.trim())}"</div>` : "";
    return `${errHtml}${rows}${noMatch}
      ${choiceRow({ choice: "manual", title: "Configure manually", desc: "Set every option yourself — optionally save it as a new template" })}`;
  }

  // Kept outside #pv-templates so typing (which re-renders only the rows)
  // never destroys the focused input.
  function templateSearchHtml() {
    if (!state.templates || state.templates.length <= SEARCH_THRESHOLD) return "";
    return `
      <div class="list-toolbar"><div class="search-field">
        <ha-icon icon="mdi:magnify"></ha-icon>
        <input class="input" id="pv-search" type="search" placeholder="Search templates…" value="${esc(state.search)}">
      </div></div>`;
  }

  function customizeSectionDefs() {
    const F = fieldsMod;
    const opts = (extra) => ({ esc, heading: false, wgProfiles: state.wgProfiles || [], ...extra });
    return [
      { id: "connection", label: "Connection", render: () => F.renderSectionHtml("connection", state.form, opts({ fields: CONNECTION_SET })) },
      { id: "appui", label: "App UI", render: () => F.renderSectionHtml("appui", state.form, opts({ fields: F.LIVE_KEYS })) },
      { id: "access", label: "Access Control", render: () => F.renderSectionHtml("access", state.form, opts({ fields: F.LIVE_KEYS })) },
      { id: "pushvpn", label: "Push & VPN", render: () => F.renderSectionHtml("pushvpn", state.form, opts({ fields: F.LIVE_KEYS })) },
      { id: "timing", label: "Timing & Security", render: () => F.renderSectionHtml("timing", state.form, opts({ fields: TIMING_SET })) },
    ];
  }

  function rerenderCustomizeSection(id) {
    if (!state || state.step !== "template" || !state.form || !fieldsMod) return;
    const def = customizeSectionDefs().find((s) => s.id === id);
    const body = refs.body.querySelector(`[data-cz-section="${id}"]`);
    if (def && body) body.innerHTML = def.render();
  }

  function expander(act, open, label) {
    return `
      <button class="btn btn--text" data-act="${esc(act)}" style="margin:6px 0; padding-left:0;">
        <ha-icon icon="mdi:chevron-down" style="transition:transform 0.15s; transform:rotate(${open ? "180deg" : "0deg"});"></ha-icon>
        ${esc(label)}
      </button>`;
  }

  function customizeHtml() {
    const sections = customizeSectionDefs()
      .map((s) => `<h5 style="margin:12px 0 6px;">${esc(s.label)}</h5><div data-cz-section="${esc(s.id)}">${s.render()}</div>`)
      .join("");
    return `
      <div id="pv-customize">${sections}</div>
      <div style="border-top:1px solid var(--casa-divider); margin-top:12px; padding-top:12px;">
        <label class="toggle">
          <input type="checkbox" data-pv="saveAsTemplate" ${state.saveAsTemplate ? "checked" : ""}>
          Also save as new template
        </label>
        ${state.saveAsTemplate ? `
          <div class="field" data-pv-field="newTemplateName" style="margin-top:8px;">
            <label>Template name *</label>
            <input class="input" data-pv="newTemplateName" value="${esc(state.newTemplateName)}" placeholder="e.g. Kitchen tablets">
          </div>` : ""}
      </div>`;
  }

  function advancedHtml() {
    const F = fieldsMod;
    return `
      <div id="pv-advanced">
        ${F.renderSectionHtml("connection", state.form, { esc, heading: false, fields: ADV_CONNECTION_SET })}
        ${F.renderSectionHtml("wifi", state.form, { esc, heading: false, fields: ADV_WIFI_SET })}
      </div>`;
  }

  function accountSummaryChip() {
    const who = state.accountMode === "new" ? (state.createdUser ? state.createdUser.username : trim(state.username)) : state.existingUsername;
    return `
      <div class="card" style="margin:0 0 16px;"><div class="card__body" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
        <ha-icon icon="mdi:cellphone" style="color:var(--casa-text-2); flex:none;"></ha-icon>
        <span style="font-size:14px;"><strong>${esc(trim(state.deviceName))}</strong> <span class="muted">→ ${esc(who)}</span></span>
        <span class="spacer"></span>
        <span class="chip ${state.accountMode === "new" ? "chip--app" : "chip--neutral"}">${state.accountMode === "new" ? "New account" : "Existing account"}</span>
      </div></div>`;
  }

  function renderTemplateStep() {
    const ready = !!(state.form && state.choice);
    return `
      ${accountSummaryChip()}
      ${state.deployError ? `<div class="errbar">${esc(state.deployError)}</div>` : ""}
      <h4 style="margin:0 0 10px; font-size:14px; font-weight:600;">Choose a template</h4>
      ${templateSearchHtml()}
      <div id="pv-templates">${templateListHtml()}</div>
      ${ready ? `
        <div style="border-top:1px solid var(--casa-divider); margin-top:8px;">
          ${expander("toggle-customize", state.customizeOpen, "Customize for this device")}
          <div ${state.customizeOpen ? "" : "hidden"}>${customizeHtml()}</div>
        </div>
        <div style="border-top:1px solid var(--casa-divider);">
          ${expander("toggle-advanced", state.advancedOpen, "Advanced (PIN, Wi-Fi join, sign out other devices)")}
          <div ${state.advancedOpen ? "" : "hidden"}>${advancedHtml()}</div>
        </div>` : ""}
      ${footer("Generate setup", "generate")}`;
  }

  function rerenderTemplateList() {
    const el = refs && refs.body.querySelector("#pv-templates");
    if (el) el.innerHTML = templateListHtml();
  }

  /* ---------- generate: create user → save template → provision ---------- */

  async function generate() {
    state.deployError = "";
    const F = fieldsMod;
    if (!state.choice || !state.form) {
      state.deployError = "Pick a template, or Configure manually.";
      return render();
    }
    if (!trim(state.form.host_url)) {
      state.deployError = "Host URL is required (under Customize for this device → Connection).";
      state.customizeOpen = true;
      return render();
    }
    if (state.saveAsTemplate && !trim(state.newTemplateName)) {
      state.deployError = "Name the new template, or untick 'Also save as new template'.";
      state.customizeOpen = true;
      render();
      return markFieldError("newTemplateName", "Required.");
    }

    const fields = F.collectFields(state.form, F.PROFILE_KEYS);
    const changed = logicMod.changedKeys(fields, state.baseline);
    const template = selectedTemplate();
    const token = mountToken;
    state.busy = true;
    render();

    // 1. Account.
    let account;
    if (state.accountMode === "new") {
      if (!state.createdUser) {
        const name = trim(state.deviceName);
        const username = trim(state.username);
        let resp;
        try {
          resp = unwrap(await api.createUser({ name, username, localOnly: true }));
        } catch (err) {
          resp = { error: ui.errMsg(err) };
        }
        if (token !== mountToken || !state) return;
        if (resp && resp.error) {
          state.busy = false;
          state.step = "account";
          state.accountError = String(resp.error);
          state.availability = null;
          return render();
        }
        state.createdUser = { name, username, password: resp && resp.password, user_id: resp && resp.user_id };
      }
      account = state.createdUser;
    } else {
      const a = casaAccounts().find((x) => x.username === state.existingUsername);
      if (!a) {
        state.busy = false;
        state.step = "account";
        state.accountError = "That account no longer exists — choose another.";
        return render();
      }
      account = { username: a.username, user_id: a.user_id };
    }

    // 2. Optional new template (skipped on retry via savedTemplateId).
    if (state.saveAsTemplate && !state.savedTemplateId) {
      try {
        const body = {
          name: trim(state.newTemplateName),
          fields: logicMod.templateFieldsToSave({
            fields,
            baseSetKeys: template ? Object.keys(template.fields || {}) : [],
            changed,
          }),
        };
        const res = await api.saveProvisionTemplate(body);
        if (token !== mountToken || !state) return;
        if (res && res.id) state.savedTemplateId = res.id;
        ui.toast(`Template '${trim(state.newTemplateName)}' created.`);
      } catch (err) {
        if (token !== mountToken || !state) return;
        state.busy = false;
        state.deployError = "Failed to save template: " + ui.errMsg(err);
        return render();
      }
    }

    // 3. Provision.
    const data = logicMod.buildProvisionRequest({
      fields,
      form: state.form,
      account,
      deviceName: trim(state.deviceName),
      lineage: logicMod.lineageFor({
        templateId: template && template.id,
        customized: changed.length > 0,
        savedTemplateId: state.saveAsTemplate ? state.savedTemplateId : null,
      }),
    });
    try {
      const resp = unwrap(await api.provision(data));
      if (token !== mountToken || !state) return;
      state.busy = false;
      if (resp && resp.error) {
        state.deployError = String(resp.error);
        return render();
      }
      state.result = resp;
      gotoStep("result");
    } catch (err) {
      if (token !== mountToken || !state) return;
      state.busy = false;
      state.deployError = ui.errMsg(err);
      render();
    }
  }

  /* ---------- step 3: done ---------- */

  function renderResultStep() {
    const r = state.result || {};
    return `
      <div style="text-align:center; margin-bottom:16px;">
        <ha-icon icon="mdi:check-circle" style="--mdc-icon-size:48px; color:var(--casa-success);"></ha-icon>
        <h3 style="margin:8px 0 0; font-size:16px; font-weight:600;">${esc(trim(state.deviceName))} is ready to set up</h3>
      </div>
      ${resultMod.setupResultHtml(r, { esc, fmtExpiry: ui.fmtExpiry })}
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

  function render() {
    if (!refs || !state) return;
    renderTabs();
    if (!fieldsMod || !utilsMod || !logicMod || !resultMod) {
      refs.body.innerHTML = `<div class="empty-state" style="padding:32px 16px;"><span class="muted">Loading…</span></div>`;
      return;
    }
    if (state.step === "account") refs.body.innerHTML = renderAccountStep();
    else if (state.step === "template") refs.body.innerHTML = renderTemplateStep();
    else refs.body.innerHTML = renderResultStep();

    if (state.step === "result") resultMod.bindCopyButtons(refs.body, ui);
    if (state.step === "template" && state.form) {
      const bind = (el) =>
        el && fieldsMod.bindFieldEvents(el, {
          values: state.form,
          onSectionRerender: (key) => rerenderCustomizeSection(KEY_SECTION[key]),
          esc,
        });
      bind(refs.body.querySelector("#pv-customize"));
      bind(refs.body.querySelector("#pv-advanced"));
      if (state.customizeOpen) ensureWgProfiles();
    }
  }

  function onBodyClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled || !(refs.body.contains(el) || refs.tabs.contains(el))) return;
    if (state.busy && (el.dataset.act === "goto-step" || el.dataset.act === "back")) return;
    switch (el.dataset.act) {
      case "goto-step":
        if (stepIndex(el.dataset.step) < stepIndex(state.step) && state.step !== "result") gotoStep(el.dataset.step);
        return;
      case "back":
        if (state.step === "template") gotoStep("account");
        return;
      case "mode":
        state.accountMode = el.dataset.mode;
        state.accountError = "";
        render();
        return;
      case "use-existing":
        state.accountMode = "existing";
        state.existingUsername = el.dataset.username;
        state.accountError = "";
        render();
        return;
      case "to-template":
        toTemplateStep();
        return;
      case "retry-templates":
        loadTemplates();
        return;
      case "choose":
        requestChoice(el.dataset.choice);
        return;
      case "toggle-customize":
        state.customizeOpen = !state.customizeOpen;
        render();
        return;
      case "toggle-advanced":
        state.advancedOpen = !state.advancedOpen;
        render();
        return;
      case "generate":
        generate();
        return;
      case "another":
        state = freshState();
        wgRequested = false;
        loadTemplates();
        render();
        if (mountedWithPresetPath) app.navigate("/provision", { replace: true });
        return;
      case "done":
        app.refresh();
        app.navigate("/");
        return;
    }
  }

  // View-private inputs carry data-pv (shared-renderer fields use data-key and
  // are handled by fieldsMod.bindFieldEvents). Typing never re-renders the
  // whole step; the template search re-renders only the rows below its
  // (separate) input, so focus is kept.
  function onBodyInput(e) {
    const t = e.target;
    if (t.id === "pv-search") {
      state.search = t.value;
      rerenderTemplateList();
      return;
    }
    switch (t.dataset && t.dataset.pv) {
      case "deviceName":
        state.deviceName = t.value;
        if (state.accountMode === "new" && !state.usernameEdited) {
          state.username = logicMod.suggestUsername(t.value, utilsMod.slugify);
          const u = refs.body.querySelector('[data-pv="username"]');
          if (u) u.value = state.username;
        }
        if (state.accountMode === "new") scheduleAvailability();
        return;
      case "username": {
        const lower = t.value.toLowerCase();
        if (lower !== t.value) t.value = lower;
        state.username = lower;
        // Clearing the field re-couples it to the device name.
        state.usernameEdited = !!lower;
        scheduleAvailability();
        return;
      }
      case "newTemplateName":
        state.newTemplateName = t.value;
        return;
    }
  }

  function onBodyChange(e) {
    const t = e.target;
    switch (t.dataset && t.dataset.pv) {
      case "existingUsername":
        state.existingUsername = t.value;
        return;
      case "saveAsTemplate":
        state.saveAsTemplate = !!t.checked;
        if (state.saveAsTemplate && !trim(state.newTemplateName)) {
          const base = selectedTemplate();
          state.newTemplateName = base ? `${base.name} (copy)` : "";
        }
        render();
        return;
    }
  }

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
      wgRequested = false;
      const presetUsername = (params && params.username) || "";
      const presetTemplateId = (params && params.templateId) || "";
      mountedWithPresetPath = !!(presetUsername || presetTemplateId);

      el.innerHTML = `
        <div class="page">
          <div class="tabs tabs--steps" id="pv-tabs"></div>
          <div id="pv-body"></div>
        </div>`;
      refs = { tabs: el.querySelector("#pv-tabs"), body: el.querySelector("#pv-body") };
      refs.tabs.addEventListener("click", onBodyClick); // step tabs live outside the body
      refs.body.addEventListener("click", onBodyClick);
      refs.body.addEventListener("input", onBodyInput);
      refs.body.addEventListener("change", onBodyChange);
      window.addEventListener("beforeunload", onBeforeUnload);
      render();

      try {
        const [fields, preview, utils, logic, result] = await Promise.all([
          fieldsMod || app.loadModule("views/profile-fields.js"),
          previewMod || app.loadModule("payload-preview.js"),
          utilsMod || app.loadModule("views/username-utils.js"),
          logicMod || app.loadModule("views/provision-logic.js"),
          resultMod || app.loadModule("views/provision-result.js"),
        ]);
        if (token !== mountToken) return;
        fieldsMod = fields;
        previewMod = preview;
        utilsMod = utils;
        logicMod = logic;
        resultMod = result;
      } catch (err) {
        if (token !== mountToken) return;
        refs.body.innerHTML = `<div class="errbar">Failed to load: ${esc(ui.errMsg(err))}</div>`;
        return;
      }

      // Deep links can land before the first summary poll.
      if (!app.summary()) await app.refresh();
      if (token !== mountToken || !state) return;
      if (presetUsername) {
        if (casaAccounts().some((a) => a.username === presetUsername)) {
          state.accountMode = "existing";
          state.existingUsername = presetUsername;
        } else {
          ui.toast("That account no longer exists.", { error: true });
        }
      }
      render();

      await loadTemplates();
      if (token !== mountToken || !state) return;
      if (presetTemplateId) {
        if ((state.templates || []).some((t) => t && t.id === presetTemplateId)) {
          state.choice = presetTemplateId;
          state.form = seedForm(selectedTemplate());
          state.baseline = fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS);
        } else {
          ui.toast("That provision template no longer exists.", { error: true });
        }
      }
      render();
    },

    unmount() {
      mountToken++;
      clearTimeout(availTimer);
      window.removeEventListener("beforeunload", onBeforeUnload);
      refs = null;
      state = null;
    },

    // ui.showConfirm has no cancel callback, so build the dialog with openModal
    // and resolve false on any dismissal (X, overlay click, Escape) by watching
    // for the overlay leaving the DOM.
    confirmLeave() {
      if (!dirty()) return true;
      const createdName = state.createdUser ? state.createdUser.username || state.createdUser.name || "" : "";
      const bodyText = state.createdUser
        ? `This device hasn't been provisioned yet. The account <strong>${ui.esc(createdName)}</strong> was already created and is kept (remove it from Accounts if unwanted). Discard this setup and leave?`
        : "This device hasn't been provisioned yet. Discard this setup and leave?";
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
            `<p style="margin:0; font-size:14px; line-height:1.5;">${bodyText}</p>`,
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
