// Starts the whole stack for a browser test: a fresh database seeded by
// scripts/seed_e2e.py, the API under uvicorn (serving web/dist), and the worker.
//
//   const stack = await startStack({ port: 8811 });
//   ... stack.url, stack.seed.books, stack.seed.gstr2b ...
//   await stack.stop();
//
// Needs python3.13 (or $PYTHON) with the API's requirements, and Postgres at
// $E2E_PG (default postgresql://reconcile:reconcile@localhost:5432).
import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const PY = process.env.PYTHON || "python3.13";
const PG = process.env.E2E_PG || "postgresql://reconcile:reconcile@localhost:5432";

let built = false;
export function buildWeb() {
  if (built) return;
  execFileSync(process.execPath, [join(root, "web/build.mjs")], { cwd: root, stdio: "inherit" });
  built = true;
}

async function waitFor(url, ms = 20000) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    try {
      const r = await fetch(url);
      if (r.status < 500) return;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error(`server did not start: ${url}`);
}

export async function startStack({ port = 8811 } = {}) {
  buildWeb();
  const dbUrl = `${PG}/reconcile_e2e_${port}`;
  const out = mkdtempSync(join(tmpdir(), "reconcile-e2e-"));
  const seed = JSON.parse(execFileSync(PY, ["scripts/seed_e2e.py", "--url", dbUrl, "--out", out, "--fresh"],
    { cwd: root, encoding: "utf8" }).trim().split("\n").pop());
  const env = { ...process.env, DATABASE_URL: dbUrl, REC_SECRET: "e2e-secret",
                REC_STATIC_DIR: join(root, "web/dist") };
  const logs = [];
  const procs = [
    spawn(PY, ["-m", "uvicorn", "--factory", "api.app:app_from_env", "--port", String(port),
               "--log-level", "warning"], { cwd: root, env }),
    spawn(PY, ["-m", "worker.run", "--sleep", "0.3"], { cwd: root, env }),
  ];
  for (const p of procs) {
    p.stdout.on("data", (d) => logs.push(String(d)));
    p.stderr.on("data", (d) => logs.push(String(d)));
  }
  const url = `http://127.0.0.1:${port}`;
  await waitFor(`${url}/api/me`);
  return {
    url, seed, logs,
    async stop() {
      for (const p of procs) p.kill("SIGTERM");
      await new Promise((r) => setTimeout(r, 300));
      execFileSync(PY, ["scripts/seed_e2e.py", "--url", dbUrl, "--drop"], { cwd: root });
    },
  };
}

export async function launchBrowser() {
  const { createRequire } = await import("node:module");
  const require = createRequire(import.meta.url);
  const { chromium } = require("playwright");
  return chromium.launch();
}

export async function login(page, url, email, password = "demo123") {
  await page.goto(url + "/");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Log in" }).click();
}
