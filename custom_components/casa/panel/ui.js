// Casa admin panel — UI primitives shared by all views: popover/kebab menu,
// confirm/info dialogs, toast, and pure text helpers. Everything renders fresh
// DOM per invocation into layers owned by the app shell (no persistent hidden
// overlays, no listener-stacking clone tricks).

/* ---------- pure helpers ---------- */

export function esc(val) {
  return String(val ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

// Human-readable message from any rejection shape. HA's callApi/callWS reject
// with plain objects ({body, message, error, code}), not Error instances, so
// naive `err.message || err` interpolation renders "[object Object]" and hides
// the real HTTP status. This never does.
export function errMsg(err) {
  if (err == null) return "Unknown error";
  if (typeof err === "string") return err;
  const candidates = [err.body && err.body.message, err.body, err.message, err.error];
  for (const v of candidates) {
    if (typeof v === "string" && v) return v;
    if (v && typeof v === "object" && typeof v.message === "string" && v.message) return v.message;
  }
  if (err.status_code) return `HTTP ${err.status_code}`;
  try { return JSON.stringify(err); } catch (_e) { return String(err); }
}

export function fmtTime(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return d.toLocaleString([], {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  } catch (_e) {
    return String(iso);
  }
}

export function fmtExpiry(epochSecs) {
  const n = Number(epochSecs);
  if (!n) return "Never";
  try {
    return new Date(n * 1000).toLocaleString([], {
      year: "numeric", month: "short", day: "numeric",
      hour: "numeric", minute: "2-digit",
    });
  } catch (_e) {
    return String(epochSecs);
  }
}

export function relTime(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return String(iso);
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 30 * 86400) return `${Math.floor(s / 86400)} d ago`;
  return fmtTime(iso);
}

export async function copyText(str) {
  try {
    await navigator.clipboard.writeText(str);
    return true;
  } catch (_e) {
    try {
      const ta = document.createElement("textarea");
      ta.value = str;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand("copy");
      ta.remove();
      return ok;
    } catch (_e2) {
      return false;
    }
  }
}

/* ---------- UI factory ---------- */

// createUi({ popoverLayer, modalLayer }) — layers are elements inside the app
// shell's shadow root. Returns the `ui` object handed to views via the app context.
export function createUi({ popoverLayer, modalLayer, shadowRoot }) {
  let activePopover = null;

  function closePopover() {
    if (activePopover) {
      activePopover.cleanup();
      activePopover = null;
    }
  }

  function openPopover({ anchor, contentEl, align = "end" }) {
    closePopover();
    const el = document.createElement("div");
    el.className = "popover";
    el.appendChild(contentEl);
    popoverLayer.appendChild(el);

    const place = () => {
      const r = anchor.getBoundingClientRect();
      const pw = el.offsetWidth;
      const ph = el.offsetHeight;
      let top = r.bottom + 6;
      if (top + ph > window.innerHeight - 8) top = Math.max(8, r.top - ph - 6);
      let left = align === "end" ? r.right - pw : r.left;
      left = Math.min(Math.max(8, left), window.innerWidth - pw - 8);
      el.style.top = `${top}px`;
      el.style.left = `${left}px`;
    };
    place();

    const onPointerDown = (e) => {
      const path = e.composedPath();
      if (!path.includes(el) && !path.includes(anchor)) close();
    };
    const onKey = (e) => {
      if (e.key === "Escape") close();
    };
    const onScrollOrResize = () => close();

    // Capture-phase listeners on window catch pointer events both inside and
    // outside the shadow tree.
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", onScrollOrResize, true);
    // Scroll listener is capture so main-pane scrolling also closes it.
    window.addEventListener("scroll", onScrollOrResize, true);

    const cleanup = () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKey, true);
      window.removeEventListener("resize", onScrollOrResize, true);
      window.removeEventListener("scroll", onScrollOrResize, true);
      el.remove();
    };
    const close = () => {
      if (activePopover && activePopover.el === el) activePopover = null;
      cleanup();
    };

    activePopover = { el, cleanup };
    return { el, close };
  }

  // items: [{icon, label, danger, disabled, onSelect}, "divider", ...]
  function openMenu({ anchor, items }) {
    const content = document.createElement("div");
    let pop = null;
    for (const item of items) {
      if (item === "divider") {
        const d = document.createElement("div");
        d.className = "menu__divider";
        content.appendChild(d);
        continue;
      }
      const btn = document.createElement("button");
      btn.className = "menu__item" + (item.danger ? " menu__item--danger" : "");
      btn.disabled = !!item.disabled;
      btn.innerHTML = `${item.icon ? `<ha-icon icon="${esc(item.icon)}"></ha-icon>` : ""}<span>${esc(item.label)}</span>`;
      if (item.title) btn.title = item.title;
      btn.addEventListener("click", () => {
        pop.close();
        item.onSelect?.();
      });
      content.appendChild(btn);
    }
    pop = openPopover({ anchor, contentEl: content });
    return pop;
  }

  /* ---------- modals ---------- */

  // openModal({title, bodyEl|bodyHtml, buttons, wide, dismissable}) → {close, el, body}
  // buttons: [{label, variant: "primary"|"outlined"|"danger"|"text", onClick, id}]
  // onClick returning false keeps the modal open.
  function openModal({ title, bodyEl, bodyHtml, buttons = [], wide = false, dismissable = true }) {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const modal = document.createElement("div");
    modal.className = "modal" + (wide ? " modal--wide" : "");
    modal.innerHTML = `
      <div class="modal__header">
        <span>${esc(title)}</span>
        <span class="spacer"></span>
        ${dismissable ? '<button class="btn btn--icon" data-x><ha-icon icon="mdi:close"></ha-icon></button>' : ""}
      </div>
      <div class="modal__body"></div>
      ${buttons.length ? '<div class="modal__footer"></div>' : ""}
    `;
    const body = modal.querySelector(".modal__body");
    if (bodyEl) body.appendChild(bodyEl);
    else if (bodyHtml) body.innerHTML = bodyHtml;

    const close = () => {
      window.removeEventListener("keydown", onKey, true);
      overlay.remove();
    };
    const onKey = (e) => {
      if (e.key === "Escape" && dismissable) close();
    };
    window.addEventListener("keydown", onKey, true);

    if (buttons.length) {
      const footer = modal.querySelector(".modal__footer");
      for (const b of buttons) {
        const btn = document.createElement("button");
        const variant = b.variant || "outlined";
        btn.className = `btn btn--${variant}`;
        btn.textContent = b.label;
        if (b.id) btn.id = b.id;
        btn.addEventListener("click", async () => {
          const result = b.onClick ? await b.onClick(btn) : undefined;
          if (result !== false) close();
        });
        footer.appendChild(btn);
      }
    }
    if (dismissable) {
      modal.querySelector("[data-x]")?.addEventListener("click", close);
      overlay.addEventListener("click", (e) => {
        if (e.target === overlay) close();
      });
    }

    overlay.appendChild(modal);
    modalLayer.appendChild(overlay);
    return { close, el: overlay, body };
  }

  // Same call signature as the legacy _showConfirm so call sites port 1:1.
  function showConfirm({ title, message, confirmLabel, confirmDanger = true, onConfirm }) {
    openModal({
      title: title || "Confirm",
      bodyHtml: `<p style="margin:0; font-size:14px; line-height:1.5;">${esc(message || "Are you sure?")}</p>`,
      buttons: [
        { label: "Cancel", variant: "text" },
        {
          label: confirmLabel || "Confirm",
          variant: confirmDanger ? "danger" : "primary",
          onClick: () => { onConfirm?.(); },
        },
      ],
    });
  }

  function showInfo({ title, message, okLabel = "OK" }) {
    openModal({
      title: title || "Notice",
      bodyHtml: `<p style="margin:0; font-size:14px; line-height:1.5;">${esc(message)}</p>`,
      buttons: [{ label: okLabel, variant: "primary" }],
    });
  }

  /* ---------- toast ---------- */

  let toastEl = null;
  let toastTimer = null;
  function toast(message, { error = false, duration = 4000 } = {}) {
    if (toastEl) toastEl.remove();
    if (toastTimer) clearTimeout(toastTimer);
    toastEl = document.createElement("div");
    toastEl.className = "toast" + (error ? " toast--error" : "");
    toastEl.textContent = message;
    modalLayer.appendChild(toastEl);
    toastTimer = setTimeout(() => {
      toastEl?.remove();
      toastEl = null;
    }, duration);
  }

  // Wire a copy button: flips its label to "Copied!" briefly.
  function bindCopyButton(btn, getText) {
    btn.addEventListener("click", async () => {
      const ok = await copyText(typeof getText === "function" ? getText() : getText);
      const original = btn.textContent;
      btn.textContent = ok ? "Copied!" : "Copy failed";
      setTimeout(() => { btn.textContent = original; }, 1500);
    });
  }

  return {
    esc, errMsg, fmtTime, fmtExpiry, relTime, copyText,
    openPopover, openMenu, closePopover,
    openModal, showConfirm, showInfo,
    toast, bindCopyButton,
    _shadowRoot: shadowRoot,
  };
}
