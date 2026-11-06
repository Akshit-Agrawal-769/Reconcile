// Capture README screenshots into docs/screenshots/ (board, period, drawer, client).
//   node scripts/screenshots.mjs
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const { startStack, launchBrowser, login } = await import(join(here, "../web/e2e/server.mjs"));
const out = join(here, "../docs/screenshots");
mkdirSync(out, { recursive: true });

const stack = await startStack({ port: 8830 });
const browser = await launchBrowser();
const shot = (page, name) => page.screenshot({ path: join(out, `${name}.png`) });
try {
  const staff = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await login(staff, stack.url, "staff.a@demo.test");
  await staff.getByRole("row", { name: /Delta/ }).click();
  await staff.getByLabel("Purchase register (CSV)").setInputFiles(stack.seed.books);
  await staff.getByRole("button", { name: "Upload purchase register" }).click();
  await staff.getByTestId("jobs-books").getByText("Waiting for GSTR-2B").waitFor({ timeout: 30000 });
  await staff.getByLabel("GSTR-2B (JSON)").setInputFiles(stack.seed.gstr2b);
  await staff.getByRole("button", { name: "Upload GSTR-2B" }).click();
  await staff.getByTestId("jobs-2b").getByText("Done").waitFor({ timeout: 30000 });
  await staff.waitForTimeout(400);
  await shot(staff, "period");
  await staff.getByLabel("Show").selectOption("MISMATCH");
  await staff.waitForTimeout(400);
  await staff.getByTestId("queue").locator("tbody tr").first().click();
  await staff.getByRole("dialog").getByLabel("Decision").waitFor();
  await staff.waitForTimeout(300);
  await shot(staff, "drawer");
  await staff.getByRole("dialog").getByRole("button", { name: "Close" }).click();
  await staff.getByRole("button", { name: "Submit for client approval" }).click();
  await staff.waitForTimeout(400);
  await staff.getByRole("link", { name: "Clients" }).click();
  await staff.getByRole("heading", { name: "Clients" }).waitFor();
  await staff.waitForTimeout(300);
  await shot(staff, "board");

  const client = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await login(client, stack.url, "delta@demo.test");
  await client.getByRole("button", { name: "Approve this month" }).waitFor();
  await shot(client, "client");
  console.log(`wrote ${out}`);
} finally {
  await browser.close();
  await stack.stop();
}
