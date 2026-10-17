(function () {
  'use strict';

  var STORAGE_KEY = 'pizza_delivery_admin_sort';

  var table = document.querySelector('table[data-pizza-delivery-sortable]');
  if (!table || !table.tBodies.length) return;

  var tbody = table.tBodies[0];
  var headers = Array.prototype.slice.call(
    table.querySelectorAll('thead th[data-sort-key]')
  );
  var originalRows = Array.prototype.slice.call(tbody.rows);
  var collator = new Intl.Collator(
    document.documentElement.lang || undefined,
    {numeric: true, sensitivity: 'base'}
  );

  function findHeader(column) {
    for (var i = 0; i < headers.length; i++) {
      if (headers[i].dataset.sortKey === column) return headers[i];
    }
    return null;
  }

  function readState() {
    var state;
    try {
      state = JSON.parse(localStorage.getItem(STORAGE_KEY));
    } catch (e) {
      return null;
    }
    if (!state || typeof state !== 'object') return null;
    if (typeof state.column !== 'string' || !findHeader(state.column)) {
      return null;
    }
    if (state.dir !== 'asc' && state.dir !== 'desc') return null;
    return {column: state.column, dir: state.dir};
  }

  function writeState(state) {
    try {
      if (state) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
      } else {
        localStorage.removeItem(STORAGE_KEY);
      }
    } catch (e) {
      // Storage unavailable: the sort still applies to this page view.
    }
  }

  function sortValue(row, cellIndex) {
    var cell = row.cells[cellIndex];
    return cell ? (cell.dataset.sortValue || '') : '';
  }

  function compareRows(cellIndex, dir) {
    return function (a, b) {
      var va = sortValue(a, cellIndex);
      var vb = sortValue(b, cellIndex);
      var result;
      if (va === vb) {
        result = 0;
      } else if (va === '') {
        result = 1;
      } else if (vb === '') {
        result = -1;
      } else {
        result = collator.compare(va, vb);
      }
      if (dir === 'desc') result = -result;
      return result || originalRows.indexOf(a) - originalRows.indexOf(b);
    };
  }

  function applyState(state) {
    var rows = originalRows;
    if (state) {
      var cellIndex = findHeader(state.column).cellIndex;
      rows = originalRows.slice().sort(compareRows(cellIndex, state.dir));
    }

    var fragment = document.createDocumentFragment();
    rows.forEach(function (row) {
      fragment.appendChild(row);
    });
    tbody.appendChild(fragment);

    headers.forEach(function (th) {
      if (state && th.dataset.sortKey === state.column) {
        th.setAttribute(
          'aria-sort', state.dir === 'asc' ? 'ascending' : 'descending'
        );
      } else {
        th.removeAttribute('aria-sort');
      }
    });
  }

  function nextState(state, column) {
    if (!state || state.column !== column) {
      return {column: column, dir: 'asc'};
    }
    if (state.dir === 'asc') {
      return {column: column, dir: 'desc'};
    }
    return null;
  }

  var currentState = readState();

  headers.forEach(function (th) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'pizza-sort-button';
    while (th.firstChild) {
      button.appendChild(th.firstChild);
    }
    th.appendChild(button);

    button.addEventListener('click', function () {
      currentState = nextState(currentState, th.dataset.sortKey);
      writeState(currentState);
      applyState(currentState);
    });
  });

  if (currentState) applyState(currentState);
})();
