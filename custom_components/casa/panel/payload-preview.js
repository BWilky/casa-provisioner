// Casa admin panel — pure helpers (no DOM, no sibling imports) that mirror the
// server's v2 provisioning payload assembly so editors can show a live,
// truthful preview. Semantics track `_provision_internal` in
// custom_components/casa/__init__.py and the field-scope constants in
// const.py (see also profile-fields.js FIELD_SCOPES); values the server
// computes or that arrive as one-time process inputs at provision time
// (username, password, PIN, timestamps, linked WireGuard config) render as
// parenthesized placeholder strings when absent.

const DEFAULT_EXPIRATION_HOURS = 336; // server default: 14 days
const DEFAULT_TIMEOUT_MINUTES = 5;

/* ---------- field coercion (mirrors the server's get_field) ---------- */

function trimStr(val) {
  return String(val ?? "").trim();
}

// Server bool fields accept real booleans or the string "true".
function toBool(val) {
  if (typeof val === "string") return val.trim().toLowerCase() === "true";
  return !!val;
}

// Server int fields run int(val); empty/non-numeric falls back to the default.
function toInt(val, fallback) {
  if (val == null || val === "") return fallback;
  const n = typeof val === "number" ? val : Number(String(val).trim());
  return Number.isFinite(n) ? Math.trunc(n) : fallback;
}

// Split on commas (arrays accepted), trim items, drop empties, re-join.
function normalizeCsv(val) {
  const items = Array.isArray(val) ? val : String(val ?? "").split(",");
  return items
    .map((item) => String(item ?? "").trim())
    .filter(Boolean)
    .join(",");
}

// true/"true" → "true", "mandatory" → "mandatory", anything else → "false".
function normalizePush(val) {
  if (val === true) return "true";
  if (typeof val === "string") {
    const s = val.trim().toLowerCase();
    if (s === "true") return "true";
    if (s === "mandatory") return "mandatory";
  }
  return "false";
}

/* ---------- v2 payload preview ---------- */

// buildV2PayloadPreview(fields, ctx) → plain object in the exact key order of
// the server's v2 `profile` dict.
//   fields: a provision-profile `fields` object (see _DEFAULT_FIELDS)
//   ctx: { siteId?, wgProfiles? }  wgProfiles: [{ id, alias, excluded_wifi }]
export function buildV2PayloadPreview(fields, ctx = {}) {
  const f = fields || {};

  const expirationHours = toInt(f.expiration_hours, DEFAULT_EXPIRATION_HOURS);
  const timeoutMinutes = toInt(f.timeout_minutes, DEFAULT_TIMEOUT_MINUTES);

  // Linked WireGuard profile wins over inline config, like the server.
  const wgProfileId = f.wireguard_profile_id;
  const linked =
    wgProfileId != null && wgProfileId !== ""
      ? (ctx.wgProfiles || []).find((p) => p && p.id === wgProfileId) || null
      : null;

  let wgConfig = "";
  if (linked) {
    wgConfig = `(from WireGuard profile '${trimStr(linked.alias) || linked.id}')`;
  } else {
    const inline = String(f.wireguard_config ?? "").trim();
    if (inline) wgConfig = `(inline config, ${inline.split(/\r?\n/).length} lines)`;
  }
  // Server falls back to the field value when the linked profile has no
  // excluded_wifi of its own.
  const wgExcludedRaw = linked && linked.excluded_wifi ? linked.excluded_wifi : f.wireguard_excluded_wifi;

  return {
    v: 2,
    server_url: trimStr(f.host_url) || "(host URL required)",
    username: trimStr(f.username) || "(entered at provision)",
    password: trimStr(f.password) || "(auto-generated at provision)",
    site_id: trimStr(ctx.siteId) || "(this site)",
    pin: trimStr(f.pin).slice(0, 6),
    default_dashboard: String(f.default_dashboard ?? ""),
    welcome_url: trimStr(f.welcome_url),
    immersive_level: String(f.immersive_level || "1"),
    theme_color_mode: String(f.theme_color_mode || "inherit"),
    custom_color: trimStr(f.custom_color || "#000000").replace(/\|/g, ""),
    session_expiration: expirationHours === 0 ? 0 : `(provision time + ${expirationHours} h)`,
    expiration: timeoutMinutes === 0 ? 0 : `(provision time + ${timeoutMinutes} min)`,
    cache_control_hours: String(f.cache_control_hours ?? ""),
    allowed_pages: toBool(f.allow_all_pages) ? "/*" : normalizeCsv(f.allowed_pages),
    allowed_wifi: normalizeCsv(f.allowed_wifi),
    push_notifications: normalizePush(f.push_notifications),
    wireguard: {
      allowed: toBool(f.allow_wireguard),
      config: wgConfig,
      excluded_wifi: trimStr(wgExcludedRaw).replace(/\|/g, ""),
    },
    connect_wifi: {
      ssid: trimStr(f.connect_wifi_ssid),
      password: trimStr(f.connect_wifi_password),
    },
  };
}

/* ---------- profile card chips ---------- */

// profileChips(profile) → [{ label, cls }] for the wizard's profile cards and
// profile lists. profile: { id, name, fields }.
export function profileChips(profile) {
  const f = (profile && profile.fields) || {};
  const chips = [];

  if (trimStr(f.pin)) chips.push({ label: "PIN", cls: "chip--neutral" });

  const push = normalizePush(f.push_notifications);
  if (push === "true") chips.push({ label: "Push", cls: "chip--app" });
  else if (push === "mandatory") chips.push({ label: "Push required", cls: "chip--warn" });

  if (toBool(f.allow_wireguard)) chips.push({ label: "VPN", cls: "chip--ok" });

  const hours = toInt(f.expiration_hours, DEFAULT_EXPIRATION_HOURS);
  if (hours === 0) chips.push({ label: "Permanent", cls: "chip--neutral" });
  else chips.push({ label: `${humanizeHours(hours)} session`, cls: "chip--neutral" });

  if (toBool(f.allow_all_pages)) chips.push({ label: "All pages", cls: "chip--warn" });
  else if (normalizeCsv(f.allowed_pages)) chips.push({ label: "Restricted", cls: "chip--neutral" });

  if (normalizeCsv(f.allowed_wifi)) chips.push({ label: "Wi-Fi lock", cls: "chip--neutral" });
  if (trimStr(f.connect_wifi_ssid)) chips.push({ label: "Auto Wi-Fi", cls: "chip--neutral" });

  return chips;
}

function humanizeHours(hours) {
  return hours < 48 ? `${hours}h` : `${Math.round(hours / 24)}d`;
}
