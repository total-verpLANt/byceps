"""
tests.unit.services.lan_tournament.test_tournament_request_selection_cue
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Static guards for workspace-dim0.19: the game-format/elimination-mode
selection cue must follow `:checked` (CSS-first, so it also works
without JS) on both the generic base theme and the totalverplant-36
"bote" override, with a visible keyboard focus ring on the hidden
format radios. The bote style partial must still carry no Jinja-looking
tokens -- the trap this issue's briefing calls out by name.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest


_CSS_PATH = pathlib.Path('byceps/static/style/lan_tournament.css')
_BOTE_STYLE_PARTIAL = pathlib.Path(
    'sites/totalverplant-36/template_overrides/site/lan_tournament'
    '/_bote_request_style.html'
)
_JS_PATH = pathlib.Path('byceps/static/behavior/lan_tournament_request.js')

_HEX_RE = re.compile(r'#[0-9a-fA-F]{3,8}')

_NODE_BIN = shutil.which('node')


def _rule(src: str, selector: str) -> str:
    """Return one rule's full text, from `selector` to its closing
    `}` (inclusive). `selector` must be the last, comma-joined
    selector directly before the declaration block, so the first `}`
    found after it is that rule's own closing brace."""
    start = src.index(selector)
    end = src.index('}', start)
    return src[start : end + 1]


# ------------------------------------------------------------------ #
# lan_tournament.css (.lt-dark theme)
# ------------------------------------------------------------------ #


def test_lt_dark_seg_option_follows_checked_state():
    """A user click on a format radio must move the highlight even
    without the JS auto-reselect path touching a class."""
    src = _CSS_PATH.read_text()

    assert '.lt-dark .seg-option:has(input:checked)' in src
    # The server-rendered class rule stays, for the no-JS/no-:has() case.
    assert '.lt-dark .seg-option.is-selected' in src


def test_lt_dark_opt_follows_checked_state():
    """A manually clicked mode card must not keep the old `.on`/
    `.is-selected` highlight once a different mode is checked."""
    src = _CSS_PATH.read_text()

    assert '.lt-dark .opt:has(input:checked)' in src
    assert '.lt-dark .opt.is-selected' in src


def test_lt_dark_seg_option_has_visible_focus_ring():
    """The format radio is `opacity: 0`; Tab must still show a
    visible cue on its label card."""
    src = _CSS_PATH.read_text()

    assert '.lt-dark .seg-option:has(input:focus-visible)' in src
    rule = _rule(src, '.lt-dark .seg-option:has(input:focus-visible)')
    assert 'outline' in rule


def test_lt_dark_new_rules_use_lt_tokens_not_hex():
    """Never hard-code a hex value; use the theme's own `--lt-*`
    custom properties."""
    src = _CSS_PATH.read_text()

    for selector in (
        '.lt-dark .seg-option:has(input:checked)',
        '.lt-dark .seg-option:has(input:focus-visible)',
        '.lt-dark .opt:has(input:checked)',
    ):
        rule = _rule(src, selector)
        assert not _HEX_RE.search(rule), f'hard-coded hex in {selector!r}'
        assert '--lt-' in rule, f'no --lt- token in {selector!r}'


# ------------------------------------------------------------------ #
# totalverplant-36 bote override
# ------------------------------------------------------------------ #


def test_bote_seg_option_follows_checked_state():
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '.request-page .seg-option:has(input:checked)' in src
    assert '.request-page .seg-option.on' in src


def test_bote_opt_follows_checked_state():
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '.request-page .opt:has(input:checked)' in src
    assert '.request-page .opt.on' in src


def test_bote_seg_option_has_visible_focus_ring():
    src = _BOTE_STYLE_PARTIAL.read_text()

    assert '.request-page .seg-option:has(input:focus-visible)' in src
    rule = _rule(src, '.request-page .seg-option:has(input:focus-visible)')
    assert 'outline' in rule


def test_bote_new_rules_use_theme_tokens_not_hex():
    src = _BOTE_STYLE_PARTIAL.read_text()

    for selector in (
        '.request-page .seg-option:has(input:checked)',
        '.request-page .seg-option:has(input:focus-visible)',
        '.request-page .opt:has(input:checked)',
    ):
        rule = _rule(src, selector)
        assert not _HEX_RE.search(rule), f'hard-coded hex in {selector!r}'
        assert 'var(--' in rule, f'no theme token in {selector!r}'


def test_bote_style_partial_has_no_jinja_looking_tokens():
    """A `{% %}` / `{{ }}` / `{# #}` sequence inside this included
    partial's CSS would parse and 500 the page -- this has broken the
    codebase before. Assert none of the six token halves appear
    anywhere in the file."""
    src = _BOTE_STYLE_PARTIAL.read_text()

    for token in ('{%', '%}', '{{', '}}', '{#', '#}'):
        assert token not in src, f'found Jinja-looking token {token!r}'


# ------------------------------------------------------------------ #
# lan_tournament_request.js
# ------------------------------------------------------------------ #


def test_js_syncs_selection_class_on_manual_game_format_change():
    """A manual click on a format radio moves `is-selected`/`on` off
    the previously-checked card and onto the newly-checked one."""
    src = _JS_PATH.read_text()

    assert "syncSelectedCard(target.form, 'game_format', '.seg-option')" in src


def test_js_syncs_selection_class_on_manual_elimination_mode_change():
    """Same guarantee for a manually clicked mode card, not just the
    JS auto-reselect path."""
    src = _JS_PATH.read_text()

    assert "syncSelectedCard(target.form, 'elimination_mode', '.opt')" in src


def test_js_has_no_html_sink_or_eval_calls():
    src = _JS_PATH.read_text()

    assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|eval\(', src)


def test_js_has_no_user_facing_string_literals():
    src = _JS_PATH.read_text()

    assert not re.search(
        r'Teilnehmer|Team ?limit|Participant'
        r'|Highscore|Only available|Not available',
        src,
    )


def test_js_reads_the_three_caption_templates():
    """The script reads all three caption templates off the limit input."""
    src = _JS_PATH.read_text()

    assert 'data-caption-template' in src
    assert 'data-caption-template-teams' in src
    assert 'data-caption-template-over' in src


def test_js_falls_back_to_the_players_template():
    """Markup without the extra templates falls back to the players one."""
    src = _JS_PATH.read_text()

    assert 'playersTemplate' in src


def test_js_substitutes_max_capacity_and_size_tokens():
    """Tokens are substituted via split/join, not a single `replace`."""
    src = _JS_PATH.read_text()

    for token in ('{max}', '{capacity}', '{size}'):
        assert src.count(f"split('{token}')") >= 1, token


def test_js_toggles_over_class_on_the_caption_element():
    """The caption gets an `over` class once the limit exceeds the max."""
    src = _JS_PATH.read_text()

    assert re.search(r"caption\.classList\.toggle\(\s*'over'", src), (
        'no over-class toggle on the caption element'
    )


def test_js_defines_mode_hint_attributes():
    """The mode hint lookup is guarded for admin selects."""
    src = _JS_PATH.read_text()

    assert 'data-mode-hint' in src
    assert 'hintTemplate' in src
    assert 'hintDefault' in src


def test_js_reads_the_checked_format_label():
    """The hint text is keyed off the checked radio's label attribute."""
    src = _JS_PATH.read_text()

    assert 'data-format-label' in src or 'formatLabel' in src


def test_js_still_has_no_html_sink_or_eval_calls():
    """The script writes `textContent` only, never `innerHTML` or `eval`."""
    src = _JS_PATH.read_text()

    assert not re.search(r'innerHTML|outerHTML|insertAdjacentHTML|eval\(', src)


# Behavioural check under Node, with a minimal hand-rolled DOM stub.

_DOM_HARNESS_JS = r"""
'use strict';
var fs = require('fs');

function toCamel(name) {
  return name.replace(/-([a-z0-9])/g, function (_, c) {
    return c.toUpperCase();
  });
}

function ClassList(node) {
  this.node = node;
}
ClassList.prototype.contains = function (c) {
  return this.node._classes.has(c);
};
ClassList.prototype.add = function (c) {
  this.node._classes.add(c);
};
ClassList.prototype.remove = function (c) {
  this.node._classes.delete(c);
};
ClassList.prototype.toggle = function (c, force) {
  var on = force === undefined ? !this.node._classes.has(c) : !!force;
  if (on) {
    this.node._classes.add(c);
  } else {
    this.node._classes.delete(c);
  }
  return on;
};

var EXCLUDED = [
  'value', 'checked', 'selected', 'disabled', 'hidden', 'id', 'name',
];

function DomNode(tag, attrs) {
  attrs = attrs || {};
  this.tagName = tag.toUpperCase();
  this._attrs = {};
  this._classes = new Set();
  this.children = [];
  this.parentNode = null;
  this.dataset = {};
  this.value = attrs.value !== undefined ? attrs.value : '';
  this.checked = !!attrs.checked;
  this.selected = !!attrs.selected;
  this.disabled = !!attrs.disabled;
  this.hidden = !!attrs.hidden;
  this.textContent = '';
  this.id = attrs.id || '';
  this.name = attrs.name || '';
  this.form = null;
  this.classList = new ClassList(this);

  Object.keys(attrs).forEach(function (key) {
    if (EXCLUDED.indexOf(key) !== -1) {
      return;
    }
    if (key === 'class') {
      String(attrs[key]).split(/\s+/).filter(Boolean).forEach(
        function (c) {
          this._classes.add(c);
        },
        this
      );
      return;
    }
    this._attrs[key] = String(attrs[key]);
    if (key.indexOf('data-') === 0) {
      this.dataset[toCamel(key.slice(5))] = String(attrs[key]);
    }
  }, this);
}

DomNode.prototype.appendChild = function (child) {
  child.parentNode = this;
  this.children.push(child);
  return child;
};

DomNode.prototype.getAttribute = function (name) {
  if (name === 'id') {
    return this.id || null;
  }
  if (name === 'name') {
    return this.name || null;
  }
  return Object.prototype.hasOwnProperty.call(this._attrs, name)
    ? this._attrs[name]
    : null;
};
DomNode.prototype.setAttribute = function (name, value) {
  this._attrs[name] = String(value);
  if (name === 'id') {
    this.id = String(value);
  }
  if (name === 'name') {
    this.name = String(value);
  }
};
DomNode.prototype.hasAttribute = function (name) {
  return this.getAttribute(name) !== null;
};
DomNode.prototype.removeAttribute = function (name) {
  delete this._attrs[name];
};

function parseCompound(sel) {
  var tag = null;
  var classes = [];
  var attrs = [];
  var pseudos = [];
  var re = /(\.[\w-]+)|(\[[^\]]*\])|(:[\w-]+)|(^[a-zA-Z][\w-]*)/g;
  var m;
  while ((m = re.exec(sel)) !== null) {
    if (m[1]) {
      classes.push(m[1].slice(1));
    } else if (m[2]) {
      var inner = m[2].slice(1, -1);
      var eq = inner.indexOf('=');
      if (eq === -1) {
        attrs.push([inner, null]);
      } else {
        var aName = inner.slice(0, eq);
        var aValue = inner
          .slice(eq + 1)
          .replace(/^["']/, '')
          .replace(/["']$/, '');
        attrs.push([aName, aValue]);
      }
    } else if (m[3]) {
      pseudos.push(m[3].slice(1));
    } else if (m[4]) {
      tag = m[4];
    }
  }
  return { tag: tag, classes: classes, attrs: attrs, pseudos: pseudos };
}

function matchesCompound(node, compound) {
  if (
    compound.tag &&
    node.tagName.toLowerCase() !== compound.tag.toLowerCase()
  ) {
    return false;
  }
  for (var i = 0; i < compound.classes.length; i++) {
    if (!node.classList.contains(compound.classes[i])) {
      return false;
    }
  }
  for (var j = 0; j < compound.attrs.length; j++) {
    var aName = compound.attrs[j][0];
    var aValue = compound.attrs[j][1];
    if (aValue === null) {
      if (node.getAttribute(aName) === null) {
        return false;
      }
    } else if (node.getAttribute(aName) !== aValue) {
      return false;
    }
  }
  for (var k = 0; k < compound.pseudos.length; k++) {
    if (compound.pseudos[k] === 'checked') {
      if (!node.checked && !node.selected) {
        return false;
      }
    } else {
      return false;
    }
  }
  return true;
}

function matchesSelector(node, selector) {
  return selector.split(',').some(function (branch) {
    return matchesCompound(node, parseCompound(branch.trim()));
  });
}

DomNode.prototype.closest = function (selector) {
  var node = this;
  while (node) {
    if (matchesSelector(node, selector)) {
      return node;
    }
    node = node.parentNode;
  }
  return null;
};

DomNode.prototype.querySelectorAll = function (selector) {
  var out = [];
  function walk(n) {
    n.children.forEach(function (child) {
      if (matchesSelector(child, selector)) {
        out.push(child);
      }
      walk(child);
    });
  }
  walk(this);
  return out;
};

DomNode.prototype.querySelector = function (selector) {
  var all = this.querySelectorAll(selector);
  return all.length ? all[0] : null;
};

var documentNode = new DomNode('document-root', {});
documentNode.addEventListener = function () {};

global.document = documentNode;

function makeForm(attrs) {
  var form = new DomNode('form', attrs);
  documentNode.appendChild(form);
  return form;
}

function makeInput(form, attrs) {
  var input = new DomNode('input', attrs);
  input.form = form;
  form.appendChild(input);
  return input;
}

function makeEl(form, tag, attrs) {
  var elNode = new DomNode(tag, attrs);
  form.appendChild(elNode);
  return elNode;
}

var CAPTION_TEMPLATES = {
  'data-caption-template': '2 to {max} - {capacity} slots',
  'data-caption-template-teams':
    '2 to {max} teams - {capacity} slots / {size}',
  'data-caption-template-over': 'Team too big for {capacity} slots',
};

function makeCaptionForm(teamSizeValue, limitValue) {
  var form = makeForm({ 'data-capacity': '240', 'data-max-limit': '1024' });
  makeInput(form, { value: String(teamSizeValue), 'data-team': '' });
  var limitAttrs = Object.assign(
    {
      value: String(limitValue),
      id: 'limit-' + Math.random(),
      'data-limit-inp': '',
    },
    CAPTION_TEMPLATES
  );
  var limitInput = makeInput(form, limitAttrs);
  var caption = makeEl(form, 'div', { 'data-limit-caption': '' });
  return { form: form, limitInput: limitInput, caption: caption };
}

var playersScenario = makeCaptionForm(1, 10);
var teamsScenario = makeCaptionForm(5, 10);
var overScenario = makeCaptionForm(200, 10);

function makeHintForm(checkedValue) {
  var form = makeForm({});
  var options = [
    ['one_v_one', 'Duell'],
    ['ffa', 'Free-for-All'],
    ['highscore', 'Highscore'],
  ];
  options.forEach(function (pair) {
    makeInput(form, {
      type: 'radio',
      name: 'game_format',
      value: pair[0],
      checked: pair[0] === checkedValue,
      'data-format-label': pair[1],
    });
  });
  var hint = makeEl(form, 'p', {
    'data-mode-hint': '',
    'data-hint-template': 'Struck-through modes do not match {format}.',
    'data-hint-default': 'Pick a format to see availability.',
  });
  return { form: form, hint: hint };
}

var hintOneVOne = makeHintForm('one_v_one');
var hintFfa = makeHintForm('ffa');
var hintHighscore = makeHintForm('highscore');
var hintNone = makeHintForm(null);

var code = fs.readFileSync(process.argv[2], 'utf8');
eval(code);

var result = {
  players: {
    text: playersScenario.caption.textContent,
    over: playersScenario.caption.classList.contains('over'),
  },
  teams: {
    text: teamsScenario.caption.textContent,
    over: teamsScenario.caption.classList.contains('over'),
  },
  over: {
    text: overScenario.caption.textContent,
    over: overScenario.caption.classList.contains('over'),
  },
  hintOneVOne: hintOneVOne.hint.textContent,
  hintFfa: hintFfa.hint.textContent,
  hintHighscore: hintHighscore.hint.textContent,
  hintNone: hintNone.hint.textContent,
};

process.stdout.write(JSON.stringify(result));
"""


@pytest.mark.skipif(_NODE_BIN is None, reason='node not available')
def test_js_caption_and_hint_behavior_under_node(tmp_path):
    """The caption and mode hint follow the size and the checked format."""
    harness_path = tmp_path / 'harness.js'
    harness_path.write_text(_DOM_HARNESS_JS)

    proc = subprocess.run(  # noqa: S603 -- fixed local node binary, no untrusted input
        [_NODE_BIN, str(harness_path), str(_JS_PATH.resolve())],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)

    assert result['players']['text'] == '2 to 240 - 240 slots'
    assert result['players']['over'] is False

    assert result['teams']['text'] == '2 to 48 teams - 240 slots / 5'
    assert result['teams']['over'] is False

    assert result['over']['text'] == 'Team too big for 240 slots'
    assert result['over']['over'] is True

    assert result['hintOneVOne'] == 'Struck-through modes do not match Duell.'
    assert (
        result['hintFfa'] == 'Struck-through modes do not match Free-for-All.'
    )
    assert (
        result['hintHighscore']
        == 'Struck-through modes do not match Highscore.'
    )
    assert result['hintNone'] == 'Pick a format to see availability.'
