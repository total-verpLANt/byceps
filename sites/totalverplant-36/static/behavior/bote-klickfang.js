// Homepage "Klickfang" popup ad — hover/idle-summoned, CTA opens the crossword overlay
(function(){
  "use strict";

  /* ---------- config (prize fixed to Rollator; accent lives in CSS as var(--kf-accent)) ---------- */
  var T = { intensity:7, prize:"rollator", confetti:true, runawayX:true, idleMin:5 };

  var PRIZES = {
    rollator: { glyph:"🦽", name:"ROLLATOR DELUXE", sub:"Modell „Nürburgring“ · mit Hupe & Getränkehalter", worth:"Wert: 249 €" },
    streusel: { glyph:"🧁", name:"TUPPERDOSE STREUSELKUCHEN", sub:"Randvoll · vom Donnerstags-Bingo · noch lauwarm", worth:"Wert: unbezahlbar" },
    pfeffi:   { glyph:"🍬", name:"1 BEUTEL PFEFFERMINZ", sub:"Echte Werthers-Konkurrenz · zuckerfrei auf Wunsch", worth:"Wert: 1,99 €" },
    kamille:  { glyph:"🍵", name:"1 JAHR KAMILLENTEE", sub:"Täglich frisch aufgebrüht in der Eule", worth:"Wert: innere Ruhe" },
    wlan:     { glyph:"📶", name:"GRATIS WLAN-PASSWORT", sub:"Heißt immernoch „Tatort2006“ · psssst", worth:"Wert: 0 €" }
  };

  /* ---------- intensity derivations (intensity=7) ---------- */
  var fast  = 1 - (T.intensity - 1) / 12;                 // 0.5
  var blink = (0.4 + fast * 1.0).toFixed(2);              // 0.90s
  var shake = (0.18 + fast * 0.5).toFixed(2);             // 0.43s
  var marq  = (16 - T.intensity * 1.1).toFixed(1);        // 8.3s
  var cCount = T.confetti ? Math.round(T.intensity * 9) : 0; // 63

  /* ---------- single-instance state ---------- */
  var overlayEl = null;       // the .kf-overlay node while open
  var leaveTimer = null;      // mouse-out grace timer
  var countId = null;         // countdown interval
  var hasOpened = false;      // once-per-page-visit latch: summon at most once per
                              // page load. Not persisted — a reload/reopen is a
                              // fresh page and is allowed to show it again.

  function el(tag, cls){ var n = document.createElement(tag); if(cls) n.className = cls; return n; }

  /* ---------- confetti ---------- */
  function buildConfetti(count){
    var palette = ["#a8281c", "#1a1812", "#b8860b", "#3b5a2a", "#7a1f6b"];
    var wrap = el("div", "kf-confetti");
    wrap.setAttribute("aria-hidden", "true");
    for(var i=0;i<count;i++){
      var s = document.createElement("span");
      var size = 6 + Math.random() * 8;
      var st = s.style;
      st.left = (Math.random() * 100) + "%";
      st.width = size + "px";
      st.height = size + "px";
      st.background = palette[i % palette.length];
      st.borderRadius = Math.random() > 0.7 ? "50%" : "0";
      st.animationDuration = (1.8 + Math.random() * 1.8) + "s";
      st.animationDelay = (-Math.random() * 2.6) + "s";
      st.setProperty("--sway", (Math.random() * 30 - 15).toFixed(0) + "px");
      st.setProperty("--r0", (Math.random() * 360) + "deg");
      wrap.appendChild(s);
    }
    return wrap;
  }

  /* ---------- countdown: 9→1, flash "VERLÄNGERT!" at the bottom, reset ---------- */
  function buildCountdown(){
    var box = el("div", "kf-count");
    var label = el("span", "kf-count-label"); label.textContent = "Angebot endet in";
    var clock = el("span", "kf-count-clock");
    box.appendChild(label); box.appendChild(clock);

    var s = 9;
    function paint(){ clock.textContent = "00 : 00 : " + (s < 10 ? "0" + s : "" + s); }
    paint();
    countId = setInterval(function(){
      if(s <= 1){
        label.textContent = "★ VERLÄNGERT! ★";
        setTimeout(function(){ label.textContent = "Angebot endet in"; }, 600);
        s = 9;
      } else {
        s -= 1;
      }
      paint();
    }, 1000);
    return box;
  }

  /* ---------- runaway close button (gag only — never the real close path) ---------- */
  function buildRunawayX(){
    var x = el("button", "kf-x");
    x.type = "button";
    x.textContent = "✕";
    x.setAttribute("aria-label", "Schließen");
    x.title = T.runawayX ? "Schließen (viel Glück)" : "Schließen";
    x.style.right = "8px";
    x.style.top = "8px";
    x.addEventListener("mouseenter", function(){
      if(!T.runawayX) return;
      x.style.left = (Math.random() * 68 + 6) + "%";
      x.style.top = (Math.random() * 60 + 8) + "%";
      x.style.right = "auto";
    });
    x.addEventListener("click", function(){ if(!T.runawayX) close(); });
    return x;
  }

  /* ---------- the over-the-top ad innards ---------- */
  function buildAdGuts(){
    var frag = document.createDocumentFragment();
    var p = PRIZES[T.prize] || PRIZES.rollator;

    if(cCount > 0) frag.appendChild(buildConfetti(cCount));
    frag.appendChild(buildRunawayX());

    var badge = el("div", "kf-badge-row");
    var b1 = el("span", "kf-blink"); b1.style.animationDuration = blink + "s"; b1.textContent = "★ ★ ★";
    var mil = el("span", "kf-1mil"); mil.innerHTML = "Sie sind der <b>1.000.000.</b> Besucher!";
    var b2 = el("span", "kf-blink"); b2.style.animationDuration = blink + "s"; b2.textContent = "★ ★ ★";
    badge.appendChild(b1); badge.appendChild(mil); badge.appendChild(b2);
    frag.appendChild(badge);

    var band = el("div", "kf-laufband");
    var track = el("div", "kf-laufband-track");
    track.style.animationDuration = marq + "s";
    for(var i=0;i<2;i++){
      var seg = document.createElement("span");
      seg.innerHTML = "GEWINNE&nbsp;·&nbsp;GEWINNE&nbsp;·&nbsp;GEWINNE&nbsp;·&nbsp;GEWINNE&nbsp;·&nbsp;GEWINNE&nbsp;·&nbsp;";
      track.appendChild(seg);
    }
    band.appendChild(track);
    frag.appendChild(band);

    var head = el("h2", "kf-head");
    head.style.animationDuration = shake + "s";
    head.innerHTML = "🎉 HERZLICHEN<br>GLÜCKWUNSCH! 🎉";
    frag.appendChild(head);

    var sub = el("p", "kf-sub");
    sub.innerHTML = "Der <i>Rentenbote</i> verlost exklusiv an <b>SIE</b>:";
    frag.appendChild(sub);

    var prize = el("div", "kf-prize");
    var glyph = el("div", "kf-prize-glyph");
    glyph.style.animationDuration = (2.4 - fast).toFixed(1) + "s";
    glyph.textContent = p.glyph;
    var ptext = el("div", "kf-prize-text");
    var pname = el("div", "kf-prize-name"); pname.textContent = p.name;
    var psub = el("div", "kf-prize-sub"); psub.textContent = p.sub;
    var pworth = el("div", "kf-prize-worth"); pworth.textContent = p.worth;
    ptext.appendChild(pname); ptext.appendChild(psub); ptext.appendChild(pworth);
    prize.appendChild(glyph); prize.appendChild(ptext);
    frag.appendChild(prize);

    frag.appendChild(buildCountdown());

    var cta = el("button", "kf-cta");
    cta.type = "button";
    cta.appendChild(document.createTextNode("▶  JETZT GEWINN GEWINNEN  ◀"));
    var hand = el("span", "kf-cta-hand"); hand.textContent = "👉";
    cta.appendChild(hand);
    cta.addEventListener("click", claim);
    frag.appendChild(cta);

    var fine = el("p", "kf-fine");
    fine.textContent = "* Gewinn nur nach erfolgreichem Lösen des Kreuzwort­rätsels (S. 27). " +
      "Persönlich abzuholen an der Pforte der Seniorenresidenz Eule, werktags 14–16 Uhr, gegen Vorlage " +
      "des Mitglieds­ausweises. Kein Bargeld, kein Versand, Mittagsschlaf wird respektiert.";
    frag.appendChild(fine);

    return frag;
  }

  /* ---------- popup window ---------- */
  function buildPopup(){
    var overlay = el("div", "kf-overlay");
    overlay.addEventListener("click", function(e){ if(e.target === overlay) close(); });

    var fenster = el("div", "kf-fenster kf-pop");
    fenster.addEventListener("mouseenter", cancelClose);
    fenster.addEventListener("mouseleave", scheduleClose);

    var titlebar = el("div", "kf-titlebar");
    titlebar.appendChild(el("span", "kf-tb-dot"));
    titlebar.appendChild(el("span", "kf-tb-dot"));
    titlebar.appendChild(el("span", "kf-tb-dot"));
    var tbTitle = el("span", "kf-tb-title");
    tbTitle.textContent = "www.gewinne-jetzt-sofort-rentenbote.biz";
    titlebar.appendChild(tbTitle);

    var body = el("div", "kf-fenster-body");
    body.appendChild(buildAdGuts());

    fenster.appendChild(titlebar);
    fenster.appendChild(body);
    overlay.appendChild(fenster);
    return overlay;
  }

  /* ---------- mouse-out-closes (with grace) ---------- */
  function cancelClose(){ if(leaveTimer){ clearTimeout(leaveTimer); leaveTimer = null; } }
  function scheduleClose(){ cancelClose(); leaveTimer = setTimeout(close, 140); }

  /* ---------- open / close (single instance) ---------- */
  function showPopup(){
    if(overlayEl) return;     // already on screen
    overlayEl = buildPopup();
    /* Append to <html>, not <body>: the site sets filter:blur(0px) on body,
       which makes body the containing block for position:fixed and traps the
       overlay at full document height (so it centers mid-page). <html> has no
       such filter, so the fixed overlay centers on the actual viewport. */
    (document.documentElement || document.body).appendChild(overlayEl);
  }
  /* Auto-trigger path (hover / idle): fires at most once per page visit, then
     tears down every hover/idle trigger so it can never auto-summon again.
     After this, only an explicit teaser click (see below) can re-show the ad. */
  function open(){
    if(hasOpened) return;
    hasOpened = true;
    teardownTriggers();
    showPopup();
  }
  function close(){
    cancelClose();
    if(countId){ clearInterval(countId); countId = null; }
    if(overlayEl){
      if(overlayEl.parentNode) overlayEl.parentNode.removeChild(overlayEl);
      overlayEl = null;
    }
  }

  /* ---------- CTA: close the ad, then launch the existing in-page crossword overlay ---------- */
  function claim(){
    close();
    if(typeof window.openKreuzwortraetsel === "function") window.openKreuzwortraetsel();
  }

  /* ---------- Esc closes ---------- */
  window.addEventListener("keydown", function(e){ if(e.key === "Escape" && overlayEl) close(); });

  /* The popup is a desktop hover gag. Only summon it on devices with a fine
     pointer and true hover; on coarse-pointer/touch (mobile, tablets) neither
     trigger is wired, so the ad never shows and the idle timer does not count.
     The crossword teaser tap (bote-kreuzwort.js) stays available on mobile. */
  var canHover = !!(window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches);

  /* ---------- trigger bookkeeping (so the once-per-visit latch can detach/re-attach all) ---------- */
  var hoverTriggers = [];     // nodes wired with the mouseenter→open handler (stable for the visit)
  var idleEvents = ["mousemove", "mousedown", "keydown", "scroll", "wheel", "touchstart"];

  /* ---------- TRIGGER 2: idle timer, re-armed on activity (desktop only) ---------- */
  var idleTimer = null;
  function clearIdle(){ if(idleTimer){ clearTimeout(idleTimer); idleTimer = null; } }
  function armIdle(){
    if(hasOpened) return;     // never re-arm while latched
    clearIdle();
    idleTimer = setTimeout(open, Math.max(0.1, T.idleMin) * 60 * 1000);
  }

  /* ---------- attach all triggers (hover + idle) ---------- */
  function armTriggers(){
    for(var h=0;h<hoverTriggers.length;h++) hoverTriggers[h].addEventListener("mouseenter", open);
    for(var e=0;e<idleEvents.length;e++) window.addEventListener(idleEvents[e], armIdle, { passive: true });
    armIdle();
  }

  /* ---------- detach every trigger after the single summon ---------- */
  function teardownTriggers(){
    for(var h=0;h<hoverTriggers.length;h++) hoverTriggers[h].removeEventListener("mouseenter", open);
    for(var e=0;e<idleEvents.length;e++) window.removeEventListener(idleEvents[e], armIdle, { passive: true });
    clearIdle();
  }

  /* ---------- TRIGGER 1: hover the crossword teaser (#teaser) or the parody
     "Anzeige" ad boxes (.box-blue / .box-green) ---------- */
  if(canHover){
    var teaser = document.getElementById("teaser");
    if(teaser) hoverTriggers.push(teaser);
    var adBoxes = document.querySelectorAll(".box-blue, .box-green");
    for(var a=0;a<adBoxes.length;a++) hoverTriggers.push(adBoxes[a]);
    for(var t=0;t<hoverTriggers.length;t++) hoverTriggers[t].classList.add("kf-trigger");

    /* RECOVERY (not a re-arm): the ad is the gag gateway to the crossword (its
       CTA opens the puzzle). If it auto-shows once and the player slips the
       mouse off it, clicking the crossword teaser re-summons the ad rather than
       opening the puzzle directly — so the CTA path stays reachable. The
       hover/idle auto-triggers remain torn down: the ad never auto-appears a
       second time; only this explicit click can bring it back.

       The listener sits on document in the CAPTURE phase so it runs before the
       crossword's own teaser click handler (bote-kreuzwort.js) regardless of
       script load order; stopPropagation prevents that handler from also firing
       (which would open the puzzle behind the ad). */
    if(teaser){
      document.addEventListener("click", function(e){
        if(!hasOpened || overlayEl) return;        // only after a dismissal, never while open
        if(!teaser.contains(e.target)) return;     // only clicks on the teaser
        e.stopPropagation();                        // suppress the crossword's direct open
        e.preventDefault();
        showPopup();                                // re-show the ad (auto-triggers stay dead)
      }, true);
    }

    armTriggers();
  }
})();
