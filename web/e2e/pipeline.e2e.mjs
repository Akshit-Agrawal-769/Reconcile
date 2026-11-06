// node --test web/e2e/pipeline.e2e.mjs
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startStack, launchBrowser, login } from "./server.mjs";

let stack, browser;
before(async () => { stack = await startStack({ port: 8812 }); browser = await launchBrowser(); });
after(async () => { await browser?.close(); await stack?.stop(); });

const money = (s) => Number(s.replace(/[^0-9.-]/g, ""));

test("staff uploads both files and watches the pipeline fill the queue", async () => {
  const page = await browser.newPage();
  await login(page, stack.url, "staff.a@demo.test");
  await page.getByRole("row", { name: /Delta Distributors/ }).click();
  await page.getByRole("heading", { name: "Delta Distributors" }).waitFor();
  await page.getByText("Sep 2026").first().waitFor();

  await page.getByLabel("Purchase register (CSV)").setInputFiles(stack.seed.books);
  await page.getByRole("button", { name: "Upload purchase register" }).click();
  await page.getByTestId("jobs-books").getByText("Waiting for GSTR-2B").waitFor({ timeout: 30000 });

  await page.getByLabel("GSTR-2B (JSON)").setInputFiles(stack.seed.gstr2b);
  await page.getByRole("button", { name: "Upload GSTR-2B" }).click();
  await page.getByTestId("jobs-2b").getByText("Done").waitFor({ timeout: 30000 });

  const books = money(await page.getByTestId("metric-itc-books").textContent());
  const twob = money(await page.getByTestId("metric-itc-2b").textContent());
  assert.ok(books > 0 && twob > 0, `metrics ${books} ${twob}`);
  const rows = page.getByTestId("queue").locator("tbody tr");
  await page.waitForFunction(() =>
    document.querySelectorAll('[data-testid="queue"] tbody tr').length > 10);
  assert.ok((await rows.count()) > 10);
  await page.getByTestId("queue").getByText("Only in books").first().waitFor();

  // filter + search
  await page.getByLabel("Show").selectOption("ONLY_IN_BOOKS");
  await page.waitForFunction(() => [...document.querySelectorAll('[data-testid="queue"] tbody .chip')]
    .every((c) => c.textContent === "Only in books"));

  // the same 2B again is refused, verbatim
  await page.getByLabel("GSTR-2B (JSON)").setInputFiles(stack.seed.gstr2b);
  await page.getByRole("button", { name: "Upload GSTR-2B" }).click();
  await page.getByRole("alert").getByText("This file was already uploaded for this period").waitFor();
  await page.close();
});

test("a malformed file is refused with the server's reason", async () => {
  const page = await browser.newPage();
  await login(page, stack.url, "staff.a@demo.test");
  await page.getByRole("row", { name: /Delta Distributors/ }).click();
  await page.getByLabel("GSTR-2B (JSON)").setInputFiles({
    name: "broken.json", mimeType: "application/json", buffer: Buffer.from("{oops"),
  });
  await page.getByRole("button", { name: "Upload GSTR-2B" }).click();
  await page.getByRole("alert").getByText("File is not valid JSON").waitFor();
  await page.close();
});
