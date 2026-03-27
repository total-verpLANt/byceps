(function () {
  'use strict';

  var el = document.getElementById('tournament-list');
  if (!el) return;

  var scriptTag = document.querySelector('script[data-sort-url]');
  var sortUrl = scriptTag ? scriptTag.getAttribute('data-sort-url') : null;
  if (!sortUrl) return;

  Sortable.create(el, {
    handle: '.drag-handle',
    ghostClass: 'sortable-ghost',
    chosenClass: 'sortable-chosen',
    animation: 150,
    onEnd: function () {
      var rows = el.querySelectorAll('tr[data-tournament-id]');
      var ids = [];
      for (var i = 0; i < rows.length; i++) {
        ids.push(rows[i].getAttribute('data-tournament-id'));
      }

      fetch(sortUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tournament_ids: ids})
      })
      .then(function (response) {
        if (!response.ok) {
          console.error('Sort failed (HTTP ' + response.status + '), reloading.');
          window.location.reload();
        }
      })
      .catch(function (err) {
        console.error('Sort request error:', err);
        window.location.reload();
      });
    }
  });
})();
