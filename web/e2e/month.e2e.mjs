// The whole month in a browser: staff → partner → client → partner → another staff.
// node --test web/e2e/month.e2e.mjs
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startStack, launchBrowser, login } from "./server.mjs";

let stack, browser;
before(async () => { stack = await startStack({ port: 8813 }); browser = await launchBrowser(); });
after(async () => { await browser?.close(); await stack?.stop(); });

async function openDelta(page) {
  await page.getByRole("row", { name: /Delta Distributors/ }).click();
  await page.getByRole("heading", { name: "Delta Distributors" }).waitFor();
}

async function firstRow(page, status) {
  await page.getByLabel("Show").selectOption(status);
  await page.waitForFunction((s) => {
    const chips = [...document.querySelectorAll('[data-testid="queue"] tbody .chip')];
    return chips.length > 0 && chips.every((c) => c.textContent === s);
  }, status === "ONLY_IN_BOOKS" ? "Only in books" : "Value mismatch");
  return page.getByTestId("queue").locator("tbody tr").first();
}

test("a month from upload to close", async () => {
  // 1. Staff uploads both files.
  const staff = await browser.newPage();
  await login(staff, stack.url, "staff.a@demo.test");
  await openDelta(staff);
  await staff.getByLabel("Purchase register (CSV)").setInputFiles(stack.seed.books);
  await staff.getByRole("button", { name: "Upload purchase register" }).click();
  await staff.getByTestId("jobs-books").getByText("Waiting for GSTR-2B").waitFor({ timeout: 30000 });
  await staff.getByLabel("GSTR-2B (JSON)").setInputFiles(stack.seed.gstr2b);
  await staff.getByRole("button", { name: "Upload GSTR-2B" }).click();
  await staff.getByTestId("jobs-2b").getByText("Done").waitFor({ timeout: 30000 });

  // 2. Chase the largest "only in books" invoice.
  const chaseRow = await firstRow(staff, "ONLY_IN_BOOKS");
  const chaseInvoice = (await chaseRow.locator("td").nth(2).textContent()).trim();
  await chaseRow.click();
  const drawer = staff.getByRole("dialog");
  await drawer.getByRole("heading", { name: chaseInvoice }).waitFor();
  await drawer.getByLabel("Decision").selectOption("CHASE");
  await drawer.getByLabel("IMS action").selectOption("PENDING");
  await drawer.getByLabel("Remark").fill("Asked supplier to file GSTR-1");
  await drawer.getByRole("button", { name: "Save decision" }).click();
  await drawer.getByText("Awaiting partner").waitFor();
  await drawer.getByRole("button", { name: "Close" }).click();
  await staff.getByRole("row", { name: new RegExp(chaseInvoice) }).getByText("Chase supplier").waitFor();

  // A partial claim needs a percentage: the form says so before the server is asked.
  const mm = await firstRow(staff, "MISMATCH");
  const mmInvoice = (await mm.locator("td").nth(2).textContent()).trim();
  await mm.click();
  await drawer.getByLabel("Decision").selectOption("PARTIAL");
  await drawer.getByLabel("Claim %").fill("140");
  await drawer.getByRole("button", { name: "Save decision" }).click();
  await drawer.getByText("Enter a percentage from 0 to 100").waitFor();
  // 3. Accept the mismatch instead. Above the firm's threshold it waits for a partner;
  //    below it the database approves it at once. Either way the drawer says which.
  await drawer.getByLabel("Decision").selectOption("ACCEPT");
  await drawer.getByLabel("IMS action").selectOption("ACCEPT");
  await drawer.getByRole("button", { name: "Save decision" }).click();
  await drawer.locator(".pill", { hasText: /Awaiting partner|Approved/ }).waitFor();
  await drawer.getByRole("button", { name: "Close" }).click();

  // 4. Submit for the client's approval.
  await staff.getByRole("button", { name: "Submit for client approval" }).click();
  await staff.getByText("Awaiting client approval").first().waitFor();
  await staff.close();

  // 5. Partner tries to close before the client approves: refused, page still usable.
  const partner = await browser.newPage();
  await login(partner, stack.url, "partner@demo.test");
  await openDelta(partner);
  await partner.getByRole("button", { name: "Close month" }).click();
  await partner.getByRole("alert").getByText("Client approval missing").waitFor();
  await partner.getByLabel("Show").selectOption("ONLY_IN_BOOKS");
  assert.ok(await partner.getByRole("button", { name: "Close month" }).isEnabled());

  // 6. The client sees four numbers, approves, and gets the IMS plan.
  const client = await browser.newPage();
  await login(client, stack.url, "delta@demo.test");
  await client.getByRole("heading", { name: /Your GST credit for Sep 2026/ }).waitFor();
  for (const t of ["Claimable now", "Deferred", "Lost", "Suppliers to hold payment from"]) {
    await client.getByText(t, { exact: true }).waitFor();
  }
  await client.getByRole("button", { name: "Approve this month" }).click();
  const link = client.getByRole("button", { name: "Download IMS action plan (CSV)" });
  await link.waitFor();
  const [download] = await Promise.all([client.waitForEvent("download"), link.click()]);
  assert.equal(download.suggestedFilename(), "ims-plan-092026.csv");
  await client.close();

  // 7. Partner closes: refused while reviews wait, approves them, closes.
  await partner.reload();
  await partner.getByRole("heading", { name: "Delta Distributors" }).waitFor();
  await partner.getByRole("button", { name: "Close month" }).click();
  await partner.getByRole("alert").getByText("Reviews awaiting approval").waitFor();
  await partner.getByLabel("Show").selectOption("");
  for (;;) {
    const waiting = partner.getByTestId("queue").locator("tbody tr", { hasText: "Awaiting partner" });
    await partner.waitForTimeout(300);
    if ((await waiting.count()) === 0) break;
    await waiting.first().click();
    const d = partner.getByRole("dialog");
    await d.getByRole("button", { name: "Approve" }).click();
    await d.getByText("Approved", { exact: true }).waitFor();
    await d.getByRole("button", { name: "Close" }).click();
  }
  assert.ok(mmInvoice);
  await partner.getByRole("button", { name: "Close month" }).click();
  await partner.locator(".pill", { hasText: "Closed" }).waitFor();
  await partner.close();

  // 8. Staff B cannot see this client at all.
  const other = await browser.newPage();
  await login(other, stack.url, "staff.b@demo.test");
  await other.getByRole("heading", { name: "Clients" }).waitFor();
  await other.getByRole("row", { name: /Cyan Consulting/ }).waitFor();
  assert.equal(await other.getByText("Delta Distributors").count(), 0);
  await other.close();
});
