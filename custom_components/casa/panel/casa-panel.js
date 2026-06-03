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
        tr.device-row { cursor: pointer; transition: background-color 0.2s; }
        tr.device-row:hover { background-color: var(--secondary-background-color, #f5f5f5); }
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

        /* WireGuard profiles */
        .wg-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
        .btn-primary { background: var(--primary-color, #03a9f4); color: var(--text-primary-color, #fff); border: none; border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: 500; cursor: pointer; }
        .btn-primary:hover, .btn-plain:hover { filter: brightness(.93); }
        .wg-card {
          background: var(--secondary-background-color, #f5f5f5); border-radius: 10px; padding: 14px 16px;
          margin-bottom: 10px; display: flex; align-items: flex-start; gap: 14px;
        }
        .wg-card .wg-info { flex: 1; min-width: 0; }
        .wg-card .wg-alias { font-weight: 600; font-size: 14px; margin-bottom: 2px; }
        .wg-card .wg-meta { font-size: 12px; color: var(--secondary-text-color, #727272); margin-bottom: 6px; }
        .wg-card .wg-preview {
          font-family: monospace; font-size: 11px; white-space: pre; overflow: hidden;
          text-overflow: ellipsis; max-height: 48px; color: var(--secondary-text-color, #727272);
          background: var(--card-background-color, #fff); border-radius: 6px; padding: 6px 8px;
        }
        .wg-card .wg-actions { display: flex; gap: 4px; flex-shrink: 0; }
        .wg-card .wg-btn {
          background: none; border: none; cursor: pointer; font-size: 16px;
          padding: 4px 6px; border-radius: 6px; line-height: 1;
        }
        .wg-card .wg-btn:hover { background: var(--divider-color, #e8e8e8); }
        .wg-card .wg-btn.del { color: var(--error-color, #db4437); }
        .wg-card .wg-btn.del:hover { background: rgba(219,68,55,.12); }
        .wg-form { background: var(--secondary-background-color, #f5f5f5); border-radius: 10px; padding: 16px; margin-bottom: 16px; }
        .wg-form .form-field { margin-bottom: 12px; }
        .wg-form label { display: block; font-size: 13px; color: var(--secondary-text-color, #727272); margin-bottom: 4px; }
        .wg-form input, .wg-form textarea {
          width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 6px;
          border: 1px solid var(--divider-color, #ddd); font-size: 13px; font-family: inherit;
          background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);
        }
        .wg-form textarea { font-family: monospace; min-height: 100px; resize: vertical; }
        .wg-form .form-btns { display: flex; gap: 8px; margin-top: 4px; }
        .wg-empty { padding: 24px 0; text-align: center; color: var(--secondary-text-color, #727272); font-size: 14px; }

        /* Provision profiles */
        .pp-card {
          background: var(--secondary-background-color, #f5f5f5); border-radius: 10px; padding: 14px 16px;
          margin-bottom: 10px; display: flex; align-items: flex-start; gap: 14px;
        }
        .pp-card .pp-info { flex: 1; min-width: 0; }
        .pp-card .pp-name { font-weight: 600; font-size: 14px; margin-bottom: 2px; }
        .pp-card .pp-meta { font-size: 12px; color: var(--secondary-text-color, #727272); }
        .pp-card .pp-actions { display: flex; gap: 4px; flex-shrink: 0; }
        .pp-card .pp-btn {
          background: none; border: none; cursor: pointer; font-size: 16px;
          padding: 4px 6px; border-radius: 6px; line-height: 1;
        }
        .pp-card .pp-btn:hover { background: var(--divider-color, #e8e8e8); }
        .pp-card .pp-btn.del { color: var(--error-color, #db4437); }
        .pp-card .pp-btn.del:hover { background: rgba(219,68,55,.12); }

        /* Profile editor overlay */
        .editor-overlay {
          position: fixed; inset: 0; z-index: 10001;
          background: rgba(0,0,0,.42);
          display: flex; align-items: center; justify-content: center;
        }
        .editor-overlay.hidden { display: none; }
        .editor-modal {
          background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);
          border-radius: 16px; width: 680px; max-width: 94vw; max-height: 88vh;
          display: flex; flex-direction: column; box-shadow: 0 24px 64px rgba(0,0,0,.36);
        }
        .editor-header {
          display: flex; align-items: center; padding: 18px 24px; gap: 12px;
          border-bottom: 1px solid var(--divider-color, #e0e0e0); flex-shrink: 0;
        }
        .editor-header h3 { flex: 1; margin: 0; font-size: 18px; }
        .editor-header .close {
          background: none; border: none; cursor: pointer; font-size: 22px;
          color: var(--secondary-text-color, #727272); line-height: 1;
        }
        .editor-body { flex: 1; overflow-y: auto; padding: 20px 24px; }
        .editor-footer {
          display: flex; justify-content: flex-end; gap: 8px; padding: 16px 24px;
          border-top: 1px solid var(--divider-color, #e0e0e0); flex-shrink: 0;
        }
        .editor-section { margin-bottom: 20px; }
        .editor-section h4 {
          margin: 0 0 10px; font-size: 13px; font-weight: 600; text-transform: uppercase;
          letter-spacing: .5px; color: var(--secondary-text-color, #727272);
        }
        .editor-section hr {
          border: none; border-top: 1px solid var(--divider-color, #eee); margin: 0 0 12px;
        }
        .editor-row { margin-bottom: 12px; }
        .editor-row label { display: block; font-size: 13px; color: var(--secondary-text-color, #727272); margin-bottom: 4px; }
        .editor-row input[type="text"], .editor-row input[type="number"], .editor-row input[type="password"],
        .editor-row select, .editor-row textarea {
          width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 6px;
          border: 1px solid var(--divider-color, #ddd); font-size: 13px; font-family: inherit;
          background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);
        }
        .editor-row textarea { font-family: monospace; min-height: 80px; resize: vertical; }
        .editor-row select { appearance: auto; }
        .editor-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
        .editor-toggle {
          display: flex; align-items: center; gap: 8px; margin-bottom: 10px; cursor: pointer; font-size: 13px;
        }
        .editor-toggle input[type="checkbox"] {
          width: 16px; height: 16px; margin: 0; accent-color: var(--primary-color, #03a9f4);
        }
        .editor-msg { font-size: 13px; margin-top: 8px; }
        ha-icon {
          --mdc-icon-size: 24px;
        }
        .icon-btn ha-icon {
          --mdc-icon-size: 24px;
        }
        .modal .close ha-icon, .editor-header .close ha-icon {
          --mdc-icon-size: 20px;
        }
        .btn-primary ha-icon, .btn-plain ha-icon {
          --mdc-icon-size: 16px;
        }
        .wg-btn ha-icon, .pp-btn ha-icon {
          --mdc-icon-size: 18px;
        }
      </style>
      <div class="toolbar">
        <button class="icon-btn menu" id="menu" title="Menu"><ha-icon icon="mdi:menu"></ha-icon></button>
        <span>Casa Admin</span>
        <span class="spacer"></span>
        <button class="icon-btn" id="settings" title="Settings"><ha-icon icon="mdi:cog"></ha-icon></button>
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
          <button class="close" id="settings-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          <div class="nav">
            <div class="title">Settings</div>
            <div class="tab active" data-tab="site"><ha-icon icon="mdi:earth"></ha-icon> Site</div>
            <div class="tab" data-tab="wireguard"><ha-icon icon="mdi:shield-key"></ha-icon> WireGuard</div>
            <div class="tab" data-tab="profiles"><ha-icon icon="mdi:clipboard-text-multiple-outline"></ha-icon> Profiles</div>
          </div>
          <div class="pane" id="settings-pane"></div>
        </div>
      </div>
      </div>
      <div class="editor-overlay hidden" id="profile-overlay">
        <div class="editor-modal">
          <div class="editor-header">
            <h3 id="editor-title">New Profile</h3>
            <button class="close" id="editor-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          </div>
          <div class="editor-body" id="editor-body"></div>
          <div class="editor-footer">
            <button class="btn-plain" id="editor-cancel">Cancel</button>
            <button class="btn-primary" id="editor-save">Save</button>
          </div>
        </div>
      </div>
      <div class="editor-overlay hidden" id="wg-overlay">
        <div class="editor-modal">
          <div class="editor-header">
            <h3 id="wg-editor-title">New WireGuard Profile</h3>
            <button class="close" id="wg-editor-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          </div>
          <div class="editor-body" id="wg-editor-body"></div>
          <div class="editor-footer">
            <button class="btn-plain" id="wg-editor-cancel">Cancel</button>
            <button class="btn-primary" id="wg-editor-save">Save</button>
          </div>
        </div>
      </div>
      <div class="editor-overlay hidden" id="device-overlay">
        <div class="editor-modal">
          <div class="editor-header">
            <h3 id="device-editor-title">Device Inspector</h3>
            <button class="close" id="device-editor-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          </div>
          <div class="editor-body" id="device-editor-body"></div>
          <div class="editor-footer">
            <button class="btn-plain" id="device-editor-cancel">Close</button>
          </div>
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

    // Settings tab switching
    sr.querySelectorAll(".modal .nav .tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        this._settingsTab = tab.dataset.tab;
        sr.querySelectorAll(".modal .nav .tab").forEach((t) => t.classList.remove("active"));
        tab.classList.add("active");
        this._renderSettingsBody();
      });
    });
    sr.getElementById("overlay").addEventListener("click", (e) => {
      if (e.target === sr.getElementById("overlay")) this._closeSettings();
    });

    if (this.isConnected && !this._timer) {
      this._timer = setInterval(() => this._load(), 30000);
    }
  }

  _renderSettingsBody() {
    if (this._settingsTab === "wireguard") {
      this._renderWireGuardPane();
      return;
    }
    if (this._settingsTab === "profiles") {
      this._renderProfilesPane();
      return;
    }
    this._renderSitePane();
  }

  _renderSitePane() {
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

  _renderWireGuardPane() {
    const pane = this.shadowRoot && this.shadowRoot.getElementById("settings-pane");
    if (!pane) return;

    // Load profiles on first visit
    if (!this._wgProfiles && !this._wgLoading) {
      this._loadWgProfiles();
    }

    const profiles = this._wgProfiles || [];

    let listHtml;
    if (this._wgLoading) {
      listHtml = `<div class="wg-empty">Loading…</div>`;
    } else if (profiles.length === 0) {
      listHtml = `<div class="wg-empty">No WireGuard profiles.</div>`;
    } else {
      listHtml = profiles.map((p) => {
        const preview = (p.config || "").split("\n").slice(0, 3).join("\n");
        return `
          <div class="wg-card">
            <div class="wg-info">
              <div class="wg-alias">${this._esc(p.alias)}</div>
              <div class="wg-meta">${this._fmtTime(p.created_at)}${p.excluded_wifi ? " · Excl: " + this._esc(p.excluded_wifi) : ""}</div>
              <div class="wg-preview">${this._esc(preview)}</div>
            </div>
            <div class="wg-actions">
              <button class="wg-btn" data-id="${this._esc(p.id)}" data-action="edit" title="Edit"><ha-icon icon="mdi:pencil"></ha-icon></button>
              <button class="wg-btn del" data-id="${this._esc(p.id)}" data-action="delete" title="Delete"><ha-icon icon="mdi:delete"></ha-icon></button>
            </div>
          </div>`;
      }).join("");
    }

    pane.innerHTML = `
      <h3>WireGuard Profiles</h3>
      <p class="sub">Manage stored WireGuard VPN configurations.</p>
      <div class="wg-toolbar">
        <button class="btn-primary" id="wg-add"><ha-icon icon="mdi:plus"></ha-icon> Add Profile</button>
        <button class="btn-plain" id="wg-refresh"><ha-icon icon="mdi:refresh"></ha-icon> Refresh</button>
      </div>
      ${listHtml}
    `;

    pane.querySelector("#wg-add").addEventListener("click", () => this._openWgEditor(null));
    pane.querySelector("#wg-refresh").addEventListener("click", () => this._loadWgProfiles());

    pane.querySelectorAll(".wg-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.id;
        if (btn.dataset.action === "edit") {
          const profile = (this._wgProfiles || []).find((p) => p.id === id);
          if (profile) this._openWgEditor(profile);
        } else {
          this._deleteWgProfile(id);
        }
      });
    });
  }

  async _loadWgProfiles() {
    if (!this._hass) return;
    this._wgLoading = true;
    this._renderWireGuardPane();
    try {
      const res = await this._hass.callApi("GET", "casa/admin/wireguard_profiles");
      this._wgProfiles = res.profiles || [];
    } catch (err) {
      this._wgProfiles = [];
    }
    this._wgLoading = false;
    this._renderWireGuardPane();
  }

  _openWgEditor(profile) {
    this._wgEditing = profile;
    this._wgFormError = "";
    const sr = this.shadowRoot;
    const overlay = sr.getElementById("wg-overlay");
    overlay.classList.remove("hidden");
    sr.getElementById("wg-editor-title").textContent = profile ? "Edit WireGuard Profile" : "New WireGuard Profile";
    this._renderWgEditorBody();

    // Bind buttons
    const bindOnce = (id, fn) => {
      const el = sr.getElementById(id);
      if (el) {
        const clone = el.cloneNode(true);
        el.parentNode.replaceChild(clone, el);
        clone.addEventListener("click", fn);
      }
    };
    bindOnce("wg-editor-close", () => this._closeWgEditor());
    bindOnce("wg-editor-cancel", () => this._closeWgEditor());
    bindOnce("wg-editor-save", () => this._saveWgProfileFromEditor());
    overlay.onclick = (e) => { if (e.target === overlay) this._closeWgEditor(); };
  }

  _closeWgEditor() {
    this.shadowRoot.getElementById("wg-overlay").classList.add("hidden");
  }

  _renderWgEditorBody() {
    const body = this.shadowRoot.getElementById("wg-editor-body");
    if (!body) return;
    const p = this._wgEditing || {};
    const esc = (val) => this._esc(val || "");

    body.innerHTML = `
      <div class="editor-section">
        <h4>WireGuard Connection</h4><hr>
        <div class="editor-row">
          <label>Alias (optional — auto-generated if blank)</label>
          <input type="text" id="wge-alias" value="${esc(p.alias)}" placeholder="e.g. Office VPN">
        </div>
        <div class="editor-row">
          <label>WireGuard Config *</label>
          <textarea id="wge-config" placeholder="[Interface]\nPrivateKey = ...\nAddress = ...\n\n[Peer]\nPublicKey = ...\nEndpoint = ...">${esc(p.config)}</textarea>
        </div>
        <div class="editor-row">
          <label>Excluded WiFi (optional)</label>
          <input type="text" id="wge-excluded" value="${esc(p.excluded_wifi)}" placeholder="HomeSSID, OfficeSSID">
        </div>
      </div>
      ${this._wgFormError ? `<div class="editor-msg" style="color:var(--error-color,#db4437)">${this._esc(this._wgFormError)}</div>` : ""}
    `;
  }

  async _saveWgProfileFromEditor() {
    const sr = this.shadowRoot;
    const body = sr.getElementById("wg-editor-body");
    const gv = (id) => { const el = body.querySelector("#" + id); return el ? el.value : ""; };

    const config = gv("wge-config").trim();
    if (!config) {
      this._wgFormError = "Config is required.";
      this._renderWgEditorBody();
      return;
    }

    const data = {
      alias: gv("wge-alias"),
      config: config,
      excluded_wifi: gv("wge-excluded"),
    };

    try {
      if (this._wgEditing) {
        data.id = this._wgEditing.id;
        await this._hass.callApi("PUT", "casa/admin/wireguard_profiles", data);
      } else {
        await this._hass.callApi("POST", "casa/admin/wireguard_profiles", data);
      }
      this._closeWgEditor();
      await this._loadWgProfiles();
    } catch (err) {
      this._wgFormError = "Failed: " + ((err && err.message) || err);
      this._renderWgEditorBody();
    }
  }
  async _deleteWgProfile(id) {
    if (!this._hass) return;
    try {
      await this._hass.callApi("DELETE", "casa/admin/wireguard_profiles?id=" + encodeURIComponent(id));
      await this._loadWgProfiles();
    } catch (err) {
      await this._loadWgProfiles();
    }
  }

  /* ===== Device Inspector ===== */

  _openDeviceInspector(device) {
    this._inspectingDevice = device;
    this._deviceFormError = "";
    this._deviceFormSuccess = "";
    this._devicePushError = "";
    this._devicePushSuccess = "";
    this._deviceWgError = "";
    this._deviceWgSuccess = "";

    const sr = this.shadowRoot;
    const overlay = sr.getElementById("device-overlay");
    overlay.classList.remove("hidden");
    this._renderDeviceInspectorBody();

    // Bind close/cancel buttons
    const bindOnce = (id, fn) => {
      const el = sr.getElementById(id);
      if (el) {
        const clone = el.cloneNode(true);
        el.parentNode.replaceChild(clone, el);
        clone.addEventListener("click", fn);
      }
    };
    bindOnce("device-editor-close", () => this._closeDeviceInspector());
    bindOnce("device-editor-cancel", () => this._closeDeviceInspector());
    overlay.onclick = (e) => { if (e.target === overlay) this._closeDeviceInspector(); };

    // Fetch WireGuard profiles if not loaded so they are available in dropdown
    if (!this._wgProfiles && !this._wgLoading) {
      this._loadWgProfiles().then(() => this._renderDeviceInspectorBody());
    }
  }

  _closeDeviceInspector() {
    this.shadowRoot.getElementById("device-overlay").classList.add("hidden");
  }

  _renderDeviceInspectorBody() {
    const body = this.shadowRoot.getElementById("device-editor-body");
    if (!body) return;
    const d = this._inspectingDevice || {};
    const esc = (val) => this._esc(val || "");

    const wgList = this._wgProfiles || [];

    body.innerHTML = `
      <style>
        .device-info-grid {
          display: grid;
          grid-template-columns: auto 1fr;
          gap: 8px 16px;
          font-size: 13px;
          margin-bottom: 8px;
        }
        .device-info-grid strong {
          color: var(--secondary-text-color, #727272);
          font-weight: 500;
        }
        .device-sec-box {
          background: var(--secondary-background-color, #f5f5f5);
          padding: 12px;
          border-radius: 8px;
          margin-bottom: 12px;
        }
        .device-sec-box h5 {
          margin: 0 0 8px 0;
          font-size: 13px;
          font-weight: 600;
        }
      </style>
      <div class="editor-section">
        <h4>Device Info</h4><hr>
        <div class="device-info-grid">
          <strong>Device ID</strong>
          <span><code>${esc(d.device_id)}</code></span>
          <strong>Associated User</strong>
          <span>${esc(d.username)}${d.native ? ' <span class="badge" style="background:var(--secondary-background-color);color:var(--primary-text-color)">native</span>' : ""}</span>
          <strong>IP Address</strong>
          <span>${esc(d.ip) || "—"}</span>
          <strong>Registered At</strong>
          <span>${this._fmtTime(d.registered_at)}</span>
          <strong>Last Seen</strong>
          <span>${this._fmtTime(d.last_seen)}</span>
          <strong>Push Status</strong>
          <span>${d.push_registered ? "Registered" : "Not Registered"}</span>
          <strong>Token Suffix</strong>
          <span>${d.last_12_token ? "..." + esc(d.last_12_token) : "—"}</span>
          <strong>Status</strong>
          <span>
            ${d.orphaned ? '<span class="badge orphan">orphan</span>' : ""}
            ${d.stale ? '<span class="badge stale">stale</span>' : ""}
            ${!d.orphaned && !d.stale && d.push_registered ? '<span class="badge ok">ok</span>' : ""}
          </span>
        </div>
      </div>

      <div class="editor-section">
        <h4>Device Settings</h4><hr>
        <div class="editor-row">
          <label>Device Alias (Friendly Name)</label>
          <div style="display:flex; gap:8px; align-items: center;">
            <input type="text" id="de-alias" value="${esc(d.alias)}" placeholder="e.g. Bryce's iPad" style="flex:1;">
            <button class="btn-primary" id="de-save-alias">Save</button>
          </div>
          ${this._deviceFormError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 4px;">${esc(this._deviceFormError)}</div>` : ""}
          ${this._deviceFormSuccess ? `<div class="editor-msg" style="color:var(--success-color,#43a047); margin-top: 4px;">${esc(this._deviceFormSuccess)}</div>` : ""}
        </div>
      </div>

      <div class="editor-section">
        <h4>Actions</h4><hr>
        
        <div class="device-sec-box">
          <h5>Test Push Notification</h5>
          <div class="editor-row">
            <label>Title</label>
            <input type="text" id="de-push-title" value="Test Notification">
          </div>
          <div class="editor-row">
            <label>Message</label>
            <input type="text" id="de-push-message" value="Hello from Home Assistant!">
          </div>
          <button class="btn-primary" id="de-send-push">Send Notification</button>
          ${this._devicePushError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 4px;">${esc(this._devicePushError)}</div>` : ""}
          ${this._devicePushSuccess ? `<div class="editor-msg" style="color:var(--success-color,#43a047); margin-top: 4px;">${esc(this._devicePushSuccess)}</div>` : ""}
        </div>

        <div class="device-sec-box">
          <h5>Push WireGuard VPN Profile</h5>
          <div class="editor-row">
            <label>WireGuard Profile</label>
            <select id="de-wg-profile" style="width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--divider-color, #ddd); font-size: 13px; font-family: inherit; background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);">
              <option value="">-- None / Revoke VPN --</option>
              ${wgList.map(p => `<option value="${esc(p.id)}">${esc(p.alias)}</option>`).join("")}
            </select>
          </div>
          <button class="btn-primary" id="de-push-wg">Push VPN Profile</button>
          ${this._deviceWgError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 4px;">${esc(this._deviceWgError)}</div>` : ""}
          ${this._deviceWgSuccess ? `<div class="editor-msg" style="color:var(--success-color,#43a047); margin-top: 4px;">${esc(this._deviceWgSuccess)}</div>` : ""}
        </div>
      </div>
    `;

    // Bind event listeners inside inspector body
    body.querySelector("#de-save-alias").addEventListener("click", () => this._saveDeviceAlias());
    body.querySelector("#de-send-push").addEventListener("click", () => this._sendDeviceTestPush());
    body.querySelector("#de-push-wg").addEventListener("click", () => this._pushDeviceWg());
  }

  async _saveDeviceAlias() {
    const sr = this.shadowRoot;
    const input = sr.getElementById("de-alias");
    if (!input) return;
    const alias = input.value.trim();

    this._deviceFormError = "";
    this._deviceFormSuccess = "";
    this._renderDeviceInspectorBody();

    try {
      await this._hass.callApi("PUT", "casa/admin/device", {
        device_id: this._inspectingDevice.device_id,
        alias: alias
      });
      this._deviceFormSuccess = "Alias updated successfully.";
      this._inspectingDevice.alias = alias;
      this._load();
    } catch (err) {
      this._deviceFormError = "Failed: " + ((err && err.message) || err);
    }
    this._renderDeviceInspectorBody();
  }

  async _sendDeviceTestPush() {
    const sr = this.shadowRoot;
    const titleEl = sr.getElementById("de-push-title");
    const msgEl = sr.getElementById("de-push-message");
    if (!titleEl || !msgEl) return;

    const title = titleEl.value.trim();
    const message = msgEl.value.trim();

    if (!title || !message) {
      this._devicePushError = "Title and message are required.";
      this._renderDeviceInspectorBody();
      return;
    }

    this._devicePushError = "";
    this._devicePushSuccess = "";
    this._renderDeviceInspectorBody();

    try {
      await this._hass.callService("casa", "notify_user", {
        device_id: this._inspectingDevice.device_id,
        title: title,
        message: message
      });
      this._devicePushSuccess = "Push notification command sent.";
    } catch (err) {
      this._devicePushError = "Failed to send: " + ((err && err.message) || err);
    }
    this._renderDeviceInspectorBody();
  }

  async _pushDeviceWg() {
    const sr = this.shadowRoot;
    const select = sr.getElementById("de-wg-profile");
    if (!select) return;

    const profileId = select.value;
    
    this._deviceWgError = "";
    this._deviceWgSuccess = "";
    this._renderDeviceInspectorBody();

    try {
      if (profileId) {
        const profile = (this._wgProfiles || []).find((p) => p.id === profileId);
        if (!profile) {
          throw new Error("Selected WireGuard profile not found.");
        }
        await this._hass.callService("casa", "update_wireguard", {
          device_id: this._inspectingDevice.device_id,
          action: "update",
          wireguard_config: profile.config,
          wireguard_excluded_wifi: profile.excluded_wifi || ""
        });
        this._deviceWgSuccess = `WireGuard profile '${profile.alias}' push command sent.`;
      } else {
        await this._hass.callService("casa", "update_wireguard", {
          device_id: this._inspectingDevice.device_id,
          action: "revoke"
        });
        this._deviceWgSuccess = "WireGuard revoke command sent.";
      }
    } catch (err) {
      this._deviceWgError = "Failed to push: " + ((err && err.message) || err);
    }
    this._renderDeviceInspectorBody();
  }

  /* ===== Provision Profiles ===== */

  _renderProfilesPane() {
    const pane = this.shadowRoot && this.shadowRoot.getElementById("settings-pane");
    if (!pane) return;

    if (!this._ppProfiles && !this._ppLoading) {
      this._loadProvisionProfiles();
    }

    const profiles = this._ppProfiles || [];

    let listHtml;
    if (this._ppLoading) {
      listHtml = `<div class="wg-empty">Loading…</div>`;
    } else if (profiles.length === 0) {
      listHtml = `<div class="wg-empty">No provisioning profiles saved.</div>`;
    } else {
      listHtml = profiles.map((p) => {
        const f = p.fields || {};
        return `
          <div class="pp-card">
            <div class="pp-info">
              <div class="pp-name">${this._esc(p.name)}</div>
              <div class="pp-meta">${this._esc(f.username || "—")} · ${this._esc(f.host_url || "—")} · ${this._fmtTime(p.created_at)}</div>
            </div>
            <div class="pp-actions">
              <button class="pp-btn" data-id="${this._esc(p.id)}" data-action="edit" title="Edit"><ha-icon icon="mdi:pencil"></ha-icon></button>
              <button class="pp-btn del" data-id="${this._esc(p.id)}" data-action="delete" title="Delete"><ha-icon icon="mdi:delete"></ha-icon></button>
            </div>
          </div>`;
      }).join("");
    }

    pane.innerHTML = `
      <h3>Provisioning Profiles</h3>
      <p class="sub">Saved provisioning templates for the casa.provision service.</p>
      <div class="wg-toolbar">
        <button class="btn-primary" id="pp-add"><ha-icon icon="mdi:plus"></ha-icon> New Profile</button>
        <button class="btn-plain" id="pp-refresh"><ha-icon icon="mdi:refresh"></ha-icon> Refresh</button>
      </div>
      ${listHtml}
    `;

    pane.querySelector("#pp-add").addEventListener("click", () => this._openProfileEditor(null));
    pane.querySelector("#pp-refresh").addEventListener("click", () => this._loadProvisionProfiles());

    pane.querySelectorAll(".pp-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.id;
        if (btn.dataset.action === "edit") {
          const profile = (this._ppProfiles || []).find((p) => p.id === id);
          if (profile) this._openProfileEditor(profile);
        } else {
          this._deleteProvisionProfile(id);
        }
      });
    });
  }

  async _openProfileEditor(profile) {
    this._ppEditing = profile;
    this._ppFormError = "";
    const sr = this.shadowRoot;
    const overlay = sr.getElementById("profile-overlay");
    overlay.classList.remove("hidden");
    sr.getElementById("editor-title").textContent = profile ? "Edit Profile" : "New Profile";
    
    // Bind buttons (cancel/close can be bound immediately)
    const bindOnce = (id, fn) => {
      const el = sr.getElementById(id);
      if (el) {
        const clone = el.cloneNode(true);
        el.parentNode.replaceChild(clone, el);
        clone.addEventListener("click", fn);
      }
    };
    bindOnce("editor-close", () => this._closeProfileEditor());
    bindOnce("editor-cancel", () => this._closeProfileEditor());
    overlay.onclick = (e) => { if (e.target === overlay) this._closeProfileEditor(); };

    const body = sr.getElementById("editor-body");
    if (body) {
      body.innerHTML = `<div style="padding: 20px; text-align: center; color: var(--secondary-text-color, #727272);">Loading config options…</div>`;
    }

    // Ensure WireGuard profiles are loaded
    if (!this._wgProfiles) {
      try {
        const res = await this._hass.callApi("GET", "casa/admin/wireguard_profiles");
        this._wgProfiles = res.profiles || [];
      } catch (err) {
        this._wgProfiles = [];
      }
    }

    this._renderProfileEditorBody();
    bindOnce("editor-save", () => this._saveProfileFromEditor());

    // Visibility toggles for linked WireGuard configuration
    const wgSelect = sr.getElementById("pp-wireguard_profile_id");
    const updateWgFieldsVisibility = () => {
      const isLinked = wgSelect && wgSelect.value !== "";
      const configRow = sr.getElementById("pp-wireguard_config-row");
      const exclRow = sr.getElementById("pp-wireguard_excluded_wifi-row");
      if (configRow) configRow.style.display = isLinked ? "none" : "";
      if (exclRow) exclRow.style.display = isLinked ? "none" : "";
    };
    if (wgSelect) {
      wgSelect.addEventListener("change", updateWgFieldsVisibility);
    }
    updateWgFieldsVisibility();
  }

  _closeProfileEditor() {
    this.shadowRoot.getElementById("profile-overlay").classList.add("hidden");
  }

  _renderProfileEditorBody() {
    const body = this.shadowRoot.getElementById("editor-body");
    if (!body) return;
    const f = (this._ppEditing && this._ppEditing.fields) || {};
    const v = (key, def) => this._esc(f[key] !== undefined ? f[key] : def);
    const c = (key, def) => (f[key] !== undefined ? f[key] : def) ? "checked" : "";
    const profileName = this._ppEditing ? this._ppEditing.name : "";

    body.innerHTML = `
      <div class="editor-section">
        <h4>Profile</h4><hr>
        <div class="editor-row">
          <label>Profile Name</label>
          <input type="text" id="pp-name" value="${this._esc(profileName)}" placeholder="Auto-generated if blank">
        </div>
      </div>
      <div class="editor-section">
        <h4>Connection</h4><hr>
        <div class="editor-grid">
          <div class="editor-row">
            <label>Host URL *</label>
            <input type="text" id="pp-host_url" value="${v("host_url", "")}" placeholder="http://192.168.1.21:8123">
          </div>
          <div class="editor-row">
            <label>Username *</label>
            <input type="text" id="pp-username" value="${v("username", "")}" placeholder="guest">
          </div>
          <div class="editor-row">
            <label>Password (optional)</label>
            <input type="text" id="pp-password" value="${v("password", "")}" placeholder="Auto-generated if blank">
          </div>
          <div class="editor-row">
            <label>PIN (optional, max 6 digits)</label>
            <input type="text" id="pp-pin" value="${v("pin", "")}" placeholder="123456" maxlength="6">
          </div>
        </div>
      </div>
      <div class="editor-section">
        <h4>App UI</h4><hr>
        <div class="editor-grid">
          <div class="editor-row">
            <label>Default Dashboard</label>
            <input type="text" id="pp-default_dashboard" value="${v("default_dashboard", "")}" placeholder="/lovelace/home">
          </div>
          <div class="editor-row">
            <label>Welcome URL</label>
            <input type="text" id="pp-welcome_url" value="${v("welcome_url", "")}" placeholder="Optional URL shown after provisioning">
          </div>
          <div class="editor-row">
            <label>Immersive Level</label>
            <select id="pp-immersive_level">
              <option value="1" ${v("immersive_level", "1") === "1" ? "selected" : ""}>Level 1 (Standard)</option>
              <option value="2" ${v("immersive_level", "1") === "2" ? "selected" : ""}>Level 2 (Edge-to-Edge)</option>
              <option value="3" ${v("immersive_level", "1") === "3" ? "selected" : ""}>Level 3 (Fullscreen)</option>
            </select>
          </div>
          <div class="editor-row">
            <label>Theme Color Mode</label>
            <select id="pp-theme_color_mode">
              <option value="inherit" ${v("theme_color_mode", "inherit") === "inherit" ? "selected" : ""}>Inherit from HA</option>
              <option value="custom" ${v("theme_color_mode", "inherit") === "custom" ? "selected" : ""}>Custom Color</option>
              <option value="inherit_with_fallback" ${v("theme_color_mode", "inherit") === "inherit_with_fallback" ? "selected" : ""}>Inherit with Fallback</option>
            </select>
          </div>
          <div class="editor-row">
            <label>Custom Color (Hex)</label>
            <input type="text" id="pp-custom_color" value="${v("custom_color", "#000000")}" placeholder="#03A9F4">
          </div>
        </div>
      </div>
      <div class="editor-section">
        <h4>Permissions</h4><hr>
        <label class="editor-toggle"><input type="checkbox" id="pp-deauthenticate_existing" ${c("deauthenticate_existing", false)}> Deauthenticate Existing Connections</label>
        <label class="editor-toggle"><input type="checkbox" id="pp-allow_all_pages" ${c("allow_all_pages", false)}> Allow All Pages</label>
        <div class="editor-grid">
          <div class="editor-row">
            <label>Allowed Pages (comma-separated)</label>
            <input type="text" id="pp-allowed_pages" value="${v("allowed_pages", "")}" placeholder="/lovelace/home, /dashboard-1/*">
          </div>
          <div class="editor-row">
            <label>Allowed WiFi (comma-separated)</label>
            <input type="text" id="pp-allowed_wifi" value="${v("allowed_wifi", "")}" placeholder="HomeSSID, OfficeSSID">
          </div>
        </div>
      </div>
      <div class="editor-section">
        <h4>Push Notifications & VPN</h4><hr>
        <div class="editor-grid">
          <div class="editor-row">
            <label>Push Notifications</label>
            <select id="pp-push_notifications">
              <option value="false" ${v("push_notifications", "false") === "false" ? "selected" : ""}>Disabled</option>
              <option value="true" ${v("push_notifications", "false") === "true" ? "selected" : ""}>Enabled</option>
              <option value="mandatory" ${v("push_notifications", "false") === "mandatory" ? "selected" : ""}>Mandatory</option>
            </select>
          </div>
        </div>
        <label class="editor-toggle"><input type="checkbox" id="pp-allow_wireguard" ${c("allow_wireguard", false)}> Allow WireGuard</label>
        <div class="editor-row">
          <label>Link WireGuard Profile</label>
          <select id="pp-wireguard_profile_id">
            <option value="" ${v("wireguard_profile_id", "") === "" ? "selected" : ""}>-- None / Custom (Paste Below) --</option>
            ${(this._wgProfiles || []).map(p => `
              <option value="${this._esc(p.id)}" ${v("wireguard_profile_id", "") === p.id ? "selected" : ""}>
                ${this._esc(p.alias)}
              </option>
            `).join("")}
          </select>
        </div>
        <div class="editor-row" id="pp-wireguard_config-row">
          <label>WireGuard Config</label>
          <textarea id="pp-wireguard_config" placeholder="[Interface]\nPrivateKey = ...">${this._esc(f.wireguard_config || "")}</textarea>
        </div>
        <div class="editor-row" id="pp-wireguard_excluded_wifi-row">
          <label>WireGuard Excluded WiFi</label>
          <input type="text" id="pp-wireguard_excluded_wifi" value="${v("wireguard_excluded_wifi", "")}" placeholder="HomeSSID">
        </div>
      </div>
      <div class="editor-section">
        <h4>Timing</h4><hr>
        <div class="editor-grid">
          <div class="editor-row">
            <label>Timeout (minutes, 0 = permanent)</label>
            <input type="number" id="pp-timeout_minutes" value="${f.timeout_minutes !== undefined ? f.timeout_minutes : 5}" min="0" max="60">
          </div>
          <div class="editor-row">
            <label>Session Expiration (hours, 0 = permanent)</label>
            <input type="number" id="pp-expiration_hours" value="${f.expiration_hours !== undefined ? f.expiration_hours : 336}" min="0" max="87600">
          </div>
        </div>
        <label class="editor-toggle"><input type="checkbox" id="pp-password_scramble" ${c("password_scramble", true)}> Scramble Password After Window</label>
        <div class="editor-row">
          <label>Password Scramble In (minutes, 0 = inherit from timeout)</label>
          <input type="number" id="pp-password_scramble_in" value="${f.password_scramble_in !== undefined ? f.password_scramble_in : 0}" min="0" max="120">
        </div>
        <div class="editor-row">
          <label>Cache Control (hours, blank = default 48h)</label>
          <input type="text" id="pp-cache_control_hours" value="${v("cache_control_hours", "")}" placeholder="48">
        </div>
      </div>
      <div class="editor-section">
        <h4>WiFi Provisioning</h4><hr>
        <div class="editor-grid">
          <div class="editor-row">
            <label>Connect WiFi SSID</label>
            <input type="text" id="pp-connect_wifi_ssid" value="${v("connect_wifi_ssid", "")}" placeholder="MyNetwork">
          </div>
          <div class="editor-row">
            <label>Connect WiFi Password</label>
            <input type="password" id="pp-connect_wifi_password" value="${v("connect_wifi_password", "")}" placeholder="Password">
          </div>
        </div>
      </div>
      ${this._ppFormError ? `<div class="editor-msg" style="color:var(--error-color,#db4437)">${this._esc(this._ppFormError)}</div>` : ""}
    `;
  }

  async _saveProfileFromEditor() {
    const sr = this.shadowRoot;
    const body = sr.getElementById("editor-body");
    const gv = (id) => { const el = body.querySelector("#" + id); return el ? el.value : ""; };
    const gc = (id) => { const el = body.querySelector("#" + id); return el ? el.checked : false; };

    const host_url = gv("pp-host_url").trim();
    const username = gv("pp-username").trim();
    if (!host_url || !username) {
      this._ppFormError = "Host URL and Username are required.";
      this._renderProfileEditorBody();
      return;
    }

    const data = {
      name: gv("pp-name"),
      host_url: host_url,
      username: username,
      password: gv("pp-password"),
      pin: gv("pp-pin"),
      default_dashboard: gv("pp-default_dashboard"),
      welcome_url: gv("pp-welcome_url"),
      immersive_level: gv("pp-immersive_level"),
      theme_color_mode: gv("pp-theme_color_mode"),
      custom_color: gv("pp-custom_color"),
      deauthenticate_existing: gc("pp-deauthenticate_existing"),
      allow_all_pages: gc("pp-allow_all_pages"),
      allowed_pages: gv("pp-allowed_pages"),
      allowed_wifi: gv("pp-allowed_wifi"),
      push_notifications: gv("pp-push_notifications"),
      allow_wireguard: gc("pp-allow_wireguard"),
      wireguard_profile_id: gv("pp-wireguard_profile_id"),
      wireguard_config: gv("pp-wireguard_config"),
      wireguard_excluded_wifi: gv("pp-wireguard_excluded_wifi"),
      timeout_minutes: parseInt(gv("pp-timeout_minutes")) || 0,
      password_scramble: gc("pp-password_scramble"),
      password_scramble_in: parseInt(gv("pp-password_scramble_in")) || 0,
      expiration_hours: parseInt(gv("pp-expiration_hours")) || 0,
      connect_wifi_ssid: gv("pp-connect_wifi_ssid"),
      connect_wifi_password: gv("pp-connect_wifi_password"),
      cache_control_hours: gv("pp-cache_control_hours"),
    };

    try {
      if (this._ppEditing) {
        data.id = this._ppEditing.id;
        await this._hass.callApi("PUT", "casa/admin/provision_profiles", data);
      } else {
        await this._hass.callApi("POST", "casa/admin/provision_profiles", data);
      }
      this._closeProfileEditor();
      await this._loadProvisionProfiles();
    } catch (err) {
      this._ppFormError = "Failed: " + ((err && err.message) || err);
      this._renderProfileEditorBody();
    }
  }

  async _loadProvisionProfiles() {
    if (!this._hass) return;
    this._ppLoading = true;
    this._renderProfilesPane();
    try {
      const res = await this._hass.callApi("GET", "casa/admin/provision_profiles");
      this._ppProfiles = res.profiles || [];
    } catch (err) {
      this._ppProfiles = [];
    }
    this._ppLoading = false;
    this._renderProfilesPane();
  }

  async _deleteProvisionProfile(id) {
    if (!this._hass) return;
    try {
      await this._hass.callApi("DELETE", "casa/admin/provision_profiles?id=" + encodeURIComponent(id));
      await this._loadProvisionProfiles();
    } catch (err) {
      await this._loadProvisionProfiles();
    }
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
            <tr class="device-row" data-id="${this._esc(d.device_id)}">
              <td>${this._esc(d.username)}${d.native ? ' <span class="badge" style="background:var(--secondary-background-color);color:var(--primary-text-color)">native</span>' : ""}</td>
              <td>${d.alias ? `<strong>${this._esc(d.alias)}</strong> <small style="color:var(--secondary-text-color)">(${this._esc((d.device_id || "").slice(0, 8))}…)</small>` : `<code>${this._esc((d.device_id || "").slice(0, 8))}…</code>`}</td>
              <td>${this._esc(d.ip) || "—"}</td>
              <td>${this._fmtTime(d.last_seen)}</td>
              <td>${d.orphaned ? '<span class="badge orphan">orphan</span>' : ""}${d.stale ? '<span class="badge stale">stale</span>' : ""}${!d.orphaned && !d.stale && d.push_registered ? '<span class="badge ok">ok</span>' : ""}</td>
            </tr>`).join("")}
          </tbody>
        </table>`
      : `<div class="empty">No devices registered.</div>`;

    // Add click listeners to rows
    root.querySelectorAll(".device-row").forEach((row) => {
      row.addEventListener("click", () => {
        const id = row.dataset.id;
        const device = (devices || []).find((d) => d.device_id === id);
        if (device) this._openDeviceInspector(device);
      });
    });

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
