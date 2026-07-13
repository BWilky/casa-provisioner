// Casa admin panel — provisioning profiles view. Simple searchable table
// listing reusable provisioning templates; create/edit happens on the
// existing profile-editor.js page, this view just lists + deletes.

export function createView(app) {
  const { api, ui } = app;

  let search = ""; // session-only
  let refs = null;
  let searchTimer = null;
  let profiles = [];
  let loading = true;

  const byId = (id) => profiles.find((p) => p.id === id);

  /* ---------- data ---------- */

  async function load() {
    loading = true;
    renderResults();
    try {
      const res = await api.getProvisionProfiles();
      profiles = (res && res.profiles) || [];
    } catch (err) {
      if (refs) refs.err.innerHTML = `<div class="errbar">${ui.esc((err && err.message) || err)}</div>`;
      profiles = [];
    }
    loading = false;
    renderResults();
  }

  /* ---------- render ---------- */

  function renderResults() {
    if (!refs) return;

    const q = search.trim().toLowerCase();
    const rows = q
      ? profiles.filter((p) =>
          [p.name, p.fields && p.fields.host_url, p.fields && p.fields.username].some((v) =>
            String(v || "").toLowerCase().includes(q)
          )
        )
      : profiles;

    refs.meta.innerHTML = `<span>${rows.length} profile${rows.length === 1 ? "" : "s"}</span>`;

    if (loading) {
      refs.results.innerHTML = `<div class="empty-state"><span class="muted">Loading…</span></div>`;
      return;
    }
    if (!profiles.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:file-cog-outline"></ha-icon>
        <div>No provisioning profiles yet</div>
        <button class="btn btn--primary" data-act="create">+ New profile</button>
      </div>`;
      return;
    }
    if (!rows.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:magnify-close"></ha-icon>
        <div>No profiles match</div>
        <button class="btn btn--text" data-act="clear-search">Clear search</button>
      </div>`;
      return;
    }

    const trs = rows
      .map((p) => {
        const f = p.fields || {};
        return `<tr data-id="${ui.esc(p.id)}">
          <td><strong>${ui.esc(p.name || "(unnamed)")}</strong></td>
          <td class="mono">${ui.esc(f.host_url || "—")}${f.username ? ` <span class="muted">(${ui.esc(f.username)})</span>` : ""}</td>
          <td>${ui.esc(ui.fmtTime(p.updated_at))}</td>
          <td class="col-actions">
            <button class="btn btn--icon" data-act="kebab" title="More actions"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
          </td>
        </tr>`;
      })
      .join("");
    refs.results.innerHTML = `<div class="card" style="overflow-x:auto;">
      <table class="table">
        <thead><tr><th>Name</th><th>Host</th><th>Updated</th><th></th></tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
  }

  /* ---------- actions ---------- */

  function gotoEdit(id) {
    app.navigate("/profiles/" + encodeURIComponent(id));
  }

  function confirmRemove(p) {
    ui.showConfirm({
      title: "Delete profile",
      message: `Are you sure you want to delete provisioning profile '${p.name || p.id}'? Devices already provisioned from it are unaffected.`,
      confirmLabel: "Delete",
      onConfirm: async () => {
        try {
          await api.deleteProvisionProfile(p.id);
          ui.toast(`Profile '${p.name || p.id}' deleted.`);
          load();
        } catch (err) {
          ui.toast("Failed: " + ((err && err.message) || err), { error: true });
        }
      },
    });
  }

  function openProfileMenu(anchor, p) {
    ui.openMenu({
      anchor,
      items: [
        { icon: "mdi:pencil", label: "Edit profile", onSelect: () => gotoEdit(p.id) },
        { icon: "mdi:delete", label: "Delete profile", danger: true, onSelect: () => confirmRemove(p) },
      ],
    });
  }

  /* ---------- delegated events ---------- */

  function onResultsClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    switch (el.dataset.act) {
      case "kebab": {
        const rowEl = el.closest("[data-id]");
        const p = rowEl ? byId(rowEl.dataset.id) : null;
        if (p) openProfileMenu(el, p);
        break;
      }
      case "create":
        app.navigate("/profiles/new");
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
    id: "profiles",
    header: () => ({ title: "Casa Admin" }),
    polling: "live",

    mount(el) {
      el.innerHTML = `
        <div class="page">
          <div id="pp-err"></div>
          <div class="tabs">
            <button class="tab" id="pp-tab-devices">Devices</button>
            <button class="tab" id="pp-tab-accounts">Accounts</button>
            <button class="tab" id="pp-tab-sessions">Sessions</button>
            <button class="tab tab--active">Provision Profiles</button>
            <button class="tab" id="pp-tab-wireguard">WireGuard Profiles</button>
          </div>
          <div class="list-toolbar">
            <div class="search-field">
              <ha-icon icon="mdi:magnify"></ha-icon>
              <input class="input" id="pp-search" type="search" placeholder="Search profiles…">
            </div>
            <span class="spacer"></span>
            <button class="btn btn--primary" id="pp-create">+ New profile</button>
          </div>
          <div class="list-meta" id="pp-meta"></div>
          <div id="pp-results"></div>
        </div>`;

      refs = {
        err: el.querySelector("#pp-err"),
        search: el.querySelector("#pp-search"),
        meta: el.querySelector("#pp-meta"),
        results: el.querySelector("#pp-results"),
      };

      refs.search.value = search;
      refs.search.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          search = refs ? refs.search.value : search;
          renderResults();
        }, 150);
      });
      el.querySelector("#pp-tab-devices").addEventListener("click", () => app.navigate("/"));
      el.querySelector("#pp-tab-accounts").addEventListener("click", () => app.navigate("/accounts"));
      el.querySelector("#pp-tab-sessions").addEventListener("click", () => app.navigate("/sessions"));
      el.querySelector("#pp-tab-wireguard").addEventListener("click", () => app.navigate("/wireguard"));
      el.querySelector("#pp-create").addEventListener("click", () => app.navigate("/profiles/new"));
      refs.results.addEventListener("click", onResultsClick);

      load();
    },

    unmount() {
      clearTimeout(searchTimer);
      searchTimer = null;
      refs = null;
    },
  };
}
