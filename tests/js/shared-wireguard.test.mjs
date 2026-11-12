import test from "node:test";
import assert from "node:assert/strict";
import { retryIfSharedAllowed, sharedWireguardConflict } from "../../custom_components/casa/panel/api.js";

const refusal = { status_code: 409, body: { code: "wireguard_in_use", message: "in use by X", holders: ["X"] } };

test("recognises the refusal in a REST error and in a service response", () => {
  assert.deepEqual(sharedWireguardConflict(refusal), { message: "in use by X", holders: ["X"] });
  assert.equal(sharedWireguardConflict({ response: { code: "wireguard_in_use", error: "e" } }).message, "e");
  assert.equal(sharedWireguardConflict({ status_code: 500, body: { message: "boom" } }), null);
  assert.equal(sharedWireguardConflict({ response: { deep_link: "x" } }), null);
  assert.equal(sharedWireguardConflict(null), null);
});

test("a clean call runs once and never asks", async () => {
  const calls = [];
  const res = await retryIfSharedAllowed(async (allow) => (calls.push(allow), { queued: 1 }), () => assert.fail("asked"));
  assert.deepEqual(res, { queued: 1 });
  assert.deepEqual(calls, [false]);
});

test("a refusal asks, then retries with the override on yes", async () => {
  const calls = [];
  const run = async (allow) => {
    calls.push(allow);
    if (!allow) throw refusal;
    return { queued: 1 };
  };
  assert.deepEqual(await retryIfSharedAllowed(run, async () => true), { queued: 1 });
  assert.deepEqual(calls, [false, true]);
});

test("a refused service response is retried the same way", async () => {
  const run = async (allow) => (allow ? { response: { deep_link: "x" } } : { response: { code: "wireguard_in_use", error: "e" } });
  assert.deepEqual(await retryIfSharedAllowed(run, async () => true), { response: { deep_link: "x" } });
});

test("answering no, or having no one to ask, rejects with the refusal message", async () => {
  const run = async () => { throw refusal; };
  await assert.rejects(retryIfSharedAllowed(run, async () => false), /in use by X/);
  await assert.rejects(retryIfSharedAllowed(run, null), /in use by X/);
});

test("other errors pass straight through", async () => {
  const boom = { status_code: 500, body: { message: "boom" } };
  await assert.rejects(retryIfSharedAllowed(async () => { throw boom; }, () => assert.fail("asked")), (e) => e === boom);
});
