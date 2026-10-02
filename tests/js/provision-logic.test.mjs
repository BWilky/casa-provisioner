import test from "node:test";
import assert from "node:assert/strict";
import { slugify } from "../../custom_components/casa/panel/views/username-utils.js";
import {
  suggestUsername,
  changedKeys,
  templateFieldsToSave,
  lineageFor,
  buildProvisionRequest,
} from "../../custom_components/casa/panel/views/provision-logic.js";
import { setupResultHtml } from "../../custom_components/casa/panel/views/provision-result.js";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

test("suggestUsername prefixes the slug", () => {
  assert.equal(suggestUsername("Kitchen iPad", slugify), "casa-kitchen-ipad");
});

test("suggestUsername returns empty for unsluggable names", () => {
  assert.equal(suggestUsername("🙂🙂", slugify), "");
  assert.equal(suggestUsername("   ", slugify), "");
});

test("changedKeys compares stringified values", () => {
  assert.deepEqual(changedKeys({ a: "1", b: true, c: 3 }, { a: "1", b: false, c: "3" }), ["b"]);
});

test("templateFieldsToSave keeps base keys, changed keys and host_url", () => {
  const fields = { host_url: "https://h", a: "x", b: "y", c: "z" };
  assert.deepEqual(templateFieldsToSave({ fields, baseSetKeys: ["a"], changed: ["c"] }), { host_url: "https://h", a: "x", c: "z" });
});

test("lineageFor", () => {
  assert.equal(lineageFor({ templateId: "t1", customized: false, savedTemplateId: null }), "t1");
  assert.equal(lineageFor({ templateId: "t1", customized: true, savedTemplateId: null }), null);
  assert.equal(lineageFor({ templateId: "t1", customized: true, savedTemplateId: "t2" }), "t2");
  assert.equal(lineageFor({ templateId: null, customized: false, savedTemplateId: null }), null);
});

test("buildProvisionRequest — existing account sends no password", () => {
  const data = buildProvisionRequest({
    fields: { host_url: "https://h" },
    form: { pin: " 1234 ", connect_wifi_ssid: "", connect_wifi_password: "pw", deauthenticate_existing: false },
    account: { username: "mobile-bryce", user_id: "u1" },
    deviceName: "Kitchen iPad",
    lineage: "t1",
  });
  assert.deepEqual(data, {
    method: "qr", host_url: "https://h", username: "mobile-bryce", user_id: "u1",
    device_alias: "Kitchen iPad", pin: "1234", profile: "t1",
  });
});

test("buildProvisionRequest — new account carries its password, wifi and deauth", () => {
  const data = buildProvisionRequest({
    fields: {},
    form: { pin: "", connect_wifi_ssid: "Home", connect_wifi_password: "secret", deauthenticate_existing: true },
    account: { username: "casa-kitchen-ipad", user_id: "u9", password: "gen" },
    deviceName: "Kitchen iPad",
    lineage: null,
  });
  assert.equal(data.password, "gen");
  assert.equal(data.connect_wifi_ssid, "Home");
  assert.equal(data.connect_wifi_password, "secret");
  assert.equal(data.deauthenticate_existing, true);
  assert.equal("profile" in data, false);
});

test("setupResultHtml renders QR, both links and validity, escaped", () => {
  const html = setupResultHtml(
    { qr_data_uri: "data:image/png;base64,AA", universal_link: "https://bonjour.casa/setup#\"x", deep_link: "hascasa://s", expires_at: 1 },
    { esc, fmtExpiry: () => "11:20 PM" },
  );
  assert.match(html, /<img src="data:image\/png;base64,AA"/);
  assert.match(html, /Universal Link/);
  assert.match(html, /hascasa:\/\/s/);
  assert.match(html, /&quot;x/);
  assert.match(html, /valid until 11:20 PM/);
});
