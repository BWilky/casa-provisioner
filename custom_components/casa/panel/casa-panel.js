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

  async _rotateDeviceKey() {
    if (!this._hass) return;
    this._keyRotBusy = true;
    this._renderSettingsBody();
    try {
      const res = await this._hass.callApi("POST", "casa/admin/regenerate_device_key", {});
      await this._load();
      this._keyRotMsg = `Encryption key rotated (new id ${res.device_key_id}). Devices update on their next heartbeat.`;
    } catch (err) {
      this._keyRotMsg = "Failed: " + ((err && err.message) || err);
    }
    this._keyRotBusy = false;
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

  _fmtExpiry(v) {
    if (!v) return "never";
    const num = Number(v);
    if (!isNaN(num)) {
      return this._fmtTime(num * 1000);
    }
    return this._fmtTime(v);
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
        .badge.pending { background: var(--info-color, #2196f3); color: #fff; }
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
        <button class="icon-btn" id="quick-provision" title="Quick Provision Device" style="margin-right: 8px;"><ha-icon icon="mdi:qrcode-scan"></ha-icon></button>
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
          <div class="card">
            <div style="display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--divider-color, #e0e0e0); padding-right:16px;">
              <h2 style="border-bottom:none;">Managed Accounts</h2>
              <button class="icon-btn" id="add-account-dash" title="Add Guest Account" style="cursor:pointer; background:none; border:none; color:var(--primary-text-color); padding:4px; line-height:1;"><ha-icon icon="mdi:plus"></ha-icon></button>
            </div>
            <div id="accounts"></div>
          </div>
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
            <div class="tab" data-tab="accounts"><ha-icon icon="mdi:account-multiple-outline"></ha-icon> Accounts</div>
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
      <div class="editor-overlay hidden" id="account-overlay">
        <div class="editor-modal">
          <div class="editor-header">
            <h3 id="account-editor-title">Create Guest Account</h3>
            <button class="close" id="account-editor-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          </div>
          <div class="editor-body" id="account-editor-body"></div>
          <div class="editor-footer" id="account-editor-footer">
            <button class="btn-plain" id="account-editor-cancel">Cancel</button>
            <button class="btn-primary" id="account-editor-save">Create</button>
          </div>
        </div>
      </div>
      <div class="editor-overlay hidden" id="provision-overlay">
        <div class="editor-modal" style="width: 600px;">
          <div class="editor-header">
            <h3>Quick Provision Device</h3>
            <button class="close" id="provision-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          </div>
          <div class="editor-body" id="provision-body"></div>
          <div class="editor-footer">
            <button class="btn-plain" id="provision-cancel">Close</button>
          </div>
        </div>
      </div>
      <div class="editor-overlay hidden" id="confirm-overlay" style="z-index: 10002;">
        <div class="editor-modal" style="width: 420px;">
          <div class="editor-header">
            <h3 id="confirm-title">Confirm</h3>
            <button class="close" id="confirm-close" title="Close"><ha-icon icon="mdi:close"></ha-icon></button>
          </div>
          <div class="editor-body" id="confirm-body"></div>
          <div class="editor-footer">
            <button class="btn-plain" id="confirm-cancel">Cancel</button>
            <button class="btn-danger" id="confirm-ok">Delete</button>
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
    sr.getElementById("add-account-dash").addEventListener("click", () => this._openAccountCreator());
    sr.getElementById("quick-provision").addEventListener("click", () => this._openQuickProvision());
    sr.getElementById("provision-close").addEventListener("click", () => this._closeQuickProvision());
    sr.getElementById("provision-cancel").addEventListener("click", () => this._closeQuickProvision());

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
    if (this._settingsTab === "accounts") {
      this._renderAccountsPane();
      return;
    }
    this._renderSitePane();
  }

  _renderSitePane() {
    const pane = this.shadowRoot && this.shadowRoot.getElementById("settings-pane");
    if (!pane) return;
    const siteId = (this._data && this._data.site_id) || "—";
    const keyId = (this._data && this._data.device_key_id) || "—";

    const keyRotInner = this._keyRotBusy
      ? `<p>Rotating…</p>`
      : `<p>Rotate the shared key used to encrypt WireGuard and update pushes. Non-destructive:
         devices pick up the new key on their next heartbeat and fall back to the secure update
         queue in the meantime. No re-provisioning required.</p>
         <button class="btn-outline" id="key-rotate">Rotate Encryption Key</button>`;

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
      <div class="field">
        <label>Encryption Key ID</label>
        <div class="val">${this._esc(keyId)}</div>
      </div>
      <div class="field">
        <h4>Encryption Key</h4>
        ${keyRotInner}
        ${this._keyRotMsg ? `<div class="regen-msg">${this._esc(this._keyRotMsg)}</div>` : ""}
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
    pin("key-rotate", () => this._rotateDeviceKey());
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
    this._deviceProfileError = "";
    this._deviceProfileSuccess = "";
    this._devicePendingError = "";
    this._deviceExpError = "";
    this._deviceExpSuccess = "";
    this._deviceDeprovError = "";

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

    // Fetch provision profiles for the profile-push dropdown
    if (!this._ppProfiles && !this._ppInspectorLoading) {
      this._ppInspectorLoading = true;
      this._hass.callApi("GET", "casa/admin/provision_profiles")
        .then((res) => { this._ppProfiles = (res && res.profiles) || []; })
        .catch(() => { this._ppProfiles = []; })
        .finally(() => { this._ppInspectorLoading = false; this._renderDeviceInspectorBody(); });
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
    const ppList = this._ppProfiles || [];

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
          <strong>App Version</strong>
          <span>${esc(d.app_version) || "—"}</span>
          <strong>Provisioned At</strong>
          <span>${d.provisioned_at ? this._fmtTime(d.provisioned_at) : "—"}</span>
          <strong>Expires At</strong>
          <span>
            ${d.expires_at ? this._fmtExpiry(d.expires_at) : "Never"}
            ${d.expires_at_override != null ? `<span class="badge pending" title="Applied on the device's next heartbeat">pending: ${d.expires_at_override === 0 ? "Never" : this._fmtExpiry(d.expires_at_override)}</span>` : ""}
          </span>
          <strong>VPN Configuration</strong>
          <span>
            ${d.wireguard_configured === true ? '<span class="badge ok">Installed</span>' :
              d.wireguard_configured === false ? '<span class="badge" style="background:var(--secondary-background-color);color:var(--primary-text-color)">Not Installed</span>' : "—"}
          </span>
          <strong>VPN Connection</strong>
          <span>
            ${d.wireguard_connected === true ? '<span class="badge ok">Connected</span>' :
              d.wireguard_connected === false ? '<span class="badge stale">Disconnected</span>' : "—"}
          </span>
          <strong>Active URL</strong>
          <span>
            ${d.current_url ? `<a href="${esc(d.current_url)}" target="_blank" rel="noopener noreferrer" style="color:var(--primary-color,#03a9f4);text-decoration:none;word-break:break-all;">${esc(d.current_url)}</a>` : "—"}
          </span>
        </div>
      </div>

      ${(d.pending_updates ?? 0) > 0 ? `
      <div class="editor-section">
        <h4>Pending Updates <span class="badge pending">${d.pending_updates}</span></h4><hr>
        <p style="font-size:12px; color:var(--secondary-text-color,#727272); margin:0 0 8px 0;">
          Queued for this device. Consumed on the device's next heartbeat (or via push), then cleared.
        </p>
        <div style="display:flex; flex-direction:column; gap:6px;">
          ${(d.pending_update_list || []).map((u) => `
            <div style="display:flex; align-items:center; gap:8px; background:var(--secondary-background-color,#f5f5f5); border-radius:6px; padding:8px 10px; font-size:13px;">
              <span class="badge" style="background:var(--primary-color,#03a9f4); color:#fff; margin:0;">${esc(u.type)}</span>
              <span>${esc(u.action)}</span>
              <span style="margin-left:auto; color:var(--secondary-text-color,#727272); font-size:12px;">${this._fmtTime(u.created_at)}</span>
              <button class="pu-del" data-id="${esc(u.id)}" title="Delete queued update" style="background:none; border:none; cursor:pointer; color:var(--error-color,#db4437); padding:2px 4px; line-height:1; display:flex; align-items:center;"><ha-icon icon="mdi:delete-outline"></ha-icon></button>
            </div>`).join("")}
        </div>
        ${this._devicePendingError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 6px;">${esc(this._devicePendingError)}</div>` : ""}
      </div>` : ""}

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
          <label class="editor-toggle" style="display:flex; align-items:center; gap:6px; font-size:13px; margin-top:6px;"><input type="checkbox" id="de-wg-send-push"> Send Update via Push</label>
          <label class="editor-toggle" style="display:flex; align-items:center; gap:6px; font-size:13px; margin-top:4px;"><input type="checkbox" id="de-wg-notify-push"> Notify via Push</label>
          <button class="btn-primary" id="de-push-wg" style="margin-top:8px;">Queue VPN Update</button>
          ${this._deviceWgError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 4px;">${esc(this._deviceWgError)}</div>` : ""}
          ${this._deviceWgSuccess ? `<div class="editor-msg" style="color:var(--success-color,#43a047); margin-top: 4px;">${esc(this._deviceWgSuccess)}</div>` : ""}
        </div>

        <div class="device-sec-box">
          <h5>Push Provisioning Profile</h5>
          <div class="editor-row">
            <label>Provision Profile</label>
            <select id="de-pp-profile" style="width: 100%; box-sizing: border-box; padding: 8px 10px; border-radius: 6px; border: 1px solid var(--divider-color, #ddd); font-size: 13px; font-family: inherit; background: var(--card-background-color, #fff); color: var(--primary-text-color, #212121);">
              <option value="">-- Select a profile --</option>
              ${ppList.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("")}
            </select>
          </div>
          <label class="editor-toggle" style="display:flex; align-items:center; gap:6px; font-size:13px; margin-top:6px;"><input type="checkbox" id="de-pp-send-push"> Send Update via Push</label>
          <label class="editor-toggle" style="display:flex; align-items:center; gap:6px; font-size:13px; margin-top:4px;"><input type="checkbox" id="de-pp-notify-push"> Notify via Push</label>
          <button class="btn-primary" id="de-push-pp" style="margin-top:8px;">Queue Profile Update</button>
          ${this._deviceProfileError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 4px;">${esc(this._deviceProfileError)}</div>` : ""}
          ${this._deviceProfileSuccess ? `<div class="editor-msg" style="color:var(--success-color,#43a047); margin-top: 4px;">${esc(this._deviceProfileSuccess)}</div>` : ""}
        </div>

        <div class="device-sec-box">
          <h5>Session Expiration</h5>
          <div style="font-size:13px; margin-bottom:8px;">
            Current: <strong>${d.expires_at ? this._fmtExpiry(d.expires_at) : "Never"}</strong>
            ${d.expires_at_override != null ? ` — pending change to <strong>${d.expires_at_override === 0 ? "Never" : this._fmtExpiry(d.expires_at_override)}</strong> <a href="#" id="de-exp-cancel" style="color:var(--primary-color,#03a9f4);">cancel</a>` : ""}
          </div>
          ${d.expires_at && (d.expires_at * 1000) < Date.now() ? `<div class="editor-msg" style="color:var(--warning-color,#f4b400); margin-bottom:6px;">This device's session has already expired — it has likely wiped itself and may never pick up a new expiration.</div>` : ""}
          <div class="editor-row">
            <label>New Expiration</label>
            <div style="display:flex; gap:8px; align-items: center;">
              <input type="datetime-local" id="de-exp-datetime" style="flex:1;">
              <button class="btn-primary" id="de-exp-set">Set</button>
            </div>
          </div>
          <div style="display:flex; gap:6px; flex-wrap:wrap; margin-top:6px;">
            <button class="btn-plain" id="de-exp-24h" style="padding:6px 10px; font-size:12px;">+24 h</button>
            <button class="btn-plain" id="de-exp-7d" style="padding:6px 10px; font-size:12px;">+7 d</button>
            <button class="btn-plain" id="de-exp-30d" style="padding:6px 10px; font-size:12px;">+30 d</button>
            <button class="btn-plain" id="de-exp-permanent" style="padding:6px 10px; font-size:12px;">Make Permanent</button>
            <button class="btn-plain" id="de-exp-now" style="padding:6px 10px; font-size:12px; color:var(--error-color,#db4437);">Expire Now</button>
          </div>
          <p style="font-size:12px; color:var(--secondary-text-color,#727272); margin:8px 0 0 0;">
            Changes are delivered on the device's next heartbeat (up to ~5 minutes).
          </p>
          ${this._deviceExpError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 4px;">${esc(this._deviceExpError)}</div>` : ""}
          ${this._deviceExpSuccess ? `<div class="editor-msg" style="color:var(--success-color,#43a047); margin-top: 4px;">${esc(this._deviceExpSuccess)}</div>` : ""}
        </div>

        <div class="device-sec-box" style="border: 1px solid var(--error-color, #db4437);">
          <h5 style="color:var(--error-color,#db4437);">Danger Zone</h5>
          <div style="display:flex; gap:8px; flex-wrap:wrap;">
            <button class="btn-plain" id="de-delete-record" style="padding:8px 14px; font-size:13px; color:var(--secondary-text-color,#727272); border:1px solid var(--divider-color,#ddd); border-radius:6px;">
              <ha-icon icon="mdi:delete-outline" style="--mdc-icon-size:16px;"></ha-icon> Delete Record
            </button>
            <button class="btn-plain del" id="de-deprovision" style="padding:8px 14px; font-size:13px; color:var(--error-color,#db4437); border:1px solid var(--error-color,#db4437); border-radius:6px;">
              <ha-icon icon="mdi:cellphone-remove" style="--mdc-icon-size:16px;"></ha-icon> Deprovision Device
            </button>
          </div>
          <p style="font-size:12px; color:var(--secondary-text-color,#727272); margin:8px 0 0 0;">
            <strong>Delete Record</strong> removes the record and revokes access without touching the app — for stale/orphaned entries.
            <strong>Deprovision</strong> additionally wipes the Casa app on the device via push.
          </p>
          ${this._deviceDeprovError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top: 6px;">${esc(this._deviceDeprovError)}</div>` : ""}
        </div>
      </div>
    `;

    // Bind event listeners inside inspector body
    body.querySelector("#de-save-alias").addEventListener("click", () => this._saveDeviceAlias());
    body.querySelector("#de-send-push").addEventListener("click", () => this._sendDeviceTestPush());
    body.querySelector("#de-push-wg").addEventListener("click", () => this._pushDeviceWg());
    body.querySelector("#de-push-pp").addEventListener("click", () => this._pushDeviceProfile());

    // Session expiration controls. Quick-extends are relative to the later of
    // now / the current (or pending) expiry, so repeated clicks stack.
    const expBase = () => {
      const cur = d.expires_at_override != null && d.expires_at_override > 0 ? d.expires_at_override : (d.expires_at || 0);
      return Math.max(Math.floor(Date.now() / 1000), cur);
    };
    body.querySelector("#de-exp-set").addEventListener("click", () => {
      const input = body.querySelector("#de-exp-datetime");
      if (!input || !input.value) {
        this._deviceExpError = "Pick a date and time first.";
        this._renderDeviceInspectorBody();
        return;
      }
      const ts = Math.floor(new Date(input.value).getTime() / 1000);
      if (!Number.isFinite(ts) || ts <= Math.floor(Date.now() / 1000)) {
        this._deviceExpError = "Expiration must be in the future (use Expire Now to end the session).";
        this._renderDeviceInspectorBody();
        return;
      }
      this._setDeviceExpiration(ts);
    });
    body.querySelector("#de-exp-24h").addEventListener("click", () => this._setDeviceExpiration(expBase() + 24 * 3600));
    body.querySelector("#de-exp-7d").addEventListener("click", () => this._setDeviceExpiration(expBase() + 7 * 24 * 3600));
    body.querySelector("#de-exp-30d").addEventListener("click", () => this._setDeviceExpiration(expBase() + 30 * 24 * 3600));
    body.querySelector("#de-exp-permanent").addEventListener("click", () => this._setDeviceExpiration(0));
    body.querySelector("#de-exp-now").addEventListener("click", () => {
      this._showConfirm({
        title: "Expire session now",
        message: "The device will wipe its session shortly after its next heartbeat and this record will stop updating. For immediate removal with full cleanup, use Deprovision instead.",
        confirmLabel: "Expire Now",
        onConfirm: () => this._setDeviceExpiration(Math.floor(Date.now() / 1000)),
      });
    });
    const expCancel = body.querySelector("#de-exp-cancel");
    if (expCancel) {
      expCancel.addEventListener("click", (e) => {
        e.preventDefault();
        this._setDeviceExpiration(null);
      });
    }

    body.querySelector("#de-deprovision").addEventListener("click", () => this._confirmDeviceAction(d, "deprovision"));
    body.querySelector("#de-delete-record").addEventListener("click", () => this._confirmDeviceAction(d, "delete"));

    body.querySelectorAll(".pu-del").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const id = btn.dataset.id;
        const entry = (d.pending_update_list || []).find((u) => u.id === id);
        const label = entry ? `${entry.type} ${entry.action}` : "this update";
        this._showConfirm({
          title: "Delete pending update",
          message: `Remove the queued "${label}" for this device? This only clears it from the server queue — if it was already delivered by push, the device may still apply it.`,
          confirmLabel: "Delete",
          onConfirm: () => this._deletePendingUpdate(id),
        });
      });
    });
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

  // value: epoch seconds (0 = permanent) to queue an override, null to cancel a pending one.
  async _setDeviceExpiration(value) {
    if (!this._inspectingDevice) return;
    this._deviceExpError = "";
    this._deviceExpSuccess = "";
    this._renderDeviceInspectorBody();

    try {
      await this._hass.callApi("PUT", "casa/admin/device", {
        device_id: this._inspectingDevice.device_id,
        expires_at_override: value,
      });
      this._deviceExpSuccess =
        value === null ? "Pending expiration change cancelled."
        : value === 0 ? "Queued: session becomes permanent on the next heartbeat."
        : `Queued: expires ${this._fmtExpiry(value)} after the next heartbeat.`;
      await this._refreshInspectingDevice();
    } catch (err) {
      this._deviceExpError = "Failed: " + ((err && err.message) || err);
      this._renderDeviceInspectorBody();
    }
  }

  // Confirmation + execution for destructive device actions, shared by the
  // devices-table row buttons and the inspector's Danger Zone.
  // kind: "delete" (record + token revoke, no wipe) or "deprovision" (remote wipe).
  _confirmDeviceAction(device, kind) {
    const label = device.alias || device.device_id;
    if (kind === "delete") {
      this._showConfirm({
        title: "Delete device record",
        message: `Delete the record for "${label}"? This revokes its Home Assistant session and push relay token and removes it from the server, but does NOT wipe the app — the device loses access when its session next fails. Use Deprovision to remotely wipe.`,
        confirmLabel: "Delete",
        onConfirm: () => this._runDeviceAction(device, "delete_device"),
      });
    } else {
      this._showConfirm({
        title: "Deprovision device",
        message: `Deprovision "${label}"? This wipes the Casa app on the device, revokes its Home Assistant session, unregisters its push token, and deletes this record. If the device is offline it is wiped the next time it contacts the server. This cannot be undone.`,
        confirmLabel: "Deprovision",
        onConfirm: () => this._runDeviceAction(device, "deprovision_device"),
      });
    }
  }

  async _runDeviceAction(device, service) {
    const inspectingThis = this._inspectingDevice && this._inspectingDevice.device_id === device.device_id;
    if (inspectingThis) {
      this._deviceDeprovError = "";
      this._renderDeviceInspectorBody();
    }

    try {
      const res = await this._hass.callWS({
        type: "call_service",
        domain: "casa",
        service: service,
        service_data: { device_id: device.device_id },
        return_response: true,
      });
      const response = (res && res.response) || res || {};
      if (inspectingThis) this._closeDeviceInspector();
      await this._load();
      if (service === "deprovision_device" && response.push_sent === false) {
        alert("Device record removed and access revoked, but the wipe push could not be delivered (no push registration or relay unreachable). The device will wipe itself the next time it contacts the server.");
      }
    } catch (err) {
      const msg = "Failed: " + ((err && err.message) || err);
      if (inspectingThis) {
        this._deviceDeprovError = msg;
        this._renderDeviceInspectorBody();
      } else {
        alert(msg);
      }
    }
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

  // Reusable confirmation modal. Pass { title, message, confirmLabel, onConfirm }.
  _showConfirm({ title, message, confirmLabel, onConfirm }) {
    const sr = this.shadowRoot;
    const overlay = sr.getElementById("confirm-overlay");
    if (!overlay) return;

    sr.getElementById("confirm-title").textContent = title || "Confirm";
    sr.getElementById("confirm-body").innerHTML =
      `<p style="margin:0; font-size:14px; line-height:1.5;">${this._esc(message || "Are you sure?")}</p>`;

    overlay.classList.remove("hidden");

    // Clone-replace to avoid stacking listeners across invocations.
    const bindOnce = (id, fn) => {
      const el = sr.getElementById(id);
      if (!el) return;
      const clone = el.cloneNode(true);
      el.parentNode.replaceChild(clone, el);
      clone.addEventListener("click", fn);
      return clone;
    };
    const okBtn = bindOnce("confirm-ok", async () => {
      this._closeConfirm();
      if (onConfirm) await onConfirm();
    });
    if (okBtn) okBtn.textContent = confirmLabel || "Confirm";
    bindOnce("confirm-cancel", () => this._closeConfirm());
    bindOnce("confirm-close", () => this._closeConfirm());
    overlay.onclick = (e) => { if (e.target === overlay) this._closeConfirm(); };
  }

  _closeConfirm() {
    const overlay = this.shadowRoot.getElementById("confirm-overlay");
    if (overlay) overlay.classList.add("hidden");
  }

  async _deletePendingUpdate(updateId) {
    if (!this._inspectingDevice) return;
    this._devicePendingError = "";
    try {
      const did = this._inspectingDevice.device_id;
      await this._hass.callApi(
        "DELETE",
        `casa/admin/queue_update?device_id=${encodeURIComponent(did)}&id=${encodeURIComponent(updateId)}`
      );
    } catch (err) {
      this._devicePendingError = "Failed to delete: " + ((err && err.message) || err);
    }
    await this._refreshInspectingDevice();
  }

  // Reload the summary and re-point the open inspector at fresh device data so the
  // Pending Updates section reflects a just-queued update immediately.
  async _refreshInspectingDevice() {
    if (!this._inspectingDevice) return;
    const id = this._inspectingDevice.device_id;
    await this._load();
    const fresh = ((this._data && this._data.devices) || []).find((d) => d.device_id === id);
    if (fresh) this._inspectingDevice = fresh;
    this._renderDeviceInspectorBody();
  }

  async _pushDeviceWg() {
    const sr = this.shadowRoot;
    const select = sr.getElementById("de-wg-profile");
    if (!select) return;

    // Read inputs before re-rendering (which would reset them).
    const profileId = select.value;
    const sendPush = !!(sr.getElementById("de-wg-send-push") || {}).checked;
    const notifyPush = !!(sr.getElementById("de-wg-notify-push") || {}).checked;

    this._deviceWgError = "";
    this._deviceWgSuccess = "";
    this._renderDeviceInspectorBody();

    try {
      const req = {
        device_id: this._inspectingDevice.device_id,
        update_type: "wireguard",
        send_update_push: sendPush,
        notify_push: notifyPush,
      };
      let label;
      if (profileId) {
        const profile = (this._wgProfiles || []).find((p) => p.id === profileId);
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
      const res = await this._hass.callApi("POST", "casa/admin/queue_update", req);
      this._deviceWgSuccess = `${label} queued (queued ${res.queued}, pushed ${res.pushed}, notified ${res.notified}, skipped ${res.skipped}).`;
      await this._refreshInspectingDevice();
      return;
    } catch (err) {
      this._deviceWgError = "Failed to queue: " + ((err && err.message) || err);
    }
    this._renderDeviceInspectorBody();
  }

  async _pushDeviceProfile() {
    const sr = this.shadowRoot;
    const select = sr.getElementById("de-pp-profile");
    if (!select) return;

    const profileId = select.value;
    const sendPush = !!(sr.getElementById("de-pp-send-push") || {}).checked;
    const notifyPush = !!(sr.getElementById("de-pp-notify-push") || {}).checked;

    this._deviceProfileError = "";
    this._deviceProfileSuccess = "";

    if (!profileId) {
      this._deviceProfileError = "Select a provisioning profile first.";
      this._renderDeviceInspectorBody();
      return;
    }

    this._renderDeviceInspectorBody();

    try {
      const profile = (this._ppProfiles || []).find((p) => p.id === profileId);
      if (!profile) throw new Error("Selected provisioning profile not found.");
      const req = {
        device_id: this._inspectingDevice.device_id,
        update_type: "profile",
        action: "update",
        profile_id: profileId,
        send_update_push: sendPush,
        notify_push: notifyPush,
      };
      if (notifyPush) {
        req.title = "Profile updated";
        req.message = `A new configuration profile (${profile.name}) is available.`;
      }
      const res = await this._hass.callApi("POST", "casa/admin/queue_update", req);
      this._deviceProfileSuccess = `Profile '${profile.name}' queued (queued ${res.queued}, pushed ${res.pushed}, notified ${res.notified}, skipped ${res.skipped}).`;
      await this._refreshInspectingDevice();
      return;
    } catch (err) {
      this._deviceProfileError = "Failed to queue: " + ((err && err.message) || err);
    }
    this._renderDeviceInspectorBody();
  }

  /* ===== Guest Account Creator & Management ===== */

  _openAccountCreator() {
    this._accCreatedCreds = null;
    this._accFormError = "";
    this._accFormLoading = false;

    const sr = this.shadowRoot;
    const overlay = sr.getElementById("account-overlay");
    overlay.classList.remove("hidden");
    this._renderAccountCreatorBody();

    // Bind close/cancel triggers
    const bindOnce = (id, fn) => {
      const el = sr.getElementById(id);
      if (el) {
        const clone = el.cloneNode(true);
        el.parentNode.replaceChild(clone, el);
        clone.addEventListener("click", fn);
      }
    };
    bindOnce("account-editor-close", () => this._closeAccountCreator());
    overlay.onclick = (e) => { if (e.target === overlay) this._closeAccountCreator(); };
  }

  _closeAccountCreator() {
    this.shadowRoot.getElementById("account-overlay").classList.add("hidden");
    this._load();
  }

  _renderAccountCreatorBody() {
    const sr = this.shadowRoot;
    const body = sr.getElementById("account-editor-body");
    const footer = sr.getElementById("account-editor-footer");
    if (!body || !footer) return;

    const esc = (val) => this._esc(val || "");

    if (this._accCreatedCreds) {
      body.innerHTML = `
        <div style="text-align:center; padding:16px 0;">
          <ha-icon icon="mdi:check-circle" style="color:var(--success-color,#43a047); --mdc-icon-size:48px; margin-bottom:12px;"></ha-icon>
          <h4 style="margin:0 0 8px 0; font-size:16px;">Guest Account Created!</h4>
          <p style="margin:0 0 16px 0; font-size:13px; color:var(--secondary-text-color,#727272);">Copy these credentials. The password is only shown once.</p>
          <div style="background:var(--secondary-background-color,#f5f5f5); border-radius:8px; padding:16px; text-align:left; font-size:13px; display:inline-block; min-width:280px; box-sizing:border-box;">
            <div style="margin-bottom:8px;"><strong>Full Name:</strong> <span>${esc(this._accCreatedCreds.name)}</span></div>
            <div style="margin-bottom:8px;"><strong>Username:</strong> <code>${esc(this._accCreatedCreds.username)}</code></div>
            <div><strong>Password:</strong> <code style="background:var(--card-background-color,#fff); padding:2px 6px; border-radius:4px; border:1px solid var(--divider-color,#ddd); font-size:14px; font-weight:bold; letter-spacing:0.5px;">${esc(this._accCreatedCreds.password)}</code></div>
          </div>
        </div>
      `;
      footer.innerHTML = `
        <button class="btn-primary" id="acc-done">Done</button>
      `;
      footer.querySelector("#acc-done").addEventListener("click", () => this._closeAccountCreator());
      return;
    }

    body.innerHTML = `
      <div class="editor-section">
        <h4>Account Details</h4><hr>
        <div class="editor-row">
          <label>Full Name *</label>
          <input type="text" id="acc-name" placeholder="e.g. John Doe">
        </div>
        <div class="editor-row">
          <label>Username *</label>
          <input type="text" id="acc-username" placeholder="e.g. john" style="text-transform: lowercase;">
        </div>
        <div class="editor-row">
          <label>Password (optional — secure password generated if blank)</label>
          <input type="password" id="acc-password" placeholder="••••••••">
        </div>
      </div>
      ${this._accFormError ? `<div class="editor-msg" style="color:var(--error-color,#db4437)">${esc(this._accFormError)}</div>` : ""}
    `;

    footer.innerHTML = `
      <button class="btn-plain" id="account-editor-cancel">Cancel</button>
      <button class="btn-primary" id="account-editor-save">${this._accFormLoading ? "Creating…" : "Create"}</button>
    `;

    footer.querySelector("#account-editor-cancel").addEventListener("click", () => this._closeAccountCreator());
    footer.querySelector("#account-editor-save").addEventListener("click", () => this._createAccount());
  }

  async _createAccount() {
    if (this._accFormLoading) return;

    const sr = this.shadowRoot;
    const body = sr.getElementById("account-editor-body");
    const name = body.querySelector("#acc-name").value.trim();
    const username = body.querySelector("#acc-username").value.trim().toLowerCase();
    const password = body.querySelector("#acc-password").value.trim();

    if (!name || !username) {
      this._accFormError = "Name and Username are required.";
      this._renderAccountCreatorBody();
      return;
    }

    this._accFormLoading = true;
    this._accFormError = "";
    this._renderAccountCreatorBody();

    try {
      const res = await this._hass.callService("casa", "create_user", {
        name: name,
        username: username,
        password: password || undefined,
        local_only: true
      });
      
      this._accCreatedCreds = {
        name: name,
        username: username,
        password: (res && res.password) || password || "(Securely generated in backend)"
      };
    } catch (err) {
      this._accFormError = "Failed: " + ((err && err.message) || err);
    }
    this._accFormLoading = false;
    this._renderAccountCreatorBody();
  }

  async _scrambleAccountPassword(username) {
    if (!confirm(`Are you sure you want to reset the password for guest '${username}'?\nThis will scramble their credentials and revoke all active login sessions.`)) {
      return;
    }

    try {
      const res = await this._hass.callService("casa", "scramble_guest_password", {
        username: username,
        deauthenticate: true
      });
      const newPwd = (res && res.password) || "(randomly scrambled)";
      alert(`Password for guest '${username}' has been reset.\n\nNew Password: ${newPwd}\n(Please write this password down as it won't be shown again.)`);
      this._load();
    } catch (err) {
      alert("Failed: " + ((err && err.message) || err));
    }
  }

  async _removeAccount(username) {
    if (!confirm(`Are you sure you want to delete guest account '${username}'?\nThis will remove their HA user profile, end all active sessions, and deregister all of their devices.`)) {
      return;
    }

    try {
      await this._hass.callService("casa", "remove_user", {
        username: username
      });
      this._load();
      const overlay = this.shadowRoot.getElementById("overlay");
      if (overlay && !overlay.classList.contains("hidden") && this._settingsTab === "accounts") {
        this._renderAccountsPane();
      }
    } catch (err) {
      alert("Failed: " + ((err && err.message) || err));
    }
  }

  _renderAccountsPane() {
    const pane = this.shadowRoot && this.shadowRoot.getElementById("settings-pane");
    if (!pane) return;

    const accounts = (this._data && this._data.accounts) || [];

    let listHtml;
    if (accounts.length === 0) {
      listHtml = `<div class="wg-empty">No guest accounts.</div>`;
    } else {
      listHtml = accounts.map((a) => {
        return `
          <div class="pp-card" style="display:flex; justify-content:space-between; align-items:center;">
            <div class="pp-info">
              <div class="pp-name" style="font-weight:600; font-size:14px;">${this._esc(a.name)}</div>
              <div class="pp-meta" style="font-size:12px; color:var(--secondary-text-color,#727272); margin-top:2px;">
                Username: <strong>${this._esc(a.username)}</strong> · Devices: ${a.device_count ?? 0} · Created: ${this._fmtTime(a.created_at)} ${a.created_by ? "by " + this._esc(a.created_by) : ""}
              </div>
            </div>
            <div class="pp-actions" style="display:flex; gap:6px;">
              <button class="btn-plain" style="padding: 6px 10px; font-size: 12px;" data-username="${this._esc(a.username)}" data-action="scramble" title="Scramble Password and revoke active sessions"><ha-icon icon="mdi:lock-reset" style="--mdc-icon-size:16px;"></ha-icon> Reset</button>
              <button class="btn-plain del" style="padding: 6px 10px; font-size: 12px; color:var(--error-color,#db4437);" data-username="${this._esc(a.username)}" data-action="delete" title="Delete Guest Account"><ha-icon icon="mdi:delete" style="--mdc-icon-size:16px;"></ha-icon> Delete</button>
            </div>
          </div>`;
      }).join("");
    }

    pane.innerHTML = `
      <h3>Guest Accounts</h3>
      <p class="sub">Manage local guest accounts and active logins.</p>
      <div class="wg-toolbar">
        <button class="btn-primary" id="acc-add"><ha-icon icon="mdi:plus"></ha-icon> Add Account</button>
        <button class="btn-plain" id="acc-refresh"><ha-icon icon="mdi:refresh"></ha-icon> Refresh</button>
      </div>
      ${listHtml}
    `;

    pane.querySelector("#acc-add").addEventListener("click", () => this._openAccountCreator());
    pane.querySelector("#acc-refresh").addEventListener("click", () => this._load());

    pane.querySelectorAll(".pp-actions button").forEach((btn) => {
      btn.addEventListener("click", () => {
        const username = btn.dataset.username;
        if (btn.dataset.action === "scramble") {
          this._scrambleAccountPassword(username);
        } else if (btn.dataset.action === "delete") {
          this._removeAccount(username);
        }
      });
    });
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
      <div class="stat"><div class="value">${s.pending_updates ?? 0}</div><div class="label">Updates Pending</div></div>
      <div class="stat site"><div class="value">${this._esc(data.site_id) || "—"}</div><div class="label">Site ID</div></div>
    `;

    const devices = data.devices || [];
    root.getElementById("devices").innerHTML = devices.length
      ? `<table>
          <thead><tr><th>User</th><th>Device</th><th>IP</th><th>Last Seen</th><th>Status</th><th></th></tr></thead>
          <tbody>${devices.map((d) => `
            <tr class="device-row" data-id="${this._esc(d.device_id)}">
              <td>${this._esc(d.username)}${d.native ? ' <span class="badge" style="background:var(--secondary-background-color);color:var(--primary-text-color)">native</span>' : ""}</td>
              <td>${d.alias ? `<strong>${this._esc(d.alias)}</strong> <small style="color:var(--secondary-text-color)">(${this._esc((d.device_id || "").slice(0, 8))}…)</small>` : `<code>${this._esc((d.device_id || "").slice(0, 8))}…</code>`}</td>
              <td>${this._esc(d.ip) || "—"}</td>
              <td>${this._fmtTime(d.last_seen)}</td>
              <td>${d.orphaned ? '<span class="badge orphan">orphan</span>' : ""}${d.stale ? '<span class="badge stale">stale</span>' : ""}${(d.pending_updates ?? 0) > 0 ? `<span class="badge pending">${d.pending_updates} pending</span>` : ""}${!d.orphaned && !d.stale && d.push_registered ? '<span class="badge ok">ok</span>' : ""}</td>
              <td style="white-space:nowrap; text-align:right;">
                <button class="dev-act" data-id="${this._esc(d.device_id)}" data-action="delete" title="Delete record (revoke access, no wipe)" style="background:none; border:none; cursor:pointer; color:var(--secondary-text-color,#727272); padding:2px 4px; line-height:1;"><ha-icon icon="mdi:delete-outline" style="--mdc-icon-size:18px;"></ha-icon></button>
                <button class="dev-act" data-id="${this._esc(d.device_id)}" data-action="deprovision" title="Deprovision (remote wipe)" style="background:none; border:none; cursor:pointer; color:var(--error-color,#db4437); padding:2px 4px; line-height:1;"><ha-icon icon="mdi:cellphone-remove" style="--mdc-icon-size:18px;"></ha-icon></button>
              </td>
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

    // Row action buttons (don't open the inspector).
    root.querySelectorAll(".dev-act").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const device = (devices || []).find((d) => d.device_id === btn.dataset.id);
        if (device) this._confirmDeviceAction(device, btn.dataset.action);
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

  _openQuickProvision() {
    this._provisionProfileSearch = "";
    this._provisionSelectedProfileId = "";
    this._provisionHostUrl = this._data?.site_id ? window.location.origin : "";
    this._provisionUsername = "";
    this._provisionPin = "";
    this._provisionMethod = "qr";
    this._provisionResult = null;
    this._provisionError = "";
    this._provisionLoading = false;

    this.shadowRoot.getElementById("provision-overlay").classList.remove("hidden");
    this._loadProvisionProfiles().then(() => this._renderQuickProvisionBody());
  }

  _closeQuickProvision() {
    this.shadowRoot.getElementById("provision-overlay").classList.add("hidden");
  }

  _renderQuickProvisionBody() {
    const body = this.shadowRoot.getElementById("provision-body");
    if (!body) return;
    const esc = (val) => this._esc(val || "");
    const profiles = this._ppProfiles || [];

    body.innerHTML = `
      <div class="editor-section">
        <h4>Select Profile & Parameters</h4>
        <hr>
        <div class="editor-row">
          <label>Filter Profiles</label>
          <input type="text" id="qp-profile-search" placeholder="Type to search profiles...">
        </div>
        <div class="editor-row">
          <label>Provisioning Profile</label>
          <select id="qp-profile-select">
            <option value="">-- No Profile / Manual Entry --</option>
            ${profiles.map(p => `<option value="${esc(p.id)}" ${this._provisionSelectedProfileId === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
          </select>
        </div>
        <div class="editor-row">
          <label>Host URL</label>
          <input type="text" id="qp-host-url" value="${esc(this._provisionHostUrl)}" placeholder="e.g. http://192.168.1.50:8123">
        </div>
        <div class="editor-row">
          <label>Username</label>
          <input type="text" id="qp-username" value="${esc(this._provisionUsername)}" placeholder="e.g. guest_user">
        </div>
        <div class="editor-row">
          <label>PIN (Optional 6-digit PIN)</label>
          <input type="text" id="qp-pin" value="${esc(this._provisionPin)}" placeholder="e.g. 123456" maxlength="6">
        </div>

        <div class="editor-row">
          <label>Output Method</label>
          <div id="qp-method-picker" style="display:flex; gap:6px;">
            ${[["qr", "QR Code"], ["deep_link", "Deep Link"], ["manual", "Manual Entry"]].map(([val, label]) => `
              <button class="${this._provisionMethod === val ? "btn-primary" : "btn-plain"} qp-method" data-method="${val}" style="flex:1; padding:8px 10px; font-size:13px; ${this._provisionMethod === val ? "" : "border:1px solid var(--divider-color,#ddd); border-radius:6px;"}">${label}</button>
            `).join("")}
          </div>
        </div>

        ${this._provisionError ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-bottom: 12px;">${esc(this._provisionError)}</div>` : ""}

        <button class="btn-primary" id="qp-generate" style="width: 100%; margin-top: 12px; height: 40px; font-weight: 600;">
          ${this._provisionLoading ? "Generating..." : (this._provisionMethod === "qr" ? "Generate Link & QR Code" : this._provisionMethod === "deep_link" ? "Generate Setup Links" : "Generate Manual Entry Values")}
        </button>
      </div>

      <div id="qp-result-container">
        ${this._renderProvisionResult(esc)}
      </div>
    `;

    const searchInput = body.querySelector("#qp-profile-search");
    const selectEl = body.querySelector("#qp-profile-select");
    const hostInput = body.querySelector("#qp-host-url");
    const userInput = body.querySelector("#qp-username");
    const pinInput = body.querySelector("#qp-pin");
    const generateBtn = body.querySelector("#qp-generate");

    if (searchInput && selectEl) {
      searchInput.addEventListener("input", (e) => {
        const q = e.target.value.toLowerCase();
        for (let i = 1; i < selectEl.options.length; i++) {
          const opt = selectEl.options[i];
          const match = opt.text.toLowerCase().includes(q);
          opt.style.display = match ? "" : "none";
        }
      });
    }

    if (selectEl) {
      selectEl.addEventListener("change", (e) => {
        const val = e.target.value;
        this._provisionSelectedProfileId = val;
        if (val) {
          const p = profiles.find(x => x.id === val);
          if (p && p.fields) {
            hostInput.value = p.fields.host_url || "";
            userInput.value = p.fields.username || "";
            pinInput.value = p.fields.pin || "";
            this._provisionHostUrl = p.fields.host_url || "";
            this._provisionUsername = p.fields.username || "";
            this._provisionPin = p.fields.pin || "";
          }
        }
      });
    }

    if (hostInput) hostInput.addEventListener("input", (e) => { this._provisionHostUrl = e.target.value; });
    if (userInput) userInput.addEventListener("input", (e) => { this._provisionUsername = e.target.value; });
    if (pinInput) pinInput.addEventListener("input", (e) => { this._provisionPin = e.target.value; });

    generateBtn.addEventListener("click", () => this._generateProvisionLink());

    body.querySelectorAll(".qp-method").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (this._provisionMethod === btn.dataset.method) return;
        this._provisionMethod = btn.dataset.method;
        this._provisionResult = null;
        this._provisionError = "";
        this._renderQuickProvisionBody();
      });
    });

    body.querySelectorAll(".qp-copy-val").forEach((btn) => {
      btn.addEventListener("click", () => {
        navigator.clipboard.writeText(btn.dataset.copy || "").then(() => {
          const original = btn.textContent;
          btn.textContent = "Copied!";
          setTimeout(() => { btn.textContent = original; }, 1500);
        });
      });
    });

    const bindCopy = (btnId, inputId, msgId) => {
      const btn = body.querySelector(`#${btnId}`);
      if (!btn) return;
      btn.addEventListener("click", () => {
        const input = body.querySelector(`#${inputId}`);
        if (!input) return;
        input.select();
        navigator.clipboard.writeText(input.value).then(() => {
          const msg = body.querySelector(`#${msgId}`);
          if (msg) {
            msg.style.display = "block";
            setTimeout(() => { msg.style.display = "none"; }, 2000);
          }
        });
      });
    };
    bindCopy("qp-copy-link", "qp-link-val", "qp-copy-msg");
    bindCopy("qp-copy-ulink", "qp-ulink-val", "qp-ucopy-msg");
  }

  _renderProvisionResult(esc) {
    const r = this._provisionResult;
    if (!r) return "";

    if (r.method === "manual") {
      const f = r.fields || {};
      const u = r.unsupported || {};
      const val = (v) => (v === undefined || v === null || String(v) === "")
        ? '<em style="color:var(--secondary-text-color,#727272);">leave blank</em>'
        : `<code style="word-break:break-all;">${esc(String(v))}</code>`;
      const row = (label, v, copyValue) => `
        <strong>${label}</strong>
        <span style="display:flex; align-items:center; gap:8px;">
          ${val(v)}
          ${copyValue ? `<button class="btn-plain qp-copy-val" data-copy="${esc(String(copyValue))}" style="padding:2px 8px; font-size:11px; border:1px solid var(--divider-color,#ddd); border-radius:4px;">Copy</button>` : ""}
        </span>`;
      const section = (label) => `
        <strong style="grid-column:1 / -1; margin-top:8px; color:var(--primary-text-color,#212121); font-weight:600; border-bottom:1px solid var(--divider-color,#ddd); padding-bottom:2px;">${label}</strong>`;

      const sessionText = Number(f.session_expiration) === 0
        ? 'Never — toggle "Session Never Expires" ON'
        : this._fmtExpiry(f.session_expiration);

      const lost = [];
      if (u.pin) lost.push("provisioning PIN");
      if (u.push_notifications && u.push_notifications !== "false") lost.push("push notifications");
      if (u.wireguard) lost.push("WireGuard VPN");

      return `
        <div class="device-sec-box" style="margin-top: 16px;">
          <h5>Manual Entry Values</h5>
          <p style="font-size:12px; color:var(--secondary-text-color,#727272); margin:0 0 10px 0;">
            Enter these in the Casa app's manual provisioning sheet, field for field.
          </p>
          ${r.expires_at ? `<div class="editor-msg" style="color:var(--warning-color,#f4b400); margin-bottom:10px;">Password valid until <strong>${this._fmtExpiry(r.expires_at)}</strong> — it is scrambled after that window.</div>` : ""}
          <div style="display:grid; grid-template-columns:auto 1fr; gap:6px 16px; font-size:13px;">
            ${section("Server Configuration")}
            ${row("Server URL", f.server_url, f.server_url)}
            ${row("Username", f.username, f.username)}
            ${row("Password", f.password, f.password)}
            ${section("Access Control & Network")}
            ${row("Allowed Paths (comma-separated)", f.allowed_paths)}
            ${row("Allowed Wi-Fi SSIDs (comma-separated)", f.allowed_wifi)}
            ${section("Client Customization")}
            ${row("Default Dashboard Path", f.default_dashboard)}
            ${row("Immersive Level", f.immersive_level)}
            ${row("Immersive Color Mode", f.theme_color_mode)}
            ${row("Custom Hex Color", f.custom_color)}
            ${section("Session & Caching")}
            ${row("Session Expiration", sessionText)}
            ${row("Cache Control Hours", f.cache_control_hours)}
            ${section("Onboarding Extras")}
            ${row("Welcome Screen URL", f.welcome_url)}
            ${row("Auto-Join Wi-Fi SSID", f.connect_wifi_ssid)}
            ${row("Auto-Join Wi-Fi Password", f.connect_wifi_password)}
          </div>
          ${lost.length ? `<div class="editor-msg" style="color:var(--error-color,#db4437); margin-top:12px;">This configuration includes settings manual entry cannot carry over: <strong>${lost.join(", ")}</strong>.</div>` : ""}
          <p style="font-size:12px; color:var(--secondary-text-color,#727272); margin:10px 0 0 0;">
            Manual entry cannot configure a PIN, push notifications (site binding), or WireGuard VPN.
            A manually provisioned device receives no pushes, remote updates, or remote deprovision;
            session expiration changes still apply on its heartbeat.
          </p>
        </div>`;
    }

    // qr / deep_link results share the link rows; qr adds the scannable image.
    return `
      <div class="device-sec-box" style="text-align: center; margin-top: 16px;">
        <h5 style="margin-bottom: 12px; font-size: 14px;">${r.method === "qr" ? "Scan with Casa App or click link" : "Send a setup link to the device"}</h5>
        ${r.method === "qr" ? `
        <div style="margin: 16px 0;">
          <img src="${esc(r.url_path)}" style="width: 220px; height: 220px; border: 1px solid var(--divider-color, #ddd); border-radius: 8px; padding: 12px; background: white;" />
        </div>` : ""}
        <div class="editor-row" style="text-align: left;">
          <label>Setup Deep Link</label>
          <div style="display:flex; gap:8px; align-items: center;">
            <input type="text" id="qp-link-val" value="${esc(r.deep_link)}" readonly style="flex:1; font-family: monospace; font-size: 11px;">
            <button class="btn-primary" id="qp-copy-link" style="padding: 8px 16px;">Copy</button>
          </div>
          <div id="qp-copy-msg" style="color:var(--success-color,#43a047); font-size: 12px; margin-top: 4px; display:none;">Link copied to clipboard!</div>
        </div>
        ${r.universal_link ? `
        <div class="editor-row" style="text-align: left;">
          <label>Universal Link (opens from Safari / iMessage)</label>
          <div style="display:flex; gap:8px; align-items: center;">
            <input type="text" id="qp-ulink-val" value="${esc(r.universal_link)}" readonly style="flex:1; font-family: monospace; font-size: 11px;">
            <button class="btn-primary" id="qp-copy-ulink" style="padding: 8px 16px;">Copy</button>
          </div>
          <div id="qp-ucopy-msg" style="color:var(--success-color,#43a047); font-size: 12px; margin-top: 4px; display:none;">Link copied to clipboard!</div>
        </div>
        ` : ""}
      </div>`;
  }

  async _generateProvisionLink() {
    const hostVal = this._provisionHostUrl.trim();
    const userVal = this._provisionUsername.trim();
    
    if (!hostVal || !userVal) {
      this._provisionError = "Host URL and Username are required.";
      this._renderQuickProvisionBody();
      return;
    }

    this._provisionError = "";
    this._provisionLoading = true;
    this._provisionResult = null;
    this._renderQuickProvisionBody();

    try {
      const payload = {
        method: this._provisionMethod,
        host_url: hostVal,
        username: userVal,
        pin: this._provisionPin.trim() || undefined,
        profile: this._provisionSelectedProfileId || undefined
      };
      if (this._provisionMethod === "manual") {
        // Typing values in takes longer than the 5-minute default window.
        payload.timeout_minutes = 30;
      }
      
      const res = await this._hass.callWS({
        type: "call_service",
        domain: "casa",
        service: "provision",
        service_data: payload,
        return_response: true
      });
      this._provisionResult = (res && res.response) || res || null;
    } catch (err) {
      this._provisionError = (err && err.message) || String(err);
    } finally {
      this._provisionLoading = false;
      this._renderQuickProvisionBody();
    }
  }
}

customElements.define("casa-admin-panel", CasaAdminPanel);
