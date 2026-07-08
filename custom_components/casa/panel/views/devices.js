// Casa admin panel — devices list view. ESPHome-style device table/card grid
// with search, sort, filters, column picker, pagination, multi-select bulk
// actions, and a per-device kebab menu. Everything arrives via the `app`
// context (no static sibling imports).

const ONLINE_WINDOW_MS = 11 * 60 * 1000;
const PAGE_SIZES = [10, 25, 50, 100];

// name + actions are always shown; the rest can be toggled from "Columns".
const COLUMNS = [
  { key: "status", label: "Status" },
  { key: "name", label: "Name", always: true },
  { key: "user", label: "User" },
  { key: "ip", label: "IP" },
  { key: "app", label: "App" },
  { key: "expires", label: "Expires" },
  { key: "last_seen", label: "Last seen" },
];
const DEFAULT_COLUMNS = ["status", "name", "user", "ip", "app", "expires"];

export function createView(app) {
  const { api, ui, store } = app;

  /* ---------- state ---------- */
  let search = ""; // session-only
  let filters = { status: [], user: "", kind: "" }; // session-only

  const savedSort = store.get("list.sort", null) || {};
  let sortKey = COLUMNS.some((c) => c.key === savedSort.key) ? savedSort.key : "name";
  let sortDir = savedSort.dir === "desc" ? "desc" : "asc";

  const savedCols = store.get("list.columns", DEFAULT_COLUMNS);
  let columns = new Set(Array.isArray(savedCols) ? savedCols : DEFAULT_COLUMNS);

  let viewMode = store.get("list.viewMode", "table") === "cards" ? "cards" : "table";
  let pageSize = Number(store.get("list.pageSize", 25)) || 25;
  let page = 1;
  let selectMode = false;
  const selected = new Set(); // device_ids

  let refs = null; // DOM refs while mounted
  let searchTimer = null;
  let lastPageRows = []; // devices on the current page (for check-all)

  /* ---------- pure helpers ---------- */

  const devices = () => (app.summary() && app.summary().devices) || [];
  const byId = (id) => devices().find((d) => d.device_id === id);
  const deviceName = (d) => d.alias || d.device_id;
  const shortId = (d) => String(d.device_id || "").slice(0, 8);
  const effExpiry = (d) => (d.expires_at_override != null ? d.expires_at_override : d.expires_at);

  function statusInfo(d) {
    if (d.orphaned) return { cls: "status-dot--warn", label: "Orphaned" };
    const t = Date.parse(d.last_seen || "");
    const online = !isNaN(t) && Date.now() - t < ONLINE_WINDOW_MS;
    return online
      ? { cls: "status-dot--online", label: "Online" }
      : { cls: "status-dot--offline", label: "Offline" };
  }
  function statusRank(d) {
    if (d.orphaned) return 2;
    if (d.stale) return 1;
    return statusInfo(d).label === "Online" ? 0 : 3;
  }

  const SORT_KEYS = {
    name: (d) => String(deviceName(d) || "").toLowerCase(),
    status: statusRank,
    user: (d) => String(d.username || "").toLowerCase(),
    ip: (d) => (d.ip ? String(d.ip).split(".").map((n) => parseInt(n, 10) || 0) : [Infinity]),
    app: (d) => {
      const segs = String(d.app_version || "").split(/[^0-9]+/).filter(Boolean).map(Number);
      return segs.length ? segs : [Infinity];
    },
    expires: (d) => Number(effExpiry(d)) || Infinity, // 0/never sorts last on asc
    last_seen: (d) => {
      const t = Date.parse(d.last_seen || "");
      return isNaN(t) ? Infinity : t; // null sorts last on asc
    },
  };
  function compareKeys(a, b) {
    if (Array.isArray(a) && Array.isArray(b)) {
      for (let i = 0; i < Math.max(a.length, b.length); i++) {
        const diff = (a[i] ?? -1) - (b[i] ?? -1);
        if (diff) return diff;
      }
      return 0;
    }
    return a < b ? -1 : a > b ? 1 : 0;
  }
  function sortDevices(list) {
    const keyFn = SORT_KEYS[sortKey] || SORT_KEYS.name;
    const dir = sortDir === "desc" ? -1 : 1;
    return [...list].sort((a, b) => dir * compareKeys(keyFn(a), keyFn(b)));
  }

  function applyFilters(list) {
    const q = search.trim().toLowerCase();
    return list.filter((d) => {
      if (q) {
        const hay = [d.alias, d.device_id, d.username, d.ip, d.app_version]
          .map((v) => String(v || "").toLowerCase())
          .join(" ");
        if (!hay.includes(q)) return false;
      }
      if (filters.status.length) {
        const label = statusInfo(d).label.toLowerCase();
        const hit = filters.status.some((s) =>
          s === "stale" ? !!d.stale : s === "orphaned" ? !!d.orphaned || label === "orphaned" : label === s
        );
        if (!hit) return false;
      }
      if (filters.user && d.username !== filters.user) return false;
      if (filters.kind === "managed" && d.native) return false;
      if (filters.kind === "native" && !d.native) return false;
      return true;
    });
  }
  const filterCount = () => filters.status.length + (filters.user ? 1 : 0) + (filters.kind ? 1 : 0);

  /* ---------- render: fragments ---------- */

  function nameHtml(d) {
    const name = d.alias
      ? `<strong>${ui.esc(d.alias)}</strong>`
      : `<span class="mono">${ui.esc(shortId(d))}…</span>`;
    const badges = [];
    if (d.orphaned) badges.push(`<span class="badge badge--orphan">orphan</span>`);
    if (d.stale) badges.push(`<span class="badge badge--stale">stale</span>`);
    if (d.pending_updates)
      badges.push(
        `<span class="badge badge--count" title="${Number(d.pending_updates)} pending update(s)">${Number(d.pending_updates)}</span>`
      );
    const icons = [];
    if (d.wireguard_connected)
      icons.push(`<ha-icon icon="mdi:shield-check" class="muted" style="--mdc-icon-size:14px;" title="WireGuard connected"></ha-icon>`);
    if (d.push_registered)
      icons.push(`<ha-icon icon="mdi:cellphone-link" class="muted" style="--mdc-icon-size:14px;" title="Push registered"></ha-icon>`);
    return (
      name +
      (badges.length ? " " + badges.join(" ") : "") +
      (icons.length ? ` <span class="row-icons">${icons.join("")}</span>` : "")
    );
  }

  function expiresHtml(d) {
    const eff = effExpiry(d);
    const n = Number(eff) || 0;
    const past = n && n * 1000 < Date.now();
    const pending = d.expires_at_override != null ? ` <span class="badge badge--pending">pending</span>` : "";
    return `<span${past ? ' style="color:var(--casa-error);"' : ""}>${ui.esc(ui.fmtExpiry(eff))}</span>${pending}`;
  }

  function userHtml(d) {
    return `${ui.esc(d.username || "—")}${d.native ? ' <span class="chip chip--neutral">native</span>' : ""}`;
  }
  function appHtml(d) {
    return d.app_version ? `<span class="chip chip--app">${ui.esc(d.app_version)}</span>` : "—";
  }
  function dotHtml(d) {
    const s = statusInfo(d);
    return `<span class="status-dot ${s.cls}" title="${ui.esc(ui.fmtTime(d.last_seen))}"></span>`;
  }

  function cellHtml(d, key) {
    switch (key) {
      case "status":
        return `<td>${dotHtml(d)}</td>`;
      case "name":
        return `<td>${nameHtml(d)}</td>`;
      case "user":
        return `<td>${userHtml(d)}</td>`;
      case "ip":
        return `<td class="mono">${ui.esc(d.ip || "—")}</td>`;
      case "app":
        return `<td>${appHtml(d)}</td>`;
      case "expires":
        return `<td>${expiresHtml(d)}</td>`;
      case "last_seen":
        return `<td>${ui.esc(ui.relTime(d.last_seen))}</td>`;
      default:
        return "<td></td>";
    }
  }

  function renderTable(rows) {
    const cols = COLUMNS.filter((c) => c.always || columns.has(c.key));
    const allChecked = rows.length > 0 && rows.every((r) => selected.has(r.device_id));
    const ths = cols
      .map((c) => {
        const arrow =
          sortKey === c.key
            ? ` <ha-icon icon="mdi:arrow-up" class="sort-arrow${sortDir === "desc" ? " desc" : ""}"></ha-icon>`
            : "";
        return `<th class="sortable" data-act="sort" data-key="${c.key}">${ui.esc(c.label)}${arrow}</th>`;
      })
      .join("");
    const trs = rows
      .map((d) => {
        const id = ui.esc(d.device_id);
        return `<tr data-id="${id}">
          ${selectMode ? `<td class="col-check"><input type="checkbox" data-act="check"${selected.has(d.device_id) ? " checked" : ""}></td>` : ""}
          ${cols.map((c) => cellHtml(d, c.key)).join("")}
          <td class="col-actions">
            <button class="btn btn--icon" data-act="edit" title="Edit device"><ha-icon icon="mdi:pencil"></ha-icon></button>
            <button class="btn btn--icon" data-act="kebab" title="More actions"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
          </td>
        </tr>`;
      })
      .join("");
    return `<div class="card" style="overflow-x:auto;">
      <table class="table">
        <thead><tr>
          ${selectMode ? `<th class="col-check"><input type="checkbox" data-act="check-all"${allChecked ? " checked" : ""}></th>` : ""}
          ${ths}<th></th>
        </tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>`;
  }

  function renderCards(rows) {
    const cards = rows
      .map((d) => {
        const id = ui.esc(d.device_id);
        return `<div class="card card--clickable" data-id="${id}" data-act="open">
          <div class="card__body">
            <div style="display:flex; align-items:center; gap:8px; min-width:0;">
              ${selectMode ? `<input type="checkbox" data-act="check"${selected.has(d.device_id) ? " checked" : ""}>` : ""}
              ${dotHtml(d)}
              <span style="min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${nameHtml(d)}</span>
              <span class="spacer"></span>
              <button class="btn btn--icon" data-act="kebab" title="More actions"><ha-icon icon="mdi:dots-vertical"></ha-icon></button>
            </div>
            <div style="margin-top:8px; display:flex; flex-direction:column; gap:5px; font-size:13px;">
              <span>${userHtml(d)}</span>
              <span class="mono muted">${ui.esc(d.ip || "—")}</span>
              <span>${appHtml(d)}</span>
              <span class="muted">Expires: ${expiresHtml(d)}</span>
            </div>
          </div>
        </div>`;
      })
      .join("");
    return `<div class="grid-cards">${cards}</div>`;
  }

  function renderMeta(filtered) {
    if (selectMode) {
      const dis = selected.size ? "" : " disabled";
      return `<div class="bulk-bar">
        <span><strong>${selected.size}</strong> selected</span>
        <button class="btn btn--outlined" data-act="bulk-reload"${dis}>Reload app</button>
        <button class="btn btn--outlined" data-act="bulk-delete"${dis}>Delete records</button>
        <button class="btn btn--danger-outlined" data-act="bulk-deprov"${dis}>Deprovision</button>
        <span class="spacer"></span>
        <button class="btn btn--text" data-act="bulk-cancel">Cancel</button>
      </div>`;
    }
    const n = filtered.length;
    const online = filtered.filter((d) => statusInfo(d).label === "Online").length;
    const stale = filtered.filter((d) => d.stale).length;
    const orphaned = filtered.filter((d) => d.orphaned).length;
    const selectBtn = n ? `<button class="btn btn--text" data-act="select-multiple">Select multiple</button>` : "";
    return `<div class="list-meta">
      <span>${n} device${n === 1 ? "" : "s"} · ${online} online · ${stale} stale · ${orphaned} orphaned</span>
      ${selectBtn}
    </div>`;
  }

  function renderFooter(total, pages) {
    if (total <= 10) return "";
    const opts = PAGE_SIZES.map((s) => `<option value="${s}"${s === pageSize ? " selected" : ""}>${s}</option>`).join("");
    return `<div class="list-footer">
      <span>${total} rows total</span>
      <span>Rows per page</span>
      <select class="select" data-act="page-size">${opts}</select>
      <span>Page ${page} of ${pages}</span>
      <button class="btn btn--icon page-btn" data-act="page-prev" title="Previous page"${page <= 1 ? " disabled" : ""}><ha-icon icon="mdi:chevron-left"></ha-icon></button>
      <button class="btn btn--icon page-btn" data-act="page-next" title="Next page"${page >= pages ? " disabled" : ""}><ha-icon icon="mdi:chevron-right"></ha-icon></button>
    </div>`;
  }

  function renderResults() {
    if (!refs) return;
    refs.err.innerHTML = api.lastError ? `<div class="errbar">${ui.esc(api.lastError)}</div>` : "";

    const summary = app.summary();
    const all = devices();
    const filtered = sortDevices(applyFilters(all));
    const total = filtered.length;
    const pages = Math.max(1, Math.ceil(total / pageSize));
    if (page > pages) page = pages;
    if (page < 1) page = 1;
    lastPageRows = filtered.slice((page - 1) * pageSize, (page - 1) * pageSize + pageSize);

    refs.meta.innerHTML = renderMeta(filtered);

    if (!summary) {
      refs.results.innerHTML = `<div class="empty-state"><span class="muted">Loading…</span></div>`;
    } else if (!all.length) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:cellphone-off"></ha-icon>
        <div>No devices provisioned yet</div>
        <button class="btn btn--primary" data-act="provision">+ Provision device</button>
      </div>`;
    } else if (!total) {
      refs.results.innerHTML = `<div class="empty-state">
        <ha-icon icon="mdi:magnify-close"></ha-icon>
        <div>No devices match</div>
        <button class="btn btn--text" data-act="clear-filters">Clear filters</button>
      </div>`;
    } else {
      refs.results.innerHTML = viewMode === "cards" ? renderCards(lastPageRows) : renderTable(lastPageRows);
    }

    refs.footer.innerHTML = renderFooter(total, pages);
  }

  function updateToolbar() {
    if (!refs) return;
    const n = filterCount();
    refs.filterBadge.hidden = n === 0;
    refs.filterBadge.textContent = String(n);
    for (const btn of refs.segmented.querySelectorAll("[data-mode]")) {
      btn.classList.toggle("segmented__btn--active", btn.dataset.mode === viewMode);
    }
    refs.columnsBtn.hidden = viewMode !== "table";
  }

  /* ---------- actions ---------- */

  const gotoEdit = (id) => app.navigate("/next/devices/" + encodeURIComponent(id));

  // TODO(wizard): launch the provision wizard module here once it lands.
  const openProvisionWizard = () => ui.toast("Provision wizard coming soon");

  function toggleSort(key) {
    if (sortKey === key) sortDir = sortDir === "asc" ? "desc" : "asc";
    else {
      sortKey = key;
      sortDir = "asc";
    }
    store.set("list.sort", { key: sortKey, dir: sortDir });
    renderResults();
  }

  function clearFilters() {
    filters = { status: [], user: "", kind: "" };
    search = "";
    if (refs) refs.search.value = "";
    page = 1;
    updateToolbar();
    renderResults();
  }

  function openDeviceMenu(anchor, d) {
    ui.openMenu({
      anchor,
      items: [
        { icon: "mdi:pencil", label: "Edit device", onSelect: () => gotoEdit(d.device_id) },
        {
          icon: "mdi:bell-ring-outline",
          label: "Test push",
          disabled: !d.push_registered,
          title: d.push_registered ? undefined : "Device has no push registration",
          onSelect: () => openTestPushModal(d),
        },
        { icon: "mdi:refresh", label: "Reload app", onSelect: () => confirmReload(d) },
        "divider",
        { icon: "mdi:delete-outline", label: "Delete record", danger: true, onSelect: () => confirmDeviceAction(d, "delete") },
        { icon: "mdi:cellphone-remove", label: "Deprovision", danger: true, onSelect: () => confirmDeviceAction(d, "deprovision") },
      ],
    });
  }

  function openTestPushModal(d) {
    const body = document.createElement("div");
    body.innerHTML = `
      <div class="field"><label>Title</label><input class="input" id="tp-title" value="Test Notification"></div>
      <div class="field"><label>Message</label><input class="input" id="tp-message" value="Hello from Home Assistant!"></div>
      <div class="field__error" id="tp-err" hidden></div>`;
    ui.openModal({
      title: `Test push — ${deviceName(d)}`,
      bodyEl: body,
      buttons: [
        { label: "Cancel", variant: "text" },
        {
          label: "Send",
          variant: "primary",
          onClick: async () => {
            const title = body.querySelector("#tp-title").value.trim();
            const message = body.querySelector("#tp-message").value.trim();
            const err = body.querySelector("#tp-err");
            if (!title || !message) {
              err.hidden = false;
              err.textContent = "Title and message are required.";
              return false;
            }
            try {
              await api.notifyUser({ deviceId: d.device_id, title, message });
              ui.toast("Push notification command sent.");
            } catch (e) {
              err.hidden = false;
              err.textContent = "Failed to send: " + ((e && e.message) || e);
              return false;
            }
          },
        },
      ],
    });
  }

  function confirmReload(d) {
    ui.showConfirm({
      title: "Reload app",
      message: `Send a silent reload push to "${deviceName(d)}"?`,
      confirmLabel: "Reload",
      confirmDanger: false,
      onConfirm: async () => {
        try {
          await api.reloadDevice(d.device_id);
          ui.toast("Reload push sent.");
        } catch (err) {
          ui.toast("Failed: " + ((err && err.message) || err), { error: true });
        }
      },
    });
  }

  // Confirmation wording ported verbatim from legacy _confirmDeviceAction.
  function confirmDeviceAction(d, kind) {
    const label = deviceName(d);
    if (kind === "delete") {
      ui.showConfirm({
        title: "Delete device record",
        message: `Delete the record for "${label}"? This revokes its Home Assistant session and push relay token and removes it from the server, but does NOT wipe the app — the device loses access when its session next fails. Use Deprovision to remotely wipe.`,
        confirmLabel: "Delete",
        onConfirm: () => runDeviceAction(d, "delete"),
      });
    } else {
      ui.showConfirm({
        title: "Deprovision device",
        message: `Deprovision "${label}"? This wipes the Casa app on the device, revokes its Home Assistant session, unregisters its push token, and deletes this record. If the device is offline it is wiped the next time it contacts the server. This cannot be undone.`,
        confirmLabel: "Deprovision",
        onConfirm: () => runDeviceAction(d, "deprovision"),
      });
    }
  }

  async function runDeviceAction(d, kind) {
    try {
      const res =
        kind === "delete" ? await api.deleteDevice(d.device_id) : await api.deprovisionDevice(d.device_id);
      selected.delete(d.device_id);
      await app.refresh();
      if (kind === "deprovision") {
        const response = api.constructor.response(res); // CasaApi.response
        if (response.push_sent === false) {
          ui.showInfo({
            title: "Deprovision device",
            message:
              "Device record removed and access revoked, but the wipe push could not be delivered (no push registration or relay unreachable). The device will wipe itself the next time it contacts the server.",
          });
        }
      }
    } catch (err) {
      ui.toast("Failed: " + ((err && err.message) || err), { error: true });
    }
  }

  function runBulk(kind) {
    const ids = [...selected];
    if (!ids.length) return;
    const n = ids.length;
    const plural = n === 1 ? "device" : "devices";
    const CONFIRMS = {
      reload: {
        title: "Reload app",
        message: `Send a silent reload push to ${n} ${plural}?`,
        confirmLabel: "Reload",
        confirmDanger: false,
      },
      delete: {
        title: "Delete device records",
        message: `Delete the records for ${n} ${plural}? This revokes each device's Home Assistant session and push relay token and removes it from the server, but does NOT wipe the app — the device loses access when its session next fails. Use Deprovision to remotely wipe.`,
        confirmLabel: "Delete",
        confirmDanger: true,
      },
      deprovision: {
        title: "Deprovision devices",
        message: `Deprovision ${n} ${plural}? This wipes the Casa app on each device, revokes its Home Assistant session, unregisters its push token, and deletes its record. If a device is offline it is wiped the next time it contacts the server. This cannot be undone.`,
        confirmLabel: "Deprovision",
        confirmDanger: true,
      },
    };
    const c = CONFIRMS[kind];
    ui.showConfirm({
      ...c,
      onConfirm: async () => {
        const failures = [];
        for (const id of ids) {
          try {
            if (kind === "reload") await api.reloadDevice(id);
            else if (kind === "delete") await api.deleteDevice(id);
            else await api.deprovisionDevice(id);
          } catch (err) {
            failures.push((err && err.message) || String(err));
          }
        }
        const ok = n - failures.length;
        const verb = kind === "reload" ? "reloaded" : kind === "delete" ? "deleted" : "deprovisioned";
        ui.toast(
          failures.length ? `${ok} done, ${failures.length} failed: ${failures[0]}` : `${ok} ${verb}`,
          { error: failures.length > 0 }
        );
        selected.clear();
        selectMode = false;
        app.refresh();
      },
    });
  }

  /* ---------- popovers ---------- */

  function openFiltersPopover(anchor) {
    const c = document.createElement("div");
    const usernames = [...new Set(devices().map((d) => d.username).filter(Boolean))].sort();
    const statusRow = (val, label) =>
      `<label class="toggle"><input type="checkbox" data-f-status="${val}"${filters.status.includes(val) ? " checked" : ""}> ${label}</label>`;
    c.innerHTML = `
      <div class="popover__section">Status</div>
      ${statusRow("online", "Online")}
      ${statusRow("offline", "Offline")}
      ${statusRow("stale", "Stale")}
      ${statusRow("orphaned", "Orphaned")}
      <div class="popover__section">User</div>
      <div style="padding:2px 14px 6px;">
        <select class="select" data-f-user>
          <option value="">Any user</option>
          ${usernames.map((u) => `<option value="${ui.esc(u)}"${filters.user === u ? " selected" : ""}>${ui.esc(u)}</option>`).join("")}
        </select>
      </div>
      <div class="popover__section">Type</div>
      <div style="padding:2px 14px 6px;">
        <select class="select" data-f-kind>
          <option value=""${filters.kind === "" ? " selected" : ""}>Any</option>
          <option value="managed"${filters.kind === "managed" ? " selected" : ""}>Managed</option>
          <option value="native"${filters.kind === "native" ? " selected" : ""}>Native</option>
        </select>
      </div>
      <div style="padding:6px 14px 4px; display:flex; justify-content:flex-end;">
        <button class="btn btn--text" data-f-clear>Clear all</button>
      </div>`;
    c.addEventListener("change", () => {
      filters.status = [...c.querySelectorAll("[data-f-status]")]
        .filter((cb) => cb.checked)
        .map((cb) => cb.dataset.fStatus);
      filters.user = c.querySelector("[data-f-user]").value;
      filters.kind = c.querySelector("[data-f-kind]").value;
      page = 1;
      updateToolbar();
      renderResults();
    });
    c.querySelector("[data-f-clear]").addEventListener("click", () => {
      for (const cb of c.querySelectorAll("[data-f-status]")) cb.checked = false;
      c.querySelector("[data-f-user]").value = "";
      c.querySelector("[data-f-kind]").value = "";
      filters = { status: [], user: "", kind: "" };
      page = 1;
      updateToolbar();
      renderResults();
    });
    ui.openPopover({ anchor, contentEl: c });
  }

  function openColumnsPopover(anchor) {
    const c = document.createElement("div");
    c.innerHTML =
      `<div class="popover__section">Columns</div>` +
      COLUMNS.filter((col) => !col.always)
        .map(
          (col) =>
            `<label class="toggle"><input type="checkbox" data-col="${col.key}"${columns.has(col.key) ? " checked" : ""}> ${ui.esc(col.label)}</label>`
        )
        .join("");
    c.addEventListener("change", (e) => {
      const cb = e.target.closest("[data-col]");
      if (!cb) return;
      if (cb.checked) columns.add(cb.dataset.col);
      else columns.delete(cb.dataset.col);
      store.set("list.columns", [...columns]);
      renderResults();
    });
    ui.openPopover({ anchor, contentEl: c });
  }

  /* ---------- delegated events ---------- */

  function onBodyClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el || el.disabled) return;
    const act = el.dataset.act;
    const idEl = el.closest("[data-id]");
    const id = el.dataset.id || (idEl && idEl.dataset.id) || "";
    const d = id ? byId(id) : null;
    switch (act) {
      case "sort":
        toggleSort(el.dataset.key);
        break;
      case "open":
      case "edit":
        if (id) gotoEdit(id);
        break;
      case "kebab":
        e.stopPropagation();
        if (d) openDeviceMenu(el, d);
        break;
      case "check":
        e.stopPropagation();
        if (id) {
          if (el.checked) selected.add(id);
          else selected.delete(id);
          renderResults();
        }
        break;
      case "check-all":
        for (const r of lastPageRows) {
          if (el.checked) selected.add(r.device_id);
          else selected.delete(r.device_id);
        }
        renderResults();
        break;
      case "select-multiple":
        selectMode = true;
        renderResults();
        break;
      case "bulk-cancel":
        selectMode = false;
        selected.clear();
        renderResults();
        break;
      case "bulk-reload":
        runBulk("reload");
        break;
      case "bulk-delete":
        runBulk("delete");
        break;
      case "bulk-deprov":
        runBulk("deprovision");
        break;
      case "page-prev":
        if (page > 1) {
          page--;
          renderResults();
        }
        break;
      case "page-next":
        page++;
        renderResults();
        break;
      case "provision":
        openProvisionWizard();
        break;
      case "clear-filters":
        clearFilters();
        break;
    }
  }

  function onBodyChange(e) {
    const el = e.target.closest("[data-act]");
    if (el && el.dataset.act === "page-size") {
      pageSize = Number(el.value) || 25;
      store.set("list.pageSize", pageSize);
      page = 1;
      renderResults();
    }
  }

  /* ---------- view ---------- */

  return {
    id: "devices",
    header: () => ({ title: "Casa Admin" }),
    polling: "live",

    mount(el) {
      el.innerHTML = `
        <div class="page">
          <div id="dv-err"></div>
          <div class="tabs">
            <button class="tab tab--active">Devices</button>
            <button class="tab" id="dv-tab-accounts">Accounts</button>
          </div>
          <div class="list-toolbar">
            <div class="search-field">
              <ha-icon icon="mdi:magnify"></ha-icon>
              <input class="input" id="dv-search" type="search" placeholder="Search devices…">
            </div>
            <div class="segmented" id="dv-segmented">
              <button class="segmented__btn" data-mode="cards" title="Card view"><ha-icon icon="mdi:view-grid"></ha-icon></button>
              <button class="segmented__btn" data-mode="table" title="Table view"><ha-icon icon="mdi:table"></ha-icon></button>
            </div>
            <button class="btn btn--outlined" id="dv-filters">
              <ha-icon icon="mdi:filter-variant"></ha-icon> Filters <span class="badge badge--count" id="dv-filter-count" hidden></span>
            </button>
            <span class="spacer"></span>
            <button class="btn btn--outlined" id="dv-columns">Columns</button>
            <button class="btn btn--primary" id="dv-provision">+ Provision device</button>
          </div>
          <div id="dv-body">
            <div id="dv-meta"></div>
            <div id="dv-results"></div>
            <div id="dv-footer"></div>
          </div>
        </div>`;

      refs = {
        err: el.querySelector("#dv-err"),
        search: el.querySelector("#dv-search"),
        segmented: el.querySelector("#dv-segmented"),
        filterBadge: el.querySelector("#dv-filter-count"),
        columnsBtn: el.querySelector("#dv-columns"),
        body: el.querySelector("#dv-body"),
        meta: el.querySelector("#dv-meta"),
        results: el.querySelector("#dv-results"),
        footer: el.querySelector("#dv-footer"),
      };

      refs.search.value = search;
      refs.search.addEventListener("input", () => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          search = refs ? refs.search.value : search;
          page = 1;
          renderResults();
        }, 150);
      });

      el.querySelector("#dv-tab-accounts").addEventListener("click", () => app.navigate("/next/accounts"));
      refs.segmented.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-mode]");
        if (!btn || btn.dataset.mode === viewMode) return;
        viewMode = btn.dataset.mode;
        store.set("list.viewMode", viewMode);
        updateToolbar();
        renderResults();
      });
      el.querySelector("#dv-filters").addEventListener("click", (e) => openFiltersPopover(e.currentTarget));
      refs.columnsBtn.addEventListener("click", (e) => openColumnsPopover(e.currentTarget));
      el.querySelector("#dv-provision").addEventListener("click", openProvisionWizard);

      // One delegated listener covers meta, results, and footer (all re-rendered).
      refs.body.addEventListener("click", onBodyClick);
      refs.body.addEventListener("change", onBodyChange);

      updateToolbar();
      renderResults();
    },

    unmount() {
      clearTimeout(searchTimer);
      searchTimer = null;
      refs = null;
      lastPageRows = [];
    },

    onSummary() {
      // Drop selections for devices that no longer exist.
      const ids = new Set(devices().map((d) => d.device_id));
      for (const id of [...selected]) if (!ids.has(id)) selected.delete(id);
      renderResults();
    },
  };
}
