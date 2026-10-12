import test from "node:test";
import assert from "node:assert/strict";
import { ACTIONS_EXAMPLE, actionsExampleText, parsePushData } from "../../custom_components/casa/panel/views/push-data.js";

test("example has three actions covering nav_path, service and destructive", () => {
  const kinds = ACTIONS_EXAMPLE.actions.map((a) => (a.nav_path ? "nav" : a.service ? "service" : a.destructive ? "destructive" : "?"));
  assert.deepEqual(kinds, ["nav", "service", "destructive"]);
  assert.deepEqual(parsePushData(actionsExampleText()), { data: ACTIONS_EXAMPLE });
});

test("blank text means no data", () => {
  assert.deepEqual(parsePushData(""), { data: undefined });
  assert.deepEqual(parsePushData("   \n"), { data: undefined });
});

test("bad JSON and non-objects report an error", () => {
  assert.match(parsePushData("{nope").error, /JSON/);
  assert.match(parsePushData("[1]").error, /object/);
  assert.match(parsePushData('"s"').error, /object/);
});
