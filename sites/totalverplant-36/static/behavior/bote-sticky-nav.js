/* bote-sticky-nav.js — keeps the Rentenbote (№36) nav strip reachable while
 * scrolling. The header sheet is `position: sticky` with a negative top offset
 * (see totalverplant-36.css), so everything above the .navstrip — topstrip,
 * masthead, rules — scrolls away and the nav row stays pinned at the viewport
 * top. This script only measures two custom properties:
 *   --bote-mast-h  distance from the header top to the navstrip top
 *   --bote-nav-h   navstrip height (drives scroll-padding-top for anchors)
 * and toggles .is-pinned on the header for the pinned-state bottom rule.
 * No framework. No-ops when .page-header-fixed or .navstrip is missing. */
document.addEventListener('DOMContentLoaded', () => {
  const header = document.querySelector('.page-header-fixed');
  const navstrip = header ? header.querySelector('.navstrip') : null;
  if (!header || !navstrip) return;

  const rootStyle = document.documentElement.style;

  const measure = () => {
    // Both rects move together when the header is stuck, so the difference is
    // stable regardless of scroll position.
    const headerTop = header.getBoundingClientRect().top;
    const navRect = navstrip.getBoundingClientRect();
    const mastHeight = Math.max(0, Math.round(navRect.top - headerTop));
    rootStyle.setProperty('--bote-mast-h', `${mastHeight}px`);
    rootStyle.setProperty('--bote-nav-h', `${Math.round(navRect.height)}px`);
  };

  let scrollScheduled = false;
  const updatePinned = () => {
    scrollScheduled = false;
    // When pinned, the navstrip sits flush with the viewport top.
    header.classList.toggle('is-pinned', navstrip.getBoundingClientRect().top <= 1);
  };

  measure();
  updatePinned();

  window.addEventListener('resize', () => {
    measure();
    updatePinned();
  });

  // Web fonts change the masthead metrics once they swap in.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => {
      measure();
      updatePinned();
    });
  }

  window.addEventListener('scroll', () => {
    if (scrollScheduled) return;
    scrollScheduled = true;
    requestAnimationFrame(updatePinned);
  }, { passive: true });
});
