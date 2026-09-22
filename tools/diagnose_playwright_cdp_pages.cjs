#!/usr/bin/env node
const { chromium } = require('/home/bandi/.local/share/ralf-playwright-mcp/node_modules/playwright-core');

async function bounded(label, fn, timeoutMs = 1500) {
  const started = Date.now();
  let timer;
  try {
    const value = await Promise.race([
      fn(),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), timeoutMs); }),
    ]);
    return { label, ok: true, ms: Date.now() - started, count: Array.isArray(value) ? value.length : undefined };
  } catch (error) {
    return { label, ok: false, ms: Date.now() - started, error: String(error && error.message || error) };
  } finally {
    clearTimeout(timer);
  }
}

(async () => {
  const started = Date.now();
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9236');
  const contexts = browser.contexts();
  const pages = contexts.flatMap(context => context.pages());
  console.log(JSON.stringify({ event: 'connected', ms: Date.now() - started, contexts: contexts.length, pages: pages.length }));
  for (let index = 0; index < pages.length; index++) {
    const page = pages[index];
    const row = { index, url: page.url() };
    row.title = await bounded('title', () => page.title());
    row.consoleMessages = await bounded('consoleMessages', () => page.consoleMessages());
    row.pageErrors = await bounded('pageErrors', () => page.pageErrors());
    row.requests = await bounded('requests', () => page.requests());
    console.log(JSON.stringify(row));
  }
  await browser.close();
})().catch(error => {
  console.error(JSON.stringify({ event: 'fatal', error: String(error && error.stack || error) }));
  process.exitCode = 1;
});
