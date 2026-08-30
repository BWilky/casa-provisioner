// Casa admin panel — WireGuard profiles view. Simple searchable table
// listing reusable WireGuard configs, with a create/edit modal (the entity
// is only 3 fields, so a modal is used rather than a dedicated editor page).

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
      const res = await api.getWireguardProfiles();
      profiles = (res && res.profiles) || [];
    } catch (err) {
      if (refs) refs.err.innerHTML = `<div class="errbar">${ui.esc(ui.errMsg(err))}</div>`;
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
      ? profiles.filter((p) => String(p.alias || "").toLowerCase().includes(q))
      : profiles;

    refs.meta.innerHTML = `<span>${rows.length} profile${rows.length === 1 ? "" : "s"}</span>`;

    if (loading) {
      refs.results.innerHTML = `<div class="empty-state"><span class="muted">Loading…</span></div>`;
      return;
    }
    if (!profiles.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:vpn"></ha-icon>
        <div>No WireGuard profiles yet</div>
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
      .map(
        (p) => `<tr data-id="${ui.esc(p.id)}">
          <td><strong>${ui.esc(p.alias || "(unnamed)")}</strong></td>
          <td class="mono">${ui.esc(p.excluded_wifi || "—")}</td>
          <td>${ui.esc(ui.fmtTime(p.created_at))}</td>
          <td class="col-actions">
            <button class="btn btn--icon" data-act="kebab" title="More actions"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
          </td>
        </tr>`
      )
      .join("");
    refs.results.innerHTML = `<div class="card" style="overflow-x:auto;">
      <table class="table">
        <thead><tr><th>Alias</th><th>Excluded Wi-Fi</th><th>Created</th><th></th></tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
  }

  /* ---------- create/edit modal ---------- */

  function openProfileModal(existing) {
    const body = document.createElement("div");
    body.innerHTML = `
      <div class="field">
        <label>Alias</label>
        <input class="input" id="wg-alias" placeholder="e.g. Home Network" value="${ui.esc((existing && existing.alias) || "")}">
        <div class="field__help">Leave blank to auto-generate a name.</div>
      </div>
      <div class="field">
        <label>Config *</label>
        <textarea class="input" id="wg-config" rows="8" placeholder="[Interface]&#10;PrivateKey = ...&#10;&#10;[Peer]&#10;PublicKey = ...">${ui.esc((existing && existing.config) || "")}</textarea>
      </div>
      <div class="field">
        <label>Excluded Wi-Fi</label>
        <input class="input" id="wg-excluded" placeholder="Comma-separated SSIDs" value="${ui.esc((existing && existing.excluded_wifi) || "")}">
      </div>
      <div class="field__error" id="wg-err" hidden></div>`;

    ui.openModal({
      title: existing ? "Edit WireGuard profile" : "New WireGuard profile",
      bodyEl: body,
      buttons: [
        { label: "Cancel", variant: "text" },
        {
          label: existing ? "Save" : "Create",
          variant: "primary",
          onClick: async (btn) => {
            const alias = body.querySelector("#wg-alias").value.trim();
            const config = body.querySelector("#wg-config").value.trim();
            const excluded_wifi = body.querySelector("#wg-excluded").value.trim();
            const errEl = body.querySelector("#wg-err");
            if (!config) {
              errEl.hidden = false;
              errEl.textContent = "Config is required.";
              return false;
            }
            btn.disabled = true;
            btn.textContent = existing ? "Saving…" : "Creating…";
            try {
              const profile = { alias, config, excluded_wifi };
              if (existing) profile.id = existing.id;
              await api.saveWireguardProfile(profile);
              ui.toast(existing ? "Profile saved." : "Profile created.");
              load();
            } catch (err) {
              errEl.hidden = false;
              errEl.textContent = "Failed: " + ui.errMsg(err);
              btn.disabled = false;
              btn.textContent = existing ? "Save" : "Create";
              return false;
            }
          },
        },
      ],
    });
  }

  function confirmRemove(p) {
    ui.showConfirm({
      title: "Delete profile",
      message: `Are you sure you want to delete WireGuard profile '${p.alias || p.id}'? Provision templates linking to it will fall back to no VPN until re-linked.`,
      confirmLabel: "Delete",
      onConfirm: async () => {
        try {
          await api.deleteWireguardProfile(p.id);
          ui.toast(`Profile '${p.alias || p.id}' deleted.`);
          load();
        } catch (err) {
          ui.toast("Failed: " + ui.errMsg(err), { error: true });
        }
      },
    });
  }

  function openProfileMenu(anchor, p) {
    ui.openMenu({
      anchor,
      items: [
        { icon: "mdi:pencil", label: "Edit profile", onSelect: () => openProfileModal(p) },
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
        openProfileModal(null);
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
    id: "wireguard-profiles",
    header: () => ({ title: "Casa Admin" }),
    polling: "live",

    mount(el) {
      el.innerHTML = `
        <div class="page">
          <div id="wg-list-err"></div>
          <div class="tabs">
            <button class="tab" id="wg-tab-devices">Devices</button>
            <button class="tab" id="wg-tab-accounts">Accounts</button>
            <button class="tab" id="wg-tab-sessions">Sessions</button>
            <button class="tab" id="wg-tab-profiles">Provision Templates</button>
            <button class="tab tab--active">WireGuard Profiles</button>
          </div>
          <div class="list-toolbar">
            <div class="search-field">
              <ha-icon icon="mdi:magnify"></ha-icon>
              <input class="input" id="wg-search" type="search" placeholder="Search profiles…">
            </div>
            <span class="spacer"></span>
            <button class="btn btn--primary" id="wg-create">+ New profile</button>
          </div>
          <div class="list-meta" id="wg-meta"></div>
          <div id="wg-results"></div>
        </div>`;

      refs = {
        err: el.querySelector("#wg-list-err"),
        search: el.querySelector("#wg-search"),
        meta: el.querySelector("#wg-meta"),
        results: el.querySelector("#wg-results"),
      };

      refs.search.value = search;
      refs.search.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          search = refs ? refs.search.value : search;
          renderResults();
        }, 150);
      });
      el.querySelector("#wg-tab-devices").addEventListener("click", () => app.navigate("/"));
      el.querySelector("#wg-tab-accounts").addEventListener("click", () => app.navigate("/accounts"));
      el.querySelector("#wg-tab-sessions").addEventListener("click", () => app.navigate("/sessions"));
      el.querySelector("#wg-tab-profiles").addEventListener("click", () => app.navigate("/templates"));
      el.querySelector("#wg-create").addEventListener("click", () => openProfileModal(null));
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
