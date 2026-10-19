/* gv-look.js: look engine for genial verpLANt.
 * Era (Owly, Clan 2003, LAN '99), light/dark scheme, the Kampf picker, unlocks,
 * the offer hint, the unlock toast, the eggs, the console and the phone menu.
 * Vanilla JS, no dependencies. Without JS the page stays Owly dark. */
(function () {
  'use strict';

  var ERAS = {
    '1': { name: 'Owly', desc: 'Owly: Bernstein-Monitor mit Leuchten und Scanlines.' },
    '2': { name: 'Clan 2003', desc: 'Clan 2003: Metallbalken, Seitenleiste, Server-Look.', hint: 'Die Eule oben links mag keine Klicks.' },
    '3': { name: "LAN '99", desc: "LAN '99: Sternenhimmel, Laufschrift, Besucherzähler.", hint: 'Im Kleingedruckten ganz unten steckt eine Zeitmaschine.' }
  };
  var ALL = ['1', '2', '3'], FREE = ['1'], DEFAULT_ERA = '1';
  var FIGHT = {
    '1': { move: 'Scanline-Blendung', stats: [5, 3, 4] },
    '2': { move: 'Metallbalken-Wall', stats: [1, 3, 4] },
    '3': { move: 'Besucherzähler-Combo', stats: [3, 1, 5] }
  };
  var STAT_NAMES = ['Glow', 'Lesbarkeit', 'Nostalgie'];
  var PICKER_CTA = 'Kämpfer wählen';
  var KICKER = 'Ein neuer Herausforderer!';
  var TRIGGER_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M7 8h10a4 4 0 0 1 4 4v2a3 3 0 0 1-5.2 2L14 14h-4l-1.8 2A3 3 0 0 1 3 14v-2a4 4 0 0 1 4-4z"/>'
    + '<path d="M8 10.5v3M6.5 12h3M15.5 11h.01M17 13h.01"/></svg>';
  var OFFERS = {
    '1': '<strong>&gt; Signal OK.</strong> Auf diesem Fernseher laufen noch mehr Kanäle.',
    '2': '<strong>Zu viel Metall?</strong> Diese Seite hat noch mehr Skins.',
    '3': '<strong>Wusstest du?</strong> Diese Homepage gibt es auch in modern!!!'
  };
  var SOURCES = {
    owl: 'Headshot. Die Eule hat genug.',
    year: 'Du hast die Zeit bis 1999 zurückgedreht.',
    konami: '↑ ↑ ↓ ↓ ← → ← → B A. Respekt.',
    console: 'sv_cheats 1. Wir haben nichts gesehen.'
  };
  var LOCK_SVG = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><rect x="3" y="7" width="10" height="7" rx="1.5"/><path d="M5 7V5a3 3 0 0 1 6 0v2"/></svg>';

  var root = document.documentElement;
  var look = document.getElementById('look');
  var btn = document.getElementById('look-btn');
  var panel = document.getElementById('look-panel');
  var hint = document.getElementById('look-hint');
  var cur = document.getElementById('look-cur');
  var desc = document.getElementById('look-desc');
  var schemes = document.getElementById('schemes');
  var schemeNote = document.getElementById('scheme-note');
  var lockNote = document.getElementById('lock-note');
  var offerText = document.getElementById('offer-text');
  var offerYes = document.getElementById('offer-yes');
  var hintOk = document.getElementById('look-hint-ok');
  var trgIco = document.getElementById('trg-ico');
  var fxBig = document.getElementById('fx-big');
  var fxName = document.getElementById('fx-name');
  var fxMove = document.getElementById('fx-move');
  var fxStats = document.getElementById('fx-stats');
  var fxFlash = document.getElementById('fx-flash');
  var thumb = document.getElementById('seg-thumb');
  var toast = document.getElementById('unlock');
  var toastThumb = document.getElementById('unlock-thumb');
  var toastKicker = document.getElementById('unlock-kicker');
  var toastTitle = document.getElementById('unlock-title');
  var toastSub = document.getElementById('unlock-sub');
  var toastGo = document.getElementById('unlock-go');
  var toastLater = document.getElementById('unlock-later');
  var eggOwl = document.getElementById('egg-owl');
  var eggYear = document.getElementById('egg-year');
  var con = document.getElementById('con');
  var conLog = document.getElementById('con-log');
  var conIn = document.getElementById('con-in');
  var mnav = document.getElementById('mnav');

  // Pages without the chrome (the phone menu is optional) get no engine.
  if (!(look && btn && panel && hint && cur && desc && schemes && schemeNote && lockNote
    && offerText && offerYes && hintOk && trgIco && fxBig && fxName && fxMove && fxStats
    && fxFlash && thumb && toast && toastThumb && toastKicker && toastTitle && toastSub
    && toastGo && toastLater && eggOwl && eggYear && con && conLog && conIn)) return;

  var eraInputs = panel.querySelectorAll('.era-in');
  var schemeInputs = panel.querySelectorAll('input[name="scheme"]');
  var fxSlots = panel.querySelectorAll('.fx-slot');
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)');

  function store(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function load(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function forget(k) { try { localStorage.removeItem(k); } catch (e) {} }
  function loadList(k) {
    try {
      var a = JSON.parse(localStorage.getItem(k) || '[]');
      return Array.isArray(a) ? a.filter(function (x) { return typeof x === 'string' && /^[23]$/.test(x); }) : [];
    } catch (e) { return []; }
  }

  // Unlocks live in this browser only.
  var unlocked = loadList('tv-unlocked');
  var fresh = loadList('tv-new');
  var btnDot = fresh.length > 0;
  function isUnlocked(v) { return FREE.indexOf(v) > -1 || unlocked.indexOf(v) > -1; }
  function lockedEras() { return ALL.filter(function (v) { return !isUnlocked(v); }); }
  function era() { return root.getAttribute('data-era'); }
  function skinName(v) { return ERAS[v].name.toUpperCase().replace(/[^A-Z0-9]/g, ''); }
  function restart(el, cls) { el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls); }

  function setEra(v, animate) {
    v = String(v);
    if (!isUnlocked(v)) return false;
    var run = function () {
      root.setAttribute('data-era', v);
      var e = ERAS[v];
      cur.textContent = ' · ' + e.name;
      desc.textContent = e.desc;
      btn.setAttribute('aria-label', 'Look der Seite: ' + e.name + '. Ändern');
      var locked = +v >= 2;
      if (locked) schemes.setAttribute('data-locked', ''); else schemes.removeAttribute('data-locked');
      schemeNote.hidden = !locked;
      schemeInputs.forEach(function (i) { i.disabled = locked; });
      var k = fresh.indexOf(v);
      if (k > -1) { fresh.splice(k, 1); store('tv-new', JSON.stringify(fresh)); }
      renderPicker();
    };
    if (animate && document.startViewTransition && !reduce.matches) document.startViewTransition(run);
    else run();
    store('tv-era', v);
    return true;
  }

  function renderPicker() {
    var v = era();
    eraInputs.forEach(function (i) {
      var ok = isUnlocked(i.value), opt = i.closest('[data-v]'), st = opt.querySelector('.lk-state');
      i.checked = i.value === v;
      i.disabled = !ok;
      opt.classList.toggle('is-locked', !ok);
      opt.classList.toggle('is-new', fresh.indexOf(i.value) > -1);
      if (st) st.textContent = ok ? '' : ' (gesperrt)';
    });
    renderFight();
    var l = lockedEras();
    if (l.length) {
      lockNote.classList.remove('is-done');
      lockNote.innerHTML = LOCK_SVG + '<span>' + (l.length === 1 ? 'Ein Look ist noch versteckt.' : l.length + ' Looks sind noch versteckt.') + ' Tipp: ' + ERAS[l[0]].hint + '</span>';
    } else {
      lockNote.classList.add('is-done');
      lockNote.textContent = 'Alle Looks gefunden. Respekt.';
    }
    btn.classList.toggle('has-new', btnDot && fresh.length > 0);
  }

  // ---------- Kampf picker: fighting-game select screen ----------
  var fxPreview = null;
  function renderFight() {
    var t = fxPreview !== null ? fxPreview : era(), e = ERAS[t];
    if (!e) return;
    var ok = isUnlocked(t);
    fxBig.setAttribute('data-p', t);
    fxBig.classList.toggle('is-locked', !ok);
    fxName.textContent = ok ? e.name : '???';
    fxMove.textContent = ok ? FIGHT[t].move : 'Gesperrt. ' + e.hint;
    fxStats.innerHTML = STAT_NAMES.map(function (n, k) {
      var on = ok ? FIGHT[t].stats[k] : 0, seg = '';
      for (var q = 0; q < 5; q++) seg += '<i' + (q < on ? ' class="on"' : '') + '></i>';
      return '<div class="fx-stat"><span>' + n + '</span><span class="fx-bar">' + seg + '</span><b>' + (ok ? on : '?') + '</b></div>';
    }).join('');
    fxSlots.forEach(function (sl) {
      var v = sl.getAttribute('data-v');
      if (ERAS[v]) sl.querySelector('.fx-slot-name').textContent = isUnlocked(v) ? ERAS[v].name : '???';
    });
  }
  fxSlots.forEach(function (sl) {
    sl.addEventListener('pointerenter', function () { fxPreview = sl.getAttribute('data-v'); renderFight(); });
    sl.addEventListener('pointerleave', function () { fxPreview = null; renderFight(); });
  });
  eraInputs.forEach(function (i) {
    i.addEventListener('change', function () {
      if (!reduce.matches) restart(fxFlash, 'is-on');
      setEra(i.value, true);
    });
  });

  function currentScheme() { return root.getAttribute('data-theme') === 'light' ? 'light' : 'dark'; }

  // Spring: damping ratio (1 = no overshoot) and response (s). It integrates from
  // the live value and keeps its velocity when re-targeted, so a motion can be
  // reversed mid-flight without a jump.
  function spring(st, target, damping, response, apply, onRest) {
    st.k = Math.pow(2 * Math.PI / response, 2);
    st.c = 4 * Math.PI * damping / response;
    st.target = target;
    st.onRest = onRest || null;
    if (st.raf) return;
    var last = performance.now();
    function step(now) {
      var dt = Math.min(.064, (now - last) / 1000), n = Math.max(1, Math.ceil(dt / .004)), h = dt / n, done = true;
      last = now;
      for (var key in st.target) {
        var x = st.v[key], vel = st.vel[key] || 0, to = st.target[key];
        for (var i = 0; i < n; i++) { vel += (-st.k * (x - to) - st.c * vel) * h; x += vel * h; }
        st.v[key] = x; st.vel[key] = vel;
        if (Math.abs(x - to) > .0005 || Math.abs(vel) > .005) done = false;
      }
      if (done) {
        for (var k2 in st.target) { st.v[k2] = st.target[k2]; st.vel[k2] = 0; }
        apply(st.v); st.raf = 0;
        if (st.onRest) st.onRest();
        return;
      }
      apply(st.v);
      st.raf = requestAnimationFrame(step);
    }
    st.raf = requestAnimationFrame(step);
  }

  var thumbState = { v: { x: 0 }, vel: {}, raf: 0 };
  function applyThumb(v) { thumb.style.transform = 'translateX(' + (v.x * 100) + '%)'; }
  function moveThumb(animate) {
    var x = currentScheme() === 'light' ? 1 : 0;
    if (!animate || reduce.matches) { thumbState.v.x = x; thumbState.vel.x = 0; applyThumb(thumbState.v); return; }
    spring(thumbState, { x: x }, 1, .3, applyThumb);
  }

  function setScheme(v) {
    if (v !== 'dark' && v !== 'light') return;
    var run = function () {
      root.setAttribute('data-theme', v);
      schemeInputs.forEach(function (i) { i.checked = i.value === v; });
      moveThumb(true);
    };
    if (document.startViewTransition && !reduce.matches) document.startViewTransition(run);
    else run();
    store('tv-theme', v);
  }

  // The panel grows out of its button and shrinks back into it on the same path.
  var isOpen = false;
  function closedPose() { return reduce.matches ? { s: 1, o: 0, y: 0 } : { s: .92, o: 0, y: -8 }; }
  var panelState = { v: closedPose(), vel: {}, raf: 0 };
  function applyPanel(v) {
    panel.style.opacity = Math.max(0, Math.min(1, v.o));
    panel.style.transform = 'translateY(' + v.y + 'px) scale(' + v.s + ')';
  }
  function open() {
    if (isOpen) return;
    isOpen = true;
    fxPreview = null;
    btnDot = false;
    if (panel.hidden) { panelState.v = closedPose(); panelState.vel = {}; applyPanel(panelState.v); panel.hidden = false; }
    renderPicker();
    btn.setAttribute('aria-expanded', 'true');
    if (mnav) mnav.open = false;
    dismissHint();
    spring(panelState, { s: 1, o: 1, y: 0 }, 1, .34, applyPanel);
    var f = panel.querySelector('.pk-fight input:checked');
    if (f) f.focus({ preventScroll: true });
  }
  function close(returnFocus) {
    if (!isOpen) return;
    isOpen = false;
    btn.setAttribute('aria-expanded', 'false');
    spring(panelState, closedPose(), 1, .26, applyPanel, function () { if (!isOpen) panel.hidden = true; });
    if (returnFocus) btn.focus();
  }

  // ---------- the offer: first impression first, then the invitation ----------
  var offerTimer = 0;
  function lockedSentence() {
    var n = lockedEras().length;
    return n === 2 ? ' Zwei davon sind noch versteckt.' : n === 1 ? ' Einer ist noch versteckt.' : '';
  }
  function showOffer() {
    if (isOpen || load('tv-offer') === 'seen') return;
    clearTimeout(offerTimer);
    offerText.innerHTML = OFFERS[era()] + lockedSentence();
    offerYes.textContent = PICKER_CTA;
    hint.hidden = false;
    btn.classList.remove('is-nudge');
    if (!reduce.matches) restart(btn, 'is-nudge');
  }
  function hideOffer() { hint.hidden = true; btn.classList.remove('is-nudge'); clearTimeout(offerTimer); }
  function dismissHint() { hideOffer(); store('tv-offer', 'seen'); }
  function armOffer() {
    clearTimeout(offerTimer);
    if (load('tv-offer') === 'seen') return;
    offerTimer = setTimeout(showOffer, 9000);
  }
  window.addEventListener('scroll', function () {
    if (hint.hidden && !isOpen && window.scrollY > window.innerHeight * .8 && load('tv-offer') !== 'seen') showOffer();
  }, { passive: true });

  // ---------- unlock toast ----------
  var toastEra = null, toastTimer = 0;
  function hideToast() { toast.hidden = true; clearTimeout(toastTimer); }
  function addUnlock(v) {
    if (isUnlocked(v)) return false;
    unlocked.push(v);
    store('tv-unlocked', JSON.stringify(unlocked));
    if (fresh.indexOf(v) < 0) fresh.push(v);
    store('tv-new', JSON.stringify(fresh));
    btnDot = true;
    return true;
  }
  function showToast(v, kicker, title, sub) {
    var first = kicker === 'Freigeschaltet';
    if (first) kicker = KICKER;
    toast.classList.toggle('is-fight', first);
    toastEra = v;
    toastThumb.className = 'thumb t' + (v || '3');
    toastKicker.textContent = kicker;
    toastTitle.textContent = title;
    toastSub.textContent = sub || '';
    toastGo.hidden = !v || era() === v;
    toast.hidden = false;
    restart(toast, 'is-in');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, 12000);
    renderPicker();
  }
  function unlock(v, src) {
    var added = addUnlock(v);
    showToast(v, added ? 'Freigeschaltet' : 'Schon freigeschaltet', ERAS[v].name, SOURCES[src]);
  }
  function unlockMany(vs, src) {
    var added = vs.filter(addUnlock);
    if (added.length === 1) showToast(added[0], 'Freigeschaltet', ERAS[added[0]].name, SOURCES[src]);
    else showToast(null, added.length ? 'Freigeschaltet' : 'Schon freigeschaltet', 'Alle Looks', SOURCES[src]);
  }
  toastGo.addEventListener('click', function () { var v = toastEra; hideToast(); setEra(v, true); });
  toastLater.addEventListener('click', hideToast);

  // ---------- eggs ----------
  var OUCH = ['Au.', 'Hey!', 'Lass das.', 'Letzte Warnung.'];
  var owlHits = 0, owlReset = 0, rolling = false;
  function pop(el, text, big) {
    var r = el.getBoundingClientRect(), p = document.createElement('span');
    p.className = 'egg-pop' + (big ? ' is-big' : '');
    p.textContent = text;
    p.style.left = (r.left + r.width / 2) + 'px';
    p.style.top = (r.bottom + 4) + 'px';
    document.body.appendChild(p);
    setTimeout(function () { p.remove(); }, 1100);
  }
  function owlEgg() { restart(eggOwl, 'egg-hit'); pop(eggOwl, 'HEADSHOT!', true); unlock('2', 'owl'); }
  // The owl sits inside the brand link: stop the link, or five clicks never happen.
  eggOwl.addEventListener('click', function (e) {
    e.preventDefault();
    owlHits++;
    clearTimeout(owlReset);
    owlReset = setTimeout(function () { owlHits = 0; }, 1600);
    if (owlHits >= 5) { owlHits = 0; owlEgg(); return; }
    restart(eggOwl, 'egg-hit');
    pop(eggOwl, OUCH[owlHits - 1], false);
  });
  function yearEgg() {
    if (rolling) return;
    rolling = true;
    var shown = eggYear.textContent, y = parseInt(shown, 10) || new Date().getFullYear();
    var done = function () {
      eggYear.textContent = '1999';
      unlock('3', 'year');
      setTimeout(function () { eggYear.textContent = shown; rolling = false; }, 2600);
    };
    if (reduce.matches || y <= 1999) { done(); return; }
    var iv = setInterval(function () {
      y--;
      eggYear.textContent = String(y);
      if (y <= 1999) { clearInterval(iv); done(); }
    }, 45);
  }
  eggYear.addEventListener('click', yearEgg);
  var KONAMI = ['ArrowUp', 'ArrowUp', 'ArrowDown', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'ArrowLeft', 'ArrowRight', 'b', 'a'], kPos = 0;

  // ---------- console: a Quake-style console on the key left of 1 (^ on German keyboards) ----------
  var CMDS = ['help', 'skin', 'sv_cheats', 'connect', 'god', 'noclip', 'clear', 'quit'];
  var conHist = [], conPos = 0, conBack = null;
  function conPrint(t, cls) {
    var l = document.createElement('div');
    if (cls) l.className = cls;
    l.textContent = t;
    conLog.appendChild(l);
    conLog.scrollTop = conLog.scrollHeight;
  }
  function conOpen() {
    if (!con.hidden) return;
    conBack = document.activeElement;
    close(false);
    hideOffer();
    con.hidden = false;
    if (!reduce.matches) restart(con, 'is-in');
    conIn.focus();
  }
  function conClose() {
    if (con.hidden) return;
    con.hidden = true;
    if (conBack && conBack.focus) conBack.focus();
  }
  function skinFind(arg) {
    var a = arg.toLowerCase().replace(/[^a-z0-9]/g, '');
    for (var i = 0; i < ALL.length; i++) if (a === ALL[i] || a === skinName(ALL[i]).toLowerCase()) return ALL[i];
    return null;
  }
  function conRun(line) {
    var parts = line.trim().split(/\s+/), cmd = (parts[0] || '').toLowerCase(), arg = parts.slice(1).join(' ');
    conPrint('] ' + line, 'con-echo');
    if (!cmd) return;
    if (cmd === 'help') conPrint('Befehle: help, skin [name], sv_cheats [0|1], connect, god, noclip, clear, quit');
    else if (cmd === 'skin') {
      if (!arg) {
        ALL.forEach(function (n) {
          conPrint('  ' + n + '  ' + skinName(n).toLowerCase() + (n === era() ? '  (aktiv)' : isUnlocked(n) ? '' : '  (gesperrt)'));
        });
        conPrint('Wechseln: skin <name>');
        return;
      }
      var v = skinFind(arg);
      if (v === null) conPrint('Skin "' + arg + '" gibt es nicht. Liste: skin', 'con-err');
      else if (!isUnlocked(v)) conPrint('Skin "' + skinName(v).toLowerCase() + '" ist gesperrt. Dafür braucht es sv_cheats 1.', 'con-err');
      else if (v === era()) conPrint('Läuft schon.');
      else { setEra(v, true); conPrint('Skin geladen: ' + ERAS[v].name, 'con-ok'); }
    }
    else if (cmd === 'sv_cheats') {
      if (arg === '1') { conPrint('sv_cheats auf 1 gesetzt. Alle Skins sind frei.', 'con-ok'); unlockMany(['2', '3'], 'console'); }
      else if (arg === '0') conPrint('sv_cheats auf 0 gesetzt. Zu spät, die Skins bleiben frei.');
      else conPrint('"sv_cheats" ist "' + (lockedEras().length ? '0' : '1') + '". Setzen: sv_cheats 1');
    }
    else if (cmd === 'connect') {
      var party = con.getAttribute('data-party') || 'genial verpLANt';
      var sold = con.getAttribute('data-sold'), total = con.getAttribute('data-total');
      conPrint('Verbinde mit ' + party + ' …' + (sold && total ? ' Server fast voll (' + sold + '/' + total + ').' : ''));
    }
    else if (cmd === 'god') conPrint('Godmode AN. Ein Freiticket gibt es trotzdem nicht.');
    else if (cmd === 'noclip') conPrint('Noclip AN. Bitte trotzdem durch den Haupteingang.');
    else if (cmd === 'clear' || cmd === 'cls') conLog.textContent = '';
    else if (cmd === 'quit' || cmd === 'exit') { conPrint('Von einer LAN geht man nicht einfach so.'); setTimeout(conClose, 900); }
    else conPrint('Unbekannter Befehl: "' + cmd + '". Versuch: help', 'con-err');
  }
  conIn.addEventListener('keydown', function (e) {
    e.stopPropagation();
    if (e.code === 'Backquote' || e.key === 'Escape') { e.preventDefault(); conClose(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      var line = conIn.value;
      if (line.trim()) conHist.push(line);
      conPos = conHist.length;
      conIn.value = '';
      conRun(line);
    }
    else if (e.key === 'ArrowUp') { e.preventDefault(); if (conPos > 0) conIn.value = conHist[--conPos]; }
    else if (e.key === 'ArrowDown') { e.preventDefault(); if (conPos < conHist.length) conIn.value = conHist[++conPos] || ''; }
    else if (e.key === 'Tab') {
      e.preventDefault();
      var m = CMDS.filter(function (c) { return c.indexOf(conIn.value.trim().toLowerCase()) === 0; });
      if (m.length === 1) conIn.value = m[0] + ' ';
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.code !== 'Backquote' || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = e.target;
    if (t && (t.isContentEditable || (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) && t.type !== 'radio' && t.type !== 'range'))) return;
    e.preventDefault();
    if (con.hidden) conOpen(); else conClose();
  });
  conPrint('genial verpLANt Konsole');
  conPrint('Tipp: help');

  // ---------- wiring ----------
  btn.addEventListener('click', function () { isOpen ? close(false) : open(); });
  schemeInputs.forEach(function (i) { i.addEventListener('change', function () { setScheme(i.value); }); });
  offerYes.addEventListener('click', function () { dismissHint(); open(); });
  hintOk.addEventListener('click', function () { dismissHint(); btn.focus(); });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (isOpen) close(true);
    else if (!hint.hidden) dismissHint();
    else if (!toast.hidden) hideToast();
  });
  document.addEventListener('keydown', function (e) {
    var t = e.target;
    if (t && (t.isContentEditable || (/^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName) && t.type !== 'radio' && t.type !== 'range'))) return;
    var k = e.key && e.key.length === 1 ? e.key.toLowerCase() : e.key;
    kPos = k === KONAMI[kPos] ? kPos + 1 : (k === KONAMI[0] ? 1 : 0);
    if (kPos === KONAMI.length) { kPos = 0; unlock('3', 'konami'); }
  });
  document.addEventListener('pointerdown', function (e) {
    if (isOpen && !look.contains(e.target)) close(false);
  });

  // phone menu
  if (mnav) {
    mnav.addEventListener('toggle', function () { if (mnav.open) close(false); });
    mnav.querySelectorAll('a').forEach(function (a) { a.addEventListener('click', function () { mnav.open = false; }); });
    document.addEventListener('pointerdown', function (e) { if (mnav.open && !mnav.contains(e.target)) mnav.open = false; });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && mnav.open) { mnav.open = false; mnav.querySelector('summary').focus(); }
    });
  }

  // ---------- init ----------
  panel.querySelectorAll('.port img').forEach(function (im) { im.src = eggOwl.src; });
  trgIco.innerHTML = TRIGGER_ICON;
  if (!setEra(era() || DEFAULT_ERA, false)) setEra(DEFAULT_ERA, false);
  schemeInputs.forEach(function (i) { i.checked = i.value === currentScheme(); });
  moveThumb(false);

  var ticking = false;
  function onScroll() { root.toggleAttribute('data-scrolled', window.scrollY > 4); ticking = false; }
  window.addEventListener('scroll', function () { if (!ticking) { ticking = true; requestAnimationFrame(onScroll); } }, { passive: true });
  onScroll();
  armOffer();
})();
