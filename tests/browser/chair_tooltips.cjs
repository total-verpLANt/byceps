// Browser regression tests for the module-local chair plan tooltip.
// Run from the repository root; see the chair_optout README for the container command.
const assert = require('node:assert/strict');
const path = require('node:path');
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setContent('<div class="area" style="height:200px;width:200px">'
      + '<div class="seat-with-tooltip" style="left:40px;top:80px">'
      + '<div class="seat seat--occupied seat--own-chair"></div></div></div>');
    await page.addStyleTag({ path: path.resolve('byceps/static/style/seating.css') });
    await page.addStyleTag({ path: path.resolve('byceps/services/chair_optout/blueprints/admin/static/style/chair_optout.css') });
    assert.equal(await page.locator('.seat').evaluate(el => el.offsetWidth), 11);
    await page.addStyleTag({ path: path.resolve('sites/totalverplant-36/static/style/seating.css') });
    assert.equal(await page.locator('.seat').evaluate(el => el.offsetWidth), 26);
    await page.addScriptTag({ path: path.resolve('byceps/services/chair_optout/blueprints/admin/static/behavior/chair_optout.js'), type: 'module' });

    // Flask integration tests cover rendering a None label as plain fallback
    // text. Here we cover the subsequent dataset -> tooltip DOM boundary.
    for (const label of ['unnamed', 'A " & B', '<img src=x onerror="window.injected=true">']) {
      const note = 'Brings own chair <img src=x onerror="window.injected=true">';
      const name = '<script>window.injected=true</script>';
      await page.locator('.seat-with-tooltip').evaluate((el, data) => {
        Object.assign(el.dataset, {
          label: data.label,
          ticketId: 'test-ticket',
          tooltipNote: data.note,
          occupierName: data.name,
          occupierAvatar: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==',
        });
      }, { label, note, name });
      await page.locator('.seat').hover();
      assert.equal(await page.locator('.seat-tooltip').count(), 1);
      assert.equal(await page.locator('.seat-tooltip .seat-label').textContent(), label);
      assert.equal(await page.locator('.seat-tooltip-note').textContent(), note);
      assert.equal(await page.locator('.seat-occupier-name strong').textContent(), name);
      assert.equal(await page.locator('.seat-tooltip img').count(), 1, 'Only the avatar may be an image');
      assert.equal(await page.locator('.seat-tooltip script').count(), 0);
      assert.equal(await page.evaluate(() => window.injected), undefined);
      assert.notEqual(await page.locator('.seat').evaluate(el => getComputedStyle(el, '::after').content), 'none');
      await page.mouse.move(300, 300);
      assert.equal(await page.locator('.seat-tooltip').count(), 0);
    }
    assert.deepEqual(errors, []);
    console.log('PASS: module-local tooltip escaping, participant/chair status, hover cleanup, default and GV36 seat sizes');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
