/* bote-bracket.js — vanilla scroll-in reveal for the Rentenbote bracket SVG.
 * Adds .in-view to any .bracket-svg when it crosses ~35% into the viewport,
 * which activates the stroke-dashoffset transition declared in the CSS.
 * No framework, no jQuery, no bundler. No-ops if no .bracket-svg is present
 * or IntersectionObserver is unavailable. */
(function () {
  const svgs = document.querySelectorAll('.bracket-svg');
  if (!svgs.length || !('IntersectionObserver' in window)) return;
  const io = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) entry.target.classList.add('in-view');
    });
  }, { threshold: 0.35 });
  svgs.forEach(function (s) { io.observe(s); });
})();
