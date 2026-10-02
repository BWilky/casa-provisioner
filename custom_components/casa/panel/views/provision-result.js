// Casa admin panel — shared "setup is ready" block: QR image, universal +
// deep link with Copy buttons, and the validity chip. Used by the provision
// wizard's Done step (views/provision.js) and the re-provision result modal
// (views/reprovision.js). Loaded lazily via app.loadModule.

function linkRow(label, value, esc) {
  return `
    <div class="field">
      <label>${esc(label)}</label>
      <div class="field-row">
        <input class="input mono" readonly value="${esc(value)}">
        <button class="btn btn--outlined" data-copy="${esc(value)}" style="flex:none;">Copy</button>
      </div>
    </div>`;
}

export function setupResultHtml(r, { esc, fmtExpiry }) {
  const qr = r.qr_data_uri || r.url_path;
  return `
    ${qr ? `
      <div style="text-align:center; margin-bottom:16px;">
        <div class="muted" style="font-size:13px; margin-bottom:12px;">Scan with the Casa app, or send a setup link.</div>
        <img src="${esc(qr)}" alt="Provisioning QR code"
          style="width:220px; height:220px; border:1px solid var(--casa-divider); border-radius:var(--casa-radius-sm); padding:12px; background:#fff;">
      </div>` : ""}
    ${r.universal_link ? linkRow("Universal Link (opens from Safari / iMessage)", r.universal_link, esc) : ""}
    ${r.deep_link ? linkRow("Setup Deep Link", r.deep_link, esc) : ""}
    ${r.expires_at ? `<div style="margin-top:6px;"><span class="chip chip--warn">valid until ${esc(fmtExpiry(r.expires_at))}</span></div>` : ""}`;
}

export function bindCopyButtons(root, ui) {
  for (const btn of root.querySelectorAll("[data-copy]")) {
    ui.bindCopyButton(btn, () => btn.dataset.copy);
  }
}
