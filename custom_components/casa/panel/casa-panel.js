// Casa admin panel — a plain web component (no build step) registered as a
// custom Home Assistant sidebar panel. Blends with the active HA theme by using
// HA's CSS custom properties.

class CasaAdminPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
      this._settingsTab = "site";
      this._render();
      this._load();
    }
  }

  // HA assigns these on custom panels; we don't need them but accept them.
  set narrow(_v) {}
  set route(_v) {}
  set panel(_v) {}

  connectedCallback() {
    if (this._initialized && !this._timer) {
      this._timer = setInterval(() => this._load(), 30000);
    }
  }

  disconnectedCallback() {
    if (this._timer) {
      clearInterval(this._timer);
      this._timer = null;
    }
  }

  async _load() {
    if (!this._hass) return;
    try {
      this._data = await this._hass.callApi("GET", "casa/admin/summary");
      this._error = null;
    } catch (err) {
      this._error = (err && err.message) || String(err);
    }
    this._update();
  }

  async _reconcile() {
    if (!this._hass) return;
    this._setStatus("Reconciling…");
    try {
      await this._hass.callService("casa", "reconcile");
      await this._load();
      this._setStatus("Reconcile complete.");
    } catch (err) {
      this._setStatus("Reconcile failed: " + ((err && err.message) || err));
    }
  }

  async _regenerate() {
    if (!this._hass) return;
    this._regenBusy = true;
    this._renderSettingsBody();
    try {
      await this._hass.callService("casa", "regenerate_site");
      await this._load();
      this._regenMsg = "Site regenerated. All devices must be re-provisioned.";
    } catch (err) {
      this._regenMsg = "Failed: " + ((err && err.message) || err);
    }
    this._regenBusy = false;
    this._regenConfirm = false;
    this._renderSettingsBody();
  }

  _setStatus(text) {
    const el = this.shadowRoot && this.shadowRoot.getElementById("status");
    if (el) el.textContent = text || "";
  }

  _esc(v) {
    if (v === null || v === undefined) return "";
    return String(v).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  _fmtTime(v) {
    if (!v) return "never";
    try {
      const d = new Date(v);
      if (isNaN(d.getTime())) return this._esc(v);
      return d.toLocaleString();
    } catch (e) {
      return this._esc(v);
    }
  }

  _openSettings() {
    this._regenConfirm = false;
    this._regenBusy = false;
    this._regenMsg = "";
    this.shadowRoot.getElementById("overlay").classList.remove("hidden");
    this._renderSettingsBody();
  }

  _closeSettings() {
    this.shadowRoot.getElementById("overlay").classList.add("hidden");
  }

  _render() {
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          background: var(--primary-background-color, #fafafa);
          color: var(--primary-text-color, #212121);
          min-height: 100%;
          font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        }
        .toolbar {
          display: flex; align-items: center;
          height: var(--header-height, 56px); padding: 0 16px;
          background: var(--app-header-background-color, var(--primary-color, #03a9f4));
          color: var(--app-header-text-color, var(--text-primary-color, #fff));
          font-size: 20px; font-weight: 400;
          box-shadow: var(--ha-card-box-shadow, 0 2px 2px rgba(0,0,0,.1));
        }
        .toolbar .spacer { flex: 1; }
        .icon-btn {
          background: none; border: none; color: inherit; cursor: pointer;
          font-size: 22px; line-height: 1; padding: 6px 8px; border-radius: 50%;
        }
        .icon-btn:hover { background: rgba(255,255,255,.15); }
        .menu { margin-right: 12px; }
        .content { padding: 16px; max-width: 1400px; margin: 0 auto; }
        .stats {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 12px; margin-bottom: 16px;
        }
        .stat, .card {
          background: var(--ha-card-background, var(--card-background-color, #fff));
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
        }
        .stat { padding: 16px; }
        .stat .value { font-size: 28px; font-weight: 500; }
        .stat .label {
          font-size: 13px; color: var(--secondary-text-color, #727272);
          text-transform: uppercase; letter-spacing: .5px; margin-top: 4px;
        }
        .stat.warn .value { color: var(--warning-color, #ffa600); }
        .stat.err .value { color: var(--error-color, #db4437); }
        .stat.site .value { font-size: 13px; font-family: monospace; word-break: break-all; font-weight: 400; }
        .actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 16px; }
        button.action {
          background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff);
          border: none; border-radius: 8px; padding: 8px 16px; font-size: 14px;
          cursor: pointer; font-weight: 500;
        }
        button.action.secondary {
          background: var(--secondary-background-color, #e5e5e5);
          color: var(--primary-text-color, #212121);
        }
        button.action:hover { filter: brightness(.95); }
        #status { color: var(--secondary-text-color, #727272); font-size: 13px; margin-left: 8px; }
        .columns { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
        @media (max-width: 900px) { .columns { grid-template-columns: 1fr; } }
        .card h2 {
          margin: 0; padding: 16px; font-size: 16px; font-weight: 500;
          border-bottom: 1px solid var(--divider-color, #e0e0e0);
        }
        table { width: 100%; border-collapse: collapse; font-size: 13px; }
        th, td {
          text-align: left; padding: 10px 16px;
          border-bottom: 1px solid var(--divider-color, #ededed);
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 220px;
        }
        th { color: var(--secondary-text-color, #727272); font-weight: 500; }
        tr:last-child td { border-bottom: none; }
        .badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; margin-left: 4px; }
        .badge.ok { background: var(--success-color, #43a047); color: #fff; }
        .badge.stale { background: var(--warning-color, #ffa600); color: #222; }
        .badge.orphan { background: var(--error-color, #db4437); color: #fff; }
        .empty { padding: 16px; color: var(--secondary-text-color, #727272); }
        .errbar { background: var(--error-color, #db4437); color: #fff; padding: 10px 16px; border-radius: 8px; margin-bottom: 16px; }
        code { font-family: monospace; }

        /* Settings overlay (claude.ai-style) */
        .overlay {
          position: fixed; inset: 0; z-index: 9999;
          background: rgba(0,0,0,.32);
          display: flex; align-items: center; justify-content: center;
        }
        .overlay.hidden { display: none; }
        .modal {
          position: relative; display: flex;
          width: 860px; max-width: 92vw; height: 560px; max-height: 86vh;
          background: var(--card-background-color, #fff);
          color: var(--primary-text-color, #212121);
          border-radius: 16px; overflow: hidden;
          box-shadow: 0 24px 64px rgba(0,0,0,.32);
        }
        .modal .nav {
          width: 220px; flex-shrink: 0; padding: 16px 12px;
          border-right: 1px solid var(--divider-color, #e0e0e0);
          display: flex; flex-direction: column; gap: 2px;
          background: var(--secondary-background-color, #f5f5f5);
        }
        .modal .nav .title { font-size: 18px; font-weight: 600; padding: 6px 12px 14px; }
        .tab {
          display: flex; align-items: center; gap: 10px;
          padding: 9px 12px; border-radius: 8px; cursor: pointer; font-size: 14px;
        }
        .tab:hover { background: var(--divider-color, #e8e8e8); }
        .tab.active { background: var(--card-background-color, #fff); font-weight: 500; box-shadow: 0 1px 2px rgba(0,0,0,.08); }
        .modal .pane { flex: 1; padding: 28px; overflow: auto; }
        .modal .close {
          position: absolute; top: 14px; right: 16px;
          background: none; border: none; cursor: pointer; font-size: 22px;
          color: var(--secondary-text-color, #727272); line-height: 1;
        }
        .pane h3 { margin: 0 0 4px; font-size: 20px; }
        .pane .sub { color: var(--secondary-text-color, #727272); font-size: 13px; margin: 0 0 24px; }
        .field { margin-bottom: 24px; }
        .field label { display: block; font-size: 13px; color: var(--secondary-text-color, #727272); margin-bottom: 6px; }
        .field .val {
          font-family: monospace; font-size: 13px; word-break: break-all;
          background: var(--secondary-background-color, #f5f5f5); padding: 10px 12px; border-radius: 8px;
        }
        .danger { border: 1px solid var(--error-color, #db4437); border-radius: 12px; padding: 16px; }
        .danger h4 { margin: 0 0 6px; color: var(--error-color, #db4437); font-size: 15px; }
        .danger p { margin: 0 0 12px; font-size: 13px; color: var(--secondary-text-color, #727272); }
        .row-btns { display: flex; gap: 8px; }
        .btn-danger { background: var(--error-color, #db4437); color: #fff; border: none; border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: 500; cursor: pointer; }
        .btn-outline { background: transparent; color: var(--error-color, #db4437); border: 1px solid var(--error-color, #db4437); border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: 500; cursor: pointer; }
        .btn-plain { background: var(--secondary-background-color, #e5e5e5); color: var(--primary-text-color, #212121); border: none; border-radius: 8px; padding: 8px 14px; font-size: 14px; cursor: pointer; }
        .regen-msg { margin-top: 12px; font-size: 13px; }
      </style>
      <div class="toolbar">
        <button class="icon-btn menu" id="menu" title="Menu">&#9776;</button>
        <span>Casa Admin</span>
        <span class="spacer"></span>
        <button class="icon-btn" id="settings" title="Settings">&#9881;</button>
      </div>
      <div class="content">
        <div id="err"></div>
        <div class="stats" id="stats"></div>
        <div class="actions">
          <button class="action" id="reconcile">Run Reconcile</button>
          <button class="action secondary" id="refresh">Refresh</button>
          <span id="status"></span>
        </div>
        <div class="columns">
          <div class="card"><h2>Managed Devices</h2><div id="devices"></div></div>
          <div class="card"><h2>Managed Accounts</h2><div id="accounts"></div></div>
        </div>
      </div>
      <div class="overlay hidden" id="overlay">
        <div class="modal">
          <button class="close" id="settings-close" title="Close">&times;</button>
          <div class="nav">
            <div class="title">Settings</div>
            <div class="tab active" data-tab="site">&#127760; Site</div>
          </div>
          <div class="pane" id="settings-pane"></div>
        </div>
      </div>
    `;

    const sr = this.shadowRoot;
    sr.getElementById("menu").addEventListener("click", () => {
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    });
    sr.getElementById("refresh").addEventListener("click", () => this._load());
    sr.getElementById("reconcile").addEventListener("click", () => this._reconcile());
    sr.getElementById("settings").addEventListener("click", () => this._openSettings());
    sr.getElementById("settings-close").addEventListener("click", () => this._closeSettings());
    sr.getElementById("overlay").addEventListener("click", (e) => {
      if (e.target === sr.getElementById("overlay")) this._closeSettings();
    });

    if (this.isConnected && !this._timer) {
      this._timer = setInterval(() => this._load(), 30000);
    }
  }

  _renderSettingsBody() {
    const pane = this.shadowRoot && this.shadowRoot.getElementById("settings-pane");
    if (!pane) return;
    const siteId = (this._data && this._data.site_id) || "—";

    let dangerInner;
    if (this._regenBusy) {
      dangerInner = `<p>Regenerating…</p>`;
    } else if (this._regenConfirm) {
      dangerInner = `
        <p>This removes the current site on the relay and mints a new Site ID. Every existing
        device profile becomes invalid — all devices must be re-provisioned. Continue?</p>
        <div class="row-btns">
          <button class="btn-danger" id="regen-confirm">Regenerate</button>
          <button class="btn-plain" id="regen-cancel">Cancel</button>
        </div>`;
    } else {
      dangerInner = `
        <p>Rotate this site's identity. Removes it from the relay and registers a fresh
        Site ID + key. Destructive — all devices must be re-provisioned afterward.</p>
        <button class="btn-outline" id="regen-start">Regenerate Site ID</button>`;
    }

    pane.innerHTML = `
      <h3>Site</h3>
      <p class="sub">Relay site identity for this Home Assistant instance.</p>
      <div class="field">
        <label>Site ID</label>
        <div class="val">${this._esc(siteId)}</div>
      </div>
      <div class="danger">
        <h4>Regenerate Site</h4>
        ${dangerInner}
        ${this._regenMsg ? `<div class="regen-msg">${this._esc(this._regenMsg)}</div>` : ""}
      </div>
    `;

    const pin = (id, fn) => {
      const el = pane.querySelector("#" + id);
      if (el) el.addEventListener("click", fn);
    };
    pin("regen-start", () => { this._regenConfirm = true; this._regenMsg = ""; this._renderSettingsBody(); });
    pin("regen-cancel", () => { this._regenConfirm = false; this._renderSettingsBody(); });
    pin("regen-confirm", () => this._regenerate());
  }

  _update() {
    if (!this.shadowRoot) return;
    const root = this.shadowRoot;

    const errEl = root.getElementById("err");
    errEl.innerHTML = this._error
      ? `<div class="errbar">Failed to load Casa data: ${this._esc(this._error)}</div>`
      : "";

    const data = this._data || { stats: {}, devices: [], accounts: [], site_id: null };
    const s = data.stats || {};

    root.getElementById("stats").innerHTML = `
      <div class="stat"><div class="value">${s.devices ?? 0}</div><div class="label">Devices</div></div>
      <div class="stat"><div class="value">${s.managed_users ?? 0}</div><div class="label">Managed Users</div></div>
      <div class="stat ${(s.orphaned ?? 0) > 0 ? "err" : ""}"><div class="value">${s.orphaned ?? 0}</div><div class="label">Orphaned</div></div>
      <div class="stat ${(s.stale ?? 0) > 0 ? "warn" : ""}"><div class="value">${s.stale ?? 0}</div><div class="label">Stale</div></div>
      <div class="stat site"><div class="value">${this._esc(data.site_id) || "—"}</div><div class="label">Site ID</div></div>
    `;

    const devices = data.devices || [];
    root.getElementById("devices").innerHTML = devices.length
      ? `<table>
          <thead><tr><th>User</th><th>Device</th><th>IP</th><th>Last Seen</th><th>Status</th></tr></thead>
          <tbody>${devices.map((d) => `
            <tr>
              <td>${this._esc(d.username)}${d.native ? ' <span class="badge" style="background:var(--secondary-background-color);color:var(--primary-text-color)">native</span>' : ""}</td>
              <td><code>${this._esc((d.device_id || "").slice(0, 8))}…</code></td>
              <td>${this._esc(d.ip) || "—"}</td>
              <td>${this._fmtTime(d.last_seen)}</td>
              <td>${d.orphaned ? '<span class="badge orphan">orphan</span>' : ""}${d.stale ? '<span class="badge stale">stale</span>' : ""}${!d.orphaned && !d.stale && d.push_registered ? '<span class="badge ok">ok</span>' : ""}</td>
            </tr>`).join("")}
          </tbody>
        </table>`
      : `<div class="empty">No devices registered.</div>`;

    const accounts = data.accounts || [];
    root.getElementById("accounts").innerHTML = accounts.length
      ? `<table>
          <thead><tr><th>Name</th><th>Username</th><th>Devices</th><th>Created</th></tr></thead>
          <tbody>${accounts.map((a) => `
            <tr>
              <td>${this._esc(a.name)}</td>
              <td>${this._esc(a.username)}</td>
              <td>${a.device_count ?? 0}</td>
              <td>${this._fmtTime(a.created_at)}</td>
            </tr>`).join("")}
          </tbody>
        </table>`
      : `<div class="empty">No managed accounts.</div>`;

    // Keep the open settings pane's Site ID in sync after a refresh.
    const overlay = root.getElementById("overlay");
    if (overlay && !overlay.classList.contains("hidden")) {
      this._renderSettingsBody();
    }
  }
}

customElements.define("casa-admin-panel", CasaAdminPanel);
