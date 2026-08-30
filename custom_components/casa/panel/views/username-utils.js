// Casa admin panel — shared username helpers: name→username slugification and
// the live (debounced, advisory) availability check against
// /api/casa/admin/check_username. Extracted from provision-guided.js so the
// guided wizard, the Reauthenticate modal, and the Create Account modal all
// derive usernames and render availability the same way. Loaded lazily via
// app.loadModule("views/username-utils.js") — never static-imported.

export const USERNAME_RE = /^[a-z0-9][a-z0-9-]*$/;

export const slugify = (s) =>
  String(s)
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9\s_-]/g, "")
    .trim()
    .replace(/[\s_]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");

const escapeHtml = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]
  );

// Availability hint markup shared with provision-guided's availabilityHtml().
// a: null | {checking:true} | {available, username_conflict, for}
export function availabilityHintHtml(a, username, esc = escapeHtml) {
  if (!username || !a) return "";
  if (a.checking) return `<span class="muted" style="font-size:12px;">Checking availability…</span>`;
  if (a.for !== username) return "";
  if (a.available) {
    return `<span class="chip chip--ok"><ha-icon icon="mdi:check-circle" style="--mdc-icon-size:14px;"></ha-icon> Available</span>`;
  }
  const what = a.username_conflict ? "Username already in use" : "A user with this name already exists";
  return `<span class="chip chip--error"><ha-icon icon="mdi:alert-circle" style="--mdc-icon-size:14px;"></ha-icon> ${esc(what)}</span>`;
}

// Wire a plain-DOM name/username input pair (modal forms):
// - username is force-lowercased as typed;
// - username auto-slugifies live from the name until the admin types in the
//   username field; clearing the username re-couples it to the name;
// - a 350ms-debounced availability check renders into hintEl. Advisory only —
//   failures render nothing and the submit path revalidates authoritatively.
// Returns { lastResult, destroy }. lastResult() is the freshest availability
// response ({available, username_conflict, ..., for}) or null.
export function attachUsernameField({ nameInput, usernameInput, hintEl, checkUsername }) {
  let usernameEdited = false;
  let availability = null;
  let timer = 0;
  let seq = 0; // bumped per (re)schedule and on destroy — stale guard

  const trim = (v) => String(v ?? "").trim();

  function render() {
    if (hintEl) hintEl.innerHTML = availabilityHintHtml(availability, trim(usernameInput.value));
  }

  function schedule() {
    clearTimeout(timer);
    const token = ++seq;
    const username = trim(usernameInput.value);
    if (!username || !USERNAME_RE.test(username)) {
      availability = null;
      render();
      return;
    }
    availability = { checking: true };
    render();
    const name = trim(nameInput ? nameInput.value : "");
    timer = setTimeout(async () => {
      try {
        const res = await checkUsername(username, name);
        if (token !== seq || trim(usernameInput.value) !== username) return;
        availability = { ...res, for: username };
      } catch {
        if (token !== seq) return;
        availability = null; // advisory only — submit revalidates
      }
      render();
    }, 350);
  }

  function onNameInput() {
    if (!usernameEdited) {
      usernameInput.value = slugify(nameInput.value);
    }
    schedule();
  }

  function onUsernameInput() {
    const lower = usernameInput.value.toLowerCase();
    if (lower !== usernameInput.value) usernameInput.value = lower;
    // Deleting everything re-couples the username to the name.
    usernameEdited = !!lower;
    schedule();
  }

  if (nameInput) nameInput.addEventListener("input", onNameInput);
  usernameInput.addEventListener("input", onUsernameInput);

  return {
    lastResult: () => availability,
    destroy() {
      clearTimeout(timer);
      seq++;
      if (nameInput) nameInput.removeEventListener("input", onNameInput);
      usernameInput.removeEventListener("input", onUsernameInput);
    },
  };
}
