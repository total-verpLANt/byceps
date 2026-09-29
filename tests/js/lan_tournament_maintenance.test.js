'use strict';

const test = require('node:test');
const assert = require('node:assert');

const {
  formatSize,
  confirmLabel,
  guardSubmit
} = require('../../byceps/static/behavior/lan_tournament_maintenance.js');

const LABELS = {
  one: '%(count)s Bild löschen (%(size)s)',
  many: '%(count)s Bilder löschen (%(size)s)',
  none: 'Nichts ausgewählt'
};

test('confirm label counts and sizes', () => {
  assert.strictEqual(
    confirmLabel(11, 18140000, LABELS, 'de'),
    '11 Bilder löschen (17,3 MB)'
  );
  assert.strictEqual(
    confirmLabel(1, 2411724, LABELS, 'de'),
    '1 Bild löschen (2,3 MB)'
  );
});

test('nothing selected disables', () => {
  assert.strictEqual(
    confirmLabel(0, 0, LABELS, 'de'),
    'Nichts ausgewählt'
  );
});

test('formatSize switches at one mebibyte', () => {
  assert.strictEqual(formatSize(634880, 'de'), '620 KB');
  assert.strictEqual(formatSize(1048576, 'de'), '1,0 MB');
  assert.strictEqual(formatSize(1048575, 'en'), '1,024 KB');
});

test('formatSize agrees with the server rounding', () => {
  assert.strictEqual(formatSize(0, 'de'), '0 KB');
  assert.strictEqual(formatSize(0, 'en'), '0 KB');
  assert.strictEqual(formatSize(1, 'de'), '1 KB');
  assert.strictEqual(formatSize(511, 'de'), '1 KB');
  assert.strictEqual(formatSize(2560, 'de'), '2 KB');
  assert.strictEqual(formatSize(1536, 'de'), '2 KB');
  assert.strictEqual(formatSize(1048576 * 12, 'en'), '12.0 MB');
});

test('placeholders in the size are not treated as patterns', () => {
  assert.strictEqual(
    confirmLabel(2, 1024, { one: '', many: '%(size)s|%(count)s', none: '' }, 'en'),
    '1 KB|2'
  );
});

test('submit guard disables the button only after the submit started', () => {
  let onSubmit;
  const form = { addEventListener: (type, fn) => { if (type === 'submit') onSubmit = fn; } };
  const go = { disabled: false };
  const queue = [];
  guardSubmit(form, go, (fn) => queue.push(fn));

  assert.strictEqual(go.disabled, false);
  onSubmit();
  assert.strictEqual(go.disabled, false);
  queue.forEach((fn) => fn());
  assert.strictEqual(go.disabled, true);
});
