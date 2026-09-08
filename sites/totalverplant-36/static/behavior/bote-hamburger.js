/* bote-hamburger.js — vanilla nav behaviour for the Rentenbote (№36) header.
 * Ported from NavStrip (bote-frontpage.jsx). No framework, no jQuery, no bundler.
 * Targets: button.nav-hamburger and nav.nav-panel. Loaded on every page; no-ops
 * when either .nav-hamburger or .nav-panel is missing from the DOM. */
document.addEventListener('DOMContentLoaded', () => {
  const hamburger = document.querySelector('.nav-hamburger'); // button.nav-hamburger
  const panel = document.querySelector('.nav-panel');         // nav.nav-panel
  if (!hamburger || !panel) return; // page without .nav-hamburger / .nav-panel

  const setLabel = (text) => {
    const label = hamburger.querySelector('.label');
    if (label) { label.textContent = text; return; }
    // Fallback: replace the last text node so we keep child spans (.bars) intact.
    const nodes = hamburger.childNodes;
    for (let i = nodes.length - 1; i >= 0; i--) {
      if (nodes[i].nodeType === Node.TEXT_NODE) {
        nodes[i].nodeValue = text;
        return;
      }
    }
    hamburger.appendChild(document.createTextNode(text));
  };

  const openPanel = () => {
    hamburger.classList.add('open');  // .nav-hamburger.open
    panel.classList.add('open');      // .nav-panel.open
    // Lock document scroll while the rail is open so gestures that start
    // outside the scrolling link list don't scroll the page behind it
    // (CSS: html.nav-open { overflow: hidden } at <=720px).
    document.documentElement.classList.add('nav-open');
    hamburger.setAttribute('aria-expanded', 'true');
    setLabel('Schließen');
  };

  const closePanel = () => {
    hamburger.classList.remove('open'); // remove .open from .nav-hamburger
    panel.classList.remove('open');     // remove .open from .nav-panel
    document.documentElement.classList.remove('nav-open'); // restore page scroll
    hamburger.setAttribute('aria-expanded', 'false');
    setLabel('Menü');
  };

  hamburger.addEventListener('click', () => {
    if (panel.classList.contains('open')) closePanel();
    else openPanel();
  });

  // Delegated smooth-scroll inside the slide-out panel — closes after navigating.
  panel.addEventListener('click', (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (!link || !panel.contains(link)) return;
    const href = link.getAttribute('href');
    if (!href || href === '#') return;
    const target = document.querySelector(href);
    event.preventDefault();
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    closePanel();
  });

  // Delegated smooth-scroll on desktop crumbs — does NOT close the panel.
  const crumbs = document.querySelector('.navstrip .crumbs');
  if (crumbs) {
    crumbs.addEventListener('click', (event) => {
      const link = event.target.closest('a[href^="#"]');
      if (!link || !crumbs.contains(link)) return;
      const href = link.getAttribute('href');
      if (!href || href === '#') return;
      const target = document.querySelector(href);
      event.preventDefault();
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!panel.classList.contains('open')) return;
    closePanel();
    hamburger.focus();
  });
});
