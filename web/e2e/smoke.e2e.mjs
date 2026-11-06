// node --test web/e2e/smoke.e2e.mjs
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { startStack, launchBrowser, login } from "./server.mjs";

let stack, browser;
before(async () => { stack = await startStack({ port: 8811 }); browser = await launchBrowser(); });
after(async () => { await browser?.close(); await stack?.stop(); });

test("lands on the login page and logs in as staff", async () => {
  const page = await browser.newPage();
  await page.goto(stack.url + "/");
  await page.getByRole("heading", { name: "Log in to Reconcile" }).waitFor();
  await login(page, stack.url, "staff.a@demo.test");
  await page.getByRole("banner").getByText("Staff").waitFor();
  await page.getByRole("heading", { name: "Clients" }).waitFor();
  await page.close();
});

test("a wrong password shows the server's message", async () => {
  const page = await browser.newPage();
  await login(page, stack.url, "staff.a@demo.test", "nope");
  const alert = page.getByRole("alert");
  await alert.waitFor();
  assert.equal((await alert.textContent()).trim(), "Wrong email or password");
  await page.close();
});

test("log out returns to the login page and forgets the session", async () => {
  const page = await browser.newPage();
  await login(page, stack.url, "partner@demo.test");
  await page.getByRole("banner").getByText("Partner").waitFor();
  await page.getByRole("button", { name: "Log out" }).click();
  await page.getByRole("heading", { name: "Log in to Reconcile" }).waitFor();
  await page.reload();
  await page.getByRole("heading", { name: "Log in to Reconcile" }).waitFor();
  await page.close();
});

test("no horizontal scroll on a phone-width login page", async () => {
  const page = await browser.newPage({ viewport: { width: 375, height: 740 } });
  await page.goto(stack.url + "/");
  await page.getByRole("heading", { name: "Log in to Reconcile" }).waitFor();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  assert.equal(overflow, false);
  await page.close();
});
