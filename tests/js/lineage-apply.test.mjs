import test from "node:test";
import assert from "node:assert/strict";
import { lineageApplyPlan, NEVER_APPLIED } from "../../custom_components/casa/panel/views/templates.js";

const LIVE = new Set(["host_url", "default_dashboard", "allow_wireguard", "wireguard_config", "wireguard_profile_id"]);
const devices = [
  { device_id: "D1", provisioning_profile_id: "t1" },
  { device_id: "D2", provisioning_profile_id: "t2" },
  { device_id: "D3", provisioning_profile_id: null },
  { device_id: "D4", provisioning_profile_id: "t1" },
];

test("targets only devices provisioned from the template", () => {
  const plan = lineageApplyPlan({ id: "t1", fields: { default_dashboard: "/x" } }, devices, LIVE);
  assert.deepEqual(plan.devices.map((d) => d.device_id), ["D1", "D4"]);
});

test("never plans the host URL, WireGuard config or non-live fields", () => {
  const fields = { host_url: "https://x", default_dashboard: "/x", allow_wireguard: true, wireguard_config: "c", wireguard_profile_id: "w", expiration_hours: 5, username: "bob" };
  assert.deepEqual(lineageApplyPlan({ id: "t1", fields }, devices, LIVE).fields, ["default_dashboard", "allow_wireguard"]);
  assert.deepEqual(NEVER_APPLIED, ["host_url"]);
});

test("an unsaved template or one with nothing to send plans nothing", () => {
  assert.deepEqual(lineageApplyPlan({ fields: { default_dashboard: "/x" } }, devices, LIVE).devices, []);
  assert.deepEqual(lineageApplyPlan({ id: "t1", fields: { host_url: "https://x" } }, devices, LIVE).fields, []);
  assert.deepEqual(lineageApplyPlan({ id: "t1", fields: {} }, undefined, LIVE), { fields: [], devices: [] });
});
