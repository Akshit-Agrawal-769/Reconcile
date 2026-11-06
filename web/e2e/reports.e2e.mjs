// node --test web/e2e/reports.e2e.mjs
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startStack, launchBrowser, login } from "./server.mjs";

let stack, browser;
before(async () => { stack = await startStack({ port: 8814 }); browser = await launchBrowser(); });
after(async () => { await browser?.close(); await stack?.stop(); });

test("reports, filing calendar and supplier follow-ups", async () => {
  const page = await browser.newPage();
  await login(page, stack.url, "staff.a@demo.test");
  await page.getByRole("row", { name: /Delta Distributors/ }).click();
  await page.getByLabel("Purchase register (CSV)").setInputFiles(stack.seed.books);
  await page.getByRole("button", { name: "Upload purchase register" }).click();
  await page.getByTestId("jobs-books").getByText("Waiting for GSTR-2B").waitFor({ timeout: 30000 });
  await page.getByLabel("GSTR-2B (JSON)").setInputFiles(stack.seed.gstr2b);
  await page.getByRole("button", { name: "Upload GSTR-2B" }).click();
  await page.getByTestId("jobs-2b").getByText("Done").waitFor({ timeout: 30000 });
  await page.close();

  const partner = await browser.newPage();
  await login(partner, stack.url, "partner@demo.test");
  await partner.getByRole("row", { name: /Delta Distributors/ }).click();
  await partner.getByRole("link", { name: "Reports and filing calendar" }).click();
  await partner.getByRole("heading", { name: "Reports for Delta Distributors" }).waitFor();

  // Supplier scores, worst first, with the formula one click away.
  const scores = partner.getByTestId("party-scores");
  await scores.locator("tbody tr").first().waitFor();
  assert.ok((await scores.locator("tbody tr").count()) >= 1);
  await partner.getByText("How is the score calculated?").click();
  await partner.getByText(/100 − 50 × share only in books/).waitFor();

  // Rule 37 section is present even when nothing is overdue.
  await partner.getByRole("heading", { name: "Unpaid invoices (Rule 37)" }).waitFor();

  // Generate the filing calendar for Q1.
  await partner.getByLabel("From").fill("2026-04-01");
  await partner.getByLabel("To").fill("2026-06-30");
  await partner.getByRole("button", { name: "Generate filings" }).click();
  await partner.getByText(/Added \d+ filings/).waitFor();
  const filings = partner.getByTestId("filings");
  await filings.getByText("GSTR3B").first().waitFor();
  await partner.getByTestId("alerts").waitFor();

  // Follow-ups: log one, see it listed.
  await partner.goBack();
  await partner.getByRole("link", { name: "supplier follow-ups" }).click();
  await partner.getByRole("heading", { name: /Supplier follow-ups/ }).waitFor();
  await partner.getByLabel("Supplier").selectOption({ index: 1 });
  await partner.getByLabel("How").selectOption("WHATSAPP");
  await partner.getByLabel("Sent on").fill("2026-10-02");
  await partner.getByLabel("Their reply").fill("Will file GSTR-1 by the 11th");
  await partner.getByLabel("Chase again on").fill("2026-10-12");
  await partner.getByLabel("Status").selectOption("SENT");
  await partner.getByRole("button", { name: "Log follow-up" }).click();
  const list = partner.getByTestId("followups");
  await list.getByText("Will file GSTR-1 by the 11th").waitFor();
  await list.getByText("WhatsApp").waitFor();
  await partner.close();
});
