// Casa admin panel — bootstrap entry. This is the only URL the backend
// registers; it defines the <casa-admin-panel> element immediately and then
// dynamically loads the rest of the panel with the same ?v= cache-buster so
// the whole module graph re-fetches together when any panel file changes.

const VERSION =
  new URL(import.meta.url).searchParams.get("v") || String(Date.now());

// All sibling modules MUST be loaded through this (never static imports):
// a module imported both with and without ?v= would instantiate twice.
const loadModule = (path) => import(`./${path}?v=${VERSION}`);

class CasaAdminPanel extends HTMLElement {
  constructor() {
    super();
    // Spec: a custom element constructor must not add attributes — HA's
    // document.createElement() throws NotSupportedError otherwise. Styling
    // happens in connectedCallback instead.
    this._props = {};
    this._app = null;
    this._booting = false;
  }

  // HA assigns these on custom panels; buffer until the app module loads.
  set hass(v) {
    this._props.hass = v;
    if (this._app) this._app.setHass(v);
    else this._boot();
  }
  set narrow(v) {
    this._props.narrow = v;
    this._app?.setNarrow(v);
  }
  set route(v) {
    this._props.route = v;
    this._app?.setRoute(v);
  }
  set panel(v) {
    this._props.panel = v;
    this._app?.setPanel?.(v);
  }

  async _boot() {
    if (this._booting) return;
    this._booting = true;
    try {
      const { createApp } = await loadModule("app.js");
      this._app = await createApp({ host: this, loadModule, version: VERSION });
      this._app.setNarrow(this._props.narrow);
      if (this._props.route) this._app.setRoute(this._props.route);
      this._app.setHass(this._props.hass);
    } catch (err) {
      this._booting = false;
      const div = document.createElement("div");
      div.style.cssText = "padding:24px; font-family:sans-serif; color:#db4437;";
      div.textContent = `Casa panel failed to load: ${String(err && err.message ? err.message : err)}`;
      (this.shadowRoot || this).appendChild(div);
      console.error("casa: panel boot failed", err);
    }
  }

  connectedCallback() {
    this.style.display = "block";
    this.style.height = "100%";
    this._app?.onConnected();
  }
  disconnectedCallback() {
    this._app?.onDisconnected();
  }
}

customElements.define("casa-admin-panel", CasaAdminPanel);
export { VERSION, loadModule };
