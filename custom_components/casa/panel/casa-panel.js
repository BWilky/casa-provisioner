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
    this._props = {};
    this._inner = null;
    this._booting = false;
    this.style.display = "block";
    this.style.height = "100%";
  }

  // HA assigns these on custom panels; buffer until the app module loads.
  set hass(v) {
    this._props.hass = v;
    if (this._inner) this._inner.hass = v;
    else this._boot();
  }
  set narrow(v) {
    this._props.narrow = v;
    if (this._inner) this._inner.narrow = v;
  }
  set route(v) {
    this._props.route = v;
    if (this._inner) this._inner.route = v;
  }
  set panel(v) {
    this._props.panel = v;
    if (this._inner) this._inner.panel = v;
  }

  async _boot() {
    if (this._booting) return;
    this._booting = true;
    try {
      await loadModule("legacy.js");
      const el = document.createElement("casa-admin-legacy");
      el.narrow = this._props.narrow;
      el.route = this._props.route;
      el.panel = this._props.panel;
      el.hass = this._props.hass;
      this._inner = el;
      this.appendChild(el);
    } catch (err) {
      this._booting = false;
      this.innerHTML = `<div style="padding:24px; font-family:sans-serif; color:#db4437;">
        Casa panel failed to load: ${String(err && err.message ? err.message : err)}</div>`;
    }
  }
}

customElements.define("casa-admin-panel", CasaAdminPanel);
export { VERSION, loadModule };
