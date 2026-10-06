const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

function harness() {
  const summary = {focused: false, focus() { this.focused = true; }};
  const dropdown = {
    open: true,
    addEventListener(_event, listener) { this.keydown = listener; },
    querySelector() { return summary; },
    contains(target) { return target === summary; }
  };
  const document = {
    querySelectorAll: () => [dropdown],
    addEventListener(_event, listener) { this.click = listener; }
  };
  vm.runInNewContext(
    readFileSync('byceps/static/behavior/lan_tournament_category_filter.js', 'utf8'),
    {document}
  );
  return {dropdown, document, summary};
}

test('Escape closes the dropdown and restores keyboard focus', () => {
  const {dropdown, summary} = harness();
  dropdown.keydown({key: 'Escape'});
  assert.equal(dropdown.open, false);
  assert.equal(summary.focused, true);
});

test('inside clicks preserve native operation; outside clicks close', () => {
  const {dropdown, document, summary} = harness();
  document.click({target: summary});
  assert.equal(dropdown.open, true);
  document.click({target: {}});
  assert.equal(dropdown.open, false);
});

test('other keys leave native details keyboard handling intact', () => {
  const {dropdown, summary} = harness();
  dropdown.keydown({key: 'Enter'});
  assert.equal(dropdown.open, true);
  assert.equal(summary.focused, false);
});
