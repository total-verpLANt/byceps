/*
 * Controller of the tournament creation wizard.
 *
 * This is convenience only. The server is the actual gate: the create form,
 * `validate_tournament_settings` and the final POST re-validate everything,
 * including authorisation, party ownership, image and request state. Nothing
 * here may be the only place a rule is enforced, and the form must keep
 * working when this file does not run.
 *
 * Progressive enhancement of `form[data-lt-wizard]`:
 *   - turns the fieldsets into steps with a stepper and Back/Continue
 *   - shows live errors, dependent-change notices and the review
 *   - asks the server for a pre-check when the review step opens
 *   - keeps the entries in sessionStorage until the form is submitted
 *
 * Rules come from `LtCreateWizardRules` (no rule logic lives here).
 * Text comes only from the JSON config island (`strings`, keyed by the
 * English msgid) via `t(msgid, params)`. All DOM patching goes through
 * `textContent` and attribute setters; user- or server-derived strings never
 * reach a markup-parsing setter.
 *
 * The image module (`lan_tournament_create_wizard_image.js`) talks to this
 * file through events on `document`:
 *   - in:  `lt-wizard:image-state`, detail
 *          {status: 'none'|'uploading'|'done'|'error', imageId: string|null,
 *           filename: string|null, url: string|null, width: number|null,
 *           height: number|null, pct: number|null}
 *   - out: `lt-wizard:image-restore`, same detail, fired once after a
 *          session restore so the module can redraw the staged image
 */
(function () {
  'use strict';

  var Rules = window.LtCreateWizardRules;

  var STEP_COUNT = 5;
  var REVIEW_STEP = 4;
  var PRECHECK_TIMEOUT_MS = 5000;
  var PREVIEW_MAX_ROWS = 32;
  var SESSION_EXCLUDE = ['submission_token', 'image_id'];
  var UNMANAGED = ['image', 'from_request_id'].concat(SESSION_EXCLUDE);
  var LABEL_PARAMS = ['other', 'mode', 'format', 'type'];
  var POINTS_TOO_HIGH = 'Points may be at most %(max)s.';
  var POINTS_TOO_LOW = 'Points may be at least %(min)s.';
  // The summary line of these errors shows the first sentence only.
  var LEAD_SENTENCE_ONLY = [
    'With at most %(n)s teams no group of at least %(min)s teams can form. ' +
      'Lower the minimum to %(n)s or raise "Max. teams" in step 3.',
    'With at most %(n)s players no group of at least %(min)s can form. ' +
      'Lower the minimum or raise "Max. players" in step 3.'
  ];
  var BOLD_ON = '\u0001';
  var BOLD_OFF = '\u0002';
  var NOTICE_STEPS = 1;
  var STEP_TITLES = [
    'Basics', 'Competition', 'Participants', 'Scoring and groups',
    'Review and create'
  ];
  var SCOPE_KEYS = {
    'solo': 'solo',
    'team': 'team',
    'team-players': 'team',
    'one-v-one': 'oneVOne',
    'highscore': 'highscore',
    'ffa': 'ffa',
    'ffa-preview': 'ffa',
    'ffa-de': 'ffaDe'
  };
  var SOLO_COUNTS = ['min_players', 'max_players'];
  var TEAM_COUNTS = [
    'min_teams', 'max_teams', 'min_players_in_team', 'max_players_in_team'
  ];
  var FFA_FIELDS = [
    'point_table', 'group_size_min', 'group_size_max', 'advancement_count'
  ];
  var COUNT_LIMIT_KEYS = {
    name: 'nameMax', game: 'gameMax', description: 'textMax',
    ruleset: 'textMax'
  };

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function qs(root, selector) {
    return root.querySelector(selector);
  }

  function qsa(root, selector) {
    return Array.prototype.slice.call(root.querySelectorAll(selector));
  }

  function copy(source) {
    var result = {};
    Object.keys(source || {}).forEach(function (key) {
      result[key] = source[key];
    });
    return result;
  }

  function chars(text) {
    return Array.from(String(text || '')).length;
  }

  function isBlank(value) {
    return value === undefined || value === null || String(value).trim() === '';
  }

  function setShown(node, shown) {
    node.hidden = !shown;
    node.style.display = shown ? '' : 'none';
  }

  function readConfig() {
    var island = document.getElementById('lt-create-wizard-config');
    if (!island) {
      return null;
    }
    try {
      return JSON.parse(island.textContent);
    } catch (e) {
      return null;
    }
  }

  function init() {
    var form = qs(document, 'form[data-lt-wizard]');
    var config = readConfig();
    if (!form || !config || !Rules) {
      return;
    }
    var layout = form.closest('.lt-wiz-layout') || form.parentNode;
    var page = layout.parentNode;
    var strings = config.strings || {};
    var limits = config.limits || {};

    var stepEls = qsa(form, '[data-wiz-step]');
    if (stepEls.length !== STEP_COUNT) {
      return;
    }
    var noticesEl = qs(document, '[data-wiz-notices]');
    var announceEl = qs(document, '[data-wiz-announce]');
    var navEl = qs(form, '[data-wiz-nav]');
    var submitBtn = qs(form, '[data-wiz-submit]');
    var reviewEl = qs(form, '[data-wiz-review]');
    var previewEl = qs(form, '[data-wiz-preview]');
    var reqMapEl = qs(form, '[data-wiz-reqmap]');
    var fileInput = qs(form, 'input[type="file"]');
    var serverRefusalEl = qs(form, '[data-wiz-refusal]');

    var state = {
      step: 0,
      visited: {},
      shown: {},
      touched: {},
      notices: [],
      reqMap: null,
      unlinked: false,
      server: {},
      check: null,
      checkSeq: 0,
      checkAbort: null,
      image: {status: 'none'},
      submitting: false,
      values: {},
      result: {errors: {}, warnings: {}}
    };
    var pending = {focus: null};

    /* ---------- text ---------- */

    function t(msgid, params) {
      var text = Object.prototype.hasOwnProperty.call(strings, msgid)
        ? strings[msgid]
        : msgid;
      return Rules.format(text, params);
    }

    function attentionMessage(count) {
      return t(
        count === 1
          ? '%(n)s entry needs attention'
          : '%(n)s entries need attention',
        {n: count}
      );
    }

    function tMessage(msg) {
      var params = {};
      var given = msg.params || {};
      Object.keys(given).forEach(function (key) {
        var value = given[key];
        if (LABEL_PARAMS.indexOf(key) !== -1) {
          value = t(value);
        } else if (
          (key === 'max' && msg.msgid === POINTS_TOO_HIGH) ||
          (key === 'min' && msg.msgid === POINTS_TOO_LOW)
        ) {
          value = formatNumber(value);
        } else if (key === 'fields' && Array.isArray(value)) {
          value = value.map(function (item) {
            return t(item.label) + ' (' + item.value + ')';
          }).join(', ');
        }
        params[key] = value;
      });
      return t(msg.msgid, params);
    }

    function announce(text) {
      if (announceEl) {
        announceEl.textContent = text;
      }
    }

    /* ---------- form values ---------- */

    function named(name) {
      return qsa(form, '[name="' + name + '"]').filter(function (node) {
        return node.type !== 'file';
      });
    }

    function readValues() {
      var values = {};
      qsa(form, 'input[name], select[name], textarea[name]').forEach(
        function (node) {
          var name = node.name;
          if (node.type === 'file' || node.type === 'submit') {
            return;
          }
          if (node.type === 'radio') {
            if (!Object.prototype.hasOwnProperty.call(values, name)) {
              values[name] = null;
            }
            if (node.checked) {
              values[name] = node.value;
            }
          } else if (node.type === 'checkbox') {
            values[name] = node.checked;
          } else {
            values[name] = node.value;
          }
        }
      );
      return values;
    }

    function writeValue(name, value) {
      named(name).forEach(function (node) {
        if (node.type === 'radio') {
          node.checked = value !== null && value !== undefined &&
            node.value === value;
        } else if (node.type === 'checkbox') {
          node.checked = !!value;
        } else {
          node.value = value === null || value === undefined ? '' : value;
        }
      });
    }

    function validationValues(values) {
      var scopes = Rules.applicableScopes(values);
      var result = copy(values);
      function blank(names) {
        names.forEach(function (name) { result[name] = ''; });
      }
      if (!scopes.solo) {
        blank(SOLO_COUNTS);
      }
      if (!scopes.team) {
        blank(TEAM_COUNTS);
      }
      if (!scopes.ffa) {
        blank(FFA_FIELDS);
      }
      if (!scopes.highscore) {
        result.score_ordering = null;
      }
      result.reqMap = state.reqMap;
      return result;
    }

    function rulesContext() {
      return {
        limits: limits,
        capacity: config.capacity,
        validCombinations: config.validCombinations,
        fromRequest: !!config.request && !state.unlinked,
        requireStructure: true
      };
    }

    function stepOf(field) {
      for (var i = 0; i < Rules.STEP_FIELDS.length; i++) {
        if (Rules.STEP_FIELDS[i].indexOf(field) !== -1) {
          return i;
        }
      }
      return -1;
    }

    function isManaged(field) {
      return UNMANAGED.indexOf(field) === -1;
    }

    /* Every error that blocks Continue: client rules plus server errors. */
    function allErrors() {
      var errors = {};
      Object.keys(state.result.errors).forEach(function (field) {
        errors[field] = state.result.errors[field];
      });
      Object.keys(state.server).forEach(function (field) {
        if (isManaged(field) && state.server[field].length) {
          errors[field] = errors[field] || {
            msgid: state.server[field][0], params: {}, server: true
          };
        }
      });
      return errors;
    }

    function stepErrors(i) {
      var errors = allErrors();
      return Rules.STEP_FIELDS[i].filter(function (field) {
        return Object.prototype.hasOwnProperty.call(errors, field) &&
          isManaged(field);
      });
    }

    function allErrorFields() {
      var found = [];
      for (var i = 0; i < REVIEW_STEP; i++) {
        found = found.concat(stepErrors(i));
      }
      return found;
    }

    /* ---------- scopes ---------- */

    function applyScopes(values) {
      var scopes = Rules.applicableScopes(values);
      qsa(form, '[data-wiz-scope]').forEach(function (scopeEl) {
        var key = SCOPE_KEYS[scopeEl.getAttribute('data-wiz-scope')];
        var active = !!scopes[key];
        setShown(scopeEl, active);
        qsa(scopeEl, 'input, select, textarea, button').forEach(
          function (control) {
            if (active) {
              if (control.hasAttribute('data-wiz-scope-off')) {
                control.disabled = false;
                control.removeAttribute('data-wiz-scope-off');
              }
            } else if (!control.disabled) {
              control.disabled = true;
              control.setAttribute('data-wiz-scope-off', '');
            }
          }
        );
      });
    }

    /* ---------- steps, stepper, navigation ---------- */

    var stepperEl = null;
    var compactLabelEl = null;
    var allStepsEl = null;
    var barsEl = null;
    var stepButtons = [];
    var backBtn = null;
    var nextBtn = null;
    var cancelLink = null;

    function buildStepper() {
      var nav = el('nav', 'lt-wiz-stepper');
      nav.setAttribute('aria-label', t('Create tournament'));

      var compact = el('div', 'lt-wiz-stepper__compact');
      var head = el('div', 'lt-wiz-stepper__head');
      compactLabelEl = el('strong', 'lt-wiz-stepper__count');
      var details = el('details', 'lt-wiz-stepper__all');
      allStepsEl = details;
      details.appendChild(el('summary', 'button is-compact', t('All steps')));
      head.appendChild(compactLabelEl);
      head.appendChild(details);
      compact.appendChild(head);
      barsEl = el('div', 'lt-wiz-stepper__bars');
      barsEl.setAttribute('aria-hidden', 'true');
      for (var b = 0; b < STEP_COUNT; b++) {
        barsEl.appendChild(el('i'));
      }
      compact.appendChild(barsEl);

      function makeList(className) {
        var list = el('ol', className);
        var items = [];
        for (var i = 0; i < STEP_COUNT; i++) {
          var item = el('li', 'lt-wiz-stepper__item');
          items.push(item);
          list.appendChild(item);
        }
        return {list: list, items: items};
      }
      var vertical = makeList('lt-wiz-stepper__list lt-wiz-stepper__list--v');
      details.appendChild(vertical.list);
      var horizontal = makeList('lt-wiz-stepper__list');

      nav.appendChild(compact);
      nav.appendChild(horizontal.list);
      layout.parentNode.insertBefore(nav, layout);
      stepperEl = nav;
      stepButtons = [vertical.items, horizontal.items];
    }

    function partsText(parts) {
      return parts.map(function (part) {
        return Object.prototype.hasOwnProperty.call(part, 'text')
          ? part.text
          : t(part.msgid, part.params);
      }).join(' · ');
    }

    function statusLabel(status) {
      return {
        cur: t('current step'), done: t('done'), err: t('has errors'),
        skip: t('not needed'), todo: t('still open')
      }[status];
    }

    function canVisit(i, status) {
      if (status === 'cur') {
        return false;
      }
      return !!state.visited[i] || status === 'skip' ||
        !!state.visited[i - 1];
    }

    function updateStepper(values) {
      var errors = allErrors();
      var snapshot = {step: state.step, visited: state.visited, values: values};
      var perStep = [];
      for (var i = 0; i < STEP_COUNT; i++) {
        perStep.push(Rules.stepStatus(i, snapshot, errors));
      }
      stepButtons.forEach(function (items) {
        items.forEach(function (item, i) {
          var status = perStep[i];
          var marker = status === 'done' || status === 'skip' ? '✓'
            : status === 'err' ? '!' : String(i + 1);
          var compactList = items === stepButtons[0];
          var errorCount = status === 'err' ? stepErrors(i).length : 0;
          var subtitle = partsText(Rules.stepSubtitle(
            status, errorCount, Rules.stepSummary(i, values), compactList
          ));
          var key = status + '|' + subtitle + '|' + canVisit(i, status);
          if (item.getAttribute('data-wiz-key') === key) {
            return;
          }
          item.setAttribute('data-wiz-key', key);
          var inner = [
            el('span', 'lt-wiz-stepper__n', marker),
            el('span', 'lt-wiz-stepper__t', t(STEP_TITLES[i])),
            el('span', 'lt-wiz-sr', ' (' + statusLabel(status) + ')')
          ];
          inner[0].setAttribute('aria-hidden', 'true');
          if (subtitle && status !== 'todo') {
            inner[1].appendChild(el('span', 'lt-wiz-stepper__s', subtitle));
          }
          var holder;
          if (canVisit(i, status)) {
            holder = el('button', 'lt-wiz-stepper__btn');
            holder.type = 'button';
            holder.setAttribute('data-wiz-goto', String(i));
          } else {
            holder = el('span', 'lt-wiz-stepper__btn');
          }
          inner.forEach(function (node) { holder.appendChild(node); });
          item.textContent = '';
          item.appendChild(holder);
          item.className = 'lt-wiz-stepper__item is-' + status;
          if (status === 'cur') {
            item.setAttribute('aria-current', 'step');
          } else {
            item.removeAttribute('aria-current');
          }
          if (barsEl && stepButtons[0] === items) {
            barsEl.childNodes[i].className = 'is-' + status;
          }
        });
      });
      compactLabelEl.textContent = t(
        'Step %(n)s of %(total)s', {n: state.step + 1, total: STEP_COUNT}
      );
    }

    function buildNav() {
      backBtn = el('button', 'button');
      backBtn.type = 'button';
      backBtn.setAttribute('data-wiz-back', '');
      nextBtn = el('button', 'button color-primary');
      nextBtn.type = 'button';
      nextBtn.setAttribute('data-wiz-next', '');
      cancelLink = qs(navEl, '[data-wiz-cancel]');
      navEl.insertBefore(nextBtn, navEl.firstChild);
      if (cancelLink) {
        navEl.insertBefore(cancelLink, navEl.firstChild);
      }
      navEl.insertBefore(backBtn, navEl.firstChild);
    }

    function updateNav(values) {
      var last = state.step === REVIEW_STEP;
      setShown(backBtn, state.step !== 0);
      backBtn.textContent = '‹ ' + t('Back');
      if (cancelLink) {
        setShown(cancelLink, state.step === 0);
      }
      setShown(nextBtn, !last);
      if (!last) {
        nextBtn.textContent = t(
          'Continue: %(step)s',
          {step: t(STEP_TITLES[Rules.nextStep(state.step, values)])}
        ) + ' ›';
      }
      setShown(submitBtn, last);
      var blocked = state.submitting || state.image.status === 'uploading';
      if (blocked) {
        submitBtn.setAttribute('aria-disabled', 'true');
      } else {
        submitBtn.removeAttribute('aria-disabled');
      }
      submitBtn.textContent = state.submitting
        ? t('Creating …')
        : t('Create draft tournament');
    }

    function showStep() {
      stepEls.forEach(function (stepEl, i) {
        var current = i === state.step;
        setShown(stepEl, current);
        stepEl.classList.toggle('is-current', current);
      });
    }

    function headingOf(i) {
      return qs(stepEls[i], '[data-wiz-step-heading]');
    }

    function focusHeading() {
      var heading = headingOf(state.step);
      if (heading) {
        heading.focus();
      }
    }

    function goTo(i) {
      state.step = i;
      if (allStepsEl) {
        allStepsEl.open = false;
      }
      state.notices = state.notices.filter(function (notice) {
        if (notice.steps > 0) {
          notice.steps -= 1;
          return true;
        }
        return false;
      });
      renderNotices();
      hideErrsum();
      if (i !== REVIEW_STEP) {
        cancelPrecheck();
      }
      render();
      focusHeading();
      announce(
        t('Step %(n)s of %(total)s', {n: i + 1, total: STEP_COUNT}) + ': ' +
        t(STEP_TITLES[i])
      );
      if (i === REVIEW_STEP) {
        startPrecheck();
      }
    }

    function goNext() {
      var errs = stepErrors(state.step);
      if (errs.length) {
        state.shown[state.step] = true;
        render();
        showErrsum(errs);
        announce(attentionMessage(errs.length));
        return;
      }
      state.visited[state.step] = true;
      goTo(Rules.nextStep(state.step, state.values));
    }

    function goBack() {
      if (!state.visited[state.step]) {
        state.visited[state.step] = stepErrors(state.step).length === 0;
      }
      goTo(Rules.prevStep(state.step, state.values));
    }

    function goToStep(i, force) {
      goTo(i);
      if (i < REVIEW_STEP && (force || state.visited[i])) {
        var first = stepErrors(i)[0];
        if (first) {
          state.shown[i] = true;
          render();
          focusField(first);
        }
      }
    }

    /* ---------- render ---------- */

    function render() {
      var values = readValues();
      state.values = copy(values);
      state.values.reqMap = state.reqMap;
      applyScopes(values);
      state.result = Rules.validate(validationValues(values), rulesContext());
      showStep();
      renderFieldMessages();
      refreshErrsum();
      renderAux(values);
      updateStepper(state.values);
      updateNav(state.values);
      saveSession();
    }

    /* ---------- labels ---------- */

    function ownText(node) {
      var text = '';
      Array.prototype.forEach.call(node.childNodes, function (child) {
        if (child.nodeType === 3) {
          text += child.nodeValue;
        }
      });
      return text.replace(/\s+/g, ' ').trim();
    }

    function labelOf(name) {
      if (name === 'req_map') {
        return t('From the request');
      }
      var wrap = qs(form, '[data-wiz-field="' + name + '"]');
      if (wrap && wrap.hasAttribute('data-wiz-label')) {
        return wrap.getAttribute('data-wiz-label');
      }
      var node = wrap &&
        qs(wrap, 'label.form-label, legend.form-label, div.form-label');
      if (!node && wrap) {
        node = qs(wrap, '.form-check-label span');
      }
      var text = node ? ownText(node) : '';
      return text || name;
    }

    function choiceLabel(field, value) {
      if (isBlank(value)) {
        return '';
      }
      var input = qs(
        form,
        '[data-wiz-choice="' + field + '"] input[value="' + value + '"]'
      );
      var title = input && input.parentNode &&
        qs(input.parentNode, '.lt-wiz-card__title');
      return title ? ownText(title) : String(value);
    }

    /* ---------- field messages ---------- */

    function fieldMessage(field) {
      if (state.server[field] && state.server[field].length) {
        return {kind: 'error', texts: state.server[field]};
      }
      var error = state.result.errors[field];
      if (
        error &&
        (state.touched[field] || state.shown[stepOf(field)])
      ) {
        return {kind: 'error', texts: [tMessage(error)]};
      }
      var warning = state.result.warnings[field];
      if (warning) {
        return {kind: 'warning', texts: [tMessage(warning)]};
      }
      return null;
    }

    // Text with the "Step 3" reference turned into a link to that step.
    function appendLinked(node, text) {
      var label = t('Step %(n)s', {n: 3});
      var at = text.toLowerCase().lastIndexOf(label.toLowerCase());
      if (at === -1) {
        node.appendChild(document.createTextNode(text));
        return;
      }
      node.appendChild(document.createTextNode(text.slice(0, at)));
      var link = el('a', null, text.slice(at, at + label.length));
      link.setAttribute('href', '#');
      link.setAttribute('data-wiz-goto', '2');
      link.setAttribute('data-wiz-force', '');
      node.appendChild(link);
      node.appendChild(document.createTextNode(text.slice(at + label.length)));
    }

    function paintMessages(container, msg) {
      container.textContent = '';
      if (!msg) {
        return;
      }
      if (msg.kind === 'error') {
        var list = el('ol', 'form-errors');
        msg.texts.forEach(function (text) {
          var item = el('li');
          item.appendChild(el('strong', null, t('Error') + ':'));
          item.appendChild(document.createTextNode(' '));
          var body = el('span');
          appendLinked(body, text);
          item.appendChild(body);
          list.appendChild(item);
        });
        container.appendChild(list);
      } else {
        var warning = el('div', 'lt-wiz-fwarn');
        var body = el('span');
        body.appendChild(el('span', 'lt-wiz-sr', t('Note:') + ' '));
        body.appendChild(document.createTextNode(msg.texts[0]));
        warning.appendChild(body);
        container.appendChild(warning);
      }
    }

    function setInvalid(field, invalid) {
      var wrap = qs(form, '[data-wiz-field="' + field + '"]');
      if (wrap) {
        wrap.classList.toggle('invalid', invalid);
      }
      var controls = wrap && wrap.hasAttribute('data-wiz-choice')
        ? [wrap]
        : named(field).filter(function (node) {
          return node.type !== 'radio' && node.type !== 'hidden';
        });
      var errEl = wrap && qs(wrap, '[data-wiz-err-for]');
      var errId = errEl ? errEl.id : '';
      controls.forEach(function (node) {
        if (!node.hasAttribute('data-wiz-dby')) {
          node.setAttribute(
            'data-wiz-dby', node.getAttribute('aria-describedby') || ''
          );
        }
        var base = node.getAttribute('data-wiz-dby');
        var described = invalid && errId ? (base + ' ' + errId).trim() : base;
        if (invalid) {
          node.setAttribute('aria-invalid', 'true');
        } else {
          node.removeAttribute('aria-invalid');
        }
        if (described) {
          node.setAttribute('aria-describedby', described);
        } else {
          node.removeAttribute('aria-describedby');
        }
      });
    }

    function renderFieldMessages() {
      Rules.STEP_FIELDS.forEach(function (fields) {
        fields.forEach(function (field) {
          if (!isManaged(field)) {
            return;
          }
          var msg = fieldMessage(field);
          var key = msg ? msg.kind + '|' + msg.texts.join('\u0001') : '';
          qsa(form, '[data-wiz-err-for="' + field + '"]').forEach(
            function (container) {
              if (container.getAttribute('data-wiz-rendered') !== key) {
                container.setAttribute('data-wiz-rendered', key);
                paintMessages(container, msg);
              }
            }
          );
          setInvalid(field, !!msg && msg.kind === 'error');
        });
      });
    }

    /* ---------- error summary and focus ---------- */

    var errsumEl = qs(document, '[data-wiz-errsum]');
    var errsumAnchor = noticesEl;
    var errsumLive = false;
    var errsumKey = '';
    var errsumTexts = {};

    function hideErrsum() {
      errsumLive = false;
      if (errsumEl && errsumEl.parentNode) {
        errsumEl.parentNode.removeChild(errsumEl);
      }
    }

    function fieldId(field) {
      var wrap = qs(form, '[data-wiz-field="' + field + '"]');
      if (wrap && wrap.hasAttribute('data-wiz-choice')) {
        return wrap.id || field;
      }
      var control = named(field).filter(function (node) {
        return node.type !== 'hidden' && node.type !== 'radio';
      })[0];
      return control && control.id ? control.id : field;
    }

    function focusField(field) {
      var target = null;
      if (field === 'req_map') {
        target = reqMapEl;
      } else if (field === 'point_table') {
        target = qs(form, '.ffa-point-card input') ||
          qs(form, '[data-ffa-add]');
      } else {
        target = document.getElementById(fieldId(field));
      }
      if (target && target.focus) {
        target.focus();
      }
    }

    function errsumText(field) {
      var msg = fieldMessage(field);
      var error = allErrors()[field];
      var text = msg && msg.kind === 'error'
        ? msg.texts[0]
        : error ? tMessage(error) : '';
      if (error && LEAD_SENTENCE_ONLY.indexOf(error.msgid) !== -1) {
        var end = text.indexOf('. ');
        if (end !== -1) {
          text = text.slice(0, end + 1);
        }
      }
      return text;
    }

    // The summary follows the step's errors: rebuilt when the set of fields
    // changes, otherwise only the texts are updated, which a screen reader
    // does not read out again.
    function paintErrsum(fields) {
      var key = fields.join('|');
      if (key === errsumKey && errsumEl.firstChild) {
        fields.forEach(function (field) {
          var node = errsumTexts[field];
          var text = errsumText(field);
          var next = text ? ': ' + text : '';
          if (node && node.nodeValue !== next) {
            node.nodeValue = next;
          }
        });
        return;
      }
      errsumKey = key;
      errsumTexts = {};
      errsumEl.textContent = '';
      errsumEl.appendChild(el(
        'strong', null,
        attentionMessage(fields.length)
      ));
      var list = el('ul');
      fields.forEach(function (field) {
        var item = el('li');
        var link = el('a', null, labelOf(field));
        link.setAttribute('href', '#' + fieldId(field));
        link.setAttribute('data-wiz-focus', field);
        item.appendChild(link);
        var text = errsumText(field);
        var node = document.createTextNode(text ? ': ' + text : '');
        item.appendChild(node);
        errsumTexts[field] = node;
        list.appendChild(item);
      });
      errsumEl.appendChild(list);
    }

    function showErrsum(fields) {
      if (!errsumEl) {
        errsumEl = el('div', 'notification color-danger block');
        errsumEl.setAttribute('role', 'alert');
        errsumEl.setAttribute('aria-relevant', 'additions');
        errsumEl.tabIndex = -1;
        errsumEl.setAttribute('data-wiz-errsum', '');
      }
      if (!errsumEl.parentNode) {
        errsumAnchor.parentNode.insertBefore(
          errsumEl, errsumAnchor.nextSibling
        );
      }
      errsumKey = '';
      errsumLive = true;
      paintErrsum(fields);
      errsumEl.focus();
    }

    // Called on every draw: a fixed field drops out of the summary at once.
    function refreshErrsum() {
      if (!errsumLive || !errsumEl || !errsumEl.parentNode) {
        return;
      }
      var fields = stepErrors(state.step);
      if (fields.length) {
        paintErrsum(fields);
      } else {
        hideErrsum();
      }
    }

    /* ---------- notices and dependent changes ---------- */

    function renderNotices() {
      noticesEl.textContent = '';
      state.notices.forEach(function (notice, index) {
        var box = el('div', 'notification color-warning block lt-wiz-notice');
        box.setAttribute('role', 'status');
        var text = tMessage(notice);
        var end = text.indexOf('. ');
        var body = el('div');
        if (end === -1) {
          body.appendChild(document.createTextNode(text));
        } else {
          body.appendChild(el('strong', null, text.slice(0, end + 1)));
          body.appendChild(document.createTextNode(text.slice(end + 1)));
        }
        box.appendChild(body);
        if (notice.undo) {
          var undo = el('button', 'button is-compact', t('Undo'));
          undo.type = 'button';
          undo.setAttribute('data-wiz-undo', String(index));
          box.appendChild(undo);
        }
        noticesEl.appendChild(box);
      });
    }

    function applyDependent(field, value) {
      var prev = copy(state.values);
      var result = Rules.dependentChange(
        prev, field, value, {validCombinations: config.validCombinations}
      );
      Object.keys(result.values).forEach(function (key) {
        if (key === 'reqMap') {
          state.reqMap = result.values.reqMap || null;
        } else if (result.values[key] !== prev[key]) {
          writeValue(key, result.values[key]);
        }
      });
      state.touched[field] = true;
      state.notices = result.notices;
      state.notices.forEach(function (notice) {
        notice.steps = NOTICE_STEPS;
      });
      renderNotices();
      render();
      if (result.notices.length) {
        announce(tMessage(result.notices[0]));
      }
    }

    function undoNotice(index) {
      var notice = state.notices[index];
      if (!notice || !notice.undo) {
        return;
      }
      Object.keys(notice.undo).forEach(function (key) {
        if (key === 'reqMap') {
          state.reqMap = notice.undo.reqMap || null;
        } else {
          writeValue(key, notice.undo[key]);
        }
      });
      state.notices = [];
      renderNotices();
      render();
      announce(t('Change undone.'));
    }

    /* ---------- request mapping box ---------- */

    // Text whose parts between BOLD_ON and BOLD_OFF are set in bold.
    function setRichText(node, text) {
      node.textContent = '';
      var bold = false;
      text.split(/([\u0001\u0002])/).forEach(function (part) {
        if (part === BOLD_ON) {
          bold = true;
        } else if (part === BOLD_OFF) {
          bold = false;
        } else if (part !== '') {
          node.appendChild(
            bold ? el('strong', null, part) : document.createTextNode(part)
          );
        }
      });
    }

    function boldValue(value) {
      return BOLD_ON + value + BOLD_OFF;
    }

    function requestMappingLine(values) {
      var request = config.request;
      var params = {
        team_size: request.teamSize, limit: request.participantLimit
      };
      return values.contestant_type === 'TEAM'
        ? t(
          'Team size %(team_size)s → min. and max. players per team = ' +
          '%(team_size)s · participant limit %(limit)s → max. teams = ' +
          '%(limit)s',
          params
        )
        : t('Participant limit %(limit)s → max. players = %(limit)s', params);
    }

    function paintReqMap(values) {
      if (!reqMapEl) {
        return;
      }
      var showBox = !!config.request && !state.unlinked;
      setShown(reqMapEl, showBox);
      if (!showBox) {
        return;
      }
      var staticEl = qs(reqMapEl, '[data-wiz-reqmap-static]');
      if (staticEl) {
        setShown(staticEl, false);
      }
      var box = qs(reqMapEl, '[data-wiz-reqmap-live]');
      if (!box) {
        box = el('div');
        box.setAttribute('data-wiz-reqmap-live', '');
        reqMapEl.insertBefore(box, reqMapEl.firstChild);
      }
      var key = [state.reqMap, values.contestant_type].join('|');
      if (box.getAttribute('data-wiz-rendered') === key) {
        return;
      }
      box.setAttribute('data-wiz-rendered', key);
      box.textContent = '';
      var request = config.request;
      var team = values.contestant_type === 'TEAM';
      box.appendChild(el(
        'div', 'data-label', t('From the request, not yet mapped')
      ));
      var value = el('div', 'data-value');
      setRichText(value, t(
        'Team size %(team_size)s · participant limit %(limit)s',
        {
          team_size: boldValue(request.teamSize),
          limit: boldValue(request.participantLimit)
        }
      ));
      box.appendChild(value);
      box.appendChild(el(
        'div', 'form-caption',
        t('These values depend on Solo or Teams, so they are not applied ' +
          'automatically.') + ' ' +
        t('Suggestion for %(type)s: %(mapping)s.', {
          type: team ? t('Teams') : t('Solo'),
          mapping: requestMappingLine(values)
        })
      ));
      if (team && request.teamSize === 1) {
        var warning = el('div', 'lt-wiz-fwarn');
        warning.appendChild(el('span', null, t(
          'The request asks for team size 1, so rather Solo. Check your ' +
          'choice in step 2.'
        )));
        box.appendChild(warning);
      }
      function change(text) {
        var line = el('div', 'data-value', text + ' ');
        var button = el('button', 'lt-wiz-linkbtn', t('Change decision'));
        button.type = 'button';
        button.setAttribute('data-wiz-req-change', '');
        line.appendChild(button);
        box.appendChild(line);
      }
      if (state.reqMap === 'applied') {
        change(t('✓ Applied. You can still adjust the fields below.'));
      } else if (state.reqMap === 'declined') {
        change(t('Not applied. You set the limits yourself.'));
      } else {
        var buttons = el('div', 'button-row');
        [
          ['data-wiz-req-apply', t('Apply'), true],
          ['data-wiz-req-decline', t("Don't apply"), false]
        ].forEach(function (action) {
          var button = el(
            'button', 'button is-compact' + (action[2] ? ' color-primary' : ''),
            action[1]
          );
          button.type = 'button';
          button.setAttribute(action[0], '');
          buttons.appendChild(button);
        });
        box.appendChild(buttons);
      }
    }

    function setRequestDecision(values) {
      Object.keys(values).forEach(function (key) {
        if (key === 'reqMap') {
          state.reqMap = values.reqMap;
        } else if (
          SOLO_COUNTS.concat(TEAM_COUNTS).indexOf(key) !== -1
        ) {
          writeValue(key, values[key]);
        }
      });
      render();
    }

    /* ---------- counters, capacity, group unit, preview ---------- */

    function intOf(raw) {
      return /^\s*\d+\s*$/.test(String(raw)) ? parseInt(raw, 10) : null;
    }

    var numberFormat = null;

    function formatNumber(value) {
      if (numberFormat === null) {
        try {
          numberFormat = new Intl.NumberFormat(config.locale || undefined);
        } catch (e) {
          numberFormat = {format: String};
        }
      }
      return numberFormat.format(value);
    }

    function paintCounters(values) {
      qsa(form, '[data-wiz-count-for]').forEach(function (node) {
        var field = node.getAttribute('data-wiz-count-for');
        var max = limits[COUNT_LIMIT_KEYS[field]];
        var length = chars(values[field]);
        node.textContent = formatNumber(length) + ' / ' + formatNumber(max);
        node.classList.toggle('over', length > max);
      });
    }

    function capacityText(scope, values, seats) {
      if (scope === 'solo') {
        var players = intOf(values.max_players);
        return players
          ? t('Up to %(n)s of %(seats)s party seats.',
            {n: boldValue(players), seats: seats})
          : t('No limit: up to %(seats)s party seats.', {seats: seats});
      }
      var teams = intOf(values.max_teams);
      var perTeam = intOf(values.max_players_in_team);
      return teams && perTeam
        ? t('Up to %(t)s teams × %(p)s players = %(n)s of %(seats)s ' +
          'party seats.',
          {t: BOLD_ON + teams, p: perTeam, n: teams * perTeam + BOLD_OFF,
            seats: seats})
        : t('Without max. teams and max. players per team there is no ' +
          'limit (party: %(seats)s seats).', {seats: seats});
    }

    function paintCapacity(values) {
      var seats = config.capacity;
      if (seats === null || seats === undefined) {
        return;
      }
      qsa(form, '[data-wiz-capacity]').forEach(function (node) {
        var holder = node.closest('[data-wiz-scope]');
        var scope = holder ? holder.getAttribute('data-wiz-scope') : 'solo';
        var contestant = scope === 'solo' ? 'SOLO' : 'TEAM';
        if (values.contestant_type !== contestant) {
          return;
        }
        var text = capacityText(scope, values, seats);
        if (node.getAttribute('data-wiz-text') !== text) {
          node.setAttribute('data-wiz-text', text);
          setRichText(node, text);
        }
      });
    }

    function paintGroupUnit(values) {
      var legend = qs(form, '.lt-wiz-groups > legend');
      var caption = document.getElementById('group_size_max-cap');
      [legend, caption].forEach(function (node) {
        if (node && node.getAttribute('data-wiz-default') === null) {
          node.setAttribute('data-wiz-default', node.textContent);
        }
      });
      var unit = values.contestant_type === 'TEAM' ? t('teams')
        : values.contestant_type === 'SOLO' ? t('players') : null;
      if (legend) {
        legend.textContent = unit
          ? t('Groups · size in %(unit)s', {unit: unit})
          : legend.getAttribute('data-wiz-default');
      }
      if (caption) {
        caption.textContent = unit
          ? t('At most this many %(unit)s play at once.', {unit: unit})
          : caption.getAttribute('data-wiz-default');
      }
    }

    // A, B, ... Z, AA, AB, ...
    function letters(index) {
      var name = '';
      var n = index + 1;
      while (n > 0) {
        var rest = (n - 1) % 26;
        name = String.fromCharCode(65 + rest) + name;
        n = Math.floor((n - 1) / 26);
      }
      return name;
    }

    function paintPreview(values) {
      if (!previewEl) {
        return;
      }
      var hint = qs(previewEl, '.empty-hint');
      var live = qs(previewEl, '[data-wiz-preview-live]');
      var groupMax = intOf(values.group_size_max);
      var active = values.game_format === 'FREE_FOR_ALL' && groupMax !== null &&
        groupMax >= 2 && !state.result.errors.group_size_max;
      var key = active
        ? [groupMax, values.advancement_count, values.point_table,
          values.contestant_type, values.elimination_mode].join('|')
        : '';
      if (live && live.getAttribute('data-wiz-rendered') === key) {
        return;
      }
      if (live) {
        previewEl.removeChild(live);
      }
      if (hint) {
        setShown(hint, !active);
      }
      if (!active) {
        return;
      }
      live = el('div');
      live.setAttribute('data-wiz-preview-live', '');
      live.setAttribute('data-wiz-rendered', key);
      var team = values.contestant_type === 'TEAM';
      var unit = team ? t('teams') : t('players');
      var advance = intOf(values.advancement_count);
      if (advance === null || state.result.errors.advancement_count) {
        advance = 0;
      }
      var points = String(values.point_table || '').split(',')
        .map(function (item) { return item.trim(); })
        .filter(function (item) { return item !== ''; });
      var double = values.elimination_mode === 'DOUBLE_ELIMINATION';

      var table = el('table', 'index');
      var caption = advance === 0
        ? t('Sample group with %(n)s %(unit)s', {n: groupMax, unit: unit})
        : advance === 1
          ? t('Sample group with %(n)s %(unit)s, 1 advances',
            {n: groupMax, unit: unit})
          : t('Sample group with %(n)s %(unit)s, %(k)s advance',
            {n: groupMax, unit: unit, k: advance});
      table.appendChild(el('caption', null, caption));
      var head = el('tr');
      [
        [t('Place'), null],
        [team ? t('Team') : t('Player'), null],
        [t('Points'), 'number'],
        [t('Result'), null]
      ].forEach(function (column) {
        head.appendChild(el('th', column[1], column[0]));
      });
      table.appendChild(el('thead')).appendChild(head);
      var body = el('tbody');
      var rows = Math.min(groupMax, PREVIEW_MAX_ROWS);
      for (var i = 0; i < rows; i++) {
        var row = el('tr', i < advance ? 'adv' : null);
        row.appendChild(el('td', null, (i + 1) + '.'));
        row.appendChild(el('td', null, team
          ? t('Team %(letter)s', {letter: letters(i)})
          : t('Player %(n)s', {n: i + 1})));
        var place = i < points.length ? points[i] : '0';
        row.appendChild(el(
          'td', 'number', /^-?\d+$/.test(place) ? place : '?'
        ));
        var result = el('td');
        if (i < advance) {
          result.appendChild(el('span', 'tag color-info', '▲ ' + t('advances')));
        } else {
          result.appendChild(el(
            'span', 'dimmed',
            double ? t('eliminated or losers bracket') : t('eliminated')
          ));
        }
        row.appendChild(result);
        body.appendChild(row);
      }
      table.appendChild(body);
      live.appendChild(table);
      previewEl.appendChild(live);
    }

    function sameText(a, b) {
      return String(a === null || a === undefined ? '' : a)
        .replace(/\r\n/g, '\n') === String(b).replace(/\r\n/g, '\n');
    }

    // A field keeps its "from request" tag only while it is unchanged.
    function paintSourceTags(values) {
      var source = config.sourceValues;
      if (!source) {
        return;
      }
      qsa(form, '[data-wiz-src-tag]').forEach(function (tag) {
        var field = tag.getAttribute('data-wiz-src-field');
        var original = source[field];
        setShown(tag, !!original && sameText(values[field], original));
      });
    }

    // Choice cards: selection marker, mode area, reasons and examples.
    function paintChoices(values) {
      qsa(form, '[data-wiz-selected-sr]').forEach(function (node) {
        var input = qs(node.closest('label'), 'input');
        node.hidden = !(input && input.checked);
      });

      var modeEl = qs(form, '[data-wiz-choice="elimination_mode"]');
      if (modeEl) {
        var area = Rules.modeArea(values.game_format, config.validCombinations);
        var cards = area.view === 'cards';
        var grid = qs(modeEl, '.lt-wiz-cards__grid');
        [
          [qs(modeEl, '[data-wiz-hint]'), area.view === 'hint'],
          [qs(modeEl, '[data-wiz-fixed]'), area.view === 'fixed'],
          [grid, cards],
          [qs(modeEl, '[data-wiz-intro]'), cards],
          [qs(modeEl, 'legend .req-mark'), area.view !== 'fixed']
        ].forEach(function (part) {
          if (part[0]) {
            setShown(part[0], part[1]);
          }
        });
        area.modes.forEach(function (choice) {
          var input = qs(grid, 'input[value="' + choice.mode + '"]');
          if (!input) {
            return;
          }
          input.disabled = !choice.available;
          var card = input.closest('label');
          qsa(card, '[data-wiz-why]').forEach(function (why) {
            setShown(why, cards && !choice.available);
          });
          qsa(card, '[data-wiz-ex]').forEach(function (example) {
            setShown(
              example,
              cards && choice.available &&
                example.getAttribute('data-wiz-ex') === values.game_format
            );
          });
        });
      }

      var modeNode = qs(form, '[data-wiz-1v1-mode]');
      if (modeNode) {
        var label = Rules.summaryLabel(values.elimination_mode);
        modeNode.textContent = label.length ? t(label[0].msgid) : '–';
        qs(form, '[data-wiz-1v1-tail]').textContent = t(
          values.elimination_mode === 'ROUND_ROBIN'
            ? 'The table results from wins and losses.'
            : 'The bracket is generated before the start.'
        );
      }
    }

    function renderAux(values) {
      paintChoices(values);
      paintSourceTags(values);
      paintCounters(values);
      paintCapacity(values);
      paintGroupUnit(values);
      paintPreview(values);
      paintReqMap(values);
    }

    /* ---------- events: fields, navigation ---------- */

    function markEdited(field) {
      delete state.server[field];
    }

    form.addEventListener('input', function (event) {
      var name = event.target.name;
      if (!name || event.target.type === 'radio') {
        return;
      }
      markEdited(name);
      render();
    });

    form.addEventListener('change', function (event) {
      var target = event.target;
      var name = target.name;
      if (!name || target.type === 'file') {
        return;
      }
      markEdited(name);
      if (target.type === 'radio') {
        if (name === 'contestant_type' || name === 'game_format') {
          applyDependent(name, target.value);
          return;
        }
        state.touched[name] = true;
      }
      if (target.type === 'radio' || target.type === 'checkbox') {
        render();
      } else {
        renderAfterPointer();
      }
    });

    form.addEventListener('focusout', function (event) {
      var name = event.target.name;
      if (!name || event.target.type === 'radio') {
        return;
      }
      if (!state.touched[name]) {
        state.touched[name] = true;
        renderAfterPointer();
      }
    });

    // A blur caused by a press elsewhere must not reflow the page before
    // the release, or press and release hit different elements and no click
    // fires. Paint after the click instead.
    // A touch tap moves focus on its compatibility mousedown, after
    // pointerup, so the mouse events count as a press too. A press that is
    // never released on the page must not hold renders back: keyboard input,
    // a context menu, a lost window focus and a move with no button held
    // all end it.
    var pointer = {down: false, pending: false};
    function flushPointerRender() {
      pointer.down = false;
      if (pointer.pending) {
        pointer.pending = false;
        render();
      }
    }
    ['pointerdown', 'mousedown'].forEach(function (type) {
      document.addEventListener(type, function () {
        pointer.down = true;
      }, true);
    });
    ['pointerup', 'mouseup'].forEach(function (type) {
      document.addEventListener(type, function () {
        setTimeout(flushPointerRender, 0);
      }, true);
    });
    document.addEventListener('pointercancel', flushPointerRender, true);
    // A release the page never saw shows up as a move with no button held.
    document.addEventListener('pointermove', function (event) {
      if (pointer.down && event.buttons === 0) {
        flushPointerRender();
      }
    }, true);
    document.addEventListener('keydown', flushPointerRender, true);
    document.addEventListener('contextmenu', flushPointerRender, true);
    window.addEventListener('blur', flushPointerRender);
    function renderAfterPointer() {
      if (pointer.down) {
        pointer.pending = true;
      } else {
        render();
      }
    }

    page.addEventListener('click', function (event) {
      var target = event.target.closest ? event.target : event.target.parentNode;
      var hit = target.closest(
        '[data-wiz-next], [data-wiz-back], [data-wiz-goto], ' +
        '[data-wiz-focus], [data-wiz-undo], [data-wiz-req-apply], ' +
        '[data-wiz-req-decline], [data-wiz-req-change], ' +
        '[data-wiz-unlink]'
      );
      if (!hit || !page.contains(hit)) {
        return;
      }
      if (hit.hasAttribute('data-wiz-next')) {
        goNext();
      } else if (hit.hasAttribute('data-wiz-back')) {
        goBack();
      } else if (hit.hasAttribute('data-wiz-goto')) {
        event.preventDefault();
        goToStep(
          parseInt(hit.getAttribute('data-wiz-goto'), 10),
          hit.hasAttribute('data-wiz-force')
        );
      } else if (hit.hasAttribute('data-wiz-focus')) {
        event.preventDefault();
        focusField(hit.getAttribute('data-wiz-focus'));
      } else if (hit.hasAttribute('data-wiz-undo')) {
        undoNotice(parseInt(hit.getAttribute('data-wiz-undo'), 10));
      } else if (hit.hasAttribute('data-wiz-req-apply')) {
        setRequestDecision(
          Rules.applyRequestValues(readValues(), config.request)
        );
        announce(t('Applied'));
      } else if (hit.hasAttribute('data-wiz-req-decline')) {
        setRequestDecision(
          Rules.declineRequestValues(readValues(), config.request)
        );
      } else if (hit.hasAttribute('data-wiz-req-change')) {
        state.reqMap = null;
        render();
      } else if (hit.hasAttribute('data-wiz-unlink')) {
        unlinkRequest();
      }
    });

    /* ---------- review ---------- */

    function hasOwn(object, key) {
      return Object.prototype.hasOwnProperty.call(object, key);
    }

    // A translated pattern whose parameters may be strings or nodes.
    function formatNodes(msgid, params) {
      var out = document.createDocumentFragment();
      t(msgid).split(/(%\(\w+\)s)/).forEach(function (part) {
        var match = /^%\((\w+)\)s$/.exec(part);
        if (match && hasOwn(params, match[1])) {
          var value = params[match[1]];
          out.appendChild(
            value && value.nodeType
              ? value
              : document.createTextNode(String(value))
          );
        } else if (part !== '') {
          out.appendChild(document.createTextNode(part));
        }
      });
      return out;
    }

    function clipNodes(text) {
      if (text === '') {
        return '';
      }
      var list = Array.from(text);
      if (list.length <= 140) {
        return text;
      }
      var out = document.createDocumentFragment();
      out.appendChild(document.createTextNode(list.slice(0, 140).join('') +
        ' …'));
      out.appendChild(document.createTextNode(' '));
      out.appendChild(el('span', 'dimmed', '(' + t(
        '%(n)s characters', {n: formatNumber(list.length)}
      ) + ')'));
      return out;
    }

    function limitNode(raw, none) {
      return isBlank(raw)
        ? el('span', 'dimmed', none)
        : document.createTextNode(String(raw).trim());
    }

    function startNodes(raw) {
      var parts = Rules.startParts(raw);
      if (!parts) {
        return String(raw || '').trim();
      }
      var date = parts.day + '.' + parts.month + '.' + parts.year;
      try {
        date = new Intl.DateTimeFormat(config.locale || undefined, {
          timeZone: 'UTC', day: '2-digit', month: '2-digit',
          year: 'numeric'
        }).format(new Date(Date.UTC(
          +parts.year, +parts.month - 1, +parts.day
        )));
      } catch (e) {
        // Keep the plain day.month.year form.
      }
      var out = formatNodes('%(date)s, %(time)s', {
        date: date, time: parts.time
      });
      if (config.timezoneName) {
        var zone = config.timezoneName + (
          config.timezoneDetail ? ', ' + config.timezoneDetail : ''
        );
        out.appendChild(document.createTextNode(' '));
        out.appendChild(el('span', 'dimmed', '(' + zone + ')'));
      }
      return out;
    }

    function safeUrl(value) {
      if (
        typeof value !== 'string' || value.charAt(0) !== '/'
        || /[\u0000-\u001f\u007f]/.test(value)
      ) {
        return null;
      }
      var url;
      try {
        url = new URL(value, location.href);
      } catch (e) {
        return null;
      }
      return url.origin === location.origin && url.pathname.charAt(0) === '/'
        ? value
        : null;
    }

    function imageCell(alt) {
      var image = state.image;
      if (image.status === 'done') {
        var cell = el('div', 'lt-wiz-review__image');
        var thumbUrl = safeUrl(image.url);
        if (thumbUrl) {
          var thumb = el('img');
          thumb.setAttribute('alt', '');
          thumb.setAttribute('src', thumbUrl);
          cell.appendChild(thumb);
        }
        var text = el('div');
        text.appendChild(document.createTextNode(image.filename || ''));
        var meta = [];
        if (image.width && image.height) {
          meta.push(image.width + ' × ' + image.height);
        }
        meta.push(alt
          ? t('Description: "%(alt)s"', {alt: alt})
          : t('decorative, no description'));
        text.appendChild(el('div', 'dimmed lt-wiz-review__meta',
          meta.join(' · ')));
        cell.appendChild(text);
        return cell;
      }
      if (image.status === 'uploading') {
        return el('span', 'lt-wiz-fwarn', t(
          'Uploading … %(pct)s · You can continue meanwhile.',
          {pct: (image.pct || 0) + '%'}
        ));
      }
      var url = String(state.values.image_url || '').trim();
      return url === '' ? null : t('URL: %(url)s', {url: url});
    }

    function requestNumber(raw) {
      return ('0000' + raw).slice(-4);
    }

    // The refusal in force: from the last pre-check, else from the server.
    function currentRefusal() {
      if (state.unlinked) {
        return null;
      }
      return (state.check && state.check.refusal) || config.refusal || null;
    }

    function requestOrigin() {
      if (state.unlinked) {
        return null;
      }
      var refusal = currentRefusal();
      if (refusal) {
        return {
          id: refusal.number, user: refusal.proposerName, linked: false,
          request: config.request || refusal.request
        };
      }
      return config.request
        ? {
          id: requestNumber(config.request.number),
          user: config.request.proposerName, linked: true,
          request: config.request
        }
        : null;
    }

    function reviewRows(values) {
      var scopes = Rules.applicableScopes(values);
      var sections = [];
      var errors = allErrors();

      function row(field, label, value) {
        var invalid = !!field && hasOwn(errors, field);
        return {label: label || labelOf(field), value: value,
          invalid: invalid, error: invalid ? errsumText(field) : ''};
      }

      function rangeRow(label, minField, maxField, noMinimum) {
        var bad = [minField, maxField].filter(function (field) {
          return hasOwn(errors, field);
        });
        return {
          label: label,
          value: formatNodes('%(min)s to %(max)s', {
            min: limitNode(values[minField], noMinimum),
            max: limitNode(values[maxField], t('no limit'))
          }),
          invalid: bad.length > 0,
          error: bad.length ? errsumText(bad[0]) : ''
        };
      }

      var alt = String(values.image_alt_text || '').trim();
      sections.push({step: 0, title: t(STEP_TITLES[0]), rows: [
        row('name', null, String(values.name || '').trim()),
        row('game', null, String(values.game || '').trim()),
        row('start_time', null, startNodes(values.start_time)),
        row('description', null, clipNodes(String(values.description || '')
          .trim())),
        row('ruleset', null, clipNodes(String(values.ruleset || '').trim())),
        row(null, t('Tournament image'), imageCell(alt))
      ]});

      var mode = choiceLabel('elimination_mode', values.elimination_mode);
      var modeValue = mode;
      if (mode && values.game_format === 'HIGHSCORE') {
        modeValue = document.createDocumentFragment();
        modeValue.appendChild(document.createTextNode(mode + ' '));
        modeValue.appendChild(el('span', 'dimmed',
          '(' + t('automatic') + ')'));
      }
      sections.push({step: 1, title: t(STEP_TITLES[1]), rows: [
        row('contestant_type', null,
          choiceLabel('contestant_type', values.contestant_type)),
        row('game_format', null,
          choiceLabel('game_format', values.game_format)),
        row('elimination_mode', null, modeValue)
      ]});

      var origin = requestOrigin();
      var people = [];
      if (scopes.solo) {
        people.push(rangeRow(
          t('Players'), 'min_players', 'max_players', t('no minimum')));
      } else if (scopes.team) {
        people.push(rangeRow(
          t('Teams'), 'min_teams', 'max_teams', t('no minimum')));
        people.push(rangeRow(
          t('Players per team'), 'min_players_in_team',
          'max_players_in_team', t('no minimum')));
      }
      if (origin && values.contestant_type) {
        var decision = config.request
          ? state.reqMap
          : Rules.initialReqMap(values, origin.request);
        people.push(row(
          'req_map', t('Values from the request'),
          decision === 'applied' ? t('applied')
            : decision === 'declined' ? t('not applied') : ''
        ));
      }
      if (people.length) {
        sections.push({step: 2, title: t(STEP_TITLES[2]), rows: people});
      }

      if (scopes.oneVOne) {
        sections.push({step: 3, title: t('Scoring'), rows: [
          row(null, t('Scoring'),
            t('Winner per match, no scoring settings needed'))
        ]});
      } else if (scopes.highscore) {
        sections.push({step: 3, title: t('Scoring'), rows: [row(
          'score_ordering', null,
          choiceLabel('score_ordering', values.score_ordering)
        )]});
      } else if (scopes.ffa) {
        var places = String(values.point_table || '').split(',')
          .map(function (item) { return item.trim(); })
          .filter(function (item) { return item !== ''; });
        var points = null;
        if (places.length) {
          points = el('span', 'ptl');
          places.forEach(function (item, i) {
            var place = el('span');
            place.appendChild(el('b', null, (i + 1) + '.'));
            place.appendChild(document.createTextNode(' ' + item));
            points.appendChild(place);
          });
        }
        var unit = values.contestant_type === 'TEAM'
          ? t('teams') : t('players');
        var minSize = String(values.group_size_min || '').trim();
        var size = formatNodes('%(min)s to %(max)s %(unit)s', {
          min: minSize === '' ? '2' : minSize,
          max: String(values.group_size_max || '').trim() || '?',
          unit: unit
        });
        if (minSize === '') {
          size.appendChild(document.createTextNode(' '));
          size.appendChild(el('span', 'dimmed', '(' +
            t('Minimum not set, technically 2') + ')'));
        }
        var sizeBad = ['group_size_min', 'group_size_max'].filter(
          function (field) { return hasOwn(errors, field); }
        );
        var scoring = [
          row('point_table', t('Points by placement'), points),
          {label: t('Group size'), value: size, invalid: sizeBad.length > 0,
            error: sizeBad.length ? errsumText(sizeBad[0]) : ''},
          row('advancement_count', t('Advancing per group'),
            String(values.advancement_count || '').trim())
        ];
        if (scopes.ffaDe) {
          scoring.push(row(
            'points_carry_to_losers', t('Points in the losers bracket'),
            values.points_carry_to_losers
              ? t('Yes, carried over')
              : t('No, the losers bracket counts separately')
          ));
        }
        sections.push({step: 3, title: t(STEP_TITLES[3]), rows: scoring});
      }

      if (origin) {
        var originParams = {id: origin.id, user: origin.user || t('Unknown')};
        sections.push({step: null, title: t('Origin'), rows: [{
          label: t('Origin request'),
          value: origin.linked
            ? t('#%(id)s by %(user)s · will be linked after creation',
              originParams)
            : t('#%(id)s by %(user)s · will not be linked', originParams)
        }]});
      }
      return sections;
    }

    function reviewSection(section) {
      var box = el('section', 'lt-wiz-review__sec');
      if (section.step === null) {
        box.appendChild(el('h3', null, section.title));
      } else {
        var head = el('div', 'lt-wiz-review__head');
        head.appendChild(el('h3', null, section.title));
        var edit = el('button', 'lt-wiz-linkbtn', t('Edit'));
        edit.appendChild(el('span', 'lt-wiz-sr', ' ' + section.title));
        edit.type = 'button';
        edit.setAttribute('data-wiz-goto', String(section.step));
        head.appendChild(edit);
        box.appendChild(head);
      }
      var list = el('dl', 'lt-wiz-review__dl');
      section.rows.forEach(function (item) {
        list.appendChild(el('dt', null, item.label));
        var cell = el('dd', item.invalid ? 'is-invalid' : null);
        if (item.value && item.value.nodeType) {
          cell.appendChild(item.value);
        } else if (item.value) {
          cell.textContent = item.value;
        } else {
          cell.appendChild(el('span', 'dimmed', t('Not set')));
        }
        if (item.invalid && item.error) {
          var problem = el('div', 'lt-wiz-review__err');
          problem.appendChild(el('span', null, '⚠'));
          problem.lastChild.setAttribute('aria-hidden', 'true');
          problem.appendChild(el('span', null, item.error));
          cell.appendChild(problem);
        }
        list.appendChild(cell);
      });
      box.appendChild(list);
      return box;
    }

    function plainCheckErrors() {
      var found = [];
      var errors = (state.check && state.check.errors) || {};
      var refused = !!currentRefusal();
      Object.keys(errors).forEach(function (field) {
        if (
          (field === 'from_request_id' && refused) ||
          (field !== '' && field !== 'from_request_id' && isManaged(field))
        ) {
          return;
        }
        errors[field].forEach(function (text) { found.push(text); });
      });
      return found;
    }

    function statusBox(className, role) {
      var box = el('div', 'notification block' + (className ? ' ' + className : ''));
      box.setAttribute('role', role);
      return box;
    }

    // The refusal box; the server draws the same one when it re-renders.
    function refusalBox(refusal) {
      var box = el('div', 'notification color-danger block');
      box.setAttribute('role', 'alert');
      box.setAttribute('tabindex', '-1');
      box.setAttribute('data-wiz-err-for', 'review');
      box.setAttribute('data-wiz-refusal', '');
      box.appendChild(el('strong', null, refusal.lead));
      box.appendChild(document.createTextNode(
        ' ' + (refusal.detail ? refusal.detail + ' ' : '') + refusal.closing
      ));
      var actions = el('div', 'button-row');
      var unlink = el('button', 'button is-compact', t(
        'Create without link to the request'
      ));
      unlink.type = 'button';
      unlink.setAttribute('data-wiz-unlink', '');
      actions.appendChild(unlink);
      var view = el('a', 'button is-compact', t('View request'));
      view.setAttribute('href', safeUrl(refusal.viewUrl) || '#');
      view.setAttribute('target', '_blank');
      view.setAttribute('rel', 'noopener noreferrer');
      actions.appendChild(view);
      box.appendChild(actions);
      return box;
    }

    function renderReview() {
      reviewEl.textContent = '';
      var check = state.check;
      var refusal = currentRefusal();
      if (refusal && !serverRefusalEl) {
        reviewEl.appendChild(refusalBox(refusal));
      }

      var fields = allErrorFields();
      var plain = plainCheckErrors();
      if (fields.length || plain.length) {
        var errBox = el('div', 'notification color-danger block');
        errBox.setAttribute('role', 'alert');
        errBox.appendChild(el(
          'strong', null, attentionMessage(fields.length + plain.length)
        ));
        var list = el('ul');
        fields.forEach(function (field) {
          var item = el('li');
          var link = el(
            'a', null, t(STEP_TITLES[stepOf(field)]) + ' › ' + labelOf(field)
          );
          link.setAttribute('href', '#');
          link.setAttribute('data-wiz-goto', String(stepOf(field)));
          link.setAttribute('data-wiz-force', '');
          item.appendChild(link);
          list.appendChild(item);
        });
        plain.forEach(function (text) {
          list.appendChild(el('li', null, text));
        });
        errBox.appendChild(list);
        reviewEl.appendChild(errBox);
      } else {
        var banner = Rules.reviewBanner(check, !!refusal);
        if (banner === 'pending') {
          var wait = statusBox('', 'status');
          var spin = el('span', 'lt-wiz-spin');
          spin.setAttribute('aria-hidden', 'true');
          wait.appendChild(spin);
          wait.appendChild(document.createTextNode(t(
            'The server is checking your entries …'
          )));
          reviewEl.appendChild(wait);
        } else if (banner === 'offline') {
          var off = statusBox('color-warning', 'status');
          off.appendChild(el('strong', null, t('Pre-check unavailable right now.')));
          off.appendChild(document.createTextNode(' ' + t(
            'You can still create: the server checks everything again. ' +
            'Your entries are kept.'
          )));
          reviewEl.appendChild(off);
        } else if (banner === 'ok') {
          var ok = statusBox('color-success', 'status');
          ok.appendChild(el('strong', null, '✓ ' + t('All set.')));
          ok.appendChild(document.createTextNode(' ' + t(
            'The server checked your entries at %(time)s.',
            {time: check.checkedAt || ''}
          )));
          reviewEl.appendChild(ok);
        }
      }

      reviewRows(state.values).forEach(function (section) {
        reviewEl.appendChild(reviewSection(section));
      });
      var info = el('div', 'notification color-info');
      info.appendChild(formatNodes(
        'The tournament is created as %(draft)s. Registration stays ' +
        'closed until you open it on the tournament page.',
        {draft: el('strong', null, t('a draft'))}
      ));
      reviewEl.appendChild(info);
    }

    /* ---------- server pre-check ---------- */

    function cancelPrecheck() {
      state.checkSeq += 1;
      if (state.checkAbort) {
        state.checkAbort.abort();
        state.checkAbort = null;
      }
    }

    function applyCheck(data) {
      var errors = {};
      var given = (data && data.errors) || {};
      Object.keys(given).forEach(function (field) {
        var list = Array.isArray(given[field]) ? given[field] : [given[field]];
        errors[field] = list.map(String);
      });
      state.server = {};
      Object.keys(errors).forEach(function (field) {
        if (field !== '') {
          state.server[field] = errors[field];
        }
      });
      state.check = {
        status: data && data.ok ? 'ok' : 'errors',
        errors: errors,
        checkedAt: data && data.checked_at,
        refusal: (data && data.refusal) || null
      };
      render();
      renderReview();
    }

    function startPrecheck() {
      cancelPrecheck();
      if (config.refusal && !state.unlinked) {
        state.check = null;
        renderReview();
        return;
      }
      if (allErrorFields().length) {
        state.check = null;
        renderReview();
        return;
      }
      var seq = state.checkSeq;
      var url = config.urls && config.urls.validate;
      if (!url || typeof fetch !== 'function') {
        state.check = {status: 'offline'};
        renderReview();
        return;
      }
      state.check = {status: 'pending'};
      renderReview();
      var controller = typeof AbortController === 'function'
        ? new AbortController()
        : null;
      state.checkAbort = controller;
      var timer = setTimeout(function () {
        if (controller) {
          controller.abort();
        }
      }, PRECHECK_TIMEOUT_MS);
      var body = new FormData(form);
      body.delete(fileInput && fileInput.name ? fileInput.name : 'image');
      var options = {
        method: 'POST',
        body: body,
        credentials: 'same-origin',
        headers: {Accept: 'application/json'}
      };
      if (controller) {
        options.signal = controller.signal;
      }
      fetch(url, options).then(function (response) {
        if (!response.ok) {
          throw new Error('pre-check status ' + response.status);
        }
        return response.json();
      }).then(function (data) {
        clearTimeout(timer);
        if (seq === state.checkSeq) {
          state.checkAbort = null;
          applyCheck(data);
        }
      }).catch(function () {
        clearTimeout(timer);
        if (seq === state.checkSeq) {
          state.checkAbort = null;
          state.check = {status: 'offline'};
          renderReview();
        }
      });
    }

    function unlinkRequest() {
      state.unlinked = true;
      writeValue('from_request_id', '');
      delete state.server.from_request_id;
      state.reqMap = null;
      if (serverRefusalEl && serverRefusalEl.parentNode) {
        serverRefusalEl.parentNode.removeChild(serverRefusalEl);
      }
      serverRefusalEl = null;
      render();
      startPrecheck();
      announce(t('Link to the request removed.'));
      focusHeading();
    }

    /* ---------- image state, submit ---------- */

    document.addEventListener('lt-wizard:image-state', function (event) {
      var detail = event.detail || {};
      state.image = {
        status: detail.status || 'none',
        imageId: detail.imageId || null,
        filename: detail.filename || null,
        url: detail.url || null,
        width: detail.width || null,
        height: detail.height || null,
        pct: detail.pct || null
      };
      // The module owns the file: it never travels with the create POST.
      if (fileInput) {
        fileInput.removeAttribute('name');
      }
      updateNav(state.values);
      if (state.step === REVIEW_STEP) {
        renderReview();
      }
      saveSession();
    });

    form.addEventListener('submit', function (event) {
      if (state.submitting || state.image.status === 'uploading') {
        event.preventDefault();
        return;
      }
      if (state.step !== REVIEW_STEP) {
        event.preventDefault();
        goNext();
        return;
      }
      var errs = allErrorFields();
      if (errs.length) {
        event.preventDefault();
        var index = stepOf(errs[0]);
        state.shown[index] = true;
        state.visited[index] = true;
        goTo(index);
        var here = stepErrors(index);
        render();
        showErrsum(here);
        announce(attentionMessage(errs.length));
        return;
      }
      state.submitting = true;
      clearSession();
      updateNav(state.values);
      submitBtn.disabled = true;
    });

    window.addEventListener('pageshow', function (event) {
      if (event.persisted && state.submitting) {
        state.submitting = false;
        submitBtn.disabled = false;
        updateNav(state.values);
      }
    });

    /* ---------- sessionStorage ---------- */

    function storage() {
      try {
        return window.sessionStorage;
      } catch (e) {
        return null;
      }
    }

    function sessionValues() {
      var values = {};
      qsa(form, 'input[name], select[name], textarea[name]').forEach(
        function (node) {
          var name = node.name;
          if (
            node.type === 'file' || node.type === 'submit' ||
            SESSION_EXCLUDE.indexOf(name) !== -1
          ) {
            return;
          }
          if (node.type === 'radio') {
            if (node.checked) {
              values[name] = node.value;
            } else if (!Object.prototype.hasOwnProperty.call(values, name)) {
              values[name] = null;
            }
          } else if (node.type === 'checkbox') {
            values[name] = node.checked;
          } else {
            values[name] = node.value;
          }
        }
      );
      return values;
    }

    function saveSession() {
      var store = storage();
      if (!config.sessionKey || !store || state.submitting) {
        return;
      }
      var image = state.image;
      var payload = {
        values: sessionValues(),
        step: state.step,
        visited: state.visited,
        reqMap: state.reqMap,
        unlinked: state.unlinked,
        image: image.status === 'done' && image.imageId ? image : null
      };
      try {
        store.setItem(config.sessionKey, JSON.stringify(payload));
      } catch (e) {
        // Storage full or blocked: the wizard works without it.
      }
    }

    function clearSession() {
      var store = storage();
      if (!config.sessionKey || !store) {
        return;
      }
      try {
        store.removeItem(config.sessionKey);
      } catch (e) {
        // Nothing to clean up.
      }
    }

    function loadSession() {
      var store = storage();
      if (!config.sessionKey || !store) {
        return null;
      }
      try {
        var payload = JSON.parse(store.getItem(config.sessionKey));
        return payload && typeof payload === 'object' && payload.values
          ? payload
          : null;
      } catch (e) {
        return null;
      }
    }

    function restorePointTable(raw) {
      var wrapper = qs(form, '[data-ffa-editor]');
      var hidden = qs(form, 'input[name="point_table"]');
      if (!wrapper || !hidden || hidden.value === raw) {
        return;
      }
      if (raw === '') {
        qsa(wrapper, '.ffa-point-card__remove').forEach(function (button) {
          button.click();
        });
        return;
      }
      // The editor only redraws through its preset buttons.
      var preset = qs(wrapper, '[data-ffa-preset]');
      if (!preset) {
        hidden.value = raw;
        return;
      }
      var original = preset.getAttribute('data-ffa-preset');
      preset.setAttribute('data-ffa-preset', raw);
      preset.click();
      preset.setAttribute('data-ffa-preset', original);
    }

    function restoreSession() {
      var noServerState = (config.firstErrorStep === null ||
        config.firstErrorStep === undefined) &&
        Object.keys(config.serverErrors || {}).length === 0;
      var payload = noServerState ? loadSession() : null;
      if (!payload) {
        return false;
      }
      Object.keys(payload.values).forEach(function (name) {
        if (SESSION_EXCLUDE.indexOf(name) !== -1) {
          return;
        }
        if (name === 'point_table') {
          restorePointTable(String(payload.values[name] || ''));
        } else {
          writeValue(name, payload.values[name]);
        }
      });
      var step = parseInt(payload.step, 10);
      state.step = step >= 0 && step < STEP_COUNT ? step : 0;
      state.visited = payload.visited && typeof payload.visited === 'object'
        ? payload.visited
        : {};
      state.reqMap = payload.reqMap || null;
      state.unlinked = !!payload.unlinked;
      if (payload.image && payload.image.imageId && !config.stagedImage) {
        state.image = payload.image;
        writeValue('image_id', payload.image.imageId);
        pending.restoreImage = payload.image;
      }
      announce(t('Entries restored from this session.'));
      return true;
    }


    function startEnhanced() {
      layout.classList.add('lt-wiz', 'is-enhanced');
      form.classList.add('is-enhanced');
      form.noValidate = true;
      qsa(form, '[data-wiz-js-only]').forEach(function (node) {
        setShown(node, true);
      });
      qsa(page, '[data-wiz-server-banner]').forEach(function (node) {
        setShown(node, true);
      });
      qsa(page, '[data-wiz-nojs-intro]').forEach(function (node) {
        if (node.parentNode) {
          node.parentNode.removeChild(node);
        }
      });
      buildStepper();
      buildNav();

      var values = readValues();
      var first = config.firstErrorStep;
      Object.keys(config.serverErrors || {}).forEach(function (field) {
        var list = config.serverErrors[field];
        state.server[field] = Array.isArray(list) ? list.map(String) : [];
      });
      state.reqMap = config.request
        ? Rules.initialReqMap(values, config.request)
        : null;
      if (typeof first === 'number' && first >= 0 && first < STEP_COUNT) {
        state.step = first;
        for (var i = 0; i < REVIEW_STEP; i++) {
          state.visited[i] = true;
          state.shown[i] = true;
        }
      } else {
        restoreSession();
      }
    }

    startEnhanced();
    render();
    if (pending.restoreImage) {
      document.dispatchEvent(new CustomEvent(
        'lt-wizard:image-restore', {detail: pending.restoreImage}
      ));
    }
    // After a server refusal: the first invalid field of its step, or the
    // refusal box when only the review holds an error.
    var firstInvalid = state.step < REVIEW_STEP ? stepErrors(state.step)[0] : null;
    if (typeof config.firstErrorStep === 'number' && firstInvalid) {
      focusField(firstInvalid);
    } else if (serverRefusalEl && state.step === REVIEW_STEP) {
      serverRefusalEl.focus();
    } else if (errsumEl && errsumEl.parentNode) {
      errsumEl.focus();
    }
    if (state.step === REVIEW_STEP) {
      startPrecheck();
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
