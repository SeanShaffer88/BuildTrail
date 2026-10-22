const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.BUILDTRAIL_PLAYWRIGHT || 'playwright');
(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage();
    const external = [];
    page.on('request', request => { if (/^https?:/.test(request.url())) external.push(request.url()); });
    await page.goto(pathToFileURL(process.argv[2]).href);
    const text = await page.locator('body').innerText();
    for (const expected of ['success', 'failed', 'Unchanged', 'compiler error', 'Dependency input differences', 'AppTrail source changes']) {
      assert(text.includes(expected), expected);
    }
    assert.equal(await page.evaluate(() => window.buildtrailInjected), undefined);
    assert.equal(await page.locator('script').count(), 0);
    const details = page.locator('details');
    assert(await details.count() > 0);
    await details.first().locator('summary').click();
    assert(await details.first().getAttribute('open') !== null);
    assert.deepEqual(external, []);
    if (process.argv[3]) await page.screenshot({path: process.argv[3], fullPage: false});
    console.log('Browser report checks passed: source/dependency/build evidence, expansion, escaped script text, no network requests.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
