"""Regenerate `golden.json` from the binding drafts."""

# Run: .venv/bin/python tests/integration/services/lan_tournament/f17_design/generate_golden.py (never writes `.design-extract/`)

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from playwright.sync_api import sync_playwright


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[4]
DESIGN_EXTRACT = REPO_ROOT / '.design-extract' / 'f17'
FONTS_CSS = (
    REPO_ROOT
    / 'sites'
    / 'totalverplant-36'
    / 'static'
    / 'style'
    / 'fonts'
    / 'fonts.css'
)
PAIRS_PATH = HERE / 'pairs.json'
GOLDEN_PATH = HERE / 'golden.json'
CHROMIUM_PATH = '/usr/bin/chromium'

GOOGLE_FONTS_LINK_RE = re.compile(
    r'<link\b[^>]*(?:fonts\.googleapis\.com|fonts\.gstatic\.com)[^>]*>',
)

# Frame chrome of the draft canvas; never part of a page's own design.
CHROME_SKIP_SELECTORS = {
    '.fr',
    '.cap',
    '.row',
    '.pg',
    '.gp',
    '.sheet',
    '.app',
    'body',
    'html',
    '*',
}

# Browser-side extraction per frame: `{label: {prop: {...}}}`.
EXTRACT_JS = r"""
([draftScope, pairs, chromeSkip, isAdmin]) => {
  const chromeSet = new Set(chromeSkip);
  const root = document.querySelector(draftScope);
  const out = {};

  function splitPseudo(sel) {
    const m = sel.match(/^(.*)::(before|after|placeholder)$/);
    if (m) return [m[1].trim(), '::' + m[2]];
    return [sel.trim(), null];
  }

  function findElement(sel) {
    const [base, pseudo] = splitPseudo(sel);
    if (!root) return [null, pseudo];
    let el;
    if (base === '') {
      el = root;
    } else if (root.matches && (() => { try { return root.matches(base); } catch (e) { return false; } })()) {
      el = root;
    } else {
      el = root.querySelector(base);
    }
    return [el || null, pseudo];
  }

  function collectDeclared(el, pseudo, draftRules) {
    // prop name -> raw declared text of the last matching rule (source order)
    const declared = {};
    function noteRule(styleDecl) {
      for (let i = 0; i < styleDecl.length; i++) {
        const prop = styleDecl[i];
        if (prop.startsWith('--')) continue;
        declared[prop] = styleDecl.getPropertyValue(prop);
      }
    }
    function handleRule(rule, sheetHasNoScope) {
      if (typeof rule.selectorText !== 'string') return;
      const original = rule.selectorText;
      // Admin core-emulation rules (e.g. ".box,.notification{padding:20px}")
      // always combine several unrelated targets with a comma. Branch-owned
      // admin rules in these drafts never do, so a comma-joined rule is
      // wholesale core scaffolding and is skipped outright.
      if (isAdmin && original.includes(',')) return;
      const branches = original.split(',').map((s) => s.trim());
      for (const branch of branches) {
        if (chromeSet.has(branch)) continue;
        if (draftRules !== null && !draftRules.some((sub) => branch.includes(sub))) continue;
        const [branchBase, branchPseudo] = splitPseudo(branch);
        if (branchPseudo !== pseudo) continue;
        let matches;
        try {
          matches = el.matches(branchBase);
        } catch (e) {
          matches = false;
        }
        if (!matches) continue;
        noteRule(rule.style);
      }
    }
    function walkRuleList(rules) {
      for (const rule of rules) {
        // A plain CSSStyleRule can report a truthy (empty) `cssRules` in
        // some engines, so a style rule is identified by `selectorText`
        // first; only a real container (media/supports/...) is recursed.
        if (typeof rule.selectorText === 'string') {
          handleRule(rule);
        } else if (rule.cssRules) {
          walkRuleList(rule.cssRules);
        }
      }
    }
    for (const sheet of document.styleSheets) {
      let rules;
      try {
        rules = sheet.cssRules;
      } catch (e) {
        continue;
      }
      if (!rules) continue;
      walkRuleList(rules);
    }
    if (!pseudo && el.style && el.style.length) {
      // Inline style="" always counts, even for admin pairs: it is never
      // core-emulation scaffolding, always a deliberate value on this one
      // mockup element.
      noteRule(el.style);
    }
    return declared;
  }

  function contentBoxRatio(el, prop) {
    const parent = el.parentElement;
    if (!parent) return null;
    const pcs = getComputedStyle(parent);
    const padLeft = parseFloat(pcs.paddingLeft) || 0;
    const padRight = parseFloat(pcs.paddingRight) || 0;
    const padTop = parseFloat(pcs.paddingTop) || 0;
    const padBottom = parseFloat(pcs.paddingBottom) || 0;
    const heightish = prop === 'height' || prop === 'min-height' || prop === 'max-height';
    const base = heightish
      ? parent.clientHeight - padTop - padBottom
      : parent.clientWidth - padLeft - padRight;
    if (!base) return null;
    const cs = getComputedStyle(el);
    const value = parseFloat(cs.getPropertyValue(prop));
    if (Number.isNaN(value)) return null;
    return value / base;
  }

  function isRelative(prop, declaredText) {
    if (prop === 'width' || prop === 'height' || prop === 'min-width') return true;
    if (declaredText && (declaredText.includes('%') || declaredText.includes('auto'))) {
      return true;
    }
    return false;
  }

  for (const pair of pairs) {
    const { label, sel, draftRules } = pair;
    const [el, pseudo] = findElement(sel);
    if (!el) {
      out[label] = null;
      continue;
    }
    const declaredMap = collectDeclared(el, pseudo, draftRules);
    const cs = getComputedStyle(el, pseudo || undefined);
    const props = {};
    for (const [prop, declaredText] of Object.entries(declaredMap)) {
      const relative = isRelative(prop, declaredText);
      let computed;
      if (relative && !pseudo && (prop === 'width' || prop === 'height' || prop === 'min-width')) {
        const ratio = contentBoxRatio(el, prop);
        computed = ratio === null ? cs.getPropertyValue(prop) : String(ratio);
      } else {
        computed = cs.getPropertyValue(prop);
      }
      props[prop] = { declared: declaredText, computed, relative };
    }
    out[label] = props;
  }
  return out;
}
"""


def _repo_relative(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def _prepare_draft_copy(tmp_dir: Path) -> None:
    """Copy the draft HTML/JS into `tmp_dir` and patch the font link."""
    for name in (
        'Papiergrund F-17.html',
        'Turnieranfragen F-17.html',
        'Turnieranfragen F-17 Admin.html',
        'papiergrund-drafts.js',
        'papiergrund-frames.js',
    ):
        shutil.copy2(DESIGN_EXTRACT / name, tmp_dir / name)

    byceps_patch_src = DESIGN_EXTRACT / 'byceps-patch'
    if byceps_patch_src.exists():
        (tmp_dir / 'byceps-patch').symlink_to(byceps_patch_src)

    fonts_href = FONTS_CSS.resolve().as_uri()
    local_link = f'<link rel="stylesheet" href="{fonts_href}">'
    for name in ('Papiergrund F-17.html', 'Turnieranfragen F-17.html'):
        path = tmp_dir / name
        content = path.read_text(encoding='utf-8')
        if 'fonts.googleapis.com' not in content:
            continue
        patched, count = GOOGLE_FONTS_LINK_RE.subn('', content)
        if count:
            patched = patched.replace('</head>', local_link + '</head>', 1)
        path.write_text(patched, encoding='utf-8')


def _pair_specs(frame_pairs: list[list[Any]]) -> list[dict[str, Any]]:
    specs = []
    for label, draft_sel, _ours_sel, opts in frame_pairs:
        draft_rules = opts.get('draft_rules')
        specs.append(
            {'label': label, 'sel': draft_sel, 'draftRules': draft_rules}
        )
    return specs


def generate() -> dict[str, Any]:
    data = json.loads(PAIRS_PATH.read_text(encoding='utf-8'))
    frames: dict[str, Any] = data['frames']
    pairs: dict[str, Any] = data['pairs']

    golden: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(
        prefix='f17-design-golden-'
    ) as tmp_dir_name:
        tmp_dir = Path(tmp_dir_name)
        _prepare_draft_copy(tmp_dir)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=CHROMIUM_PATH)
            page = browser.new_page()
            try:
                for frame_key in sorted(frames):
                    frame = frames[frame_key]
                    width, height = frame['viewport']
                    page.set_viewport_size({'width': width, 'height': height})
                    draft_uri = (
                        (tmp_dir / frame['draft_file']).resolve().as_uri()
                    )
                    page.goto(draft_uri)
                    page.evaluate('() => document.fonts.ready')

                    frame_pairs = pairs[frame_key]
                    is_admin = frame['app'] == 'admin'
                    specs = _pair_specs(frame_pairs)
                    result = page.evaluate(
                        EXTRACT_JS,
                        [
                            frame['draft_scope'],
                            specs,
                            sorted(CHROME_SKIP_SELECTORS),
                            is_admin,
                        ],
                    )
                    for label, draft_sel, _ours_sel, _opts in frame_pairs:
                        props = result.get(label)
                        if props is None:
                            raise RuntimeError(
                                f'{frame_key} / {label}: draft element not found for '
                                f'selector {draft_sel!r} within {frame["draft_scope"]!r}'
                            )
                    golden[frame_key] = result
            finally:
                browser.close()

    return golden


def main() -> None:
    golden = generate()
    with GOLDEN_PATH.open('w', encoding='utf-8') as f:
        json.dump(golden, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    print(f'wrote {_repo_relative(GOLDEN_PATH)}')


if __name__ == '__main__':
    main()
