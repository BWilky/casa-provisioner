// Casa admin panel — guest accounts view. Simple searchable table with a
// create-account modal (one-time credentials screen), password reset, and
// account removal. Everything arrives via the `app` context (no static
// sibling imports).

export function createView(app) {
  const { api, ui } = app;

  let search = ""; // session-only
  let refs = null;
  let searchTimer = null;

  const unwrap = (res) => api.constructor.response(res); // CasaApi.response
  const accounts = () => (app.summary() && app.summary().accounts) || [];
  const byUsername = (u) => accounts().find((a) => a.username === u);

  /* ---------- render ---------- */

  function renderResults() {
    if (!refs) return;
    refs.err.innerHTML = api.lastError ? `<div class="errbar">${ui.esc(api.lastError)}</div>` : "";

    const summary = app.summary();
    const all = accounts();
    const q = search.trim().toLowerCase();
    const rows = q
      ? all.filter((a) =>
          [a.name, a.username].some((v) => String(v || "").toLowerCase().includes(q))
        )
      : all;

    refs.meta.innerHTML = `<span>${rows.length} account${rows.length === 1 ? "" : "s"}</span>`;

    if (!summary) {
      refs.results.innerHTML = `<div class="empty-state"><span class="muted">Loading…</span></div>`;
      return;
    }
    if (!all.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:account-off"></ha-icon>
        <div>No guest accounts yet</div>
        <button class="btn btn--primary" data-act="create">+ Create account</button>
      </div>`;
      return;
    }
    if (!rows.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:magnify-close"></ha-icon>
        <div>No accounts match</div>
        <button class="btn btn--text" data-act="clear-search">Clear search</button>
      </div>`;
      return;
    }

    const trs = rows
      .map(
        (a) => `<tr data-username="${ui.esc(a.username)}">
          <td><strong>${ui.esc(a.name || a.username)}</strong></td>
          <td class="mono">${ui.esc(a.username)}</td>
          <td>${Number(a.device_count) || 0}</td>
          <td>${ui.esc(ui.fmtTime(a.created_at))}${a.created_by ? ` <span class="muted">by ${ui.esc(a.created_by)}</span>` : ""}</td>
          <td class="col-actions">
            <button class="btn btn--icon" data-act="kebab" title="More actions"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
          </td>
        </tr>`
      )
      .join("");
    refs.results.innerHTML = `<div class="card" style="overflow-x:auto;">
      <table class="table">
        <thead><tr><th>Name</th><th>Username</th><th>Devices</th><th>Created</th><th></th></tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
  }

  /* ---------- one-time credentials modal ---------- */

  // Non-dismissable on purpose: the password is shown exactly once.
  function showCredentials({ modalTitle, heading, name, username, password }) {
    const body = document.createElement("div");
    const row = (label, value, mono) => `
      <div class="field-row" style="margin-bottom:10px;">
        <span style="width:88px; flex:none; font-weight:600; font-size:13px;">${ui.esc(label)}</span>
        <span class="${mono ? "mono" : ""}" style="flex:1; min-width:0; word-break:break-all;">${ui.esc(value)}</span>
        <button class="btn btn--outlined" style="height:28px; padding:0 10px; font-size:12px;" data-copy="${ui.esc(value)}">Copy</button>
      </div>`;
    body.innerHTML = `
      <div style="text-align:center; margin-bottom:16px;">
        <ha-icon icon="mdi:check-circle" style="--mdc-icon-size:48px; color:var(--casa-success);"></ha-icon>
        <h4 style="margin:8px 0 0; font-size:16px;">${ui.esc(heading)}</h4>
      </div>
      ${name != null ? row("Name", name) : ""}
      ${row("Username", username, true)}
      ${row("Password", password, true)}
      <div style="display:flex; gap:8px; align-items:center; margin-top:14px; padding:10px 12px; border-radius:8px; background:color-mix(in srgb, var(--casa-warning) 16%, transparent); color:var(--casa-warning); font-size:13px;">
        <ha-icon icon="mdi:alert" style="--mdc-icon-size:18px; flex:none;"></ha-icon>
        <span>This password is shown only once.</span>
      </div>`;
    for (const btn of body.querySelectorAll("[data-copy]")) {
      ui.bindCopyButton(btn, () => btn.dataset.copy);
    }
    ui.openModal({
      title: modalTitle,
      bodyEl: body,
      dismissable: false,
      buttons: [{ label: "Done", variant: "primary", onClick: () => { app.refresh(); } }],
    });
  }

  /* ---------- actions ---------- */

  function openCreateModal() {
    const body = document.createElement("div");
    body.innerHTML = `
      <div class="field">
        <label>Full Name *</label>
        <input class="input" id="acc-name" placeholder="e.g. John Doe">
      </div>
      <div class="field">
        <label>Username *</label>
        <input class="input" id="acc-username" placeholder="e.g. john" autocapitalize="none" autocomplete="off">
      </div>
      <div class="field">
        <label>Password</label>
        <input class="input" id="acc-password" type="password" placeholder="••••••••">
        <div class="field__help">Leave blank to auto-generate a secure password.</div>
      </div>
      <div class="field__error" id="acc-err" hidden></div>`;
    const userInput = body.querySelector("#acc-username");
    userInput.addEventListener("input", () => {
      userInput.value = userInput.value.toLowerCase();
    });
    ui.openModal({
      title: "Create guest account",
      bodyEl: body,
      buttons: [
        { label: "Cancel", variant: "text" },
        {
          label: "Create",
          variant: "primary",
          onClick: async (btn) => {
            const name = body.querySelector("#acc-name").value.trim();
            const username = userInput.value.trim().toLowerCase();
            const password = body.querySelector("#acc-password").value.trim();
            const errEl = body.querySelector("#acc-err");
            if (!name || !username) {
              errEl.hidden = false;
              errEl.textContent = "Name and Username are required.";
              return false;
            }
            btn.disabled = true;
            btn.textContent = "Creating…";
            try {
              const res = await api.createUser({ name, username, password: password || undefined, localOnly: true });
              const resp = unwrap(res);
              // Form modal closes (return undefined); credentials get their own
              // non-dismissable modal so they can't be lost to a stray click.
              showCredentials({
                modalTitle: "One-time credentials",
                heading: "Guest Account Created!",
                name,
                username,
                password: resp.password || password || "(Securely generated in backend)",
              });
              app.refresh();
            } catch (err) {
              errEl.hidden = false;
              errEl.textContent = "Failed: " + ((err && err.message) || err);
              btn.disabled = false;
              btn.textContent = "Create";
              return false;
            }
          },
        },
      ],
    });
  }

  function confirmResetPassword(a) {
    ui.showConfirm({
      title: "Reset password",
      message: `Are you sure you want to reset the password for guest '${a.username}'? This will scramble their credentials and revoke all active login sessions.`,
      confirmLabel: "Reset",
      onConfirm: async () => {
        try {
          const res = await api.scrambleGuestPassword(a.username, true);
          const resp = unwrap(res);
          showCredentials({
            modalTitle: "Password reset",
            heading: `Password for '${a.username}' has been reset`,
            username: a.username,
            password: resp.password || "(randomly scrambled)",
          });
        } catch (err) {
          ui.toast("Failed: " + ((err && err.message) || err), { error: true });
        }
      },
    });
  }

  function confirmRemove(a) {
    ui.showConfirm({
      title: "Remove account",
      message: `Are you sure you want to delete guest account '${a.username}'? This will remove their HA user profile, end all active sessions, and deregister all of their devices.`,
      confirmLabel: "Remove",
      onConfirm: async () => {
        try {
          await api.removeUser(a.username);
          ui.toast(`Account '${a.username}' removed.`);
          app.refresh();
        } catch (err) {
          ui.toast("Failed: " + ((err && err.message) || err), { error: true });
        }
      },
    });
  }

  function openAccountMenu(anchor, a) {
    ui.openMenu({
      anchor,
      items: [
        { icon: "mdi:lock-reset", label: "Reset password", onSelect: () => confirmResetPassword(a) },
        { icon: "mdi:account-remove", label: "Remove account", danger: true, onSelect: () => confirmRemove(a) },
      ],
    });
  }

  /* ---------- delegated events ---------- */

  function onResultsClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    switch (el.dataset.act) {
      case "kebab": {
        const rowEl = el.closest("[data-username]");
        const a = rowEl ? byUsername(rowEl.dataset.username) : null;
        if (a) openAccountMenu(el, a);
        break;
      }
      case "create":
        openCreateModal();
        break;
      case "clear-search":
        search = "";
        if (refs) refs.search.value = "";
        renderResults();
        break;
    }
  }

  /* ---------- view ---------- */

  return {
    id: "accounts",
    header: () => ({ title: "Casa Admin" }),
    polling: "live",

    mount(el) {
      el.innerHTML = `
        <div class="page">
          <div id="ac-err"></div>
          <div class="tabs">
            <button class="tab" id="ac-tab-devices">Devices</button>
            <button class="tab tab--active">Accounts</button>
            <button class="tab" id="ac-tab-sessions">Sessions</button>
            <button class="tab" id="ac-tab-profiles">Provision Profiles</button>
            <button class="tab" id="ac-tab-wireguard">WireGuard Profiles</button>
          </div>
          <div class="list-toolbar">
            <div class="search-field">
              <ha-icon icon="mdi:magnify"></ha-icon>
              <input class="input" id="ac-search" type="search" placeholder="Search accounts…">
            </div>
            <span class="spacer"></span>
            <button class="btn btn--primary" id="ac-create">+ Create account</button>
          </div>
          <div class="list-meta" id="ac-meta"></div>
          <div id="ac-results"></div>
        </div>`;

      refs = {
        err: el.querySelector("#ac-err"),
        search: el.querySelector("#ac-search"),
        meta: el.querySelector("#ac-meta"),
        results: el.querySelector("#ac-results"),
      };

      refs.search.value = search;
      refs.search.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          search = refs ? refs.search.value : search;
          renderResults();
        }, 150);
      });
      el.querySelector("#ac-tab-devices").addEventListener("click", () => app.navigate("/"));
      el.querySelector("#ac-tab-sessions").addEventListener("click", () => app.navigate("/sessions"));
      el.querySelector("#ac-tab-profiles").addEventListener("click", () => app.navigate("/profiles"));
      el.querySelector("#ac-tab-wireguard").addEventListener("click", () => app.navigate("/wireguard"));
      el.querySelector("#ac-create").addEventListener("click", openCreateModal);
      refs.results.addEventListener("click", onResultsClick);

      renderResults();
    },

    unmount() {
      clearTimeout(searchTimer);
      searchTimer = null;
      refs = null;
    },

    onSummary() {
      renderResults();
    },
  };
}
