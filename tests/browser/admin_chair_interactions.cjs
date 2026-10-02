// Exercise real admin GET/POST routes against the disposable browser fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const fixturePath = process.argv[2];
const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const output = path.dirname(fixturePath);
const base = id => `${fixture.baseUrl}/chair_optout/for_party/${id}/chair_information`;
const labels = fixture.labels;
const confirmation = labels['Are you sure you want to enable this for the party?'];

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.addCookies([{ name: 'session', value: fixture.cookies.writer, url: fixture.baseUrl }]);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const posts = [];
    page.on('request', request => {
      if (request.method() === 'POST') posts.push(request);
    });

    // Every matrix state is loaded from real stored settings/ticket sources.
    for (const [name, data] of Object.entries(fixture.parties)) {
      const visible = name.startsWith('on-') || name.endsWith('-rental');
      for (const suffix of ['', '/seating_plan']) {
        assert.equal((await page.goto(base(data.id) + suffix)).status(), 200);
        assert.equal(await page.locator('nav[aria-label] .tabs-tab').count(), 3);
        assert.equal(await page.locator('nav[aria-label] .tabs-tab--current').textContent(),
          suffix ? labels['Graphical seating plan'] : labels['Participant list']);
        assert.equal(await page.locator('[name="enabled"]').count(), 0);
        assert.equal(await page.locator('.main-tabs a[href*="filter=rented_chair"]').count(), visible ? 1 : 0);
        if (suffix) {
          assert.equal(await page.locator('.chair-seats-legend .seat--chair-rental').count(), visible ? 1 : 0);
          assert.equal(await page.locator('.area .seat--chair-rental').count(), name.endsWith('-rental') ? 1 : 0);
        }
      }
    }

    // Cancellation must issue no POST and reset the visible selection to OFF.
    await page.goto(base('off-empty') + '/rental_selection');
    assert.equal(await page.locator('nav[aria-label] .tabs-tab--current').textContent(), labels['Rental chair selection']);
    await page.selectOption('[name="enabled"]', 'true');
    const cancelled = page.waitForEvent('dialog');
    const cancelClick = page.locator('button[type="submit"]').click();
    const cancelDialog = await cancelled;
    assert.equal(cancelDialog.message(), confirmation);
    await cancelDialog.dismiss();
    await cancelClick;
    assert.equal(posts.length, 0);
    assert.equal(await page.locator('[name="enabled"]').inputValue(), 'false');
    await page.reload();
    assert.equal(await page.locator('[name="enabled"]').inputValue(), 'false');

    const save = async (value, confirm) => {
      await page.selectOption('[name="enabled"]', value);
      let dialogSeen = false;
      const handler = async dialog => {
        dialogSeen = true;
        assert.equal(dialog.message(), confirmation);
        await dialog.accept();
      };
      page.on('dialog', handler);
      await Promise.all([
        page.waitForURL(url => url.pathname.endsWith('/rental_selection'), { waitUntil: 'load' }),
        page.waitForResponse(response => response.request().method() === 'POST'),
        page.locator('button[type="submit"]').click(),
      ]);
      await page.waitForLoadState('networkidle');
      page.off('dialog', handler);
      assert.equal(dialogSeen, confirm);
      assert.equal(await page.locator('[name="enabled"]').inputValue(), value);
    };

    await save('true', true);
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url(), base('off-empty') + '/rental_selection');
    await page.reload();
    assert.equal(await page.locator('[name="enabled"]').inputValue(), 'true');
    await save('true', false); // Already enabled: no activation dialog.
    await page.locator('nav[aria-label] .tabs-tab').filter({ hasText: labels['Participant list'] }).click();
    await page.locator('.main-tabs a[href*="filter=rented_chair"]').click();
    assert.equal(await page.locator('table.itemlist tbody tr').count(), 0);
    await page.locator('nav[aria-label] .tabs-tab').filter({ hasText: labels['Graphical seating plan'] }).click();
    assert.ok(page.url().endsWith('filter=rented_chair'));
    assert.equal(await page.locator('.area .seat--filter-dimmed').count(), 3);
    assert.equal(await page.locator('.area .seat--chair-rental').count(), 0);
    await page.locator('nav[aria-label] .tabs-tab').filter({ hasText: labels['Rental chair selection'] }).click();
    await save('false', false);

    // Disabling retains actual rental markers, legend, filters and tooltip.
    await page.goto(base('on-rental') + '/rental_selection');
    await save('false', false);
    for (const suffix of ['', '/seating_plan']) {
      await page.goto(base('on-rental') + suffix);
      await page.locator('.main-tabs a[href*="filter=rented_chair"]').click();
      if (!suffix) {
        assert.equal(await page.locator('table.itemlist tbody tr').count(), 1);
        await page.screenshot({ path: path.join(output, 'admin-chair-participants.png'), fullPage: true });
      } else {
        const rental = page.locator('.area .seat--chair-rental');
        assert.equal(await rental.count(), 1);
        assert.equal(await rental.evaluate(el => el.offsetWidth), 26, 'GV36 seating geometry is loaded');
        assert.equal(await page.locator('.area .seat--filter-dimmed').count(), 2);
        assert.equal(await page.locator('.chair-seats-legend .seat--chair-rental').count(), 1);
        const symbol = await rental.evaluate(el => {
          const style = getComputedStyle(el, '::after');
          return { content: style.content, shape: style.clipPath, color: style.backgroundColor,
            outline: getComputedStyle(el).boxShadow, rotation: getComputedStyle(el).transform };
        });
        assert.notEqual(symbol.content, 'none');
        assert.ok(symbol.shape.startsWith('polygon('));
        assert.equal(symbol.color, 'rgb(204, 136, 255)');
        assert.ok(symbol.outline.includes('rgb(204, 136, 255)'));
        assert.notEqual(symbol.rotation, 'none');
        assert.equal(await page.locator('.area .seat--own-chair').evaluate(el => getComputedStyle(el, '::after').borderRadius), '50%');
        await rental.hover();
        assert.equal(await page.locator('.seat-tooltip-note').textContent(), labels['Rental chair']);
        await page.screenshot({ path: path.join(output, 'admin-chair-rental-tooltip.png'), fullPage: true });
        await page.mouse.move(0, 0);
        assert.equal(await page.locator('.seat-tooltip').count(), 0);
      }
    }
    await page.goto(base('off-empty') + '/rental_selection');
    await page.screenshot({ path: path.join(output, 'admin-chair-setting.png'), fullPage: true });
    await page.goto(base('on-empty') + '/rental_selection');
    assert.equal(await page.locator('[name="enabled"]').inputValue(), 'true', 'Other party remains enabled');

    const reader = await browser.newContext();
    await reader.addCookies([{ name: 'session', value: fixture.cookies.reader, url: fixture.baseUrl }]);
    const readPage = await reader.newPage();
    assert.equal((await readPage.goto(base('off-empty') + '/rental_selection')).status(), 200);
    assert.equal(await readPage.locator('[name="enabled"]').count(), 0);
    const forbidden = await reader.request.post(base('off-empty') + '/rental_selection', { form: { enabled: 'true' } });
    assert.equal(forbidden.status(), 403);
    await page.goto(base('off-empty') + '/rental_selection');
    assert.equal(await page.locator('[name="enabled"]').inputValue(), 'false');
    assert.deepEqual(errors, []);
    console.log('PASS: real admin routes, three tabs, visibility matrix, cancel without POST, confirmed party-local save, no redundant dialogs, retained rentals, diamond/tooltip/filter and read-only authorization');
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
