// Casa admin panel — pure decision logic for the provision wizard
// (views/provision.js): username suggestion, change detection, sparse
// template saves, lineage and the casa.provision request body. No DOM, no
// imports — unit-tested under node (tests/js/provision-logic.test.mjs).

export const USERNAME_PREFIX = "casa-";

// "Kitchen iPad" → "casa-kitchen-ipad"; a name with nothing slug-able
// suggests "" so the form shows "required" rather than a bare prefix.
export function suggestUsername(deviceName, slugify) {
  const slug = slugify(deviceName || "");
  return slug ? USERNAME_PREFIX + slug : "";
}

// Keys whose collected value differs from the seeded baseline. Values are
// compared as strings (collectFields coerces types; the baseline may hold
// raw template values).
export function changedKeys(fields, baseline) {
  return Object.keys(fields).filter((k) => String(fields[k] ?? "") !== String((baseline || {})[k] ?? ""));
}

// Sparse body for "Also save as new template": the base template's set keys
// plus whatever the admin changed — and always host_url, which every
// provision needs.
export function templateFieldsToSave({ fields, baseSetKeys, changed }) {
  const keep = new Set([...(baseSetKeys || []), ...(changed || []), "host_url"]);
  const out = {};
  for (const key of Object.keys(fields)) {
    if (keep.has(key)) out[key] = fields[key];
  }
  return out;
}

// Template lineage stamped on the device: a newly saved template wins; an
// unchanged saved template is recorded; customized-but-unsaved or manual
// carries none (the fields alone describe the device).
export function lineageFor({ templateId, customized, savedTemplateId }) {
  if (savedTemplateId) return savedTemplateId;
  if (templateId && !customized) return templateId;
  return null;
}

// casa.provision service_data. A new account's generated password is sent
// explicitly so a retry never rotates it; an existing account sends none
// and the server rotates it. Process-only inputs ride along only when set.
export function buildProvisionRequest({ fields, form, account, deviceName, lineage }) {
  const data = { method: "qr", ...fields, username: account.username };
  if (account.user_id) data.user_id = account.user_id;
  if (account.password) data.password = account.password;
  data.device_alias = deviceName;
  const pin = String(form.pin ?? "").trim();
  if (pin) data.pin = pin;
  const ssid = String(form.connect_wifi_ssid ?? "").trim();
  if (ssid) {
    data.connect_wifi_ssid = ssid;
    data.connect_wifi_password = String(form.connect_wifi_password ?? "");
  }
  if (form.deauthenticate_existing) data.deauthenticate_existing = true;
  if (lineage) data.profile = lineage;
  return data;
}
