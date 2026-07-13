// Casa admin panel — provisioning-profile editor view. Three-pane editor
// (editor-shell.js) mounted at /profiles/new and /profiles/{profileId}:
// left navigator sections, center form bound to `state.form` (the profile's
// `fields` object, PROFILE-scope keys per profile-fields.js FIELD_SCOPES /
// const.py PROFILE_PROVISIONING_FIELDS), right live v2 payload preview
// (payload-preview.js). Saves the legacy flat body shape — { id?, name,
// ...fields } — because the server view reads every field key from the top
// level of the JSON body.
//
// Profiles hold reusable template settings only. One-time provisioning
// process inputs (username/password/pin, deauth, timeout, scramble, Wi-Fi
// join) are entered in the provisioning wizard / casa.provision service call
// instead. Legacy profiles may still carry those keys server-side; this
// editor neither shows nor resubmits them.
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

  /* ---------- per-mount state ---------- */
  let mountToken = 0;
  let loaded = false;
  let handle = null; // editor-shell handle
  let saveBtn = null;
  let discardBtn = null;

  let profileId = null; // null → new profile (POST); set → existing (PUT)
  let state = { name: "", form: {} }; // form populated from fieldsMod.DEFAULTS once loaded
  let pristineState = null; // deep copy for "Discard changes"
  let pristine = ""; // normalized snapshot for the dirty getter
  let wgProfiles = []; // [{id, alias, excluded_wifi}] for the link select + preview
  let activeSection = "connection";
  let previewTimer = null;

  /* ---------- dirty tracking ---------- */

  // Inputs hand back strings, so normalize every non-bool field to a string
  // before comparing — otherwise 5 vs "5" would read as dirty. Only
  // profile-scope keys participate, so legacy process keys stored on old
  // profiles can't trip dirty-tracking.
  function snapshot(s) {
    const norm = {};
    for (const key of fieldsMod.PROFILE_KEYS) {
      norm[key] = typeof fieldsMod.DEFAULTS[key] === "boolean" ? !!s.form[key] : String(s.form[key] ?? "");
    }
    return JSON.stringify({ name: String(s.name ?? ""), fields: norm });
  }
  const dirty = () => loaded && snapshot(state) !== pristine;
  const deepCopy = (obj) => JSON.parse(JSON.stringify(obj));

  const titleText = () => `Editing ${state.name || "New profile"}`;

  /* ---------- form section rendering ---------- */

  function renderSection(id) {
    activeSection = fieldsMod.sectionsFor(fieldsMod.PROFILE_KEYS).some((s) => s.id === id) ? id : "connection";
    if (!handle) return;
    // "Profile name" is a profile-editor-only concept (not part of the shared
    // fields schema device provisioning views use), so it's spliced in ahead
    // of the shared Connection section markup rather than living in
    // profile-fields.js.
    const nameFieldHtml = activeSection === "connection" ? `
      <div class="field" data-field="name">
        <label>Profile name</label>
        <input class="input" data-key="name" value="${esc(state.name)}" placeholder="e.g. Guest tablet">
        <div class="field__help">Auto-generated if blank.</div>
      </div>` : "";
    handle.formEl.innerHTML = nameFieldHtml + fieldsMod.renderSectionHtml(activeSection, state.form, { readOnly: false, wgProfiles, esc, fields: fieldsMod.PROFILE_KEYS });
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
    <div class="muted" style="padding:6px 16px 14px; font-size:12px; line-height:1.5;">
      Username, password, PIN, Wi-Fi join, provisioning timeout, password
      scramble, and session sign-out are provisioning-process settings —
      entered in the wizard (or casa.provision service call) each time, not
      stored in this profile.
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
  }

  function discard() {
    state = deepCopy(pristineState);
    renderSection(activeSection);
    updateDirtyUi();
    schedulePreview();
  }

  /* ---------- name field (profile-specific; not part of the shared fields form) ---------- */

  function onNameInput(e) {
    const t = e.target;
    if (t.dataset.key !== "name") return;
    state.name = t.value;
    afterChange();
  }

  /* ---------- save ---------- */

  function autoName(hostUrl) {
    let host = hostUrl;
    try {
      host = new URL(hostUrl).host || hostUrl;
    } catch (_e) {
      /* not a parsable URL — use as-is */
    }
    return `Profile @ ${host}`;
  }

  function markRequiredError(key) {
    const wrap = handle.formEl.querySelector(`[data-field="${key}"]`);
    if (!wrap || wrap.classList.contains("field--error")) return;
    wrap.classList.add("field--error");
    wrap.insertAdjacentHTML("beforeend", '<div class="field__error">Required.</div>');
  }

  async function save() {
    if (!handle) return;
    const hostUrl = String(state.form.host_url ?? "").trim();
    if (!hostUrl) {
      handle.setActive("connection");
      renderSection("connection");
      markRequiredError("host_url");
      ui.toast("Host URL is required.", { error: true });
      return;
    }

    // Legacy/server body shape: flat — name (+ id for updates) alongside every
    // field key at the top level; ints coerced like the legacy editor. Only
    // profile-scope keys are sent (the server filters too).
    const payload = { name: String(state.name ?? "").trim() || autoName(hostUrl), ...fieldsMod.collectFields(state.form, fieldsMod.PROFILE_KEYS) };
    payload.host_url = hostUrl;
    if (profileId) payload.id = profileId;

    saveBtn.disabled = true;
    saveBtn.textContent = "Saving…";
    try {
      const res = await api.saveProvisionProfile(payload); // PUT if id else POST
      state.name = payload.name;
      pristineState = deepCopy(state);
      pristine = snapshot(state);
      ui.toast("Profile saved");
      if (!profileId && res && res.id) {
        // POST returns the created profile; adopt its id and fix the URL. The
        // router remounts this view for the new path (state is clean now).
        profileId = res.id;
        app.navigate("/profiles/" + encodeURIComponent(res.id), { replace: true });
        return;
      }
      app.setHeader({ title: titleText() });
      updateDirtyUi();
    } catch (err) {
      ui.toast("Save failed: " + ((err && err.message) || err), { error: true });
    } finally {
      // The success path may have navigated away and unmounted us.
      if (saveBtn) {
        saveBtn.textContent = "Save";
        saveBtn.disabled = !dirty();
      }
    }
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
      storeKey: "editor.profile",
      title: titleText(),
      titleChips: "",
      navHint: "Configure the provisioning profile. The preview shows the payload a device will receive.",
      sections: fieldsMod.sectionsFor(fieldsMod.PROFILE_KEYS),
      activeSection,
      onSelectSection: (id) => renderSection(id),
      codeTitle: "payload.json — live preview (v2)",
      onCopyCode: () => JSON.stringify(currentPreview(), null, 2),
      footerHtml: `
        <span class="muted" style="font-size:12px;">Saved profiles appear in the provision device flow.</span>
        <span class="spacer"></span>
        <button class="btn btn--text" data-act="discard" hidden>Discard changes</button>
        <button class="btn btn--primary" data-act="save" disabled>Save</button>`,
    });
    saveBtn = handle.footerEl.querySelector('[data-act="save"]');
    discardBtn = handle.footerEl.querySelector('[data-act="discard"]');
    saveBtn.addEventListener("click", save);
    discardBtn.addEventListener("click", discard);
    fieldsMod.bindFieldEvents(handle.formEl, {
      values: state.form,
      readOnly: false,
      onChange: afterChange,
      onSectionRerender: () => renderSection(activeSection),
    });
    // The profile "name" field lives in the shell chrome, not the shared
    // fields form, so it needs its own listener alongside the shared one.
    handle.formEl.addEventListener("input", onNameInput);
    renderSection(activeSection);
    renderPreview();
  }

  /* ---------- view ---------- */

  return {
    id: "profile-editor",
    header: (params) => ({
      title: loaded ? titleText() : params && params.profileId ? "Editing profile…" : "Editing New profile",
      back: "/profiles",
    }),
    polling: "paused",

    async mount(el, params) {
      const token = ++mountToken;
      profileId = (params && params.profileId) || null;
      loaded = false;
      activeSection = "connection";

      const hostEl = document.createElement("div");
      hostEl.className = "page--flush";
      hostEl.innerHTML = '<div class="empty-state"><span class="muted">Loading profile…</span></div>';
      el.appendChild(hostEl);

      try {
        const [shell, preview, fields, ppRes, wgRes] = await Promise.all([
          shellMod || app.loadModule("editor-shell.js"),
          previewMod || app.loadModule("payload-preview.js"),
          fieldsMod || app.loadModule("views/profile-fields.js"),
          profileId ? api.getProvisionProfiles() : null,
          api.getWireguardProfiles().catch(() => ({ profiles: [] })),
        ]);
        if (token !== mountToken) return;
        shellMod = shell;
        previewMod = preview;
        fieldsMod = fields;
        wgProfiles = (wgRes && wgRes.profiles) || [];

        // Seed only profile-scope keys — legacy process keys stored on old
        // profiles are neither rendered nor resubmitted.
        const seedForm = (src) => {
          const form = {};
          for (const key of fieldsMod.PROFILE_KEYS) {
            form[key] = src && src[key] !== undefined ? src[key] : fieldsMod.DEFAULTS[key];
          }
          return form;
        };
        if (profileId) {
          const profile = ((ppRes && ppRes.profiles) || []).find((p) => p && p.id === profileId);
          if (!profile) {
            ui.showInfo({ title: "Profile not found", message: "That provisioning profile no longer exists." });
            app.navigate("/profiles", { replace: true });
            return;
          }
          state = { name: profile.name || "", form: seedForm(profile.fields) };
        } else {
          state = { name: "", form: seedForm(null) };
        }
      } catch (err) {
        if (token !== mountToken) return;
        hostEl.innerHTML = `<div class="page"><div class="errbar">Failed to load profile: ${esc(String((err && err.message) || err))}</div></div>`;
        return;
      }

      pristineState = deepCopy(state);
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
            '<p style="margin:0; font-size:14px; line-height:1.5;">This profile has unsaved changes. Discard them and leave?</p>',
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
