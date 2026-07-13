// Casa admin panel — sessions view. Two-pane layout: left nav listing "All"
// plus every HA account (casa-managed ones get a marker icon), main pane
// showing the selected account's login sessions / refresh tokens with
// per-session revoke. Data comes from the dedicated sessions endpoint
// (fetched on mount, quietly refreshed on the summary tick), not the summary.

export function createView(app) {
  const { api, ui } = app;

  let data = null; // GET /casa/admin/sessions payload
  let loading = true;
  let selectedUserId = null; // null = "All"
  let refs = null;

  const users = () => (data && data.users) || [];
  const byUserId = (id) => users().find((u) => u.user_id === id);

  /* ---------- data ---------- */

  async function load({ quiet = false } = {}) {
    if (!quiet) {
      loading = true;
      render();
    }
    try {
      data = await api.getSessions();
      if (refs) refs.err.innerHTML = "";
    } catch (err) {
      if (refs) refs.err.innerHTML = `<div class="errbar">${ui.esc((err && err.message) || err)}</div>`;
      if (!quiet) data = null;
    }
    if (selectedUserId && !byUserId(selectedUserId)) selectedUserId = null;
    loading = false;
    render();
  }

  /* ---------- render ---------- */

  function render() {
    renderNav();
    renderResults();
  }

  function navItem({ uid, icon, iconTitle, label, labelSuffix, count, active }) {
    return `<button class="nav-item ${active ? "nav-item--active" : ""}" data-uid="${ui.esc(uid)}">
      <ha-icon icon="${icon}"${iconTitle ? ` title="${ui.esc(iconTitle)}"` : ""}></ha-icon>
      <span style="min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${ui.esc(label)}${labelSuffix || ""}</span>
      <span class="badge badge--count">${count}</span>
    </button>`;
  }

  function renderNav() {
    if (!refs) return;
    if (loading && !data) {
      refs.nav.innerHTML = "";
      return;
    }
    const total = users().reduce((n, u) => n + (u.session_count || 0), 0);
    const items = [
      navItem({
        uid: "",
        icon: "mdi:account-group",
        label: "All",
        count: total,
        active: selectedUserId === null,
      }),
      ...users().map((u) =>
        navItem({
          uid: u.user_id,
          icon: u.casa_managed ? "mdi:home-account" : "mdi:account",
          iconTitle: u.casa_managed ? "Casa-managed account" : undefined,
          label: u.name,
          labelSuffix: u.is_active ? "" : ` <span class="muted">(inactive)</span>`,
          count: u.session_count || 0,
          active: selectedUserId === u.user_id,
        })
      ),
    ];
    refs.nav.innerHTML = items.join("");
  }

  function typeChips(s) {
    const chips = [];
    if (s.is_current) chips.push(`<span class="chip chip--app">Current</span>`);
    if (s.token_type === "long_lived_access_token") {
      chips.push(`<span class="chip chip--warn">Long-lived</span>`);
    } else if (!s.is_current) {
      chips.push(`<span class="chip chip--neutral">Session</span>`);
    }
    if (s.device_id) chips.push(`<span class="chip chip--ok">Casa device</span>`);
    return chips.join(" ");
  }

  function renderResults() {
    if (!refs) return;

    const showUserCol = selectedUserId === null;
    const selected = selectedUserId ? byUserId(selectedUserId) : null;
    const rows = selected
      ? selected.sessions.map((s) => ({ user: selected, s }))
      : users().flatMap((u) => u.sessions.map((s) => ({ user: u, s })));

    refs.meta.innerHTML = `<span>${rows.length} session${rows.length === 1 ? "" : "s"}${selected ? ` for ${ui.esc(selected.name)}` : ""}</span>`;

    if (loading && !data) {
      refs.results.innerHTML = `<div class="empty-state"><span class="muted">Loading…</span></div>`;
      return;
    }
    if (!data) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:key-remove"></ha-icon>
        <div>Could not load sessions</div>
        <button class="btn btn--text" data-act="reload">Retry</button>
      </div>`;
      return;
    }
    if (!rows.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:key-remove"></ha-icon>
        <div>No active sessions${selected ? ` for ${ui.esc(selected.name)}` : ""}</div>
      </div>`;
      return;
    }

    const trs = rows
      .map(({ user, s }) => {
        const client = s.device_alias || s.client_name || s.client_id || s.token_suffix;
        const lastUsed = s.last_used_at
          ? `${ui.esc(ui.relTime(s.last_used_at))}${s.last_used_ip ? ` <span class="muted mono">${ui.esc(s.last_used_ip)}</span>` : ""}`
          : `<span class="muted">never</span>`;
        return `<tr data-uid="${ui.esc(user.user_id)}" data-token="${ui.esc(s.token_id)}">
          ${showUserCol ? `<td><strong>${ui.esc(user.name)}</strong>${user.casa_managed ? ` <ha-icon icon="mdi:home-account" title="Casa-managed account" style="--mdc-icon-size:14px; color:var(--casa-text-2); vertical-align:middle;"></ha-icon>` : ""}</td>` : ""}
          <td>
            <div>${ui.esc(client)}</div>
            <div class="muted mono" style="font-size:11px;">…${ui.esc(s.token_suffix)}</div>
          </td>
          <td>${typeChips(s)}</td>
          <td>${ui.esc(ui.fmtTime(s.created_at))}</td>
          <td>${lastUsed}</td>
          <td class="col-actions">
            <button class="btn btn--icon" data-act="revoke" title="Revoke session" style="color:var(--casa-error);"><ha-icon icon="mdi:logout-variant"></ha-icon></button>
          </td>
        </tr>`;
      })
      .join("");

    refs.results.innerHTML = `<div class="card" style="overflow-x:auto;">
      <table class="table">
        <thead><tr>
          ${showUserCol ? "<th>User</th>" : ""}
          <th>Client / Device</th><th>Type</th><th>Created</th><th>Last used</th><th></th>
        </tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
  }

  /* ---------- actions ---------- */

  function confirmRevoke(user, s) {
    let message;
    if (s.is_current) {
      message = `This is YOUR current session. Revoking it will sign you out of Home Assistant in this browser immediately.`;
    } else {
      message = `Revoke this session for '${user.name}'? The device or browser using it will be signed out.`;
      if (s.device_id) {
        message += ` This session belongs to casa device '${s.device_alias || s.device_id}' — the device will need to be re-provisioned or re-registered.`;
      }
    }
    ui.showConfirm({
      title: "Revoke session",
      message,
      confirmLabel: "Revoke",
      onConfirm: async () => {
        try {
          await api.revokeSession(user.user_id, s.token_id);
          ui.toast("Session revoked.");
          load({ quiet: true });
        } catch (err) {
          ui.toast("Failed: " + ((err && err.message) || err), { error: true });
        }
      },
    });
  }

  /* ---------- delegated events ---------- */

  function onNavClick(e) {
    const el = e.target.closest("[data-uid]");
    if (!el) return;
    selectedUserId = el.dataset.uid || null;
    render();
  }

  function onResultsClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    switch (el.dataset.act) {
      case "revoke": {
        const rowEl = el.closest("[data-token]");
        const user = rowEl ? byUserId(rowEl.dataset.uid) : null;
        const s = user ? user.sessions.find((x) => x.token_id === rowEl.dataset.token) : null;
        if (user && s) confirmRevoke(user, s);
        break;
      }
      case "reload":
        load();
        break;
    }
  }

  /* ---------- view ---------- */

  return {
    id: "sessions",
    header: () => ({ title: "Casa Admin" }),
    polling: "live",

    mount(el) {
      el.innerHTML = `
        <div class="page">
          <div id="ss-err"></div>
          <div class="tabs">
            <button class="tab" id="ss-tab-devices">Devices</button>
            <button class="tab" id="ss-tab-accounts">Accounts</button>
            <button class="tab tab--active">Sessions</button>
            <button class="tab" id="ss-tab-profiles">Provision Profiles</button>
            <button class="tab" id="ss-tab-wireguard">WireGuard Profiles</button>
          </div>
          <div class="list-meta" id="ss-meta"></div>
          <div class="split">
            <nav class="split__nav" id="ss-nav"></nav>
            <div class="split__main" id="ss-results"></div>
          </div>
        </div>`;

      refs = {
        err: el.querySelector("#ss-err"),
        meta: el.querySelector("#ss-meta"),
        nav: el.querySelector("#ss-nav"),
        results: el.querySelector("#ss-results"),
      };

      el.querySelector("#ss-tab-devices").addEventListener("click", () => app.navigate("/"));
      el.querySelector("#ss-tab-accounts").addEventListener("click", () => app.navigate("/accounts"));
      el.querySelector("#ss-tab-profiles").addEventListener("click", () => app.navigate("/profiles"));
      el.querySelector("#ss-tab-wireguard").addEventListener("click", () => app.navigate("/wireguard"));
      refs.nav.addEventListener("click", onNavClick);
      refs.results.addEventListener("click", onResultsClick);

      load();
    },

    unmount() {
      refs = null;
    },

    // Piggyback the 30s summary tick to keep last-used timestamps fresh.
    onSummary() {
      if (refs && !loading) load({ quiet: true });
    },
  };
}
