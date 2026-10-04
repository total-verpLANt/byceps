// Real ticket templates/assets; a disposable HTTP stub models Core's 204 response.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('playwright');

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const root = process.cwd();
let source = 'unknown';
let failNextSave = false;
let pendingFlash = false;
let getCount = 0;
let postCount = 0;
let rentalEnabled = false;
let secondSource = 'venue';
let flashMode = 'normal';
let failNextGet = false;
let invalidNextGet = null;
let sourceAfterSave = null;
const logEntries = [];

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (url.pathname === '/tickets/mine' && request.method === 'GET') {
    getCount += 1;
    if (failNextGet) {
      failNextGet = false;
      response.writeHead(503);
      response.end();
      return;
    }
    response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'});
    const pages = rentalEnabled ? fixture.pages : fixture.pagesRentalOff;
    let notification = pendingFlash && flashMode !== 'missing' ? fixture.notification : '';
    if (flashMode.startsWith('foreign')) {
      notification = fixture.notification.replaceAll('FIXTURE-1', 'FIXTURE-2');
      if (flashMode === 'foreign-error') notification = notification.replaceAll('color-success', 'color-danger');
    }
    let html = pages[source][secondSource].replace('<!-- save-result -->', notification);
    if (invalidNextGet === 'missing-ticket') html = html.replace('id="ticket-' + fixture.ticketIds[0] + '"', 'id="unrelated-ticket"');
    if (invalidNextGet === 'invalid-source') html = html.replace('data-chair-source="' + source + '"', 'data-chair-source="invalid"');
    if (invalidNextGet === 'missing-value') html = html.replace('class="data-value">' + fixture.labels[source], 'class="missing-value">' + fixture.labels[source]);
    invalidNextGet = null;
    response.end(html);
    pendingFlash = false;
    return;
  }
  if (request.method === 'POST') {
    postCount += 1;
    const match = url.pathname.match(/^\/tickets\/tickets\/([^/]+)\/chair_source\/(user|venue|rental)$/);
    assert.ok(match, 'Choice must use the real Core URL');
    const ticketIndex = fixture.ticketIds.indexOf(match[1]);
    assert.notEqual(ticketIndex, -1);
    if (failNextSave || (match[2] === 'rental' && !rentalEnabled)) {
      failNextSave = false;
      response.writeHead(403);
    } else {
      if (ticketIndex === 0) source = sourceAfterSave || match[2];
      else secondSource = match[2];
      sourceAfterSave = null;
      logEntries.push({ticketId: match[1], source: match[2]});
      pendingFlash = true;
      response.writeHead(204);
    }
    response.end();
    return;
  }
  let relative;
  if (url.pathname.startsWith('/static/')) {
    relative = 'byceps/static/' + url.pathname.slice('/static/'.length);
  } else if (url.pathname.startsWith('/static_sites/totalverplant-36/')) {
    relative = 'sites/totalverplant-36/static/'
      + url.pathname.slice('/static_sites/totalverplant-36/'.length);
  }
  if (relative && fs.existsSync(path.join(root, relative))) {
    const contentType = relative.endsWith('.css') ? 'text/css'
      : relative.endsWith('.js') ? 'text/javascript' : 'image/svg+xml';
    response.writeHead(200, {'Content-Type': contentType});
    response.end(fs.readFileSync(path.join(root, relative)));
  } else {
    response.writeHead(404);
    response.end();
  }
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = 'http://127.0.0.1:' + server.address().port;
  const browser = await chromium.launch({headless: true});
  try {
    for (const theme of ['light', 'dark']) {
      for (const [width, enabled] of [[1280, false], [1280, true], [390, false], [390, true]]) {
        rentalEnabled = enabled;
        source = 'unknown';
        failNextSave = false;
        pendingFlash = false;
        getCount = 0;
        postCount = 0;
        const page = await browser.newPage({viewport: {width, height: 900}});
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.addInitScript(value => {
          document.addEventListener('DOMContentLoaded', () => {
            document.documentElement.dataset.theme = value;
          });
        }, theme);
        const target = origin + '/tickets/mine#ticket-' + fixture.ticketIds[0];
        await page.goto(target);
        await page.waitForLoadState('networkidle');
        const field = () => page.locator('#ticket-' + fixture.ticketIds[0] + ' .chair-information');
        assert.equal(await field().locator('.data-value').textContent(), fixture.labels.unknown);
        assert.equal(await field().locator('[data-action="set-chair-source"]').count(), enabled ? 3 : 2);
        assert.equal(await field().getAttribute('data-chair-source'), 'unknown');
        assert.equal(await field().locator('a[href$="/chair_source/unknown"]').count(), 0);

        // The last option must be clickable even over the next card/footer.
        for (const id of fixture.ticketIds) {
          const field = page.locator('#ticket-' + id + ' .chair-information');
          await field.locator('.dropdown-toggle').click();
          const lastChoice = field.locator('[data-action="set-chair-source"]').last();
          await lastChoice.scrollIntoViewIfNeeded();
          assert.equal(await lastChoice.evaluate(element => {
            const bounds = element.getBoundingClientRect();
            const hit = document.elementFromPoint(bounds.x + bounds.width / 2,
              bounds.y + bounds.height / 2);
            return element.contains(hit);
          }), true, `Menu is obscured at ${width}px in ${theme} theme`);
          const menuBounds = await field.locator('.dropdown-menu').boundingBox();
          assert.ok(menuBounds.x >= 0 && menuBounds.x + menuBounds.width <= width,
            'Menu must remain inside the horizontal viewport');
          await field.locator('.dropdown-toggle').click();
        }

        // The running app previously served old markup alongside the new asset.
        await field().locator('.chair-update-success').evaluate(element => element.remove());
        await field().locator('.dropdown-toggle').click();
        const getBeforeSave = getCount;
        const scrollBeforeSave = await page.evaluate(() => window.scrollY);
        await page.evaluate(() => { window.chairPageMarker = true; });
        await field().locator('a[href$="/chair_source/user"]').click();
        await field().locator('.chair-update-success').waitFor({state: 'visible'});
        assert.equal(getCount, getBeforeSave + 1, 'Saving must fetch the page to consume the flash');
        assert.equal(postCount, 1, 'A choice must be submitted exactly once');
        assert.equal(pendingFlash, false, 'The background GET must consume the queued flash');
        assert.equal(await page.locator('.bote-notices').count(), 0, 'The confirmation belongs at the ticket');
        assert.equal(page.url(), target, 'Save must preserve the URL');
        assert.equal(await page.evaluate(() => window.chairPageMarker), true, 'Save must not navigate');
        assert.equal(await page.evaluate(() => window.scrollY), scrollBeforeSave, 'Save must preserve the scroll position');
        assert.equal(await field().locator('.dropdown').evaluate(element => element.classList.contains('open')), false);
        assert.equal(await page.locator('#ticket-' + fixture.ticketIds[1] + ' .chair-update-success').isVisible(), false);
        assert.equal(await field().locator('.data-value').textContent(), fixture.labels.user);
        assert.ok((await field().locator('.chair-update-success').textContent()).includes('FIXTURE-1'));

        // A failed save must keep the current answer and permit a retry.
        // Reproduce a page rendered by an older container without a status slot.
        await field().locator('.chair-update-success').evaluate(element => element.remove());
        await field().locator('.dropdown-toggle').click();
        failNextSave = true;
        const getBeforeFailure = getCount;
        const scrollBeforeFailure = await page.evaluate(() => window.scrollY);
        await field().locator('a[href$="/chair_source/venue"]').click();
        await field().locator('.chair-update-error').waitFor({state: 'visible'});
        assert.equal(getCount, getBeforeFailure);
        assert.equal(await field().locator('.data-value').textContent(), fixture.labels.user);
        assert.equal(await field().locator('.dropdown-toggle').isEnabled(), true);
        assert.equal(await field().locator('.chair-update-success').isVisible(), false);
        assert.equal(await page.evaluate(() => window.scrollY), scrollBeforeFailure);

        await field().locator('a[href$="/chair_source/venue"]').click();
        await field().locator('.chair-update-success').waitFor({state: 'visible'});
        assert.equal(await field().locator('.data-value').textContent(), fixture.labels.venue);
        assert.equal(page.url(), target);
        assert.equal(await page.evaluate(() => window.scrollY), scrollBeforeFailure);
        assert.equal(postCount, 3);
        await field().locator('.dropdown-toggle').click();
        await field().locator('a[href$="/chair_source/user"]').click();
        await field().locator('.chair-update-success').waitFor({state: 'visible'});
        assert.equal(await field().locator('.data-value').textContent(), fixture.labels.user);
        assert.equal(postCount, 4, 'The menu must remain usable after both choices');
        await page.reload();
        assert.equal(await field().locator('.data-value').textContent(), fixture.labels.user, 'The saved choice must survive a reload');
        assert.equal(await page.locator('.bote-notices').count(), 0, 'The consumed flash must not reappear after reload');
        await field().locator('.dropdown-toggle').click();
        assert.equal(await field().locator('.dropdown-menu').isVisible(), true, 'The menu must still open after reload');
        if (enabled) {
          await field().locator('a[href$="/chair_source/rental"]').click();
          await field().locator('.chair-update-success').waitFor({state: 'visible'});
          assert.equal(await field().locator('.data-value').textContent(), fixture.labels.rental);
          assert.equal(await field().getAttribute('data-chair-source'), 'rental');
          const postsBeforeDuplicate = postCount;
          await field().locator('.dropdown-toggle').click();
          await field().locator('a[href$="/chair_source/rental"]').click();
          await field().locator('.dropdown-menu').waitFor({state: 'hidden'});
          assert.equal(postCount, postsBeforeDuplicate, 'Repeated rental selection must not write again');
          rentalEnabled = false;
          await page.reload();
          assert.equal(await field().locator('.data-value').textContent(), fixture.labels.rental);
          assert.equal(await field().locator('a[href$="/chair_source/rental"]').count(), 0);
        }
        await page.waitForLoadState('networkidle');
        await page.screenshot({path: path.join(path.dirname(process.argv[2]), `ticket-chair-${theme}-${width}-${enabled ? 'on' : 'off'}.png`), fullPage: true, animations: 'disabled'});
        assert.deepEqual(errors, []);
        await page.close();
      }
    }

    // Independent controls reproduce stale displays, consumed/unrelated flashes,
    // invalid refreshes and competing editors using the actual feature script.
    for (const scenario of ['no-flash', 'foreign-success', 'foreign-error',
      'stale-after-refresh-failure', 'second-editor', 'verified-no-op',
      'failed-precheck', 'conflicting-refresh', 'missing-ticket',
      'invalid-source', 'missing-value', 'second-ticket', 'legacy-refresh',
      'overlapping-actions']) {
      source = 'venue';
      secondSource = 'venue';
      rentalEnabled = true;
      pendingFlash = false;
      flashMode = 'normal';
      failNextGet = false;
      invalidNextGet = null;
      sourceAfterSave = null;
      const page = await browser.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(origin + '/tickets/mine');
      const field = index => page.locator('#ticket-' + fixture.ticketIds[index] + ' .chair-information');
      const choose = async (value, index = 0) => {
        if (!await field(index).locator('.dropdown-menu').isVisible()) {
          await field(index).locator('.dropdown-toggle').click();
        }
        await field(index).locator(`a[href$="/chair_source/${value}"]`).click();
      };
      const success = async (value, index = 0) => {
        await field(index).locator('.chair-update-success').waitFor({state: 'visible'});
        assert.equal(await field(index).getAttribute('data-chair-source'), value, scenario);
        assert.equal(await field(index).locator('.data-value').textContent(), fixture.labels[value], scenario);
        assert.equal(await field(index).locator('.chair-update-error').isVisible(), false, scenario);
        assert.ok((await field(index).locator('.chair-update-success').textContent()).includes(`FIXTURE-${index + 1}`), scenario);
        assert.equal(await field(1 - index).locator('.chair-update-success').isVisible(), false, scenario);
      };
      const error = async () => {
        await field(0).locator('.chair-update-error').waitFor({state: 'visible'});
        assert.equal(await field(0).locator('.chair-update-success').isVisible(), false, scenario);
        assert.equal(await field(0).locator('.dropdown-toggle').isEnabled(), true, scenario);
      };
      const postsBefore = postCount;
      const getsBefore = getCount;
      const logsBefore = logEntries.length;
      if (['no-flash', 'foreign-success', 'foreign-error', 'second-ticket', 'legacy-refresh'].includes(scenario)) {
        flashMode = scenario === 'no-flash' ? 'missing' : scenario;
        if (scenario === 'legacy-refresh') {
          // Both loaded and refreshed markup may lack the success slot.
          await field(0).locator('.chair-update-success').evaluate(element => element.remove());
          await page.route('**/tickets/mine', async route => {
            const response = await route.fetch();
            await route.fulfill({response, body: (await response.text()).replace(/<p class="chair-update-success"[^>]*>.*?<\/p>/g, '')});
          });
          flashMode = 'missing';
        }
        const index = scenario === 'second-ticket' ? 1 : 0;
        await choose('user', index);
        await success('user', index);
        assert.equal(index === 0 ? source : secondSource, 'user');
        assert.equal(postCount, postsBefore + 1);
      } else if (scenario === 'overlapping-actions') {
        let releaseRefresh, refreshStarted;
        const heldRefresh = new Promise(resolve => { releaseRefresh = resolve; });
        const started = new Promise(resolve => { refreshStarted = resolve; });
        await page.route('**/tickets/mine', async route => {
          const response = await route.fetch();
          refreshStarted();
          await heldRefresh;
          await route.fulfill({response});
        });
        await choose('user');
        await started;
        assert.equal(await field(0).locator('.dropdown-toggle').isEnabled(), false);
        // Dispatch while refresh is outstanding, including on another ticket.
        for (const index of [0, 1]) {
          await field(index).locator('a[href$="/chair_source/venue"]').evaluate(element => element.click());
        }
        assert.equal(postCount, postsBefore + 1, 'Overlapping choices must not write');
        assert.equal(logEntries.length, logsBefore + 1);
        releaseRefresh();
        await success('user');
        await page.unroute('**/tickets/mine');
      } else if (scenario === 'verified-no-op') {
        await choose('venue');
        await field(0).locator('.dropdown-menu').waitFor({state: 'hidden'});
        assert.equal(getCount, getsBefore + 1, 'No-op requires a server GET');
        assert.equal(postCount, postsBefore);
        assert.equal(logEntries.length, logsBefore, 'No-op must not add a ticket log');
        assert.equal(await field(0).locator('.chair-update-success').isVisible(), false);
      } else if (scenario === 'second-editor') {
        source = 'user';
        await choose('venue');
        await success('venue');
        assert.equal(source, 'venue', 'The explicit selection must win over the stale display');
        assert.equal(postCount, postsBefore + 1);
        assert.equal(getCount, getsBefore + 2, 'Precheck and post-save verification');
      } else if (scenario === 'failed-precheck') {
        failNextGet = true;
        await choose('venue');
        await error();
        assert.equal(postCount, postsBefore, 'Failed precheck cannot claim a successful no-op');
        assert.equal(logEntries.length, logsBefore);
        await choose('venue');
        await field(0).locator('.dropdown-menu').waitFor({state: 'hidden'});
        assert.equal(await field(0).locator('.chair-update-error').isVisible(), false);
        assert.equal(postCount, postsBefore, 'Verified retry is a real no-op');
      } else if (scenario === 'conflicting-refresh') {
        sourceAfterSave = 'rental';
        await choose('user');
        await error();
        assert.equal(await field(0).getAttribute('data-chair-source'), 'rental');
        assert.equal(await field(0).locator('.data-value').textContent(), fixture.labels.rental);
        assert.equal(await field(0).locator('.chair-update-error').textContent(), await field(0).getAttribute('data-conflict-text'));
      } else {
        if (scenario === 'stale-after-refresh-failure') failNextGet = true;
        else invalidNextGet = scenario;
        await choose('user');
        await error();
        assert.equal(source, 'user', 'POST succeeded even though verification failed');
        assert.equal(await field(0).locator('.data-value').textContent(), fixture.labels.venue);
        assert.equal(await field(0).getAttribute('data-chair-source'), null, 'Old answer is uncertain');
        assert.equal(await field(0).locator('.chair-update-error').textContent(), await field(0).getAttribute('data-refresh-error-text'));
        await choose('venue');
        await success('venue');
        assert.equal(source, 'venue', 'Selecting the old display must actually save');
        assert.equal(postCount, postsBefore + 2);
      }
      await page.reload();
      assert.equal(await field(0).getAttribute('data-chair-source'), source, scenario);
      assert.equal(await field(1).getAttribute('data-chair-source'), secondSource, scenario);
      assert.deepEqual(errors, [], scenario);
      await page.close();
    }
    console.log('PASS: light/dark desktop/mobile, rental OFF/ON, retained rentals, dropdown reachability, legacy markup, scroll/navigation preservation, POST retry; consumed/foreign flashes, stale-state recovery, competing editor, verified no-op without writes/logs, precheck retry, conflicting/invalid refreshes and ticket-scoped synchronization');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => {
  console.error(error);
  server.close();
  process.exitCode = 1;
});
