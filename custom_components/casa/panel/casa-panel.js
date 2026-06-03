// Casa admin panel — a plain web component (no build step) registered as a
// custom Home Assistant sidebar panel. Blends with the active HA theme by using
// HA's CSS custom properties.

class CasaAdminPanel extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._initialized) {
      this._initialized = true;
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
          display: flex;
          align-items: center;
          height: var(--header-height, 56px);
          padding: 0 16px;
          background: var(--app-header-background-color, var(--primary-color, #03a9f4));
          color: var(--app-header-text-color, var(--text-primary-color, #fff));
          font-size: 20px;
          font-weight: 400;
          box-shadow: var(--ha-card-box-shadow, 0 2px 2px rgba(0,0,0,.1));
        }
        .menu {
          background: none; border: none; color: inherit; cursor: pointer;
          font-size: 24px; margin-right: 16px; line-height: 1; padding: 4px 8px;
        }
        .content { padding: 16px; max-width: 1400px; margin: 0 auto; }
        .stats {
          display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 12px; margin-bottom: 16px;
        }
        .stat, .card {
          background: var(--ha-card-background, var(--card-background-color, #fff));
          border-radius: var(--ha-card-border-radius, 12px);
          box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.08));
          border: var(--ha-card-border-width, 0) solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
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
        .actions {
          display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 16px;
        }
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
        .badge {
          display: inline-block; padding: 1px 8px; border-radius: 10px;
          font-size: 11px; font-weight: 500; margin-left: 4px;
        }
        .badge.ok { background: var(--success-color, #43a047); color: #fff; }
        .badge.stale { background: var(--warning-color, #ffa600); color: #222; }
        .badge.orphan { background: var(--error-color, #db4437); color: #fff; }
        .empty { padding: 16px; color: var(--secondary-text-color, #727272); }
        .errbar {
          background: var(--error-color, #db4437); color: #fff;
          padding: 10px 16px; border-radius: 8px; margin-bottom: 16px;
        }
        code { font-family: monospace; }
      </style>
      <div class="toolbar">
        <button class="menu" id="menu" title="Menu">&#9776;</button>
        <span>Casa Admin</span>
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
          <div class="card">
            <h2>Managed Devices</h2>
            <div id="devices"></div>
          </div>
          <div class="card">
            <h2>Managed Accounts</h2>
            <div id="accounts"></div>
          </div>
        </div>
      </div>
    `;

    this.shadowRoot.getElementById("menu").addEventListener("click", () => {
      this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
    });
    this.shadowRoot.getElementById("refresh").addEventListener("click", () => this._load());
    this.shadowRoot.getElementById("reconcile").addEventListener("click", () => this._reconcile());

    if (this.isConnected && !this._timer) {
      this._timer = setInterval(() => this._load(), 30000);
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
  }
}

customElements.define("casa-admin-panel", CasaAdminPanel);
