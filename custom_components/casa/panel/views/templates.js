// Casa admin panel — provision templates view. Simple searchable table
// listing reusable provision templates; create/edit happens on the
// template-editor.js page, this view just lists + deletes + hosts the
// shared "Apply to devices" modal (one-time bulk apply of a template's set
// live fields — devices do not become attached to the template).

/* ---------- shared apply-to-devices modal ---------- */

// openApplyTemplateModal(app, template) — template: {id, name, fields}
// (sparse fields). Also imported lazily by template-editor.js.
export async function openApplyTemplateModal(app, template) {
  const { api, ui } = app;
  const esc = ui.esc;

  let fieldsMod;
  try {
    fieldsMod = await app.loadModule("views/profile-fields.js");
  } catch (err) {
    ui.toast("Failed to load: " + ui.errMsg(err), { error: true });
    return;
  }

  const setLive = Object.keys(template.fields || {}).filter((k) => fieldsMod.LIVE_KEYS.has(k));
  if (!setLive.length) {
    ui.showInfo({
      title: "Nothing to apply",
      message: "This template sets no device-live fields. Session expiration is resolved at provision time only — set a live field (connection, UI, access, VPN…) to apply it to devices.",
    });
    return;
  }

  const devices = (app.summary()?.devices || []).slice().sort((a, b) =>
    String(a.alias || a.username || "").localeCompare(String(b.alias || b.username || ""))
  );
  if (!devices.length) {
    ui.showInfo({ title: "No devices", message: "There are no registered devices to apply this template to." });
    return;
  }

  const chipList = setLive.map((k) => `<span class="chip chip--neutral">${esc(k)}</span>`).join(" ");
  const deviceRow = (d) => {
    const label = d.alias || (d.device_id || "").slice(0, 12);
    const badges = [
      d.orphaned ? '<span class="badge badge--orphan">orphaned</span>' : "",
      d.stale ? '<span class="badge badge--stale">stale</span>' : "",
    ].join(" ");
    return `
      <label class="toggle" data-apply-row data-search="${esc(`${label} ${d.username || ""}`.toLowerCase())}" style="margin-bottom:6px;">
        <input type="checkbox" data-apply-device value="${esc(d.device_id)}">
        <span>${esc(label)} <span class="muted">(${esc(d.username || "—")})</span> ${badges}</span>
      </label>`;
  };

  // Two-column layout: explanation/chips/toggles on the left, the scrollable
  // device picker on the right (stacks when the modal is narrow).
  const body = document.createElement("div");
  body.innerHTML = `
    <div style="display:flex; gap:18px; flex-wrap:wrap; align-items:stretch;">
      <div style="flex:1 1 260px; min-width:240px; display:flex; flex-direction:column;">
        <div class="muted" style="font-size:12px; line-height:1.5; margin-bottom:10px;">
          One-time apply: pushes this template's ${setLive.length} set device
          field${setLive.length === 1 ? "" : "s"} to the selected devices. Devices
          do not follow the template afterwards — later template edits change
          nothing until you apply again.
        </div>
        <div style="display:flex; flex-wrap:wrap; gap:4px; margin-bottom:14px;">${chipList}</div>
        <span style="flex:1;"></span>
        <label class="toggle"><input type="checkbox" data-apply-send checked> Send update via push</label>
        <label class="toggle"><input type="checkbox" data-apply-notify> Notify via push</label>
        <div class="field__error" data-apply-err hidden></div>
      </div>
      <div style="flex:1 1 260px; min-width:240px; display:flex; flex-direction:column;">
        <div class="field" style="margin-bottom:8px;">
          <input class="input" type="search" data-apply-search placeholder="Filter devices…">
        </div>
        <label class="toggle" style="margin-bottom:6px;"><input type="checkbox" data-apply-all> Select all</label>
        <div data-apply-list style="flex:1; max-height:340px; overflow-y:auto; padding:6px 0; border-top:1px solid var(--casa-divider); border-bottom:1px solid var(--casa-divider);">
          ${devices.map(deviceRow).join("")}
        </div>
      </div>
    </div>`;

  const listEl = body.querySelector("[data-apply-list]");
  const allBox = body.querySelector("[data-apply-all]");
  const boxes = () => [...listEl.querySelectorAll("[data-apply-device]")];
  const visibleBoxes = () => boxes().filter((b) => !b.closest("[data-apply-row]").hidden);

  body.querySelector("[data-apply-search]").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    for (const row of listEl.querySelectorAll("[data-apply-row]")) {
      row.hidden = !!q && !row.dataset.search.includes(q);
    }
  });
  allBox.addEventListener("change", () => {
    for (const b of visibleBoxes()) b.checked = allBox.checked;
  });

  ui.openModal({
    title: `Apply template — ${template.name || template.id}`,
    bodyEl: body,
    wide: true,
    buttons: [
      { label: "Cancel", variant: "text" },
      {
        label: "Apply Template",
        variant: "primary",
        onClick: async (btn) => {
          const errEl = body.querySelector("[data-apply-err]");
          errEl.hidden = true;
          const deviceIds = boxes().filter((b) => b.checked).map((b) => b.value);
          if (!deviceIds.length) {
            errEl.hidden = false;
            errEl.textContent = "Select at least one device.";
            return false;
          }
          const notifyPush = body.querySelector("[data-apply-notify]").checked;

          const idleLabel = btn.innerHTML;
          btn.disabled = true;
          btn.innerHTML = '<span class="spinner"></span> Applying…';
          const restore = () => {
            btn.disabled = false;
            btn.innerHTML = idleLabel;
          };
          const successText = (res) => {
            const extras = [];
            if (res.skipped) extras.push(`${res.skipped} without push`);
            if (res.not_found) extras.push(`${res.not_found} not found`);
            return `Template queued for ${res.queued} device${res.queued === 1 ? "" : "s"}` +
              `${extras.length ? ` (${extras.join(", ")})` : ""} — pushes sending in background.`;
          };

          const applyPromise = api.applyTemplateToDevices({
            templateId: template.id,
            deviceIds,
            sendPush: body.querySelector("[data-apply-send]").checked,
            notifyPush,
            title: notifyPush ? "Settings updated" : "",
            message: notifyPush ? `New settings from template '${template.name || template.id}' were applied.` : "",
          });
          // hass.callApi has no timeout/abort of its own; race it against a
          // 20s timer so a hung backend can't leave the button spinning
          // forever (the request itself keeps running server-side).
          const TIMEOUT = {};
          let res;
          try {
            res = await Promise.race([
              applyPromise,
              new Promise((resolve) => setTimeout(resolve, 20000, TIMEOUT)),
            ]);
          } catch (err) {
            restore();
            errEl.hidden = false;
            errEl.textContent = "Failed to apply: " + ui.errMsg(err);
            return false;
          }
          if (res === TIMEOUT) {
            restore();
            errEl.hidden = false;
            errEl.textContent = "Timed out waiting for the server — the update may still have been queued. Check the devices' pending updates before retrying.";
            ui.toast("Apply template timed out.", { error: true });
            // If the request does land later, surface it instead of dropping it.
            applyPromise.then((late) => {
              ui.toast(successText(late));
              app.refresh();
            }, () => {});
            return false;
          }
          ui.toast(successText(res));
          app.refresh();
        },
      },
    ],
  });
}

/* ---------- view ---------- */

export function createView(app) {
  const { api, ui } = app;

  let search = ""; // session-only
  let refs = null;
  let searchTimer = null;
  let templates = [];
  let loading = true;

  const byId = (id) => templates.find((p) => p.id === id);

  /* ---------- data ---------- */

  async function load() {
    loading = true;
    renderResults();
    try {
      const res = await api.getProvisionTemplates();
      templates = (res && res.profiles) || [];
    } catch (err) {
      if (refs) refs.err.innerHTML = `<div class="errbar">${ui.esc(ui.errMsg(err))}</div>`;
      templates = [];
    }
    loading = false;
    renderResults();
  }

  /* ---------- render ---------- */

  function renderResults() {
    if (!refs) return;

    const q = search.trim().toLowerCase();
    const rows = q
      ? templates.filter((p) =>
          [p.name, p.fields && p.fields.host_url].some((v) =>
            String(v || "").toLowerCase().includes(q)
          )
        )
      : templates;

    refs.meta.innerHTML = `<span>${rows.length} template${rows.length === 1 ? "" : "s"}</span>`;

    if (loading) {
      refs.results.innerHTML = `<div class="empty-state"><span class="muted">Loading…</span></div>`;
      return;
    }
    if (!templates.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:file-cog-outline"></ha-icon>
        <div>No provision templates yet</div>
        <button class="btn btn--primary" data-act="create">+ New template</button>
      </div>`;
      return;
    }
    if (!rows.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:magnify-close"></ha-icon>
        <div>No templates match</div>
        <button class="btn btn--text" data-act="clear-search">Clear search</button>
      </div>`;
      return;
    }

    const trs = rows
      .map((p) => {
        const f = p.fields || {};
        const setCount = Object.keys(f).length;
        return `<tr data-id="${ui.esc(p.id)}">
          <td><strong>${ui.esc(p.name || "(unnamed)")}</strong></td>
          <td class="mono">${ui.esc(f.host_url || "—")}</td>
          <td><span class="chip chip--neutral">sets ${setCount} field${setCount === 1 ? "" : "s"}</span></td>
          <td>${ui.esc(ui.fmtTime(p.updated_at))}</td>
          <td class="col-actions">
            <button class="btn btn--icon" data-act="kebab" title="More actions"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
          </td>
        </tr>`;
      })
      .join("");
    refs.results.innerHTML = `<div class="card" style="overflow-x:auto;">
      <table class="table">
        <thead><tr><th>Name</th><th>Host</th><th>Fields</th><th>Updated</th><th></th></tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
  }

  /* ---------- actions ---------- */

  function gotoEdit(id) {
    app.navigate("/templates/" + encodeURIComponent(id));
  }

  function confirmRemove(p) {
    ui.showConfirm({
      title: "Delete template",
      message: `Are you sure you want to delete provision template '${p.name || p.id}'? Devices already provisioned from it are unaffected — templates stamp values at provision time.`,
      confirmLabel: "Delete",
      onConfirm: async () => {
        try {
          await api.deleteProvisionTemplate(p.id);
          ui.toast(`Template '${p.name || p.id}' deleted.`);
          load();
        } catch (err) {
          ui.toast("Failed: " + ui.errMsg(err), { error: true });
        }
      },
    });
  }

  function openTemplateMenu(anchor, p) {
    ui.openMenu({
      anchor,
      items: [
        { icon: "mdi:qrcode", label: "Provision with this template", onSelect: () => app.navigate("/provision/template/" + encodeURIComponent(p.id)) },
        { icon: "mdi:send", label: "Apply to devices…", onSelect: () => openApplyTemplateModal(app, p) },
        { icon: "mdi:pencil", label: "Edit template", onSelect: () => gotoEdit(p.id) },
        { icon: "mdi:delete", label: "Delete template", danger: true, onSelect: () => confirmRemove(p) },
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
        if (p) openTemplateMenu(el, p);
        break;
      }
      case "create":
        app.navigate("/templates/new");
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
    id: "templates",
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
            <button class="tab tab--active">Provision Templates</button>
            <button class="tab" id="pp-tab-wireguard">WireGuard Profiles</button>
          </div>
          <div class="list-toolbar">
            <div class="search-field">
              <ha-icon icon="mdi:magnify"></ha-icon>
              <input class="input" id="pp-search" type="search" placeholder="Search templates…">
            </div>
            <span class="spacer"></span>
            <button class="btn btn--primary" id="pp-create">+ New template</button>
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
      el.querySelector("#pp-create").addEventListener("click", () => app.navigate("/templates/new"));
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
