// Casa admin panel — app shell. Owns the shadow root, the ESPHome-style app
// header, the URL router (piggybacking HA's route plumbing), the view
// lifecycle, and the popover/modal layers. Views are factory objects (see the
// view contract below); modules are loaded lazily with the propagated ?v=.
//
// View contract — each views/*.js exports createView(app) returning:
//   {
//     id,
//     header: (params) => ({ title, subtitle, back }),  // back != null → back arrow
//     chrome: true|false,          // false hides the app header (unused today)
//     polling: "live" | "paused",  // paused in editors
//     confirmLeave?: async () => boolean,   // dirty-guard; router awaits it
//     mount(el, params), unmount(), onSummary?(summary, error)
//   }

export async function createApp({ host, loadModule, version }) {
  const [stylesMod, apiMod, uiMod] = await Promise.all([
    loadModule("styles.js"),
    loadModule("api.js"),
    loadModule("ui.js"),
  ]);

  const shadow = host.shadowRoot || host.attachShadow({ mode: "open" });
  const sheet = stylesMod.makeSheet();
  if (sheet) shadow.adoptedStyleSheets = [sheet];
  shadow.innerHTML = `
    ${sheet ? "" : `<style>${stylesMod.cssText}</style>`}
    <div class="app-header" id="hdr">
      <button class="btn btn--icon" id="hdr-nav" title="Menu"><ha-icon icon="mdi:menu"></ha-icon></button>
      <div class="app-header__icon-tile"><ha-icon icon="mdi:shield-home"></ha-icon></div>
      <div class="app-header__titles">
        <div class="app-header__title" id="hdr-title">Casa Admin</div>
        <div class="app-header__subtitle" id="hdr-sub">Provision and manage Casa devices</div>
      </div>
      <span class="spacer"></span>
      <button class="btn btn--icon" id="hdr-kebab" title="More"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
    </div>
    <main id="casa-main"></main>
    <div id="popover-layer"></div>
    <div id="modal-layer"></div>
  `;

  const api = new apiMod.CasaApi();
  const ui = uiMod.createUi({
    popoverLayer: shadow.getElementById("popover-layer"),
    modalLayer: shadow.getElementById("modal-layer"),
    shadowRoot: shadow,
  });

  const hdr = shadow.getElementById("hdr");
  const hdrNav = shadow.getElementById("hdr-nav");
  const hdrTitle = shadow.getElementById("hdr-title");
  const hdrSub = shadow.getElementById("hdr-sub");
  const main = shadow.getElementById("casa-main");

  /* ---------- localStorage store ---------- */
  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem("casa:" + key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch (_e) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem("casa:" + key, JSON.stringify(value));
      } catch (_e) {
        /* storage full/blocked — non-fatal */
      }
    },
  };

  /* ---------- route table ---------- */
  // matcher: path segments; ":name" captures a param. First match wins;
  // the empty pattern is the catch-all default.
  const ROUTES = [
    { pattern: ["devices"], view: "views/devices.js" },
    { pattern: ["accounts"], view: "views/accounts.js" },
    { pattern: ["sessions"], view: "views/sessions.js" },
    { pattern: ["profiles"], view: "views/profiles.js" },
    { pattern: ["wireguard"], view: "views/wireguard-profiles.js" },
    { pattern: ["devices", ":deviceId"], view: "views/device-editor.js" },
    { pattern: ["profiles", "new"], view: "views/profile-editor.js" },
    { pattern: ["profiles", ":profileId"], view: "views/profile-editor.js" },
    { pattern: ["settings"], view: "views/settings.js" },
    { pattern: ["settings", ":tab"], view: "views/settings.js" },
    { pattern: ["provision"], view: "views/provision.js" },
    { pattern: ["provision", "guided"], view: "views/provision-guided.js" },
    { pattern: ["provision", "user", ":username"], view: "views/provision.js" },
    { pattern: ["provision", "profile", ":profileId"], view: "views/provision.js" },
    { pattern: [], view: "views/devices.js" }, // default = device list
  ];

  function matchRoute(path) {
    const segs = (path || "/").split("/").filter(Boolean);
    for (const r of ROUTES) {
      if (r.pattern.length === 0) continue; // catch-all handled below
      const params = segsMatch(r.pattern, segs);
      if (params) return { route: r, params };
    }
    return { route: ROUTES.find((r) => r.pattern.length === 0), params: {} };
  }
  function segsMatch(pattern, segs) {
    if (pattern.length !== segs.length) return null;
    const params = {};
    for (let i = 0; i < pattern.length; i++) {
      if (pattern[i].startsWith(":")) params[pattern[i].slice(1)] = decodeURIComponent(segs[i]);
      else if (pattern[i] !== segs[i]) return null;
    }
    return params;
  }

  /* ---------- app context ---------- */
  const viewCache = new Map(); // module path -> view instance
  let currentView = null;
  let currentParams = null;
  let hass = null;
  let narrow = false;
  let pendingPath = "/";

  const app = {
    api,
    ui,
    store,
    loadModule,
    version,
    hass: () => hass,
    narrow: () => narrow,
    summary: () => api.summary,
    refresh: () => api.refreshSummary(),
    navigate(path, { replace = false } = {}) {
      const url = "/casa" + (path.startsWith("/") ? path : "/" + path);
      if (replace) history.replaceState(null, "", url);
      else history.pushState(null, "", url);
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace } }));
    },
    back() {
      const backPath = currentView?.header?.(currentParams)?.back;
      if (backPath != null) app.navigate(backPath, { replace: false });
      else app.navigate("/", { replace: false });
    },
    setHeader({ title, subtitle } = {}) {
      if (title != null) hdrTitle.textContent = title;
      if (subtitle != null) hdrSub.textContent = subtitle;
    },
  };

  /* ---------- header wiring ---------- */
  hdrNav.addEventListener("click", () => {
    const h = currentView?.header?.(currentParams);
    if (h && h.back != null) app.back();
    else host.dispatchEvent(new CustomEvent("hass-toggle-menu", { bubbles: true, composed: true }));
  });
  shadow.getElementById("hdr-kebab").addEventListener("click", (e) => {
    ui.openMenu({
      anchor: e.currentTarget,
      items: [
        { icon: "mdi:refresh", label: "Refresh now", onSelect: () => api.refreshSummary() },
        {
          icon: "mdi:sync", label: "Run reconcile",
          onSelect: async () => {
            try {
              await api.reconcile();
              ui.toast("Reconcile complete.");
              api.refreshSummary();
            } catch (err) {
              ui.toast("Reconcile failed: " + ((err && err.message) || err), { error: true });
            }
          },
        },
        { icon: "mdi:cog-outline", label: "Settings", onSelect: () => app.navigate("/settings") },
        "divider",
        {
          icon: "mdi:content-copy", label: "Copy Site ID",
          onSelect: async () => {
            const ok = await uiMod.copyText(api.summary?.site_id || "");
            ui.toast(ok ? "Site ID copied." : "Copy failed.", { error: !ok });
          },
        },
      ],
    });
  });

  /* ---------- view lifecycle ---------- */
  async function loadView(modulePath) {
    if (viewCache.has(modulePath)) return viewCache.get(modulePath);
    const mod = await loadModule(modulePath);
    const view = mod.createView(app);
    viewCache.set(modulePath, view);
    return view;
  }

  function renderHeader(view, params) {
    const isChromeless = view.chrome === false;
    hdr.style.display = isChromeless ? "none" : "";
    main.style.height = isChromeless ? "100%" : "";
    if (isChromeless) return;
    const h = (view.header && view.header(params)) || {};
    hdrTitle.textContent = h.title || "Casa Admin";
    const site = api.summary?.site_id;
    const count = api.summary?.stats?.devices;
    hdrSub.textContent =
      h.subtitle ||
      (site ? `Site ${site}${count != null ? ` · ${count} devices` : ""}` : "Provision and manage Casa devices");
    hdrNav.innerHTML = `<ha-icon icon="${h.back != null ? "mdi:arrow-left" : "mdi:menu"}"></ha-icon>`;
    hdrNav.title = h.back != null ? "Back" : "Menu";
  }

  let routeToken = 0;
  async function applyRoute(path) {
    pendingPath = path || "/";
    const token = ++routeToken;
    const { route, params } = matchRoute(pendingPath);

    if (currentView?.confirmLeave) {
      const ok = await currentView.confirmLeave();
      if (!ok || token !== routeToken) return;
    }

    let view;
    try {
      view = await loadView(route.view);
    } catch (err) {
      console.error("casa: failed to load view", route.view, err);
      main.innerHTML = `<div class="page"><div class="errbar">Failed to load ${uiMod.esc(route.view)}: ${uiMod.esc(String((err && err.message) || err))}</div></div>`;
      return;
    }
    if (token !== routeToken) return;

    ui.closePopover();
    try {
      currentView?.unmount?.();
    } catch (e) {
      console.error("casa: unmount failed", e);
    }
    currentView = view;
    currentParams = params;
    renderHeader(view, params);
    main.innerHTML = "";
    view.mount(main, params);
    if (view.polling === "paused") api.pausePolling();
    else api.resumePolling();
  }

  api.subscribe((summary, error) => {
    if (currentView) {
      renderHeader(currentView, currentParams);
      currentView.onSummary?.(summary, error);
    }
  });

  /* ---------- external property plumbing ---------- */
  let started = false;
  return {
    setHass(h) {
      if (!h) return;
      hass = h;
      api.setHass(h);
      currentView?.onHass?.(h);
      if (!started) {
        started = true;
        api.refreshSummary();
        api.startPolling(30000);
        applyRoute(pendingPath);
      }
    },
    setRoute(r) {
      const path = (r && r.path) || "/";
      if (started) applyRoute(path);
      else pendingPath = path;
    },
    setNarrow(v) {
      narrow = !!v;
    },
    setPanel(_p) {},
    onConnected() {
      if (started) api.startPolling(30000);
    },
    onDisconnected() {
      api.stopPolling();
      ui.closePopover();
    },
  };
}
