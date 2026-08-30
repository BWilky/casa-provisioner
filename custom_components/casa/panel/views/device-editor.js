// Casa admin panel — device editor view. Three-pane editor (editor-shell.js)
// for a single device record: overview/info grid, session expiration, push,
// VPN/WireGuard queueing, pending updates, and the danger zone. All action
// logic (confirm wording, queue_update request shapes, expiration stacking) is
// ported faithfully from the legacy Device Inspector. Everything arrives via
// the `app` context (no static sibling imports).

// Record keys in the summary's documented order — the code pane and the
// change detector both use this so the JSON rendering is stable.
const RECORD_KEYS = [
  "username", "device_id", "ip", "last_seen", "push_registered", "orphaned",
  "stale", "native", "alias", "registered_at", "push_token", "last_12_token",
  "refresh_token_id", "app_version", "wireguard_configured",
  "wireguard_connected", "current_url", "provisioned_at", "expires_at",
  "expires_at_override", "expires_at_override_set_at", "pending_updates",
  "pending_update_list", "provisioning_fields", "provisioning_reported_at",
  "provisioning_pending_push",
];

function orderedRecord(d) {
  const out = {};
  for (const k of RECORD_KEYS) out[k] = d && d[k] !== undefined ? d[k] : null;
  return out;
}

/* ---------- queue_update request builders (ported from legacy
   _pushDeviceWg / _pushDeviceProfile; shared with openPushModal) ---------- */

function buildWireguardRequest(deviceId, profileId, profiles, sendPush, notifyPush) {
  const req = {
    device_id: deviceId,
    update_type: "wireguard",
    send_update_push: sendPush,
    notify_push: notifyPush,
  };
  let label;
  if (profileId) {
    const profile = (profiles || []).find((p) => p.id === profileId);
    if (!profile) throw new Error("Selected WireGuard profile not found.");
    req.action = "update";
    req.wireguard_config = profile.config;
    req.wireguard_excluded_wifi = profile.excluded_wifi || "";
    if (notifyPush) {
      req.title = "VPN profile updated";
      req.message = `A new WireGuard profile (${profile.alias}) is available.`;
    }
    label = `WireGuard profile '${profile.alias}'`;
  } else {
    req.action = "revoke";
    if (notifyPush) {
      req.title = "VPN access revoked";
      req.message = "Your WireGuard VPN profile has been removed.";
    }
    label = "WireGuard revoke";
  }
  return { req, label };
}

function buildProfileRequest(deviceId, profileId, profiles, sendPush, notifyPush) {
  const profile = (profiles || []).find((p) => p.id === profileId);
  if (!profile) throw new Error("Selected provision template not found.");
  const req = {
    device_id: deviceId,
    update_type: "profile",
    action: "update",
    profile_id: profileId,
    send_update_push: sendPush,
    notify_push: notifyPush,
  };
  if (notifyPush) {
    req.title = "Settings updated";
    req.message = `New settings from template '${profile.name}' were applied.`;
  }
  return { req, label: `Template '${profile.name}'` };
}

const queueSuccessText = (label, res) => {
  const extras = [];
  if (res.skipped) extras.push(`${res.skipped} without push`);
  if (res.not_found) extras.push(`${res.not_found} not found`);
  return `${label} queued for ${res.queued} device${res.queued === 1 ? "" : "s"}` +
    `${extras.length ? ` (${extras.join(", ")})` : ""} — push sending in background.`;
};

/* ---------- shared kebab modal (used by the device list) ---------- */

// openPushModal(app, device, kind) — kind: "wireguard" | "profile". Same
// select + toggles + queue logic as the editor's VPN / Pending Updates cards.
export async function openPushModal(app, device, kind) {
  const { api, ui } = app;
  const esc = ui.esc;
  const isWg = kind === "wireguard";

  let profiles;
  try {
    const res = isWg ? await api.getWireguardProfiles() : await api.getProvisionTemplates();
    profiles = (res && res.profiles) || [];
  } catch (err) {
    ui.toast(`Failed to load ${isWg ? "profiles" : "templates"}: ` + ui.errMsg(err), { error: true });
    return;
  }

  const options = profiles
    .map((p) => `<option value="${esc(p.id)}">${esc(isWg ? p.alias : p.name)}</option>`)
    .join("");
  const body = document.createElement("div");
  body.innerHTML = `
    <div class="field">
      <label>${isWg ? "WireGuard Profile" : "Provision template"}</label>
      <select class="select" data-pm-profile style="width:100%;">
        <option value="">${isWg ? "-- None / Revoke VPN --" : "-- Select a template --"}</option>
        ${options}
      </select>
    </div>
    ${isWg ? "" : '<div class="muted" style="font-size:12px; margin:0 0 10px;">One-time apply of the template\'s set device fields — the device does not follow the template afterwards.</div>'}
    <label class="toggle"><input type="checkbox" data-pm-send> Send update via push</label>
    <label class="toggle"><input type="checkbox" data-pm-notify> Notify via push</label>
    <div class="field__error" data-pm-err hidden></div>`;

  ui.openModal({
    title: `${isWg ? "Queue VPN update" : "Apply template"} — ${device.alias || device.device_id}`,
    bodyEl: body,
    buttons: [
      { label: "Cancel", variant: "text" },
      {
        label: isWg ? "Queue VPN Update" : "Apply Template",
        variant: "primary",
        onClick: async () => {
          const errEl = body.querySelector("[data-pm-err]");
          errEl.hidden = true;
          const profileId = body.querySelector("[data-pm-profile]").value;
          const sendPush = body.querySelector("[data-pm-send]").checked;
          const notifyPush = body.querySelector("[data-pm-notify]").checked;
          if (!isWg && !profileId) {
            errEl.hidden = false;
            errEl.textContent = "Select a template first.";
            return false;
          }
          try {
            const { req, label } = isWg
              ? buildWireguardRequest(device.device_id, profileId, profiles, sendPush, notifyPush)
              : buildProfileRequest(device.device_id, profileId, profiles, sendPush, notifyPush);
            const res = await api.queueUpdate(req);
            ui.toast(queueSuccessText(label, res));
            app.refresh();
          } catch (err) {
            errEl.hidden = false;
            errEl.textContent = "Failed to queue: " + ui.errMsg(err);
            return false;
          }
        },
      },
    ],
  });
}

// openReauthModal(app, device) — reauthenticate a device with a new
// username/password: pick an existing (non-admin) user or create one inline,
// auto-generate or supply the password, deliver via encrypted push and/or the
// durable heartbeat queue. Generated passwords get the same non-dismissable
// one-time reveal the Accounts view uses.
export async function openReauthModal(app, device) {
  const { api, ui } = app;
  const esc = ui.esc;

  let sessionUsers;
  let usernameUtils = null;
  try {
    const [res, utils] = await Promise.all([
      api.getSessions(),
      app.loadModule("views/username-utils.js").catch(() => null),
    ]);
    usernameUtils = utils; // null → degrade to manual username entry
    sessionUsers = ((res && res.users) || []).filter(
      (u) => !u.is_admin && !u.is_owner && u.is_active !== false
    );
  } catch (err) {
    ui.toast("Failed to load users: " + ui.errMsg(err), { error: true });
    return;
  }

  const currentUsername = device.username || "";
  const userOptions = sessionUsers
    .map((u) => {
      const uname = u.username || u.name;
      const label = u.username && u.username !== u.name ? `${u.name} (${u.username})` : u.name;
      const selected = uname && uname.toLowerCase() === currentUsername.toLowerCase() ? " selected" : "";
      const chip = u.casa_managed ? " · casa" : "";
      return `<option value="${esc(u.user_id)}" data-username="${esc(uname || "")}"${selected}>${esc(label)}${chip}</option>`;
    })
    .join("");

  const body = document.createElement("div");
  body.innerHTML = `
    <div class="field">
      <label>Target account</label>
      <label class="toggle"><input type="radio" name="ra-mode" value="existing" checked> Existing user</label>
      <label class="toggle"><input type="radio" name="ra-mode" value="create"> Create new user</label>
    </div>
    <div data-ra-existing>
      <div class="field">
        <select class="select" data-ra-user style="width:100%;">${userOptions}</select>
      </div>
    </div>
    <div data-ra-create hidden>
      <div class="field">
        <label>Full Name *</label>
        <input class="input" data-ra-name placeholder="e.g. John Doe">
      </div>
      <div class="field">
        <label>Username *</label>
        <input class="input" data-ra-username placeholder="e.g. john" autocapitalize="none" autocomplete="off">
        <div class="field__help">Auto-generated from the name — edit to override.</div>
        <div data-ra-avail style="margin-top:4px;"></div>
      </div>
    </div>
    <div class="field">
      <label>Password</label>
      <input class="input" data-ra-password type="password" placeholder="••••••••" autocomplete="new-password">
      <div class="field__help" data-ra-pwhelp>Leave blank to auto-generate a secure password.</div>
    </div>
    <label class="toggle" data-ra-scramble-row hidden>
      <input type="checkbox" data-ra-scramble checked>
      <span data-ra-scramble-label></span>
    </label>
    <label class="toggle">
      <input type="checkbox" data-ra-push ${device.push_registered ? "checked" : "disabled"}>
      Send via encrypted push${device.push_registered ? "" : " (device has no push registration)"}
    </label>
    <div class="muted" style="font-size:12px; margin:6px 0 10px;">
      Always queued durably — applied on the device's next check-in if the push is missed.
      Older app builds ignore this command; a stuck request can be cancelled from Pending Updates.
    </div>
    <div style="display:flex; gap:8px; align-items:center; margin:0 0 10px; padding:10px 12px; border-radius:8px; background:color-mix(in srgb, var(--casa-warning) 16%, transparent); color:var(--casa-warning); font-size:13px;">
      <ha-icon icon="mdi:alert" style="--mdc-icon-size:18px; flex:none;"></ha-icon>
      <span>The device will log out and sign back in as the selected user. If the login fails, the device resets and must be re-provisioned.</span>
    </div>
    <div class="field__error" data-ra-err hidden></div>`;

  const existingWrap = body.querySelector("[data-ra-existing]");
  const createWrap = body.querySelector("[data-ra-create]");
  const userSelect = body.querySelector("[data-ra-user]");
  const nameInput = body.querySelector("[data-ra-name]");
  const usernameInput = body.querySelector("[data-ra-username]");
  const passwordInput = body.querySelector("[data-ra-password]");
  const pwHelp = body.querySelector("[data-ra-pwhelp]");
  const scrambleRow = body.querySelector("[data-ra-scramble-row]");
  const scrambleLabel = body.querySelector("[data-ra-scramble-label]");
  const errEl = body.querySelector("[data-ra-err]");

  // Auto-slug the username from the name + live availability chip (shared
  // with the guided wizard). Falls back to plain lowercase-on-input if the
  // module failed to load; the submit-time checkUsername stays authoritative.
  if (usernameUtils) {
    usernameUtils.attachUsernameField({
      nameInput,
      usernameInput,
      hintEl: body.querySelector("[data-ra-avail]"),
      checkUsername: (u, n) => api.checkUsername(u, n),
    });
  } else {
    usernameInput.addEventListener("input", () => {
      usernameInput.value = usernameInput.value.toLowerCase();
    });
  }

  const mode = () => body.querySelector("input[name='ra-mode']:checked").value;
  const selectedUsername = () => {
    if (mode() === "create") return usernameInput.value.trim();
    const opt = userSelect.selectedOptions[0];
    return (opt && opt.dataset.username) || "";
  };
  const isSwitching = () => {
    const target = selectedUsername();
    return !!currentUsername && !!target && target.toLowerCase() !== currentUsername.toLowerCase();
  };

  function refreshDynamic() {
    const creating = mode() === "create";
    existingWrap.hidden = creating;
    createWrap.hidden = !creating;
    pwHelp.textContent = creating
      ? "Leave blank to auto-generate a secure password."
      : "Leave blank to auto-generate. A typed password must already be that user's password — it will not be changed.";
    const switching = isSwitching();
    scrambleRow.hidden = !switching;
    if (switching) {
      scrambleLabel.textContent = `Also scramble '${currentUsername}'s password and revoke their other sessions`;
    }
  }
  for (const radio of body.querySelectorAll("input[name='ra-mode']")) {
    radio.addEventListener("change", refreshDynamic);
  }
  userSelect.addEventListener("change", refreshDynamic);
  usernameInput.addEventListener("input", refreshDynamic);
  // Typing the name can rewrite the username via auto-slug (no input event
  // fires on programmatic writes), so re-evaluate the scramble row here too.
  nameInput.addEventListener("input", refreshDynamic);
  refreshDynamic();

  // Non-dismissable one-time reveal for generated passwords — same UX as
  // the Accounts view's showCredentials.
  function showRevealModal(username, password) {
    const rbody = document.createElement("div");
    const row = (label, value) => `
      <div class="field-row" style="margin-bottom:10px;">
        <span style="width:88px; flex:none; font-weight:600; font-size:13px;">${esc(label)}</span>
        <span class="mono" style="flex:1; min-width:0; word-break:break-all;">${esc(value)}</span>
        <button class="btn btn--outlined" style="height:28px; padding:0 10px; font-size:12px;" data-copy="${esc(value)}">Copy</button>
      </div>`;
    rbody.innerHTML = `
      <div style="text-align:center; margin-bottom:16px;">
        <ha-icon icon="mdi:check-circle" style="--mdc-icon-size:48px; color:var(--casa-success);"></ha-icon>
        <h4 style="margin:8px 0 0; font-size:16px;">Reauthentication queued</h4>
      </div>
      ${row("Username", username)}
      ${row("Password", password)}
      <div style="display:flex; gap:8px; align-items:center; margin-top:14px; padding:10px 12px; border-radius:8px; background:color-mix(in srgb, var(--casa-warning) 16%, transparent); color:var(--casa-warning); font-size:13px;">
        <ha-icon icon="mdi:alert" style="--mdc-icon-size:18px; flex:none;"></ha-icon>
        <span>This password is shown only once.</span>
      </div>`;
    for (const btn of rbody.querySelectorAll("[data-copy]")) {
      ui.bindCopyButton(btn, () => btn.dataset.copy);
    }
    ui.openModal({
      title: "One-time credentials",
      bodyEl: rbody,
      dismissable: false,
      buttons: [{ label: "Done", variant: "primary", onClick: () => { app.refresh(); } }],
    });
  }

  ui.openModal({
    title: `Reauthenticate — ${device.alias || device.device_id}`,
    bodyEl: body,
    buttons: [
      { label: "Cancel", variant: "text" },
      {
        label: "Reauthenticate",
        variant: "primary",
        onClick: async (btn) => {
          errEl.hidden = true;
          const creating = mode() === "create";
          const password = passwordInput.value.trim();
          const req = {
            device_id: device.device_id,
            password: password || undefined,
            scramble_old: !scrambleRow.hidden && body.querySelector("[data-ra-scramble]").checked,
            send_update_push: body.querySelector("[data-ra-push]").checked,
          };
          if (creating) {
            const name = nameInput.value.trim();
            const username = usernameInput.value.trim().toLowerCase();
            if (!name || !username) {
              errEl.hidden = false;
              errEl.textContent = "Name and Username are required.";
              return false;
            }
            req.create_user = { name, username };
          } else {
            const userId = userSelect.value;
            if (!userId) {
              errEl.hidden = false;
              errEl.textContent = "Select a user first.";
              return false;
            }
            req.user_id = userId;
          }
          btn.disabled = true;
          btn.textContent = "Queueing…";
          try {
            if (creating) {
              const avail = await api.checkUsername(req.create_user.username, req.create_user.name);
              if (avail && avail.available === false) {
                errEl.hidden = false;
                errEl.textContent = avail.username_conflict
                  ? "That username is already taken."
                  : "That name is already taken.";
                btn.disabled = false;
                btn.textContent = "Reauthenticate";
                return false;
              }
            }
            const res = await api.reauthDevice(req);
            const deliveredNote = res.pushed
              ? "encrypted push sent"
              : res.push_skipped
                ? "no push possible — queued for next check-in"
                : "queued for next check-in";
            if (res.password) {
              showRevealModal(res.username, res.password);
            } else {
              ui.toast(`Reauthentication to '${res.username}' queued (${deliveredNote}).`);
            }
            app.refresh();
          } catch (err) {
            errEl.hidden = false;
            errEl.textContent = "Failed: " + ui.errMsg(err);
            btn.disabled = false;
            btn.textContent = "Reauthenticate";
            return false;
          }
        },
      },
    ],
  });
}

/* ---------- view ---------- */

export function createView(app) {
  const { api, ui, store } = app;
  const esc = ui.esc;

  /* ----- state ----- */
  let mounted = false;
  let leaving = false; // danger action in flight — suppress "record gone" handling
  let missingHandled = false;
  let deviceId = null;
  let device = null;
  let host = null; // .page--flush container
  let shellMod = null; // editor-shell module
  let fieldsMod = null; // profile-fields.js — shared field schema/renderer, loaded lazily
  let fieldsModLoading = false;
  let shell = null; // editor shell handle
  let active = "overview";
  let wgProfiles = null; // null = not loaded yet (loaded lazily, once)
  let wgLoading = false;
  let ppProfiles = null;
  let ppLoading = false;
  let provisioningEditing = false; // "Force Device Changes" active?
  let provisioningDraft = null; // editable copy of provisioning fields while editing
  let provisioningSubSection = "connection"; // sub-nav within the Provisioning section
  let msgs = freshMsgs();

  function freshMsgs() {
    return { alias: {}, session: {}, push: {}, vpn: {}, updates: {}, pending: {}, danger: {}, provisioning: {} };
  }

  /* ----- pure helpers ----- */

  const findDevice = (id) =>
    ((app.summary() && app.summary().devices) || []).find((d) => d.device_id === id);
  const displayName = (d) => (d && (d.alias || String(d.device_id || "").slice(0, 8))) || "";
  const nowStr = () =>
    new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

  function statusChipsHtml(d) {
    const chips = [];
    if (d.orphaned) chips.push('<span class="badge badge--orphan">orphan</span>');
    if (d.stale) chips.push('<span class="badge badge--stale">stale</span>');
    if (d.native) chips.push('<span class="chip chip--neutral">native</span>');
    if (d.push_registered && !d.orphaned && !d.stale) chips.push('<span class="chip chip--ok">ok</span>');
    return chips.join("");
  }

  // Tri-state VPN chips — preserve "—" when the value is unknown (null).
  const triChip = (v, yesLabel, noLabel, noCls) =>
    v === true
      ? `<span class="chip chip--ok">${yesLabel}</span>`
      : v === false
        ? `<span class="chip ${noCls}">${noLabel}</span>`
        : "—";

  function pendingExpiryBadge(d) {
    if (d.expires_at_override == null) return "";
    const val = d.expires_at_override === 0 ? "Never" : ui.fmtExpiry(d.expires_at_override);
    return ` <span class="badge badge--pending" title="Applied on the device's next heartbeat">pending: ${esc(val)}</span>`;
  }

  function msgHtml(m) {
    if (!m) return "";
    let out = "";
    if (m.error) out += `<div class="field__error" style="margin-top:6px;">${esc(m.error)}</div>`;
    if (m.success)
      out += `<div style="font-size:12px; color:var(--casa-success); margin-top:6px;">${esc(m.success)}</div>`;
    return out;
  }

  /* ----- lazy profile lists (fetched once, kept for the session) ----- */

  function ensureWgProfiles() {
    if (wgProfiles !== null || wgLoading) return;
    wgLoading = true;
    api
      .getWireguardProfiles()
      .then((res) => { wgProfiles = (res && res.profiles) || []; })
      .catch(() => { wgProfiles = []; })
      .finally(() => {
        wgLoading = false;
        if (mounted && active === "vpn") renderSection();
      });
  }

  function ensurePpProfiles() {
    if (ppProfiles !== null || ppLoading) return;
    ppLoading = true;
    api
      .getProvisionTemplates()
      .then((res) => { ppProfiles = (res && res.profiles) || []; })
      .catch(() => { ppProfiles = []; })
      .finally(() => {
        ppLoading = false;
        if (mounted && active === "updates") renderSection();
      });
  }

  function ensureFieldsMod() {
    if (fieldsMod || fieldsModLoading) return;
    fieldsModLoading = true;
    app
      .loadModule("views/profile-fields.js")
      .then((mod) => { fieldsMod = mod; })
      .catch(() => { /* renderProvisioning shows a fallback while fieldsMod is null */ })
      .finally(() => {
        fieldsModLoading = false;
        if (mounted && active === "provisioning") renderSection();
      });
  }

  /* ----- shell / lifecycle plumbing ----- */

  function handleMissing() {
    if (missingHandled) return;
    missingHandled = true;
    ui.showInfo({ title: "Device not found", message: "Device record no longer exists" });
    app.navigate("/", { replace: true });
  }

  async function init() {
    if (!shellMod) {
      try {
        shellMod = await app.loadModule("editor-shell.js");
      } catch (err) {
        if (mounted && host)
          host.innerHTML = `<div class="page"><div class="errbar">Failed to load editor shell: ${esc(ui.errMsg(err))}</div></div>`;
        return;
      }
    }
    if (mounted) tryBuild();
  }

  function tryBuild() {
    if (shell || !mounted || !shellMod) return;
    if (!app.summary()) {
      host.innerHTML = `<div class="page"><div class="empty-state"><span class="muted">Loading…</span></div></div>`;
      return; // onSummary will retry
    }
    const d = findDevice(deviceId);
    if (!d) {
      handleMissing();
      return;
    }
    device = d;
    buildShell();
  }

  function buildShell() {
    shell = shellMod.renderEditorShell(host, {
      ui,
      store,
      storeKey: "editor.device",
      title: `Editing ${displayName(device)}`,
      titleChips: statusChipsHtml(device),
      navHint: "Changes apply directly to this device record.",
      sections: [
        { id: "overview", label: "Overview", icon: "mdi:cellphone" },
        { id: "provisioning", label: "Provisioning", icon: "mdi:clipboard-text-outline" },
        { id: "session", label: "Session & Expiration", icon: "mdi:timer-sand" },
        { id: "push", label: "Push & Notifications", icon: "mdi:bell-outline" },
        { id: "vpn", label: "VPN / WireGuard", icon: "mdi:shield-lock-outline" },
        { id: "updates", label: "Pending Updates", icon: "mdi:tray-full" },
        { id: "danger", label: "Danger Zone", icon: "mdi:alert-outline", danger: true },
      ],
      activeSection: active,
      onSelectSection: (id) => {
        active = id;
        renderSection();
      },
      codeTitle: "device.json — read-only",
      codeCaption: `updated ${nowStr()}`,
      onCopyCode: () => JSON.stringify(orderedRecord(device), null, 2),
      footerHtml: `<span class="muted" style="font-size:12px;">All changes apply immediately — there is no separate save step.</span>`,
    });
    renderCode();
    updateNavBadges();
    renderSection();
  }

  function renderCode() {
    if (!shell) return;
    shellMod.renderJsonCode(shell.codeEl, orderedRecord(device));
    shell.setCodeCaption(`updated ${nowStr()}`);
  }

  function updateNavBadges() {
    if (!shell) return;
    shell.setBadge("provisioning", device.provisioning_pending_push ? "pending" : null, "badge--pending");
    shell.setBadge("session", device.expires_at_override != null ? "pending" : null, "badge--pending");
    shell.setBadge(
      "updates",
      Number(device.pending_updates) > 0 ? String(device.pending_updates) : null,
      "badge--count"
    );
    // Push section: small red dot when the device has no push registration.
    // Injected directly (the shell's badge slot escapes its text content).
    const pushBtn = host && host.querySelector('.editor__nav [data-section="push"]');
    if (pushBtn) {
      pushBtn.querySelector(".status-dot")?.remove();
      if (!device.push_registered)
        pushBtn.insertAdjacentHTML(
          "beforeend",
          '<span class="status-dot status-dot--offline" style="margin-left:auto; flex:none;" title="Push not registered"></span>'
        );
    }
  }

  /* ----- section renderers ----- */

  function renderSection() {
    if (!shell || !mounted) return;
    const RENDERERS = {
      overview: renderOverview,
      provisioning: renderProvisioning,
      session: renderSession,
      push: renderPush,
      vpn: renderVpn,
      updates: renderUpdates,
      danger: renderDanger,
    };
    (RENDERERS[active] || renderOverview)();
  }

  function infoGridRowsHtml(d) {
    const label = (text) => `<strong style="color:var(--casa-text-2); font-weight:500;">${esc(text)}</strong>`;
    const rows = [];
    rows.push(
      label("Device ID"),
      `<span><span class="mono">${esc(d.device_id)}</span> <button class="btn btn--text" id="dev-copy-id" style="padding:0 8px; height:22px; font-size:12px;">Copy</button></span>`
    );
    rows.push(
      label("User"),
      `<span>${esc(d.username || "—")}${d.native ? ' <span class="chip chip--neutral">native</span>' : ""}</span>`
    );
    rows.push(label("IP"), `<span class="mono">${esc(d.ip || "—")}</span>`);
    rows.push(label("Registered"), `<span>${esc(ui.fmtTime(d.registered_at))}</span>`);
    rows.push(
      label("Last seen"),
      `<span title="${esc(ui.fmtTime(d.last_seen))}">${esc(ui.relTime(d.last_seen))}</span>`
    );
    rows.push(label("App version"), `<span>${d.app_version ? esc(d.app_version) : "—"}</span>`);
    rows.push(
      label("Token suffix"),
      `<span>${d.last_12_token ? `<span class="mono">…${esc(d.last_12_token)}</span>` : "—"}</span>`
    );
    rows.push(
      label("Active URL"),
      `<span>${
        d.current_url
          ? `<a href="${esc(d.current_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--casa-primary); text-decoration:none; word-break:break-all;">${esc(d.current_url)}</a>`
          : "—"
      }</span>`
    );
    rows.push(
      label("Provisioned at"),
      `<span>${d.provisioned_at ? esc(ui.fmtTime(d.provisioned_at)) : "—"}</span>`
    );
    rows.push(
      label("Expires at"),
      `<span>${esc(ui.fmtExpiry(d.expires_at))}${pendingExpiryBadge(d)}</span>`
    );
    rows.push(
      label("VPN Installed"),
      `<span>${triChip(d.wireguard_configured, "Installed", "Not installed", "chip--neutral")}</span>`
    );
    rows.push(
      label("VPN Connected"),
      `<span>${triChip(d.wireguard_connected, "Connected", "Disconnected", "chip--warn")}</span>`
    );
    return rows.join("");
  }

  function bindInfoGrid(grid) {
    const copyBtn = grid.querySelector("#dev-copy-id");
    if (copyBtn) ui.bindCopyButton(copyBtn, () => device.device_id);
  }

  function renderOverview() {
    const d = device;
    shell.formEl.innerHTML = `
      <h2>Overview</h2>
      <p class="editor__form-desc">Device record and identity at a glance.</p>
      <div class="card section-card"><div class="card__body">
        <div id="dev-ov-grid" style="display:grid; grid-template-columns:auto 1fr; gap:8px 16px; font-size:13px; align-items:baseline;">
          ${infoGridRowsHtml(d)}
        </div>
      </div></div>
      <div class="card section-card"><div class="card__body">
        <h5>Device alias</h5>
        <div class="field" style="margin-bottom:0;">
          <label>Device Alias (Friendly Name)</label>
          <div class="field-row">
            <input class="input" id="dev-alias" value="${esc(d.alias || "")}" placeholder="e.g. Bryce's iPad">
            <button class="btn btn--primary" id="dev-save-alias">Save</button>
          </div>
          ${msgHtml(msgs.alias)}
        </div>
      </div></div>`;

    bindInfoGrid(shell.formEl.querySelector("#dev-ov-grid"));
    shell.formEl.querySelector("#dev-save-alias").addEventListener("click", saveAlias);
  }

  async function saveAlias() {
    const input = shell.formEl.querySelector("#dev-alias");
    if (!input) return;
    const alias = input.value.trim();
    msgs.alias = {};
    try {
      await api.updateDevice({ device_id: device.device_id, alias });
      ui.toast("Alias updated successfully.");
      await app.refresh();
    } catch (err) {
      msgs.alias = { error: "Failed: " + ui.errMsg(err) };
    }
    if (mounted && active === "overview") renderSection();
  }

  // Read-only by default, sourced from the device's self-reported state (the
  // only durable source of "what this device actually has" — see
  // CasaDeviceProfileReportView). "Force Device Changes" swaps into an
  // editable copy of the same fields/markup the template editor uses
  // (profile-fields.js) — saving pushes the changes straight to this one
  // device (one-time; templates never sync automatically).
  function renderProvisioning() {
    const d = device;
    ensureFieldsMod();
    if (!fieldsMod) {
      shell.formEl.innerHTML = `
        <h2>Provisioning</h2>
        <p class="editor__form-desc">What this specific device is currently configured with.</p>
        <div class="empty-state" style="padding:24px 0;"><span class="muted">Loading…</span></div>`;
      return;
    }
    ensureWgProfiles(); // the Push & VPN group's wireguard_profile_id select wants this

    // LIVE scope only: process-only settings (password, PIN, provisioning
    // timeout, scramble, Wi-Fi join, ...) are one-time wizard inputs, not
    // ongoing device state — they're neither shown here nor pushable.
    const liveKeys = fieldsMod.LIVE_KEYS;
    const baseline = {};
    for (const key of liveKeys) {
      baseline[key] =
        d.provisioning_fields && d.provisioning_fields[key] !== undefined
          ? d.provisioning_fields[key]
          : fieldsMod.DEFAULTS[key];
    }
    const values = provisioningEditing ? provisioningDraft : baseline;
    const hasReport = !!d.provisioning_reported_at;

    const freshness = d.provisioning_pending_push
      ? '<span class="badge badge--pending" title="Applied on the device\'s next heartbeat or refresh">Pending device confirmation</span>'
      : hasReport
        ? `<span class="muted" style="font-size:12px;">Live as of ${esc(ui.fmtTime(d.provisioning_reported_at))}</span>`
        : '<span class="muted" style="font-size:12px;">Never reported yet — awaiting first check-in</span>';

    // Captured at provision time (see async_register_device's
    // pending_profile_by_user bridge) — best-effort; devices that predate
    // this feature, or whose registration missed the 30-min pending window,
    // simply won't have this and the line is omitted.
    const provisionedFromHtml = d.provisioning_profile_name
      ? `<div class="muted" style="font-size:12px; margin-top:6px;">Provisioned from template: ${
          d.provisioning_profile_id
            ? `<a href="#" data-prov-profile-link="${esc(d.provisioning_profile_id)}" style="color:var(--casa-primary);">${esc(d.provisioning_profile_name)}</a>`
            : esc(d.provisioning_profile_name)
        }</div>`
      : "";

    const provSections = fieldsMod.sectionsFor(liveKeys);
    // A previously-persisted sub-section (e.g. "wifi") may no longer exist
    // under the live-only scope — snap to the first available one.
    if (!provSections.some((s) => s.id === provisioningSubSection)) {
      provisioningSubSection = provSections[0]?.id || "connection";
    }
    const subNav = provSections
      .map(
        (s) =>
          `<button class="btn ${s.id === provisioningSubSection ? "btn--primary" : "btn--outlined"}" data-prov-tab="${esc(s.id)}" style="font-size:12px; padding:4px 10px; height:28px;">${esc(s.label)}</button>`
      )
      .join("");

    const actionsHtml = provisioningEditing
      ? `<button class="btn btn--text" id="dev-prov-cancel">Cancel</button>
         <button class="btn btn--primary" id="dev-prov-save">Save &amp; Push to Device</button>`
      : `<button class="btn btn--outlined" id="dev-prov-refresh">Refresh Now</button>
         <button class="btn btn--danger-outlined" id="dev-prov-force">Force Device Changes</button>`;

    // While waiting for the device's first self-report, showing the
    // DEFAULTS-filled field form would look like real data — show a plain
    // explanation instead. Force Device Changes still works in this state
    // (it doesn't depend on a report having arrived), so editing mode
    // always shows the real form regardless of report status.
    const bodyHtml = !provisioningEditing && !hasReport
      ? `<div class="empty-state" style="padding:24px 0;">
           <ha-icon icon="mdi:clock-outline"></ha-icon>
           <div>This device hasn't reported its provisioning details yet.</div>
           <p class="field__help" style="max-width:420px; margin:8px auto 0;">
             It will check in automatically on its next cycle, or use
             "Refresh Now" below to request an immediate report.
           </p>
         </div>`
      : `<div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:10px;">${subNav}</div>
         <div id="dev-prov-fields">${fieldsMod.renderSectionHtml(provisioningSubSection, values, { readOnly: !provisioningEditing, wgProfiles: wgProfiles || [], esc, fields: liveKeys })}</div>`;

    shell.formEl.innerHTML = `
      <h2>Provisioning</h2>
      <p class="editor__form-desc">What this specific device is currently configured with — read-only unless "Force Device Changes" is active.</p>
      <div class="card section-card"><div class="card__body">
        <div style="display:flex; align-items:center; gap:10px; flex-wrap:wrap;">
          ${freshness}
        </div>
        ${provisionedFromHtml}
        ${provisioningEditing ? '<p class="field__help" style="margin:8px 0 0;">Changes are pushed directly to this device (one-time — templates never sync automatically).</p>' : ""}
      </div></div>
      ${bodyHtml}
      ${msgHtml(msgs.provisioning)}
      <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:14px;">${actionsHtml}</div>`;

    for (const btn of shell.formEl.querySelectorAll("[data-prov-tab]")) {
      btn.addEventListener("click", () => {
        provisioningSubSection = btn.dataset.provTab;
        renderSection();
      });
    }

    const profLink = shell.formEl.querySelector("[data-prov-profile-link]");
    if (profLink) {
      profLink.addEventListener("click", (e) => {
        e.preventDefault();
        app.navigate("/templates/" + encodeURIComponent(profLink.dataset.provProfileLink));
      });
    }

    if (provisioningEditing) {
      fieldsMod.bindFieldEvents(shell.formEl.querySelector("#dev-prov-fields"), {
        values: provisioningDraft,
        readOnly: false,
        onSectionRerender: () => renderSection(),
      });
      shell.formEl.querySelector("#dev-prov-save").addEventListener("click", saveProvisioning);
      shell.formEl.querySelector("#dev-prov-cancel").addEventListener("click", () => {
        provisioningEditing = false;
        provisioningDraft = null;
        msgs.provisioning = {};
        renderSection();
      });
    } else {
      shell.formEl.querySelector("#dev-prov-refresh").addEventListener("click", requestProvisioningRefresh);
      shell.formEl.querySelector("#dev-prov-force").addEventListener("click", () => {
        provisioningDraft = { ...baseline };
        provisioningEditing = true;
        msgs.provisioning = {};
        renderSection();
      });
    }
  }

  async function saveProvisioning() {
    msgs.provisioning = {};
    try {
      const fields = fieldsMod.collectFields(provisioningDraft, fieldsMod.LIVE_KEYS);
      await api.updateDeviceProvisioning(device.device_id, fields);
      ui.toast("Device settings pushed.");
      provisioningEditing = false;
      provisioningDraft = null;
      await app.refresh();
    } catch (err) {
      msgs.provisioning = { error: "Failed to save: " + ui.errMsg(err) };
    }
    if (mounted && active === "provisioning") renderSection();
  }

  function requestProvisioningRefresh() {
    ui.showConfirm({
      title: "Refresh provisioning now",
      message: `Ask "${displayName(device)}" to report its current provisioning state right now, instead of waiting for its next scheduled check-in?`,
      confirmLabel: "Refresh Now",
      confirmDanger: false,
      onConfirm: async () => {
        try {
          await api.requestDeviceReport(device.device_id);
          ui.toast("Refresh request sent.");
        } catch (err) {
          ui.toast("Failed: " + ui.errMsg(err), { error: true });
        }
      },
    });
  }

  function renderSession() {
    const d = device;
    const pendingLine =
      d.expires_at_override != null
        ? ` — pending change to <strong>${
            d.expires_at_override === 0 ? "Never" : esc(ui.fmtExpiry(d.expires_at_override))
          }</strong> <a href="#" id="dev-exp-cancel" style="color:var(--casa-primary);">cancel</a>`
        : "";
    const expiredWarn =
      d.expires_at && d.expires_at * 1000 < Date.now()
        ? `<div style="font-size:12px; color:var(--casa-warning); margin-bottom:6px;">This device's session has already expired — it has likely wiped itself and may never pick up a new expiration.</div>`
        : "";
    shell.formEl.innerHTML = `
      <h2>Session &amp; Expiration</h2>
      <p class="editor__form-desc">Control when this device's Home Assistant session ends.</p>
      <div class="card section-card"><div class="card__body">
        <h5>Session Expiration</h5>
        <div style="font-size:13px; margin-bottom:8px;">
          Current: <strong>${esc(ui.fmtExpiry(d.expires_at))}</strong>${pendingLine}
        </div>
        ${expiredWarn}
        <div class="field">
          <label>New Expiration</label>
          <div class="field-row">
            <input type="datetime-local" class="input" id="dev-exp-datetime">
            <button class="btn btn--primary" id="dev-exp-set">Set</button>
          </div>
        </div>
        <div style="display:flex; gap:6px; flex-wrap:wrap;">
          <button class="btn btn--outlined" data-exp="24h">+24 h</button>
          <button class="btn btn--outlined" data-exp="7d">+7 d</button>
          <button class="btn btn--outlined" data-exp="30d">+30 d</button>
          <button class="btn btn--outlined" data-exp="permanent">Make Permanent</button>
          <button class="btn btn--outlined" data-exp="now" style="color:var(--casa-error);">Expire Now</button>
        </div>
        <p class="field__help" style="margin-top:8px;">Changes are delivered on the device's next heartbeat (up to ~5 minutes).</p>
        ${msgHtml(msgs.session)}
      </div></div>`;

    // Quick-extends stack: relative to the later of now / the current (or
    // pending) expiry, so repeated clicks keep adding time (legacy expBase).
    const expBase = () => {
      const cur =
        d.expires_at_override != null && d.expires_at_override > 0
          ? d.expires_at_override
          : d.expires_at || 0;
      return Math.max(Math.floor(Date.now() / 1000), cur);
    };

    shell.formEl.querySelector("#dev-exp-set").addEventListener("click", () => {
      const input = shell.formEl.querySelector("#dev-exp-datetime");
      if (!input || !input.value) {
        msgs.session = { error: "Pick a date and time first." };
        renderSection();
        return;
      }
      const ts = Math.floor(new Date(input.value).getTime() / 1000);
      if (!Number.isFinite(ts) || ts <= Math.floor(Date.now() / 1000)) {
        msgs.session = { error: "Expiration must be in the future (use Expire Now to end the session)." };
        renderSection();
        return;
      }
      setExpiration(ts);
    });
    const EXP_HANDLERS = {
      "24h": () => setExpiration(expBase() + 24 * 3600),
      "7d": () => setExpiration(expBase() + 7 * 24 * 3600),
      "30d": () => setExpiration(expBase() + 30 * 24 * 3600),
      permanent: () => setExpiration(0),
      now: () =>
        ui.showConfirm({
          title: "Expire session now",
          message:
            "The device will wipe its session shortly after its next heartbeat and this record will stop updating. For immediate removal with full cleanup, use Deprovision instead.",
          confirmLabel: "Expire Now",
          onConfirm: () => setExpiration(Math.floor(Date.now() / 1000)),
        }),
    };
    for (const btn of shell.formEl.querySelectorAll("[data-exp]")) {
      btn.addEventListener("click", () => EXP_HANDLERS[btn.dataset.exp]?.());
    }
    const cancel = shell.formEl.querySelector("#dev-exp-cancel");
    if (cancel)
      cancel.addEventListener("click", (e) => {
        e.preventDefault();
        setExpiration(null);
      });
  }

  // value: epoch seconds (0 = permanent) to queue an override, null to cancel
  // a pending one (ported from legacy _setDeviceExpiration).
  async function setExpiration(value) {
    msgs.session = {};
    try {
      await api.updateDevice({ device_id: device.device_id, expires_at_override: value });
      msgs.session = {
        success:
          value === null
            ? "Pending expiration change cancelled."
            : value === 0
              ? "Queued: session becomes permanent on the next heartbeat."
              : `Queued: expires ${ui.fmtExpiry(value)} after the next heartbeat.`,
      };
      await app.refresh();
    } catch (err) {
      msgs.session = { error: "Failed: " + ui.errMsg(err) };
    }
    if (mounted && active === "session") renderSection();
  }

  function renderPush() {
    const d = device;
    const dis = d.push_registered ? "" : " disabled";
    shell.formEl.innerHTML = `
      <h2>Push &amp; Notifications</h2>
      <p class="editor__form-desc">Push registration status and delivery tools for this device.</p>
      <div class="card section-card"><div class="card__body">
        <h5>Push status</h5>
        <div style="display:flex; align-items:center; gap:8px; font-size:13px;">
          ${
            d.push_registered
              ? '<span class="chip chip--ok">Registered</span>'
              : '<span class="chip chip--error">Not registered</span>'
          }
          ${d.last_12_token ? `<span class="mono muted">…${esc(d.last_12_token)}</span>` : ""}
        </div>
      </div></div>
      <div class="card section-card"><div class="card__body">
        <h5>Test Push Notification</h5>
        ${
          d.push_registered
            ? ""
            : '<p class="field__help" style="margin:0 0 10px;">This device has no push registration — test notifications cannot be delivered.</p>'
        }
        <div class="field"><label>Title</label><input class="input" id="dev-push-title" value="Test Notification"${dis}></div>
        <div class="field"><label>Message</label><input class="input" id="dev-push-message" value="Hello from Home Assistant!"${dis}></div>
        <button class="btn btn--primary" id="dev-send-push"${dis}>Send Notification</button>
        ${msgHtml(msgs.push)}
      </div></div>
      <div class="card section-card"><div class="card__body">
        <h5>Reload app</h5>
        <p class="field__help" style="margin:0 0 10px;">Sends a silent push that reloads the Casa app UI on the device.</p>
        <button class="btn btn--outlined" id="dev-reload">Reload app</button>
      </div></div>`;

    shell.formEl.querySelector("#dev-send-push").addEventListener("click", sendTestPush);
    shell.formEl.querySelector("#dev-reload").addEventListener("click", confirmReload);
  }

  async function sendTestPush() {
    const titleEl = shell.formEl.querySelector("#dev-push-title");
    const msgEl = shell.formEl.querySelector("#dev-push-message");
    if (!titleEl || !msgEl) return;
    const title = titleEl.value.trim();
    const message = msgEl.value.trim();
    if (!title || !message) {
      msgs.push = { error: "Title and message are required." };
      renderSection();
      return;
    }
    msgs.push = {};
    try {
      await api.notifyUser({ deviceId: device.device_id, title, message });
      msgs.push = { success: "Push notification command sent." };
    } catch (err) {
      msgs.push = { error: "Failed to send: " + ui.errMsg(err) };
    }
    if (mounted && active === "push") renderSection();
  }

  function confirmReload() {
    ui.showConfirm({
      title: "Reload app",
      message: `Send a silent reload push to "${displayName(device)}"?`,
      confirmLabel: "Reload",
      confirmDanger: false,
      onConfirm: async () => {
        try {
          await api.reloadDevice(device.device_id);
          ui.toast("Reload push sent.");
        } catch (err) {
          ui.toast("Failed: " + ui.errMsg(err), { error: true });
        }
      },
    });
  }

  function renderVpn() {
    const d = device;
    ensureWgProfiles();
    const options = (wgProfiles || [])
      .map((p) => `<option value="${esc(p.id)}">${esc(p.alias)}</option>`)
      .join("");
    shell.formEl.innerHTML = `
      <h2>VPN / WireGuard</h2>
      <p class="editor__form-desc">Queue a WireGuard profile update or revoke VPN access on this device.</p>
      <div class="card section-card"><div class="card__body">
        <h5>Status</h5>
        <div style="display:flex; align-items:center; gap:8px; font-size:13px;">
          ${triChip(d.wireguard_configured, "Installed", "Not installed", "chip--neutral")}
          ${triChip(d.wireguard_connected, "Connected", "Disconnected", "chip--warn")}
        </div>
      </div></div>
      <div class="card section-card"><div class="card__body">
        <h5>Push WireGuard VPN Profile</h5>
        <div class="field">
          <label>WireGuard Profile</label>
          <select class="select" id="dev-wg-profile" style="width:100%;">
            <option value="">-- None / Revoke VPN --</option>
            ${options}
          </select>
          ${wgProfiles === null ? '<div class="field__help">Loading profiles…</div>' : ""}
        </div>
        <label class="toggle"><input type="checkbox" id="dev-wg-send-push"> Send update via push</label>
        <label class="toggle"><input type="checkbox" id="dev-wg-notify-push"> Notify via push</label>
        <button class="btn btn--primary" id="dev-push-wg">Queue VPN Update</button>
        ${msgHtml(msgs.vpn)}
      </div></div>`;

    shell.formEl.querySelector("#dev-push-wg").addEventListener("click", queueWg);
  }

  // Ported from legacy _pushDeviceWg (request shape in buildWireguardRequest).
  async function queueWg() {
    const select = shell.formEl.querySelector("#dev-wg-profile");
    if (!select) return;
    // Read inputs before re-rendering (which would reset them).
    const profileId = select.value;
    const sendPush = !!shell.formEl.querySelector("#dev-wg-send-push")?.checked;
    const notifyPush = !!shell.formEl.querySelector("#dev-wg-notify-push")?.checked;
    msgs.vpn = {};
    try {
      const { req, label } = buildWireguardRequest(
        device.device_id, profileId, wgProfiles, sendPush, notifyPush
      );
      const res = await api.queueUpdate(req);
      msgs.vpn = { success: queueSuccessText(label, res) };
      await app.refresh();
    } catch (err) {
      msgs.vpn = { error: "Failed to queue: " + ui.errMsg(err) };
    }
    if (mounted && active === "vpn") renderSection();
  }

  function renderUpdates() {
    const d = device;
    ensurePpProfiles();
    const list = d.pending_update_list || [];
    const rows = list
      .map(
        (u) => `
        <div style="display:flex; align-items:center; gap:8px; font-size:13px;">
          <span class="chip chip--app">${esc(u.type)}</span>
          <span>${esc(u.action)}</span>
          <span class="muted" style="margin-left:auto; font-size:12px;">${esc(ui.fmtTime(u.created_at))}</span>
          <button class="btn btn--icon danger" data-pu-del="${esc(u.id)}" title="Delete queued update"><ha-icon icon="mdi:delete-outline"></ha-icon></button>
        </div>`
      )
      .join("");
    const listHtml = list.length
      ? `<div class="card section-card"><div class="card__body">
          <p class="field__help" style="margin:0 0 10px;">Queued for this device. Consumed on the device's next heartbeat (or via push), then cleared.</p>
          <div style="display:flex; flex-direction:column; gap:6px;">${rows}</div>
          ${msgHtml(msgs.pending)}
        </div></div>`
      : `<div class="card section-card"><div class="card__body">
          <div class="empty-state" style="padding:18px 0;">
            <ha-icon icon="mdi:tray-remove"></ha-icon>
            <div>No pending updates.</div>
          </div>
          ${msgHtml(msgs.pending)}
        </div></div>`;
    const options = (ppProfiles || [])
      .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`)
      .join("");
    shell.formEl.innerHTML = `
      <h2>Pending Updates</h2>
      <p class="editor__form-desc">Updates queued for this device and tools to queue new ones.</p>
      ${listHtml}
      <div class="card section-card"><div class="card__body">
        <h5>Apply Template</h5>
        <div class="field">
          <label>Provision template</label>
          <select class="select" id="dev-pp-profile" style="width:100%;">
            <option value="">-- Select a template --</option>
            ${options}
          </select>
          ${ppProfiles === null ? '<div class="field__help">Loading templates…</div>' : ""}
          <div class="field__help">One-time apply of the template's set device fields — the device does not follow the template afterwards.</div>
          <div class="field__help"><a href="#" id="dev-pp-manage" hidden style="color:var(--casa-primary);">Manage template</a></div>
        </div>
        <label class="toggle"><input type="checkbox" id="dev-pp-send-push"> Send update via push</label>
        <label class="toggle"><input type="checkbox" id="dev-pp-notify-push"> Notify via push</label>
        <button class="btn btn--primary" id="dev-push-pp">Apply Template</button>
        ${msgHtml(msgs.updates)}
      </div></div>`;

    for (const btn of shell.formEl.querySelectorAll("[data-pu-del]")) {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        confirmDeletePending(btn.dataset.puDel);
      });
    }
    shell.formEl.querySelector("#dev-push-pp").addEventListener("click", queueProfile);

    const select = shell.formEl.querySelector("#dev-pp-profile");
    const manage = shell.formEl.querySelector("#dev-pp-manage");
    const syncManage = () => { manage.hidden = !select.value; };
    select.addEventListener("change", syncManage);
    syncManage();
    manage.addEventListener("click", (e) => {
      e.preventDefault();
      if (select.value) app.navigate("/templates/" + encodeURIComponent(select.value));
    });
  }

  // Confirm wording ported verbatim from the legacy pending-update delete.
  function confirmDeletePending(updateId) {
    const entry = (device.pending_update_list || []).find((u) => u.id === updateId);
    const label = entry ? `${entry.type} ${entry.action}` : "this update";
    ui.showConfirm({
      title: "Delete pending update",
      message: `Remove the queued "${label}" for this device? This only clears it from the server queue — if it was already delivered by push, the device may still apply it.`,
      confirmLabel: "Delete",
      onConfirm: async () => {
        msgs.pending = {};
        try {
          await api.deleteQueuedUpdate(device.device_id, updateId);
        } catch (err) {
          msgs.pending = { error: "Failed to delete: " + ui.errMsg(err) };
        }
        await app.refresh();
        if (mounted && active === "updates") renderSection();
      },
    });
  }

  // Ported from legacy _pushDeviceProfile (request shape in buildProfileRequest).
  async function queueProfile() {
    const select = shell.formEl.querySelector("#dev-pp-profile");
    if (!select) return;
    const profileId = select.value;
    const sendPush = !!shell.formEl.querySelector("#dev-pp-send-push")?.checked;
    const notifyPush = !!shell.formEl.querySelector("#dev-pp-notify-push")?.checked;
    msgs.updates = {};
    if (!profileId) {
      msgs.updates = { error: "Select a template first." };
      renderSection();
      return;
    }
    try {
      const { req, label } = buildProfileRequest(
        device.device_id, profileId, ppProfiles, sendPush, notifyPush
      );
      const res = await api.queueUpdate(req);
      msgs.updates = { success: queueSuccessText(label, res) };
      await app.refresh();
    } catch (err) {
      msgs.updates = { error: "Failed to queue: " + ui.errMsg(err) };
    }
    if (mounted && active === "updates") renderSection();
  }

  function renderDanger() {
    shell.formEl.innerHTML = `
      <h2>Danger Zone</h2>
      <p class="editor__form-desc">Destructive actions — read the confirmations carefully.</p>
      <div class="danger-zone">
        <h5>Danger Zone</h5>
        <div style="display:flex; gap:8px; flex-wrap:wrap;">
          <button class="btn btn--outlined" id="dev-delete-record"><ha-icon icon="mdi:delete-outline"></ha-icon> Delete Record</button>
          <button class="btn btn--danger" id="dev-deprovision"><ha-icon icon="mdi:cellphone-remove"></ha-icon> Deprovision Device</button>
        </div>
        <p class="field__help" style="margin:8px 0 0;">
          <strong>Delete Record</strong> removes the record and revokes access without touching the app — for stale/orphaned entries.
          <strong>Deprovision</strong> additionally wipes the Casa app on the device via push.
        </p>
        ${msgHtml(msgs.danger)}
      </div>`;

    shell.formEl
      .querySelector("#dev-delete-record")
      .addEventListener("click", () => confirmDeviceAction("delete"));
    shell.formEl
      .querySelector("#dev-deprovision")
      .addEventListener("click", () => confirmDeviceAction("deprovision"));
  }

  // Confirmation wording ported verbatim from legacy _confirmDeviceAction.
  function confirmDeviceAction(kind) {
    const label = device.alias || device.device_id;
    if (kind === "delete") {
      ui.showConfirm({
        title: "Delete device record",
        message: `Delete the record for "${label}"? This revokes its Home Assistant session and push relay token and removes it from the server, but does NOT wipe the app — the device loses access when its session next fails. Use Deprovision to remotely wipe.`,
        confirmLabel: "Delete",
        onConfirm: () => runDeviceAction("delete"),
      });
    } else {
      ui.showConfirm({
        title: "Deprovision device",
        message: `Deprovision "${label}"? This wipes the Casa app on the device, revokes its Home Assistant session, unregisters its push token, and deletes this record. If the device is offline it is wiped the next time it contacts the server. This cannot be undone.`,
        confirmLabel: "Deprovision",
        onConfirm: () => runDeviceAction("deprovision"),
      });
    }
  }

  async function runDeviceAction(kind) {
    msgs.danger = {};
    leaving = true; // the record disappearing is expected — skip "gone" handling
    try {
      const res =
        kind === "delete"
          ? await api.deleteDevice(device.device_id)
          : await api.deprovisionDevice(device.device_id);
      await app.refresh();
      app.navigate("/");
      if (kind === "deprovision") {
        const response = api.constructor.response(res); // CasaApi.response
        if (response.push_sent === false) {
          ui.showInfo({
            title: "Deprovision device",
            message:
              "Device record removed and access revoked, but the wipe push could not be delivered (no push registration or relay unreachable). The device will wipe itself the next time it contacts the server.",
          });
        }
      }
    } catch (err) {
      leaving = false;
      msgs.danger = { error: "Failed: " + ui.errMsg(err) };
      if (mounted && active === "danger") renderSection();
    }
  }

  /* ----- view contract ----- */

  return {
    id: "device-editor",
    header: (params) => {
      const d = findDevice(params && params.deviceId);
      const label = d ? displayName(d) : String((params && params.deviceId) || "").slice(0, 8);
      return { title: `Editing ${label}`, back: "/" };
    },
    polling: "live",

    mount(el, params) {
      mounted = true;
      leaving = false;
      missingHandled = false;
      deviceId = params.deviceId;
      device = null;
      shell = null;
      active = "overview";
      provisioningEditing = false;
      provisioningDraft = null;
      provisioningSubSection = "connection";
      msgs = freshMsgs();
      host = document.createElement("div");
      host.className = "page--flush";
      el.appendChild(host);
      if (app.summary() && !findDevice(deviceId)) {
        handleMissing();
        return;
      }
      void init();
    },

    unmount() {
      mounted = false;
      leaving = false;
      shell = null;
      host = null;
      device = null;
    },

    onSummary(_summary, _error) {
      if (!mounted || leaving) return;
      if (!shell) {
        if (shellMod) tryBuild();
        return;
      }
      const fresh = findDevice(deviceId);
      if (!fresh) {
        handleMissing();
        return;
      }
      const before = JSON.stringify(orderedRecord(device));
      device = fresh;
      const changed = JSON.stringify(orderedRecord(device)) !== before;

      // Always: title chips, code pane, nav badges.
      shell.setTitle(`Editing ${displayName(device)}`, statusChipsHtml(device));
      renderCode();
      updateNavBadges();

      // Form pane: only re-render when focus isn't inside it, so live polling
      // never clobbers an input mid-edit. When focus is inside and Overview is
      // active, refresh just the (input-free) info grid.
      const activeEl = ui._shadowRoot ? ui._shadowRoot.activeElement : null;
      const focusInForm = !!(activeEl && shell.formEl.contains(activeEl));
      if (!focusInForm) {
        if (changed) renderSection();
      } else if (changed && active === "overview") {
        const grid = shell.formEl.querySelector("#dev-ov-grid");
        if (grid && !grid.contains(activeEl)) {
          grid.innerHTML = infoGridRowsHtml(device);
          bindInfoGrid(grid);
        }
      }
    },
  };
}
