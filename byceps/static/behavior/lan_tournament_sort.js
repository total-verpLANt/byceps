(function () {
  'use strict';

  var groups = Array.prototype.slice.call(
    document.querySelectorAll('[data-tournament-group]')
  );
  if (!groups.length) return;

  var scriptTag = document.querySelector('script[data-sort-url]');
  var sortUrl = scriptTag ? scriptTag.getAttribute('data-sort-url') : null;
  if (!sortUrl) return;

  var sortables = [];
  function setDisabled(disabled) {
    sortables.forEach(function (sortable) {
      sortable.option('disabled', disabled);
    });
  }

  groups.forEach(function (group) {
    sortables.push(Sortable.create(group, {
      handle: '.drag-handle',
      draggable: 'tr[data-tournament-id]',
      ghostClass: 'sortable-ghost',
      chosenClass: 'sortable-chosen',
      animation: 150,
      onEnd: function () {
        setDisabled(true);
        var ids = [];
        groups.forEach(function (group) {
          var rows = group.querySelectorAll('tr[data-tournament-id]');
          for (var i = 0; i < rows.length; i++) {
            ids.push(rows[i].getAttribute('data-tournament-id'));
          }
        });

        fetch(sortUrl, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({tournament_ids: ids})
        })
        .then(function (response) {
          if (!response.ok) {
            console.error('Sort failed (HTTP ' + response.status + '), reloading.');
            window.location.reload();
          } else {
            setDisabled(false);
          }
        })
        .catch(function (err) {
          console.error('Sort request error:', err);
          window.location.reload();
        });
      }
    }));
  });
})();
