(function () {
  'use strict';

  var filter = document.querySelector('[data-tournament-filter]');
  if (!filter) return;

  var buttons = Array.prototype.slice.call(
    filter.querySelectorAll('[data-category-filter]')
  );
  var sections = Array.prototype.slice.call(
    document.querySelectorAll('[data-tournament-category]')
  );
  var emptyMessage = document.querySelector('[data-tournament-filter-empty]');

  buttons.forEach(function (button) {
    button.addEventListener('click', function () {
      var selected = button.getAttribute('data-category-filter');
      buttons.forEach(function (item) {
        var active = item === button;
        item.setAttribute('aria-pressed', String(active));
        item.classList.toggle('color-primary', active);
        item.classList.toggle('is-outlined', !active);
      });

      var hasTournaments = false;
      sections.forEach(function (section) {
        section.hidden = selected !== 'ALL' && (
          section.getAttribute('data-tournament-category') !== selected ||
          section.getAttribute('data-tournament-count') === '0'
        );
        if (!section.hidden && section.getAttribute('data-tournament-count') !== '0') {
          hasTournaments = true;
        }
      });
      if (emptyMessage) {
        emptyMessage.hidden = selected === 'ALL' || hasTournaments || !sections.length;
      }
    });
  });

  filter.hidden = false;
})();
