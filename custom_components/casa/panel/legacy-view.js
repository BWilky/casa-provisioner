// Wraps the pre-redesign panel (legacy.js) as a routed view so the new shell
// can host it unchanged while the redesigned views land. Deleted at the flip.

export function createView(app) {
  let el = null;
  return {
    id: "legacy",
    chrome: false, // legacy has its own toolbar; hide the new app header
    polling: "paused", // legacy runs its own 30s summary timer
    header: () => ({ title: "Casa Admin" }),
    mount(container) {
      app.loadModule("legacy.js").then(() => {
        el = document.createElement("casa-admin-legacy");
        el.narrow = app.narrow();
        el.hass = app.hass();
        container.appendChild(el);
      });
    },
    unmount() {
      el?.remove();
      el = null;
    },
    onHass(h) {
      if (el) el.hass = h;
    },
  };
}
