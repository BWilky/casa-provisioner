// Casa admin panel — shared three-pane editor shell (ESPHome Device Builder
// style): top bar with title/chips/pane toggles, left navigator accordion,
// center form pane, right read-only JSON code pane, optional footer. Consumed
// by the device and profile editor views via app.loadModule("editor-shell.js").
// No sibling imports — the `ui` object and `store` are passed in by the view.

/* ---------- editor shell ---------- */

// renderEditorShell(hostEl, opts) → handle
//   hostEl: a `.page--flush` container owned by the view.
//   opts: { ui, store, storeKey, title, titleChips, navHint, sections,
//           activeSection, onSelectSection, codeTitle, codeCaption,
//           onCopyCode, footerHtml }
//   sections: [{ id, label, icon, badge, badgeClass, danger, group }]
// handle: { formEl, codeEl, footerEl, setActive, setBadge, setTitle,
//           setCodeCaption, setSections }
export function renderEditorShell(hostEl, opts) {
  const {
    ui,
    store,
    storeKey,
    title = "",
    titleChips = "",
    navHint = "",
    sections = [],
    activeSection = null,
    onSelectSection,
    codeTitle = "",
    codeCaption = "",
    onCopyCode,
    footerHtml = null,
  } = opts;
  const esc = ui.esc;

  let currentSections = sections.slice();
  let activeId = activeSection;

  // hostEl becomes the flex column so bar/editor/footer stack and the editor
  // row absorbs the remaining height (its stylesheet height:100% would
  // otherwise overflow past the bar).
  hostEl.style.display = "flex";
  hostEl.style.flexDirection = "column";
  hostEl.style.height = "100%";
  hostEl.style.overflow = "hidden";

  hostEl.innerHTML = `
    <div class="editor__bar">
      <span class="title" data-ref="title">${esc(title)}</span>
      <span data-ref="chips" style="display:inline-flex; gap:6px; align-items:center;">${titleChips || ""}</span>
      <span class="spacer"></span>
      <button class="btn btn--icon" data-ref="toggle-nav" title="Toggle navigator"><ha-icon icon="mdi:dock-left"></ha-icon></button>
      <button class="btn btn--icon" data-ref="toggle-code" title="Toggle code pane"><ha-icon icon="mdi:dock-right"></ha-icon></button>
    </div>
    <div class="editor" style="flex:1; min-height:0; height:auto;">
      <nav class="editor__nav" data-ref="nav"></nav>
      <div class="editor__form" data-ref="form"></div>
      <aside class="editor__code" data-ref="code-pane">
        <div class="editor__code-header">
          <span data-ref="code-title">${esc(codeTitle)}</span>
          <span class="spacer"></span>
          <span data-ref="code-caption">${esc(codeCaption)}</span>
          <button class="btn btn--icon" data-ref="copy" title="Copy" style="width:28px; height:28px;"><ha-icon icon="mdi:content-copy"></ha-icon></button>
        </div>
        <div class="editor__code-body" data-ref="code-body"></div>
      </aside>
    </div>
    ${footerHtml != null ? `<div class="editor__footer" data-ref="footer">${footerHtml}</div>` : ""}
  `;

  const ref = (name) => hostEl.querySelector(`[data-ref="${name}"]`);
  const titleEl = ref("title");
  const chipsEl = ref("chips");
  const navEl = ref("nav");
  const formEl = ref("form");
  const codePaneEl = ref("code-pane");
  const codeTitleEl = ref("code-title");
  const codeCaptionEl = ref("code-caption");
  const copyBtn = ref("copy");
  const toggleNavBtn = ref("toggle-nav");
  const toggleCodeBtn = ref("toggle-code");
  const footerEl = ref("footer"); // null when footerHtml == null

  /* ----- navigator ----- */

  function badgeHtml(s) {
    if (s.badge == null || s.badge === "") return "";
    return `<span class="badge ${esc(s.badgeClass || "badge--count")}">${esc(s.badge)}</span>`;
  }

  function renderNav() {
    let html = navHint ? `<div class="editor__nav-hint">${esc(navHint)}</div>` : "";
    let lastGroup = null;
    for (const s of currentSections) {
      if (s.group && s.group !== lastGroup) {
        html += `<div class="editor__nav-group">${esc(s.group)}</div>`;
      }
      if (s.group) lastGroup = s.group;
      const cls = ["nav-item"];
      if (s.id === activeId) cls.push("nav-item--active");
      if (s.danger) cls.push("nav-item--danger");
      html += `
        <button class="${cls.join(" ")}" data-section="${esc(s.id)}">
          ${s.icon ? `<ha-icon icon="${esc(s.icon)}"></ha-icon>` : ""}
          <span>${esc(s.label)}</span>
          ${badgeHtml(s)}
        </button>`;
    }
    navEl.innerHTML = html;
  }
  renderNav();

  // Delegated so renderNav() can rebuild items without re-wiring listeners.
  navEl.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-section]");
    if (!btn || !navEl.contains(btn)) return;
    const id = btn.dataset.section;
    setActive(id);
    onSelectSection?.(id);
  });

  function setActive(id) {
    activeId = id;
    for (const btn of navEl.querySelectorAll(".nav-item")) {
      btn.classList.toggle("nav-item--active", btn.dataset.section === id);
    }
  }

  /* ----- pane collapse (persisted) ----- */

  const navKey = `${storeKey}.nav`;
  const codeKey = `${storeKey}.code`;
  let navHidden = !!store.get(navKey, false);
  let codeHidden = !!store.get(codeKey, false);

  function setPressed(btn, pressed) {
    // Inline "pressed" affordance mirroring .segmented__btn--active.
    btn.style.background = pressed ? "color-mix(in srgb, var(--casa-primary) 12%, transparent)" : "";
    btn.style.color = pressed ? "var(--casa-primary)" : "";
  }
  function applyPanes() {
    navEl.style.display = navHidden ? "none" : "";
    codePaneEl.style.display = codeHidden ? "none" : "";
    setPressed(toggleNavBtn, navHidden);
    setPressed(toggleCodeBtn, codeHidden);
  }
  applyPanes();

  toggleNavBtn.addEventListener("click", () => {
    navHidden = !navHidden;
    store.set(navKey, navHidden);
    applyPanes();
  });
  toggleCodeBtn.addEventListener("click", () => {
    codeHidden = !codeHidden;
    store.set(codeKey, codeHidden);
    applyPanes();
  });

  /* ----- code header copy ----- */

  copyBtn.addEventListener("click", async () => {
    const text = onCopyCode ? String(onCopyCode() ?? "") : "";
    const ok = await ui.copyText(text);
    // Icon flip instead of ui.bindCopyButton (which swaps textContent and
    // would wipe the <ha-icon> child of an icon-only button).
    copyBtn.innerHTML = `<ha-icon icon="${ok ? "mdi:check" : "mdi:alert-circle-outline"}"></ha-icon>`;
    copyBtn.title = ok ? "Copied!" : "Copy failed";
    setTimeout(() => {
      copyBtn.innerHTML = '<ha-icon icon="mdi:content-copy"></ha-icon>';
      copyBtn.title = "Copy";
    }, 1500);
  });

  /* ----- handle ----- */

  return {
    formEl,
    codeEl: ref("code-body"),
    footerEl,
    setActive,
    setBadge(id, badge, badgeClass) {
      const s = currentSections.find((x) => x.id === id);
      if (!s) return;
      s.badge = badge;
      if (badgeClass !== undefined) s.badgeClass = badgeClass;
      const btn = navEl.querySelector(`[data-section="${cssEscape(id)}"]`);
      if (!btn) return;
      btn.querySelector(".badge")?.remove();
      if (badgeHtml(s)) btn.insertAdjacentHTML("beforeend", badgeHtml(s));
    },
    setTitle(newTitle, chipsHtml) {
      titleEl.textContent = newTitle ?? "";
      if (chipsHtml !== undefined) chipsEl.innerHTML = chipsHtml || "";
    },
    setCodeCaption(text) {
      codeCaptionEl.textContent = text ?? "";
    },
    setSections(newSections) {
      currentSections = (newSections || []).slice();
      renderNav();
    },
  };

  function cssEscape(v) {
    return typeof CSS !== "undefined" && CSS.escape ? CSS.escape(String(v)) : String(v).replace(/["\\]/g, "\\$&");
  }
}

/* ---------- read-only JSON code pane ---------- */

// renderJsonCode(containerEl, obj, { placeholderPattern }) — pretty-prints
// `obj` into `.code-line` rows with a line-number gutter and light, line-based
// syntax coloring. String values matching placeholderPattern (default:
// parenthesized strings like "(auto-generated at provision)") render as
// `.code-placeholder` instead of `.code-str`.
export function renderJsonCode(containerEl, obj, { placeholderPattern = /^\(.*\)$/ } = {}) {
  const text = JSON.stringify(obj, null, 2) ?? "";
  const html = text
    .split("\n")
    .map(
      (line, i) => `
        <div class="code-line">
          <span class="code-line__num">${i + 1}</span>
          <span class="code-line__txt">${colorizeLine(line, placeholderPattern)}</span>
        </div>`
    )
    .join("");
  containerEl.innerHTML = html;
}

// JSON.stringify output is regular enough for line-based regexes: an optional
// `"key": ` prefix, then a single value / opening bracket / closer.
const KEY_LINE_RE = /^(\s*)("(?:[^"\\]|\\.)*")(:)(\s?)(.*)$/;
const STR_VALUE_RE = /^("(?:[^"\\]|\\.)*")(,?)$/;
const NUM_VALUE_RE = /^(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null)(,?)$/;

function colorizeLine(line, placeholderPattern) {
  const key = line.match(KEY_LINE_RE);
  if (key) {
    return (
      key[1] +
      `<span class="code-key">${escapeHtml(key[2])}</span>` +
      escapeHtml(key[3] + key[4]) +
      colorizeValue(key[5], placeholderPattern)
    );
  }
  const [, indent, rest] = line.match(/^(\s*)(.*)$/);
  return indent + colorizeValue(rest, placeholderPattern);
}

function colorizeValue(value, placeholderPattern) {
  if (!value) return "";
  const str = value.match(STR_VALUE_RE);
  if (str) {
    const inner = str[1].slice(1, -1);
    const cls = placeholderPattern && placeholderPattern.test(inner) ? "code-placeholder" : "code-str";
    return `<span class="${cls}">${escapeHtml(str[1])}</span>${str[2]}`;
  }
  const num = value.match(NUM_VALUE_RE);
  if (num) return `<span class="code-num">${num[1]}</span>${num[2]}`;
  return escapeHtml(value); // structural: { } [ ] and closers
}

// Local escaper: renderJsonCode has no `ui` parameter and sibling modules are
// never statically imported.
function escapeHtml(val) {
  return String(val ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
