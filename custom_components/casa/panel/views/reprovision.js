// Casa admin panel — one-click "Re-provision" for an existing device, always
// on its current account (CasaAdminReprovisionDeviceView). Push-registered
// devices get a fresh login over encrypted push (their old session is
// revoked once they sign back in); others — or "Show QR instead" for a
// wiped/replaced phone — get a new QR/link and are signed out immediately.
// Other devices on the account are never touched; passwords are never shown.
// Opened from the device list's row menu and the device page's Overview.

export async function openReprovisionModal(app, device) {
  const { api, ui } = app;
  const esc = ui.esc;
  const label = device.alias || device.device_id;
  const account = device.username || "this account";
  let forceQr = !device.push_registered;

  const body = document.createElement("div");
  const draw = () => {
    const text = forceQr
      ? `This signs <strong>${esc(label)}</strong> out now and gives you a new QR code / link to set it up again.`
      : `Sends a new login to <strong>${esc(label)}</strong> over push.`;
    body.innerHTML = `
      <p style="margin:0 0 8px; font-size:14px; line-height:1.5;">${text}</p>
      <p class="muted" style="margin:0 0 8px; font-size:13px;">Other devices on <span class="mono">${esc(account)}</span> stay signed in.</p>
      ${device.push_registered && !forceQr
        ? `<button class="btn btn--text" data-act="force-qr" style="padding-left:0;">Phone wiped or replaced? Show QR instead</button>`
        : ""}
      <div class="field__error" data-err hidden></div>`;
  };
  draw();
  body.addEventListener("click", (e) => {
    if (e.target.closest('[data-act="force-qr"]')) {
      forceQr = true;
      draw();
    }
  });

  ui.openModal({
    title: `Re-provision ${label}?`,
    bodyEl: body,
    buttons: [
      { label: "Cancel", variant: "text" },
      {
        label: "Re-provision",
        variant: "primary",
        onClick: async (btn) => {
          btn.disabled = true;
          btn.textContent = "Working…";
          try {
            const res = await api.reprovisionDevice({
              device_id: device.device_id,
              method: forceQr ? "qr" : "auto",
              host_url: window.location.origin,
            });
            await showResult(app, label, res || {});
            app.refresh();
            return undefined; // close the confirm
          } catch (err) {
            const errEl = body.querySelector("[data-err]");
            errEl.hidden = false;
            errEl.textContent = "Failed: " + ((err && err.body && err.body.error) || ui.errMsg(err));
            btn.disabled = false;
            btn.textContent = "Re-provision";
            return false;
          }
        },
      },
    ],
  });
}

async function showResult(app, label, res) {
  const { ui } = app;
  if (res.method === "push") {
    ui.showInfo({
      title: "Re-provision sent",
      message: `${label} will sign back in when it gets it (queued if it's offline). Track it under the device's Pending Updates.`,
    });
    return;
  }
  const resultMod = await app.loadModule("views/provision-result.js");
  const body = document.createElement("div");
  body.innerHTML = resultMod.setupResultHtml(res, { esc: ui.esc, fmtExpiry: ui.fmtExpiry });
  resultMod.bindCopyButtons(body, ui);
  ui.openModal({
    title: `Set up ${label} again`,
    bodyEl: body,
    buttons: [{ label: "Done", variant: "primary" }],
  });
}
