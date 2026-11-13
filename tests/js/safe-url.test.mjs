import test from "node:test";
import assert from "node:assert/strict";
import { safeHttpUrl } from "../../custom_components/casa/panel/ui.js";

test("only http(s) URLs come back", () => {
  assert.equal(safeHttpUrl("https://ha.local/lovelace/0"), "https://ha.local/lovelace/0");
  assert.equal(safeHttpUrl("http://192.168.1.21:8123/"), "http://192.168.1.21:8123/");
});

test("script-capable and odd schemes are refused", () => {
  for (const bad of ["javascript:alert(1)", " JaVaScRiPt:alert(1)", "java\tscript:alert(1)", "data:text/html,<b>x", "vbscript:x", "file:///etc/passwd", "//evil.example/x", "/relative", "", null, undefined, 42]) {
    assert.equal(safeHttpUrl(bad), null, String(bad));
  }
});
