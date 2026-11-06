// Unit tests for src/format.ts. Run: node --test web/src/format.test.mjs
// The TypeScript module is bundled to a temp .mjs with esbuild, then imported.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);
const esbuild = require("esbuild");
const here = dirname(fileURLToPath(import.meta.url));
const out = join(mkdtempSync(join(tmpdir(), "fmt-")), "format.mjs");
esbuild.buildSync({ entryPoints: [join(here, "format.ts")], bundle: true, format: "esm", outfile: out });
const f = await import(pathToFileURL(out).href);

test("money uses rupees with Indian grouping", () => {
  assert.equal(f.money(123456), "₹1,23,456.00");
  assert.equal(f.money(-5000.5), "-₹5,000.50");
  assert.equal(f.money(null), "—");
});

test("status labels and tones", () => {
  assert.equal(f.statusLabel("ONLY_IN_BOOKS"), "Only in books");
  assert.equal(f.statusLabel("ONLY_IN_2B"), "Only in 2B");
  assert.equal(f.statusLabel("PERIOD_MISMATCH"), "Period mismatch");
  assert.equal(f.statusTone("PROBABLE"), "accent");
  assert.equal(f.statusTone("EXACT"), "success");
  assert.equal(f.statusTone("ONLY_IN_2B"), "warning");
  assert.equal(f.statusTone("DUPLICATE"), "danger");
  assert.equal(f.statusTone("SOMETHING_NEW"), "neutral");
});

test("periods, states and dates read as words", () => {
  assert.equal(f.periodLabel("092026"), "Sep 2026");
  assert.equal(f.stateLabel("AWAITING_APPROVAL"), "Awaiting client approval");
  assert.equal(f.dateLabel("2026-09-10"), "10 Sep 2026");
  assert.equal(f.dateLabel(null), "—");
  assert.equal(f.decisionLabel("CHASE"), "Chase supplier");
});

test("sentence() capitalises a server message without changing it otherwise", () => {
  assert.equal(f.sentence("client approval missing"), "Client approval missing");
  assert.equal(f.sentence(""), "");
});
