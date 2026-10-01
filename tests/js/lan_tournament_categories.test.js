const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const Rules = require('../../byceps/static/behavior/lan_tournament_create_wizard_rules.js');

test('category choices are validated in Basics independently of request origin', () => {
  for (const category of ['MAIN', 'FUN', 'STAGE', 'USER_ORGANIZED']) {
    for (const fromRequest of [false, true]) {
      const result = Rules.validate({name: 'Cup', category}, {fromRequest});
      assert.equal(result.errors.category, undefined);
    }
  }
  for (const category of [undefined, '', 'UNKNOWN']) {
    const result = Rules.validate({name: 'Cup', category}, {});
    assert.equal(result.errors.category.msgid, 'Please choose a valid tournament category.');
  }
  assert.ok(Rules.STEP_FIELDS[0].includes('category'));
});

function harness() {
  const groups = [['main-1', 'main-draft'], [], ['stage-1'], ['user-1']].map(ids => ({
    ids,
    querySelectorAll() {
      return this.ids.map(id => ({getAttribute: () => id}));
    }
  }));
  const instances = [];
  const calls = [];
  let finish, fail;
  let reloaded = 0;
  const context = {
    document: {
      querySelectorAll: () => groups,
      querySelector: () => ({getAttribute: () => '/sort'})
    },
    Sortable: {
      create(group, options) {
        const instance = {group, options, option(key, value) { this.options[key] = value; }};
        instances.push(instance);
        return instance;
      }
    },
    fetch(url, options) {
      calls.push({url, options});
      return new Promise((resolve, reject) => { finish = resolve; fail = reject; });
    },
    window: {location: {reload() { reloaded++; }}},
    console: {error() {}}
  };
  vm.runInNewContext(readFileSync('byceps/static/behavior/lan_tournament_sort.js', 'utf8'), context);
  return {groups, instances, calls, finish: value => finish(value), fail: value => fail(value), reloads: () => reloaded};
}

test('each category sorts alone and sends every ID including drafts', async () => {
  const h = harness();
  assert.equal(h.instances.length, 4);
  for (const instance of h.instances) {
    assert.equal(instance.options.group, undefined);
    assert.equal(instance.options.draggable, 'tr[data-tournament-id]');
  }
  h.groups[0].ids.reverse();
  h.instances[0].options.onEnd();
  assert.deepEqual(JSON.parse(h.calls[0].options.body), {
    tournament_ids: ['main-draft', 'main-1', 'stage-1', 'user-1']
  });
  assert.ok(h.instances.every(instance => instance.options.disabled));
  h.finish({ok: true});
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(h.instances.every(instance => !instance.options.disabled));
  assert.equal(h.reloads(), 0);
});

test('failed HTTP or network requests reload and keep sorting disabled', async () => {
  for (const networkError of [false, true]) {
    const h = harness();
    h.instances[0].options.onEnd();
    if (networkError) h.fail(new Error('offline'));
    else h.finish({ok: false, status: 400});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(h.reloads(), 1);
    assert.ok(h.instances.every(instance => instance.options.disabled));
  }
});
