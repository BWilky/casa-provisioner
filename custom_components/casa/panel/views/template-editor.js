// Casa admin panel — provision-template editor view. Three-pane editor
// (editor-shell.js) mounted at /templates/new and /templates/{templateId}:
// left navigator sections, center form bound to `state.form` (effective
// values, PROFILE-scope keys per profile-fields.js FIELD_SCOPES / const.py
// PROFILE_PROVISIONING_FIELDS), right live v2 payload preview
// (payload-preview.js).
//
// Templates are sparse: `state.setKeys` tracks which fields this template
// explicitly sets (touched = set; the per-field × button un-sets). Saves the
// nested body shape — { id?, name, fields: {only-set-keys} } — the server
// replaces the stored fields wholesale, which is what makes un-setting work.
//
// Templates hold reusable settings only. One-time provisioning process
// inputs (username/password/pin, deauth, timeout, scramble, Wi-Fi join) are
// entered in the provisioning wizard / casa.provision service call instead.
//
// The field schema/markup/coercion is shared with a single device's
// "Provisioning" section (device-editor.js) and the wizard via
// profile-fields.js — same fields rendered the same way, different scope and
// save path afterward.

export function createView(app) {
  const { api, ui, store } = app;
  const esc = ui.esc;

  /* ---------- lazily loaded siblings (never static imports) ---------- */
  let shellMod = null; // editor-shell.js
  let previewMod = null; // payload-preview.js
  let fieldsMod = null; // profile-fields.js (shared field schema/renderer)
  let templatesMod = null; // views/templates.js (apply-to-devices modal)

  /* ---------- per-mount state ---------- */
  let mountToken = 0;
  let loaded = false;
  let handle = null; // editor-shell handle
  let saveBtn = null;
  let discardBtn = null;
  let applyBtn = null;

  let templateId = null; // null → new template (POST); set → existing (PUT)
  let state = { name: "", form: {}, setKeys: new Set() }; // form populated from fieldsMod.DEFAULTS once loaded
  let pristineState = null; // copy for "Discard changes"
  let pristine = ""; // normalized snapshot for the dirty getter
  let wgProfiles = []; // [{id, alias, excluded_wifi}] for the link select + preview
  let activeSection = "connection";
  let previewTimer = null;

  /* ---------- dirty tracking ---------- */

  // Inputs hand back strings, so normalize every non-bool field to a string
  // before comparing — otherwise 5 vs "5" would read as dirty. Only set keys
  // participate (plus the set list itself), so un-setting a field is a change
  // even when its effective value stays the default.
  function snapshot(s) {
    const norm = {};
    for (const key of fieldsMod.PROFILE_KEYS) {
      if (!s.setKeys.has(key)) continue;
      norm[key] = typeof fieldsMod.DEFAULTS[key] === "boolean" ? !!s.form[key] : String(s.form[key] ?? "");
    }
    return JSON.stringify({ name: String(s.name ?? ""), set: [...s.setKeys].sort(), fields: norm });
  }
  const dirty = () => loaded && snapshot(state) !== pristine;
  const copyState = (s) => ({ name: s.name, form: JSON.parse(JSON.stringify(s.form)), setKeys: new Set(s.setKeys) });

  const titleText = () => `Editing ${state.name || "New template"}`;

  /* ---------- form section rendering ---------- */

  function renderSection(id) {
    activeSection = fieldsMod.sectionsFor(fieldsMod.PROFILE_KEYS).some((s) => s.id === id) ? id : "connection";
    if (!handle) return;
    // "Template name" is a template-editor-only concept (not part of the
    // shared fields schema device provisioning views use), so it's spliced in
    // ahead of the shared Connection section markup rather than living in
    // profile-fields.js.
    const nameFieldHtml = activeSection === "connection" ? `
      <div class="field" data-field="name">
        <label>Template name</label>
        <input class="input" data-key="name" value="${esc(state.name)}" placeholder="e.g. Guest tablet">
        <div class="field__help">Auto-generated if blank.</div>
      </div>` : "";
    handle.formEl.innerHTML = nameFieldHtml + fieldsMod.renderSectionHtml(activeSection, state.form, {
      readOnly: false, wgProfiles, esc, fields: fieldsMod.PROFILE_KEYS, setKeys: state.setKeys,
    });
    handle.formEl.scrollTop = 0;
  }

  function afterChange() {
    updateDirtyUi();
    schedulePreview();
  }

  /* ---------- live payload preview ---------- */

  const PREVIEW_NOTES_HTML = `
    <div class="muted" style="padding:12px 16px 2px; font-size:12px; line-height:1.5;">
      Parenthesized values are placeholders the server resolves at provision time.
    </div>
    <div class="muted" style="padding:6px 16px 2px; font-size:12px; line-height:1.5;">
      Dimmed "(default)" fields are not set by this template — the preview
      shows the system default the provision wizard will prefill for the
      admin to review.
    </div>
    <div class="muted" style="padding:6px 16px 14px; font-size:12px; line-height:1.5;">
      Username, password, PIN, Wi-Fi join, provisioning timeout, password
      scramble, and session sign-out are provisioning-process settings —
      entered in the wizard (or casa.provision service call) each time, not
      stored on this template.
    </div>`;

  function currentPreview() {
    return previewMod.buildV2PayloadPreview(state.form, {
      siteId: app.summary()?.site_id,
      wgProfiles,
    });
  }

  function renderPreview() {
    if (!handle || !previewMod) return;
    shellMod.renderJsonCode(handle.codeEl, currentPreview());
    handle.codeEl.insertAdjacentHTML("beforeend", PREVIEW_NOTES_HTML);
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      previewTimer = null;
      renderPreview();
    }, 150);
  }

  /* ---------- dirty chrome ---------- */

  function updateDirtyUi() {
    if (!handle) return;
    const d = dirty();
    handle.setTitle(titleText(), d ? '<span class="chip chip--warn">unsaved</span>' : "");
    if (saveBtn) saveBtn.disabled = !d;
    if (discardBtn) discardBtn.hidden = !d;
    if (applyBtn) applyBtn.disabled = d || !templateId || !setLiveKeys().length;
  }

  function setLiveKeys() {
    return [...state.setKeys].filter((k) => fieldsMod.LIVE_KEYS.has(k));
  }

  function discard() {
    // Mutate in place — bindFieldEvents captured `state.form`/`state.setKeys`
    // at mount, so swapping the objects out would orphan the bindings.
    state.name = pristineState.name;
    for (const key of Object.keys(state.form)) delete state.form[key];
    Object.assign(state.form, JSON.parse(JSON.stringify(pristineState.form)));
    state.setKeys.clear();
    for (const k of pristineState.setKeys) state.setKeys.add(k);
    renderSection(activeSection);
    updateDirtyUi();
    schedulePreview();
  }

  /* ---------- name field (template-specific; not part of the shared fields form) ---------- */

  function onNameInput(e) {
    const t = e.target;
    if (t.dataset.key !== "name") return;
    state.name = t.value;
    afterChange();
  }

  /* ---------- save ---------- */

  function autoName() {
    const hostUrl = state.setKeys.has("host_url") ? String(state.form.host_url ?? "").trim() : "";
    if (hostUrl) {
      let host = hostUrl;
      try {
        host = new URL(hostUrl).host || hostUrl;
      } catch (_e) {
        /* not a parsable URL — use as-is */
      }
      return `Template @ ${host}`;
    }
    return `Template (${new Date().toISOString().slice(0, 10)})`;
  }

  async function save() {
    if (!handle) return;
    // Nested sparse body: only explicitly-set fields are sent; the server
    // replaces the stored fields wholesale (that's what makes un-setting
    // possible) and drops values equal to the defaults.
    const payload = {
      name: String(state.name ?? "").trim() || autoName(),
      fields: fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS, { setKeys: state.setKeys }),
    };
    if (templateId) payload.id = templateId;

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      const res = await api.saveProvisionTemplate(payload); // PUT if id else POST
      state.name = payload.name;
      pristineState = copyState(state);
      pristine = snapshot(state);
      ui.toast("Template saved");
      if (!templateId && res && res.id) {
        // POST returns the created template; adopt its id and fix the URL. The
        // router remounts this view for the new path (state is clean now).
        templateId = res.id;
        app.navigate("/templates/" + encodeURIComponent(res.id), { replace: true });
        return;
      }
      app.setHeader({ title: titleText() });
      updateDirtyUi();
    } catch (err) {
      ui.toast("Save failed: " + ui.errMsg(err), { error: true });
    } finally {
      // The success path may have navigated away and unmounted us.
      if (saveBtn) {
        saveBtn.textContent = "Save";
        saveBtn.disabled = !dirty();
      }
    }
  }

  /* ---------- apply to devices ---------- */

  async function applyToDevices() {
    if (!templateId) return;
    try {
      templatesMod = templatesMod || (await app.loadModule("views/templates.js"));
    } catch (err) {
      ui.toast("Failed to load: " + ui.errMsg(err), { error: true });
      return;
    }
    templatesMod.openApplyTemplateModal(app, {
      id: templateId,
      name: state.name,
      fields: fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS, { setKeys: state.setKeys }),
    });
  }

  /* ---------- unload guard ---------- */

  function onBeforeUnload(e) {
    if (!dirty()) return;
    e.preventDefault();
    e.returnValue = "";
  }

  /* ---------- shell assembly ---------- */

  function buildShell(hostEl) {
    handle = shellMod.renderEditorShell(hostEl, {
      ui,
      store,
      storeKey: "editor.template",
      title: titleText(),
      titleChips: "",
      navHint: "Set only the fields this template should decide — unset fields are filled in by the admin at provision time. The preview shows the payload a device would receive.",
      sections: fieldsMod.sectionsFor(fieldsMod.PROFILE_KEYS),
      activeSection,
      onSelectSection: (id) => renderSection(id),
      codeTitle: "payload.json — live preview (v2)",
      onCopyCode: () => JSON.stringify(currentPreview(), null, 2),
      footerHtml: `
        <span class="muted" style="font-size:12px;">Saved templates appear in the provision flow.</span>
        <span class="spacer"></span>
        <button class="btn btn--text" data-act="apply" disabled>Apply to devices…</button>
        <button class="btn btn--text" data-act="discard" hidden>Discard changes</button>
        <button class="btn btn--primary" data-act="save" disabled>Save</button>`,
    });
    saveBtn = handle.footerEl.querySelector('[data-act="save"]');
    discardBtn = handle.footerEl.querySelector('[data-act="discard"]');
    applyBtn = handle.footerEl.querySelector('[data-act="apply"]');
    saveBtn.addEventListener("click", save);
    discardBtn.addEventListener("click", discard);
    applyBtn.addEventListener("click", applyToDevices);
    fieldsMod.bindFieldEvents(handle.formEl, {
      values: state.form,
      readOnly: false,
      onChange: afterChange,
      onSectionRerender: () => renderSection(activeSection),
      setKeys: state.setKeys,
      esc,
    });
    // The template "name" field lives in the shell chrome, not the shared
    // fields form, so it needs its own listener alongside the shared one.
    handle.formEl.addEventListener("input", onNameInput);
    renderSection(activeSection);
    renderPreview();
    updateDirtyUi();
  }

  /* ---------- view ---------- */

  return {
    id: "template-editor",
    header: (params) => ({
      title: loaded ? titleText() : params && params.templateId ? "Editing template…" : "Editing New template",
      back: "/templates",
    }),
    polling: "paused",

    async mount(el, params) {
      const token = ++mountToken;
      templateId = (params && params.templateId) || null;
      loaded = false;
      activeSection = "connection";

      const hostEl = document.createElement("div");
      hostEl.className = "page--flush";
      hostEl.innerHTML = '<div class="empty-state"><span class="muted">Loading template…</span></div>';
      el.appendChild(hostEl);

      try {
        const [shell, preview, fields, ppRes, wgRes] = await Promise.all([
          shellMod || app.loadModule("editor-shell.js"),
          previewMod || app.loadModule("payload-preview.js"),
          fieldsMod || app.loadModule("views/profile-fields.js"),
          templateId ? api.getProvisionTemplates() : null,
          api.getWireguardProfiles().catch(() => ({ profiles: [] })),
        ]);
        if (token !== mountToken) return;
        shellMod = shell;
        previewMod = preview;
        fieldsMod = fields;
        wgProfiles = (wgRes && wgRes.profiles) || [];

        // `form` holds effective values (defaults backfilled) so gating and
        // the preview always have something to render; `setKeys` records
        // which fields the template actually sets (stored keys are sparse).
        const seedForm = (src) => {
          const form = {};
          for (const key of fieldsMod.PROFILE_KEYS) {
            form[key] = src && src[key] !== undefined ? src[key] : fieldsMod.DEFAULTS[key];
          }
          return form;
        };
        if (templateId) {
          const template = ((ppRes && ppRes.profiles) || []).find((p) => p && p.id === templateId);
          if (!template) {
            ui.showInfo({ title: "Template not found", message: "That provision template no longer exists." });
            app.navigate("/templates", { replace: true });
            return;
          }
          const fieldsSrc = template.fields || {};
          state = {
            name: template.name || "",
            form: seedForm(fieldsSrc),
            setKeys: new Set(Object.keys(fieldsSrc).filter((k) => fieldsMod.PROFILE_KEYS.has(k))),
          };
        } else {
          state = { name: "", form: seedForm(null), setKeys: new Set() };
        }
      } catch (err) {
        if (token !== mountToken) return;
        hostEl.innerHTML = `<div class="page"><div class="errbar">Failed to load template: ${esc(ui.errMsg(err))}</div></div>`;
        return;
      }

      pristineState = copyState(state);
      pristine = snapshot(state);
      loaded = true;

      buildShell(hostEl);
      app.setHeader({ title: titleText() });
      window.addEventListener("beforeunload", onBeforeUnload);
    },

    unmount() {
      mountToken++;
      clearTimeout(previewTimer);
      previewTimer = null;
      window.removeEventListener("beforeunload", onBeforeUnload);
      handle = null;
      saveBtn = null;
      discardBtn = null;
      applyBtn = null;
      loaded = false;
    },

    // ui.showConfirm has no cancel callback, so build the dialog with openModal
    // and resolve false on any dismissal (X, overlay click, Escape) by watching
    // for the overlay leaving the DOM.
    confirmLeave() {
      if (!dirty()) return true;
      return new Promise((resolve) => {
        let settled = false;
        let observer = null;
        const done = (v) => {
          if (settled) return;
          settled = true;
          observer?.disconnect();
          resolve(v);
        };
        const modal = ui.openModal({
          title: "Discard changes?",
          bodyHtml:
            '<p style="margin:0; font-size:14px; line-height:1.5;">This template has unsaved changes. Discard them and leave?</p>',
          buttons: [
            { label: "Keep editing", variant: "text", onClick: () => done(false) },
            { label: "Discard", variant: "danger", onClick: () => done(true) },
          ],
        });
        observer = new MutationObserver(() => {
          if (!modal.el.isConnected) done(false);
        });
        observer.observe(modal.el.parentNode, { childList: true });
      });
    },

    onSummary() {
      // site_id may arrive after mount; refresh the preview's placeholder.
      if (loaded && handle) schedulePreview();
    },
  };
}
