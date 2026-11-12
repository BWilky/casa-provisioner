import test from "node:test";
import assert from "node:assert/strict";
import { vpnOffPrompts } from "../../custom_components/casa/panel/views/device-editor.js";

test("turning the VPN off takes two distinct prompts naming the device", () => {
  const prompts = vpnOffPrompts("Sam's iPhone");
  assert.equal(prompts.length, 2);
  assert.notEqual(prompts[0].title, prompts[1].title);
  assert.notEqual(prompts[0].confirmLabel, prompts[1].confirmLabel);
  for (const p of prompts) assert.match(p.message, /Sam's iPhone/);
  assert.match(prompts[1].message, /re-provisioning/);
});
