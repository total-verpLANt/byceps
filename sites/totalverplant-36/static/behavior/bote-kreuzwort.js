// Homepage crossword easter egg — playable engine
(function(){
  "use strict";
  var DATA = JSON.parse(document.getElementById('kw-data').textContent);
  var R = DATA.rows, C = DATA.cols, SOL = DATA.solution, NUM = DATA.numbers;
  var isCoarse = window.matchMedia && matchMedia('(pointer:coarse)').matches;

  /* ---------- mark lookup ---------- */
  var markMap = {}; DATA.marks.forEach(function(m){ markMap[m.r+','+m.c]=m.idx; });

  /* ---------- mini decorative grid (teaser) ---------- */
  (function(){
    var mg = document.createElement('div'); mg.className='mgrid';
    mg.style.gridTemplateColumns='repeat('+C+',15px)';
    for(var r=0;r<R;r++)for(var c=0;c<C;c++){
      var d=document.createElement('div');
      if(SOL[r][c]===null){ d.className='mcell blk'; }
      else { d.className='mcell'; if(NUM[r][c]){ var n=document.createElement('span'); n.className='mn'; n.textContent=NUM[r][c]; d.appendChild(n); } }
      mg.appendChild(d);
    }
    document.getElementById('miniGrid').appendChild(mg);
  })();

  /* ---------- state ---------- */
  var KEY='kw-entries-36';
  var entries={};
  try{ var raw=localStorage.getItem(KEY); if(raw) entries=JSON.parse(raw)||{}; }catch(e){}
  function save(){ try{ localStorage.setItem(KEY, JSON.stringify(entries)); }catch(e){} }
  function getE(r,c){ return entries[r+','+c]||''; }
  function setE(r,c,v){ if(v) entries[r+','+c]=v; else delete entries[r+','+c]; }

  var cur=null;      // {r,c}
  var dir='A';       // 'A' across | 'D' down
  var cellEl={};     // "r,c" -> element

  /* ---------- build playable grid ---------- */
  var grid=document.getElementById('grid');
  grid.style.gridTemplateColumns='repeat('+C+',var(--cell))';
  for(var r=0;r<R;r++)for(var c=0;c<C;c++){
    var d=document.createElement('div');
    if(SOL[r][c]===null){ d.className='cell blk'; }
    else{
      d.className='cell'; d.dataset.r=r; d.dataset.c=c;
      if(NUM[r][c]){ var n=document.createElement('span'); n.className='num'; n.textContent=NUM[r][c]; d.appendChild(n); }
      var mk=markMap[r+','+c];
      if(mk){ d.classList.add('mark'); d.setAttribute('data-mk', mk); }
      var l=document.createElement('span'); l.className='let'; d.appendChild(l);
      d.addEventListener('click', cellClick);
      cellEl[r+','+c]=d;
    }
    grid.appendChild(d);
  }

  /* ---------- clue lists ---------- */
  function buildClues(list, slots, d){
    slots.forEach(function(s){
      var li=document.createElement('li');
      var cn=document.createElement('span'); cn.className='cn'; cn.textContent=s.n;
      var ct=document.createElement('span'); ct.textContent=s.clue;
      var cs=document.createElement('span'); cs.className='cstat'; cs.setAttribute('aria-hidden','true');
      li.appendChild(cn); li.appendChild(ct); li.appendChild(cs);
      li.dataset.dir=d; li.dataset.n=s.n;
      li.addEventListener('click', function(){ selectSlot(s, d); });
      list.appendChild(li);
      s._li=li; s._stat=cs;
    });
  }
  buildClues(document.getElementById('acrossList'), DATA.across, 'A');
  buildClues(document.getElementById('downList'), DATA.down, 'D');

  function slotsFor(d){ return d==='A'?DATA.across:DATA.down; }
  function slotAt(r,c,d){
    var arr=slotsFor(d), i,j;
    for(i=0;i<arr.length;i++){ var cc=arr[i].cells; for(j=0;j<cc.length;j++) if(cc[j][0]===r&&cc[j][1]===c) return arr[i]; }
    return null;
  }
  function hasSlot(r,c,d){ return !!slotAt(r,c,d); }

  /* ---------- rendering ---------- */
  function render(){
    for(var k in cellEl){
      var el=cellEl[k], rc=k.split(','), r=+rc[0], c=+rc[1];
      el.classList.remove('word','active','wrong');
      el.querySelector('.let').textContent=getE(r,c);
    }
    // clue active state
    var allLi=document.querySelectorAll('.clue-list li');
    for(var i=0;i<allLi.length;i++) allLi[i].classList.remove('on');
    if(cur){
      var s=slotAt(cur.r,cur.c,dir) || slotAt(cur.r,cur.c,dir==='A'?'D':'A');
      if(s){
        if(slotAt(cur.r,cur.c,dir)===null) dir=(dir==='A'?'D':'A');
        s=slotAt(cur.r,cur.c,dir);
        s.cells.forEach(function(p){ var e=cellEl[p[0]+','+p[1]]; if(e) e.classList.add('word'); });
        if(s._li) s._li.classList.add('on');
      }
      var ae=cellEl[cur.r+','+cur.c]; if(ae){ ae.classList.remove('word'); ae.classList.add('active'); }
    }
    refreshLoes();
  }
  function gradeClues(){
    function chk(slots){ slots.forEach(function(s){
      var full=true, empty=false;
      for(var i=0;i<s.cells.length;i++){ var p=s.cells[i], v=getE(p[0],p[1]); if(!v) empty=true; if(v!==SOL[p[0]][p[1]]) full=false; }
      s._li.classList.toggle('done', full);
      if(full){ s._stat.className='cstat ok'; s._stat.textContent='✓'; }
      else if(!empty){ s._stat.className='cstat no'; s._stat.textContent='✗'; }
      else { s._stat.className='cstat'; s._stat.textContent=''; }
    }); }
    chk(DATA.across); chk(DATA.down);
  }

  /* ---------- Lösungswort bar ---------- */
  var loesRow=document.getElementById('loesRow'), loesBox=document.getElementById('loes');
  var loesVerdict=document.getElementById('loesVerdict');
  function clearVerdict(){ loesVerdict.className='loes-verdict'; loesVerdict.textContent='';
    var lis=document.querySelectorAll('.clue-list li'); for(var i=0;i<lis.length;i++) lis[i].classList.remove('done');
    var ss=document.querySelectorAll('.cstat'); for(var j=0;j<ss.length;j++){ ss[j].className='cstat'; ss[j].textContent=''; } }
  DATA.marks.forEach(function(m){
    var b=document.createElement('div'); b.className='lbox';
    b.innerHTML='<span class="li">'+m.idx+'</span><span class="ll"></span>';
    b.dataset.r=m.r; b.dataset.c=m.c;
    b.addEventListener('click', function(){ select(m.r,m.c); var s=slotAt(m.r,m.c,dir)||slotAt(m.r,m.c,'A')||slotAt(m.r,m.c,'D'); render(); ensureFocus(); });
    loesRow.appendChild(b); m._box=b;
  });
  function refreshLoes(){
    var word='';
    DATA.marks.forEach(function(m){ var v=getE(m.r,m.c); m._box.querySelector('.ll').textContent=v; m._box.classList.toggle('has', !!v); word+=v; });
    var solved = word===DATA.loesung;
    loesBox.classList.toggle('solved', solved);
    return solved;
  }

  /* ---------- selection / navigation ---------- */
  function select(r,c){ if(SOL[r][c]===null) return; cur={r:r,c:c}; }
  function selectSlot(s,d){ dir=d; var first=s.cells[0];
    for(var i=0;i<s.cells.length;i++){ var p=s.cells[i]; if(!getE(p[0],p[1])){ first=p; break; } }
    select(first[0],first[1]); render(); ensureFocus();
  }
  function cellClick(e){
    var r=+this.dataset.r, c=+this.dataset.c;
    if(cur && cur.r===r && cur.c===c){ // toggle direction if both exist
      if(hasSlot(r,c,dir==='A'?'D':'A')) dir=(dir==='A'?'D':'A');
    } else {
      select(r,c);
      if(!hasSlot(r,c,dir)) dir=(dir==='A'?'D':'A');
    }
    render(); ensureFocus();
  }
  function curSlot(){ return cur ? slotAt(cur.r,cur.c,dir) : null; }
  function idxInSlot(s){ for(var i=0;i<s.cells.length;i++) if(s.cells[i][0]===cur.r&&s.cells[i][1]===cur.c) return i; return -1; }
  function advance(step){
    var s=curSlot(); if(!s) return;
    var i=idxInSlot(s)+step;
    if(i>=0&&i<s.cells.length){ select(s.cells[i][0], s.cells[i][1]); }
  }
  function moveGrid(dr,dc){
    if(!cur) return;
    var r=cur.r, c=cur.c;
    for(var k=0;k<Math.max(R,C);k++){
      r+=dr; c+=dc;
      if(r<0||r>=R||c<0||c>=C) return;
      if(SOL[r][c]!==null){ select(r,c); return; }
    }
  }
  function nextClue(delta){
    var arr=slotsFor(dir); if(!arr.length) return;
    var s=curSlot(), idx=0;
    if(s){ for(var i=0;i<arr.length;i++) if(arr[i]===s){ idx=i; break; } idx=(idx+delta+arr.length)%arr.length; }
    selectSlot(arr[idx], dir);
  }

  /* ---------- typing ---------- */
  function typeLetter(ch){
    if(!cur) return;
    setE(cur.r,cur.c, ch.toUpperCase());
    save(); clearVerdict(); advance(1); render(); checkWin();
  }
  function backspace(){
    if(!cur) return;
    if(getE(cur.r,cur.c)){ setE(cur.r,cur.c,''); }
    else { advance(-1); setE(cur.r,cur.c,''); }
    save(); clearVerdict(); render(); checkWin();
  }
  var LETTER=/^[a-zA-ZäöüÄÖÜß]$/;
  document.addEventListener('keydown', function(e){
    if(!overlayOpen) return;
    if(e.key==='Escape'){ closeOverlay(); return; }
    if(e.key==='Tab'){ e.preventDefault();
      // While playing (focus in the grid input) Tab jumps to the next clue (advertised);
      // Shift+Tab and Tab-from-a-button cycle the modal controls — trapped inside the modal.
      if(document.activeElement===kwInput && !e.shiftKey){ nextClue(1); }
      else { cycleFocus(e.shiftKey?-1:1); }
      return; }
    if(e.key==='ArrowLeft'){ e.preventDefault(); dir='A'; moveGrid(0,-1); render(); return; }
    if(e.key==='ArrowRight'){ e.preventDefault(); dir='A'; moveGrid(0,1); render(); return; }
    if(e.key==='ArrowUp'){ e.preventDefault(); dir='D'; moveGrid(-1,0); render(); return; }
    if(e.key==='ArrowDown'){ e.preventDefault(); dir='D'; moveGrid(1,0); render(); return; }
    if(e.key==='Backspace'){ e.preventDefault(); backspace(); return; }
    if(e.key==='Delete'){ e.preventDefault(); if(cur){ setE(cur.r,cur.c,''); save(); render(); checkWin(); } return; }
    if(e.key===' '){ e.preventDefault(); if(cur&&hasSlot(cur.r,cur.c,dir==='A'?'D':'A')){ dir=(dir==='A'?'D':'A'); render(); } return; }
    if(!isCoarse && LETTER.test(e.key)){ e.preventDefault(); typeLetter(e.key); }
  });
  // mobile / IME path
  var kwInput=document.getElementById('kwInput');
  kwInput.addEventListener('input', function(){
    var v=kwInput.value; kwInput.value='';
    if(!v) return;
    var ch=v.slice(-1);
    if(LETTER.test(ch)) typeLetter(ch);
  });
  kwInput.addEventListener('keydown', function(e){
    if(e.key==='Backspace' && !kwInput.value){ e.preventDefault(); backspace(); }
  });
  function ensureFocus(){ try{ kwInput.focus({preventScroll:true}); }catch(e){ kwInput.focus(); } }
  // Keyboard focus trap: cycle the modal's interactive controls (grid input + toolbar).
  function cycleFocus(step){
    var ring=[kwInput, document.getElementById('checkBtn'),
              document.getElementById('clearBtn'), document.getElementById('closeBtn')];
    var i=ring.indexOf(document.activeElement); if(i<0) i=0;
    ring[(i+step+ring.length)%ring.length].focus();
  }

  /* ---------- tools ---------- */
  document.getElementById('checkBtn').addEventListener('click', function(){
    for(var k in cellEl){ var rc=k.split(','), r=+rc[0], c=+rc[1], v=getE(r,c);
      if(v && v!==SOL[r][c]){ var el=cellEl[k]; el.classList.add('wrong');
        (function(el){ setTimeout(function(){ el.classList.remove('wrong'); }, 1400); })(el); } }
    // per-clue + Lösungswort verdict (only after Prüfen)
    gradeClues();
    var word=''; DATA.marks.forEach(function(m){ word+=getE(m.r,m.c); });
    if(word===DATA.loesung){ loesVerdict.className='loes-verdict ok'; loesVerdict.textContent='✓ Lösungswort stimmt'; }
    else { loesVerdict.className='loes-verdict no'; loesVerdict.textContent='✗ nicht korrekt'; }
  });
  document.getElementById('clearBtn').addEventListener('click', function(){
    if(!confirm('Alle Eingaben löschen?')) return;
    entries={}; save(); clearVerdict(); document.getElementById('stamp').classList.remove('show'); won=false; render();
  });

  /* ---------- win ---------- */
  var won=false;
  function isComplete(){ for(var r=0;r<R;r++)for(var c=0;c<C;c++) if(SOL[r][c]!==null && getE(r,c)!==SOL[r][c]) return false; return true; }
  function checkWin(){ var done=isComplete(), st=document.getElementById('stamp');
    if(done && !won){ won=true; st.classList.add('show'); }
    else if(!done && won){ won=false; st.classList.remove('show'); } }

  /* ---------- FLIP zoom overlay ---------- */
  var overlay=document.getElementById('overlay'), modal=document.getElementById('modal');
  var teaser=document.getElementById('teaser'), backdrop=document.getElementById('backdrop');
  // The overlay is position:fixed, but the site sets filter:blur(0px) on body,
  // which makes an ancestor the containing block and pins the overlay to the
  // document (so the modal is off-centre, drifting as the page scrolls).
  // Re-home it under <html> (no filter) so it centres on the actual viewport.
  // backdrop + modal move with it; the off-screen #kwInput is looked up by id.
  if(overlay && document.documentElement && overlay.parentNode !== document.documentElement){
    document.documentElement.appendChild(overlay);
  }
  var overlayOpen=false;
  function openOverlay(){
    overlay.classList.add('show'); overlay.setAttribute('aria-hidden','false'); overlayOpen=true;
    document.body.style.overflow='hidden';
    // FLIP: from teaser rect to modal rect
    modal.style.transition='none'; modal.style.transform='none';
    var tr=teaser.getBoundingClientRect();
    var mr=modal.getBoundingClientRect();
    var sx=tr.width/mr.width, sy=tr.height/mr.height;
    var tx=tr.left-mr.left, ty=tr.top-mr.top;
    modal.style.transformOrigin='top left';
    modal.style.transform='translate('+tx+'px,'+ty+'px) scale('+sx+','+sy+')';
    modal.style.opacity='0.4';
    modal.getBoundingClientRect(); // force reflow
    modal.style.transition='transform .42s cubic-bezier(.22,1,.36,1), opacity .3s';
    modal.style.transform='none'; modal.style.opacity='1';
    // select first cell
    if(!cur){ var f=DATA.across[0].cells[0]; select(f[0],f[1]); dir='A'; }
    render(); checkWin();
    setTimeout(ensureFocus, 60);
  }
  function closeOverlay(){
    var tr=teaser.getBoundingClientRect();
    var mr=modal.getBoundingClientRect();
    var sx=tr.width/mr.width, sy=tr.height/mr.height;
    var tx=tr.left-mr.left, ty=tr.top-mr.top;
    modal.style.transition='transform .34s cubic-bezier(.4,0,.6,1), opacity .3s';
    modal.style.transform='translate('+tx+'px,'+ty+'px) scale('+sx+','+sy+')';
    modal.style.opacity='0.2';
    overlay.classList.remove('show'); // backdrop fades via class
    overlayOpen=false; document.body.style.overflow='';
    overlay.setAttribute('aria-hidden','true');
    try{ teaser.focus(); }catch(e){}
    setTimeout(function(){ if(!overlayOpen){ modal.style.transform='none'; modal.style.opacity='1'; modal.style.transition='none'; } }, 360);
  }
  // backdrop visible during close: keep overlay shown until anim done
  function closeOverlayAnimated(){
    var tr=teaser.getBoundingClientRect();
    var mr=modal.getBoundingClientRect();
    var sx=tr.width/mr.width, sy=tr.height/mr.height;
    var tx=tr.left-mr.left, ty=tr.top-mr.top;
    backdrop.style.transition='opacity .34s'; backdrop.style.opacity='0';
    modal.style.transition='transform .34s cubic-bezier(.4,0,.6,1), opacity .3s';
    modal.style.transformOrigin='top left';
    modal.style.transform='translate('+tx+'px,'+ty+'px) scale('+sx+','+sy+')';
    modal.style.opacity='0.15';
    overlayOpen=false; document.body.style.overflow=''; overlay.setAttribute('aria-hidden','true');
    try{ teaser.focus(); }catch(e){}
    setTimeout(function(){
      overlay.classList.remove('show');
      modal.style.transition='none'; modal.style.transform='none'; modal.style.opacity='1';
      backdrop.style.opacity=''; backdrop.style.transition='';
    }, 340);
  }
  teaser.addEventListener('click', openOverlay);
  teaser.addEventListener('keydown', function(e){ if(e.key==='Enter'||e.key===' '){ e.preventDefault(); openOverlay(); } });
  document.getElementById('closeBtn').addEventListener('click', closeOverlayAnimated);
  backdrop.addEventListener('click', closeOverlayAnimated);

  // Global hook: let the Klickfang popup CTA reuse this same overlay
  window.openKreuzwortraetsel = openOverlay;

  render(); checkWin();
})();
