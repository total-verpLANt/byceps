(function () {
  'use strict';

  document.querySelectorAll('[data-category-dropdown]').forEach(function (dropdown) {
    dropdown.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && dropdown.open) {
        dropdown.open = false;
        dropdown.querySelector('summary').focus();
      }
    });
    document.addEventListener('click', function (event) {
      if (!dropdown.contains(event.target)) dropdown.open = false;
    });
  });
})();
