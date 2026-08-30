// Casa admin panel — settings view. Two-pane layout reusing the editor nav
// classes: Site (relay identity + encryption key rotation + regenerate-site
// danger zone), WireGuard Profiles (stored VPN configs with an add/edit
// modal), and Location Zones (lazy-loaded map editor, views/location-zones.js).
// Mounted at /settings and /settings/{tab}.

export function createView(app) {
  const { api, ui } = app;
  const esc = ui.esc;

  let tab = "site"; // "site" | "wireguard" | "location"
  let refs = null; // { nav, form }
  let wgProfiles = null; // null = not loaded yet
  let wgLoading = false;
  let lzMod = null; // views/location-zones.js, loaded lazily on first visit
  let lzModLoading = false;
  let lzView = null; // instance from lzMod.createView(app); persists across tab switches

  /* ---------- shell ---------- */

  function setTab(next) {
    if (next !== "site" && next !== "wireguard" && next !== "location") next = "site";
    if (tab === "location" && next !== "location" && lzView) lzView.unmount();
    tab = next;
    app.navigate("/settings/" + tab, { replace: true });
    renderNav();
    renderForm();
  }

  function renderNav() {
    if (!refs) return;
    for (const item of refs.nav.querySelectorAll("[data-tab]")) {
      item.classList.toggle("nav-item--active", item.dataset.tab === tab);
    }
  }

  function renderForm() {
    if (!refs) return;
    if (tab === "wireguard") renderWireguard();
    else if (tab === "location") renderLocationZones();
    else renderSite();
  }

  /* ---------- Location Zones section ---------- */

  function renderLocationZones() {
    if (!refs) return;
    if (!lzMod) {
      if (!lzModLoading) {
        lzModLoading = true;
        refs.form.innerHTML = `<div class="empty-state" style="padding:48px 16px;"><span class="muted">Loading…</span></div>`;
        app
          .loadModule("views/location-zones.js")
          .then((mod) => { lzMod = mod; })
          .catch((err) => {
            if (refs && tab === "location") {
              refs.form.innerHTML = `<div class="errbar">Failed to load location zones editor: ${esc(ui.errMsg(err))}</div>`;
            }
          })
          .finally(() => {
            lzModLoading = false;
            if (refs && tab === "location" && lzMod) renderLocationZones();
          });
      }
      return;
    }
    if (!lzView) lzView = lzMod.createView(app);
    refs.form.innerHTML = "";
    lzView.mount(refs.form);
  }

  /* ---------- Site section ---------- */

  function renderSite() {
    const summary = app.summary() || {};
    const siteId = summary.site_id || "—";
    const keyId = summary.device_key_id || "—";

    const readonlyCard = (label, value) => `
      <div class="card section-card">
        <div class="card__body">
          <h5>${esc(label)}</h5>
          <div class="field-row">
            <span class="mono" style="flex:1; min-width:0; word-break:break-all;">${esc(value)}</span>
            <button class="btn btn--outlined" data-copy="${esc(value)}" style="flex:none;">Copy</button>
          </div>
        </div>
      </div>`;

    refs.form.innerHTML = `
      <h2>Site</h2>
      <p class="editor__form-desc">Relay identity and encryption for this Home Assistant instance.</p>
      ${readonlyCard("Site ID", siteId)}
      ${readonlyCard("Encryption Key ID", keyId)}
      <div class="card section-card">
        <div class="card__body">
          <h5>Encryption Key</h5>
          <p class="muted" style="font-size:13px; margin:0 0 12px;">
            Rotate the shared key used to encrypt WireGuard and update pushes. Non-destructive:
            devices pick up the new key on their next heartbeat and fall back to the secure update
            queue in the meantime. No re-provisioning required.
          </p>
          <button class="btn btn--outlined" id="st-rotate-key">Rotate Encryption Key</button>
        </div>
      </div>
      <div class="card section-card">
        <div class="card__body">
          <h5>Require device alias</h5>
          <p class="muted" style="font-size:13px; margin:0 0 12px;">
            When enabled, every provisioned device without an alias blocks the user with a
            name prompt until one is provided (delivered on the next heartbeat). Admin-set
            aliases satisfy it. Can also be enforced per provision template.
          </p>
          <label class="toggle">
            <input type="checkbox" id="st-require-alias" ${summary.require_device_alias ? "checked" : ""}>
            <span>Require an alias on every device</span>
          </label>
        </div>
      </div>
      <div class="card section-card">
        <div class="card__body">
          <h5>Heartbeat interval</h5>
          <p class="muted" style="font-size:13px; margin:0 0 12px;">
            How often (in seconds) provisioned devices check in with this site.
            Applied on each device's next heartbeat — no re-provisioning
            required. Default 300s (5 min).
          </p>
          <div class="field-row">
            <input type="number" class="input" id="st-heartbeat-interval" min="60" max="3600" step="1"
              value="${esc(summary.heartbeat_interval_seconds || 300)}" style="max-width:140px; flex:none;">
            <span class="muted">seconds (60–3600)</span>
          </div>
        </div>
      </div>
      <div class="card section-card">
        <div class="card__body">
          <h5>Profile report interval</h5>
          <p class="muted" style="font-size:13px; margin:0 0 12px;">
            How often (in seconds) devices self-report their provisioning
            details to this site, for the per-device Provisioning view.
            Applied on the device's next heartbeat. Default 3600s (1 hour).
            Use "Refresh Now" on a device's Provisioning tab for an immediate,
            out-of-cycle report.
          </p>
          <div class="field-row">
            <input type="number" class="input" id="st-profile-report-interval" min="300" max="86400" step="1"
              value="${esc(summary.profile_report_interval_seconds || 3600)}" style="max-width:140px; flex:none;">
            <span class="muted">seconds (300–86400)</span>
          </div>
        </div>
      </div>
      <div class="danger-zone">
        <h5>Regenerate Site ID</h5>
        <p class="muted" style="font-size:13px; margin:0 0 12px;">
          Rotate this site's identity. Removes it from the relay and registers a fresh
          Site ID + key. Destructive — all push registrations break and every device
          must be re-provisioned afterward.
        </p>
        <button class="btn btn--danger-outlined" id="st-regen-site">Regenerate Site ID</button>
      </div>`;

    for (const btn of refs.form.querySelectorAll("[data-copy]")) {
      ui.bindCopyButton(btn, () => btn.dataset.copy);
    }
    refs.form.querySelector("#st-rotate-key").addEventListener("click", rotateKey);
    refs.form.querySelector("#st-regen-site").addEventListener("click", confirmRegenerateSite);
    refs.form.querySelector("#st-require-alias").addEventListener("change", toggleRequireAlias);
    refs.form.querySelector("#st-heartbeat-interval").addEventListener("change", updateHeartbeatInterval);
    refs.form.querySelector("#st-profile-report-interval").addEventListener("change", updateProfileReportInterval);
  }

  async function toggleRequireAlias(e) {
    const input = e.currentTarget;
    const value = input.checked;
    input.disabled = true;
    try {
      await api.updateSettings({ require_device_alias: value });
      ui.toast(value ? "Device alias is now required." : "Device alias requirement disabled.");
      app.refresh(); // summary re-render restores the toggle state
    } catch (err) {
      ui.toast("Failed to update setting: " + ui.errMsg(err), { error: true });
      input.checked = !value;
      input.disabled = false;
    }
  }

  async function updateHeartbeatInterval(e) {
    const input = e.currentTarget;
    const previous = (app.summary() || {}).heartbeat_interval_seconds || 300;
    const value = parseInt(input.value, 10);
    if (!Number.isFinite(value) || value < 60 || value > 3600) {
      ui.toast("Heartbeat interval must be between 60 and 3600 seconds.", { error: true });
      input.value = previous;
      return;
    }
    input.disabled = true;
    try {
      await api.updateSettings({ heartbeat_interval_seconds: value });
      ui.toast(`Heartbeat interval updated to ${value}s.`);
      app.refresh(); // summary re-render restores the field
    } catch (err) {
      ui.toast("Failed to update setting: " + ui.errMsg(err), { error: true });
      input.value = previous;
      input.disabled = false;
    }
  }

  async function updateProfileReportInterval(e) {
    const input = e.currentTarget;
    const previous = (app.summary() || {}).profile_report_interval_seconds || 3600;
    const value = parseInt(input.value, 10);
    if (!Number.isFinite(value) || value < 300 || value > 86400) {
      ui.toast("Profile report interval must be between 300 and 86400 seconds.", { error: true });
      input.value = previous;
      return;
    }
    input.disabled = true;
    try {
      await api.updateSettings({ profile_report_interval_seconds: value });
      ui.toast(`Profile report interval updated to ${value}s.`);
      app.refresh(); // summary re-render restores the field
    } catch (err) {
      ui.toast("Failed to update setting: " + ui.errMsg(err), { error: true });
      input.value = previous;
      input.disabled = false;
    }
  }

  async function rotateKey(e) {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "Rotating…";
    try {
      // POST /api/casa/admin/regenerate_device_key → {status: "ok", device_key_id}
      const res = await api.regenerateDeviceKey();
      const keyId = (res && res.device_key_id) || "unknown";
      ui.toast(`Encryption key rotated. New key id: ${keyId}`);
      app.refresh(); // summary re-render restores the button
    } catch (err) {
      ui.toast("Key rotation failed: " + ui.errMsg(err), { error: true });
      btn.disabled = false;
      btn.textContent = "Rotate Encryption Key";
    }
  }

  function confirmRegenerateSite() {
    ui.showConfirm({
      title: "Regenerate Site ID",
      message:
        "This removes the current site on the relay and mints a new Site ID. Every existing " +
        "device profile becomes invalid — all push registrations break and all devices must " +
        "be re-provisioned. Continue?",
      confirmLabel: "Regenerate",
      onConfirm: async () => {
        try {
          await api.regenerateSite();
          ui.toast("Site regenerated. All devices must be re-provisioned.");
          app.refresh();
        } catch (err) {
          ui.toast("Regenerate failed: " + ui.errMsg(err), { error: true });
        }
      },
    });
  }

  /* ---------- WireGuard section ---------- */

  async function loadWgProfiles() {
    wgLoading = true;
    if (refs && tab === "wireguard") renderWireguard();
    try {
      const res = await api.getWireguardProfiles();
      wgProfiles = (res && res.profiles) || [];
    } catch (err) {
      wgProfiles = [];
      ui.toast("Failed to load WireGuard profiles: " + ui.errMsg(err), { error: true });
    }
    wgLoading = false;
    if (refs && tab === "wireguard") renderWireguard();
  }

  function wgCard(p) {
    const preview = String(p.config || "").split("\n").slice(0, 3).join("\n");
    return `
      <div class="card section-card">
        <div class="card__body" style="display:flex; gap:12px;">
          <div style="flex:1; min-width:0;">
            <div style="font-weight:600; font-size:14px;">${esc(p.alias || p.id)}</div>
            <div class="muted" style="font-size:12px; margin-top:2px;">
              ${esc(ui.fmtTime(p.created_at))}${p.excluded_wifi ? ` · Excluded Wi-Fi: ${esc(p.excluded_wifi)}` : ""}
            </div>
            <pre class="mono muted" style="margin:8px 0 0; white-space:pre-wrap; word-break:break-all;">${esc(preview)}</pre>
          </div>
          <div style="display:flex; gap:2px; align-items:flex-start; flex:none;">
            <button class="btn btn--icon" data-act="edit" data-id="${esc(p.id)}" title="Edit"><ha-icon icon="mdi:pencil"></ha-icon></button>
            <button class="btn btn--icon danger" data-act="delete" data-id="${esc(p.id)}" title="Delete"><ha-icon icon="mdi:delete"></ha-icon></button>
          </div>
        </div>
      </div>`;
  }

  function renderWireguard() {
    if (!refs) return;
    if (wgProfiles === null && !wgLoading) {
      loadWgProfiles();
      return; // loadWgProfiles re-renders immediately in its loading state
    }

    let listHtml;
    if (wgLoading && wgProfiles === null) {
      listHtml = `<div class="empty-state" style="padding:32px 16px;"><span class="muted">Loading…</span></div>`;
    } else if (!wgProfiles || !wgProfiles.length) {
      listHtml = `
        <div class="empty-state">
          <ha-icon icon="mdi:shield-key-outline"></ha-icon>
          <div>No WireGuard profiles yet</div>
          <button class="btn btn--primary" data-act="add">+ Add profile</button>
        </div>`;
    } else {
      listHtml = wgProfiles.map(wgCard).join("");
    }

    refs.form.innerHTML = `
      <h2>WireGuard Profiles</h2>
      <p class="editor__form-desc">Stored WireGuard VPN configurations available to provision templates.</p>
      <div class="list-toolbar">
        <button class="btn btn--primary" data-act="add">+ Add profile</button>
        <button class="btn btn--outlined" data-act="refresh" ${wgLoading ? "disabled" : ""}>
          <ha-icon icon="mdi:refresh"></ha-icon> Refresh
        </button>
      </div>
      <div id="st-wg-list">${listHtml}</div>`;
  }

  function openWgEditor(profile) {
    const body = document.createElement("div");
    body.innerHTML = `
      <div class="field">
        <label>Alias</label>
        <input class="input" id="wge-alias" value="${esc((profile && profile.alias) || "")}" placeholder="e.g. Office VPN">
        <div class="field__help">Optional — auto-generated if blank.</div>
      </div>
      <div class="field">
        <label>WireGuard Config *</label>
        <textarea class="textarea" id="wge-config" placeholder="[Interface]&#10;PrivateKey = ...&#10;Address = ...&#10;&#10;[Peer]&#10;PublicKey = ...&#10;Endpoint = ...">${esc((profile && profile.config) || "")}</textarea>
      </div>
      <div class="field">
        <label>Excluded Wi-Fi</label>
        <input class="input" id="wge-excluded" value="${esc((profile && profile.excluded_wifi) || "")}" placeholder="HomeSSID, OfficeSSID">
        <div class="field__help">Optional — SSIDs where the VPN stays down.</div>
      </div>
      <div class="field__error" id="wge-err" hidden></div>`;

    ui.openModal({
      title: profile ? "Edit WireGuard profile" : "New WireGuard profile",
      bodyEl: body,
      buttons: [
        { label: "Cancel", variant: "text" },
        {
          label: "Save",
          variant: "primary",
          onClick: async (btn) => {
            const config = body.querySelector("#wge-config").value.trim();
            const errEl = body.querySelector("#wge-err");
            if (!config) {
              errEl.hidden = false;
              errEl.textContent = "Config is required.";
              return false;
            }
            // Field names match the legacy editor: alias, config, excluded_wifi (+ id for PUT).
            const data = {
              alias: body.querySelector("#wge-alias").value,
              config,
              excluded_wifi: body.querySelector("#wge-excluded").value,
            };
            if (profile && profile.id) data.id = profile.id;
            btn.disabled = true;
            btn.textContent = "Saving…";
            try {
              await api.saveWireguardProfile(data); // PUT if data.id else POST
              ui.toast("WireGuard profile saved.");
              loadWgProfiles();
            } catch (err) {
              errEl.hidden = false;
              errEl.textContent = "Failed: " + ui.errMsg(err);
              btn.disabled = false;
              btn.textContent = "Save";
              return false;
            }
          },
        },
      ],
    });
  }

  async function confirmDeleteWg(profile) {
    // In-use check: provision templates that link this WG profile by id.
    let usedBy = [];
    try {
      const res = await api.getProvisionTemplates();
      usedBy = ((res && res.profiles) || [])
        .filter((pp) => pp && pp.fields && pp.fields.wireguard_profile_id === profile.id)
        .map((pp) => pp.name || pp.id);
    } catch (_e) {
      /* best-effort — deletion still gets a plain confirm */
    }

    let message = `Delete WireGuard profile '${profile.alias || profile.id}'?`;
    if (usedBy.length) {
      message +=
        ` Used by provision template(s): ${usedBy.join(", ")} — ` +
        "provisioning with them will fall back to inline/empty config.";
    }

    ui.showConfirm({
      title: "Delete WireGuard profile",
      message,
      confirmLabel: "Delete",
      onConfirm: async () => {
        try {
          await api.deleteWireguardProfile(profile.id);
          ui.toast("WireGuard profile deleted.");
        } catch (err) {
          ui.toast("Delete failed: " + ui.errMsg(err), { error: true });
        }
        loadWgProfiles();
      },
    });
  }

  /* ---------- delegated events ---------- */

  function onFormClick(e) {
    if (tab !== "wireguard") return; // site pane binds its buttons directly
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    switch (el.dataset.act) {
      case "add":
        openWgEditor(null);
        break;
      case "refresh":
        loadWgProfiles();
        break;
      case "edit": {
        const p = (wgProfiles || []).find((x) => x && x.id === el.dataset.id);
        if (p) openWgEditor(p);
        break;
      }
      case "delete": {
        const p = (wgProfiles || []).find((x) => x && x.id === el.dataset.id);
        if (p) confirmDeleteWg(p);
        break;
      }
    }
  }

  /* ---------- view ---------- */

  return {
    id: "settings",
    header: () => ({ title: "Settings", back: "/" }),
    polling: "paused",

    mount(el, params) {
      const p = params && params.tab;
      tab = p === "wireguard" || p === "location" ? p : "site";
      el.innerHTML = `
        <div class="editor" style="height:100%;">
          <nav class="editor__nav" id="st-nav">
            <button class="nav-item" data-tab="site"><ha-icon icon="mdi:earth"></ha-icon>Site</button>
            <button class="nav-item" data-tab="wireguard"><ha-icon icon="mdi:shield-key"></ha-icon>WireGuard Profiles</button>
            <button class="nav-item" data-tab="location"><ha-icon icon="mdi:map-marker-radius"></ha-icon>Location Zones</button>
          </nav>
          <div class="editor__form" id="st-form"></div>
        </div>`;

      refs = {
        nav: el.querySelector("#st-nav"),
        form: el.querySelector("#st-form"),
      };

      refs.nav.addEventListener("click", (e) => {
        const item = e.target.closest("[data-tab]");
        if (item) setTab(item.dataset.tab);
      });
      refs.form.addEventListener("click", onFormClick);

      renderNav();
      renderForm();
    },

    unmount() {
      if (tab === "location" && lzView) lzView.unmount();
      refs = null;
    },

    onSummary() {
      // Polling is paused here, but app.refresh() after rotate/regenerate
      // pushes a new summary — refresh the read-only Site values.
      if (refs && tab === "site") renderSite();
    },
  };
}
