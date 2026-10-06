// Casa admin panel — an account's unredeemed setup link, as listed in the
// Devices view ("awaiting setup" / "link expired" rows). Backed by
// /api/casa/admin/pending_provision: the QR is rendered on demand (the 30 s
// summary poll never carries links), Revoke scrambles the password now so the
// link dies, Regenerate makes a fresh single-use link for the same account
// from the request the original was made with, Dismiss drops an expired row.
// Opened from the row's QR button / kebab menu. Loaded lazily via
// app.loadModule.

const label = (p) => p.name || p.username || "this device";

export async function openPendingModal(app, p) {
  const { api, ui } = app;
  let res;
  try {
    res = await api.getPendingProvision(p.user_id);
  } catch (err) {
    if (err && err.status_code === 404) {
      ui.toast("That link is no longer open — it was used, revoked or has expired.", { error: true });
      app.refresh();
      return;
    }
    ui.toast("Couldn't load the link: " + ui.errMsg(err), { error: true });
    return;
  }
  const resultMod = await app.loadModule("views/provision-result.js");
  const body = document.createElement("div");
  body.innerHTML = `
    <p class="muted" style="margin:0 0 12px; font-size:13px;">
      For <strong>${ui.esc(label(res))}</strong> on <span class="mono">${ui.esc(res.username || "")}</span>.
      Single-use: it stops working as soon as the device signs in.
    </p>
    ${resultMod.setupResultHtml(res, { esc: ui.esc, fmtExpiry: ui.fmtExpiry })}`;
  resultMod.bindCopyButtons(body, ui);
  const modal = ui.openModal({
    title: `Set up ${label(res)}`,
    bodyEl: body,
    buttons: [
      { label: "Revoke link", variant: "danger", onClick: () => { confirmRevoke(app, p); } },
      { label: "New link", variant: "outlined", onClick: () => { confirmRegenerate(app, p); } },
      { label: "Close", variant: "primary" },
    ],
  });
  return modal;
}

export async function copyUniversalLink(app, p) {
  const { api, ui } = app;
  try {
    const res = await api.getPendingProvision(p.user_id);
    const text = res.universal_link || res.deep_link;
    if (!text) {
      ui.toast("This link has no copyable form.", { error: true });
      return;
    }
    const ok = await ui.copyText(text);
    ui.toast(ok ? "Setup link copied." : "Copy failed — open the link instead.", { error: !ok });
  } catch (err) {
    if (err && err.status_code === 404) {
      ui.toast("That link is no longer open.", { error: true });
      app.refresh();
      return;
    }
    ui.toast("Couldn't load the link: " + ui.errMsg(err), { error: true });
  }
}

export function confirmRevoke(app, p) {
  const { api, ui } = app;
  ui.showConfirm({
    title: "Revoke setup link",
    message: `Revoke the setup link for "${label(p)}"? The QR code and links stop working immediately. The account stays; you can make a new link any time.`,
    confirmLabel: "Revoke",
    onConfirm: async () => {
      try {
        await api.pendingProvisionAction(p.user_id, "revoke");
        ui.toast("Setup link revoked.");
      } catch (err) {
        ui.toast("Failed: " + apiErr(err, ui), { error: true });
      }
      app.refresh();
    },
  });
}

export function confirmDismiss(app, p) {
  const { api, ui } = app;
  api.pendingProvisionAction(p.user_id, "dismiss")
    .catch((err) => ui.toast("Failed: " + apiErr(err, ui), { error: true }))
    .then(() => app.refresh());
}

export function confirmRegenerate(app, p) {
  const { api, ui } = app;
  const expired = p.state === "expired";
  ui.showConfirm({
    title: expired ? "New setup link" : "Replace setup link",
    message: expired
      ? `Make a new setup link for "${label(p)}" with the same settings as before?`
      : `Make a new setup link for "${label(p)}"? The current QR code and links stop working; the new one uses the same settings.`,
    confirmLabel: expired ? "Make link" : "Replace",
    confirmDanger: false,
    onConfirm: async () => {
      let res;
      try {
        res = await api.pendingProvisionAction(p.user_id, "regenerate", { host_url: window.location.origin });
      } catch (err) {
        ui.toast("Failed: " + apiErr(err, ui), { error: true });
        return;
      }
      app.refresh();
      const resultMod = await app.loadModule("views/provision-result.js");
      const body = document.createElement("div");
      body.innerHTML = resultMod.setupResultHtml(res || {}, { esc: ui.esc, fmtExpiry: ui.fmtExpiry });
      resultMod.bindCopyButtons(body, ui);
      ui.openModal({
        title: `Set up ${label(p)}`,
        bodyEl: body,
        buttons: [{ label: "Done", variant: "primary" }],
      });
    },
  });
}

function apiErr(err, ui) {
  return (err && err.body && err.body.error) || ui.errMsg(err);
}
