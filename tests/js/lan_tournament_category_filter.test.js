const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

function harness({admin = false, empty = false} = {}) {
  const categories = ['ALL', 'MAIN', 'FUN', 'STAGE', 'USER_ORGANIZED'];
  const buttons = categories.map(category => ({
    attributes: {'data-category-filter': category, 'aria-pressed': String(category === 'ALL')},
    classes: new Set([category === 'ALL' ? 'color-primary' : 'is-outlined']),
    getAttribute(name) { return this.attributes[name]; },
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(event, listener) { this.click = listener; },
    classList: {toggle(name, active) {
      if (active) this.owner.classes.add(name);
      else this.owner.classes.delete(name);
    }}
  }));
  buttons.forEach(button => { button.classList.owner = button; });
  const sections = empty ? [] : categories.slice(1)
    .filter(category => admin || category !== 'STAGE')
    .map(category => ({
      hidden: false,
      ids: category === 'STAGE' ? [] : [`${category}-1`, `${category}-2`],
      getAttribute(name) {
        if (name === 'data-tournament-category') return category;
        if (name === 'data-tournament-count' && admin) return String(this.ids.length);
        return null;
      }
    }));
  const filter = {hidden: true, querySelectorAll: () => buttons};
  const message = {hidden: true};
  vm.runInNewContext(
    readFileSync('byceps/static/behavior/lan_tournament_category_filter.js', 'utf8'),
    {document: {
      querySelector(selector) {
        return selector === '[data-tournament-filter]' ? filter : message;
      },
      querySelectorAll: () => sections
    }}
  );
  return {filter, buttons, sections, message};
}

for (const admin of [false, true]) {
  test(`${admin ? 'admin' : 'site'} filters categories, handles empty categories and restores All`, () => {
    const h = harness({admin});
    const originalIds = h.sections.map(section => [...section.ids]);
    assert.equal(h.filter.hidden, false);
    assert.ok(h.sections.every(section => !section.hidden));
    for (const button of h.buttons.slice(1)) {
      button.click();
      const category = button.getAttribute('data-category-filter');
      const visible = h.sections.filter(section => !section.hidden);
      assert.deepEqual(visible.map(section => section.getAttribute('data-tournament-category')),
        category === 'STAGE' ? [] : [category]);
      assert.equal(h.message.hidden, category !== 'STAGE');
      assert.equal(h.buttons.filter(item => item.getAttribute('aria-pressed') === 'true').length, 1);
      assert.equal(button.getAttribute('aria-pressed'), 'true');
      assert.ok(button.classes.has('color-primary'));
      assert.ok(!button.classes.has('is-outlined'));
    }
    h.buttons[0].click();
    assert.ok(h.sections.every(section => !section.hidden));
    assert.equal(h.message.hidden, true);
    assert.deepEqual(h.sections.map(section => section.ids), originalIds);
  });
}

test('an entirely empty list keeps its existing empty state', () => {
  const h = harness({empty: true});
  h.buttons[1].click();
  assert.equal(h.message.hidden, true);
});
