/*
 * Live confirm counter of the maintenance preview.
 *
 * The button label states what the delete does with the current selection.
 * This is convenience only: the server rechecks every posted key. All label
 * templates come from the `data-label-*` attributes of the form, so this
 * file holds no UI strings.
 *
 * Node: `module.exports`. Browser: the global `LtMaintenance`.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.LtMaintenance = factory();
    if (typeof document !== 'undefined') {
      root.LtMaintenance.init(document);
    }
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MEBIBYTE = 1048576;

  function numberFormat(locale, options) {
    try {
      return new Intl.NumberFormat(locale, options);
    } catch (e) {
      return new Intl.NumberFormat(undefined, options);
    }
  }

  // Round half to even, like the server's `round`.
  function roundHalfEven(value) {
    var floor = Math.floor(value);
    var diff = value - floor;
    if (diff < 0.5) return floor;
    if (diff > 0.5) return floor + 1;
    return floor % 2 === 0 ? floor : floor + 1;
  }

  function formatSize(bytes, locale) {
    if (bytes <= 0) {
      return numberFormat(locale, { maximumFractionDigits: 0 }).format(0) + ' KB';
    }
    if (bytes < MEBIBYTE) {
      var kilobytes = Math.max(1, roundHalfEven(bytes / 1024));
      return (
        numberFormat(locale, { maximumFractionDigits: 0 }).format(kilobytes) +
        ' KB'
      );
    }
    return (
      numberFormat(locale, {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1
      }).format(bytes / MEBIBYTE) + ' MB'
    );
  }

  function confirmLabel(count, bytes, labels, locale) {
    if (count === 0) return labels.none;
    var template = count === 1 ? labels.one : labels.many;
    var size = formatSize(bytes, locale);
    return template
      .replace('%(count)s', function () {
        return String(count);
      })
      .replace('%(size)s', function () {
        return size;
      });
  }

  // Disable the button once the submit has started (never before, or the
  // browser would drop the click); a second click would post the keys twice.
  function guardSubmit(form, go, defer) {
    form.addEventListener('submit', function () {
      defer(function () {
        go.disabled = true;
      });
    });
  }

  function bind(form) {
    var boxes = Array.prototype.slice.call(
      form.querySelectorAll('input[type=checkbox][name=key]')
    );
    var all = form.querySelector('[data-maint-all]');
    var go = form.querySelector('[data-maint-go]');
    var labels = {
      one: form.getAttribute('data-label-one'),
      many: form.getAttribute('data-label-many'),
      none: form.getAttribute('data-label-none')
    };
    var locale = form.getAttribute('data-locale') || undefined;

    if (!go || labels.one === null || labels.many === null || labels.none === null) {
      return;
    }

    function update() {
      var count = 0;
      var bytes = 0;
      boxes.forEach(function (box) {
        var row = box.closest('.lt-maint-item');
        if (row) row.classList.toggle('is-off', !box.checked);
        if (box.checked) {
          count += 1;
          bytes += Number(box.getAttribute('data-bytes')) || 0;
        }
      });
      go.disabled = count === 0;
      go.textContent = confirmLabel(count, bytes, labels, locale);
      if (all) {
        all.checked = count === boxes.length;
        all.indeterminate = count > 0 && count < boxes.length;
      }
    }

    guardSubmit(form, go, function (fn) {
      setTimeout(fn, 0);
    });
    if (form.ownerDocument.defaultView) {
      form.ownerDocument.defaultView.addEventListener('pageshow', function (e) {
        if (e.persisted) update();
      });
    }
    boxes.forEach(function (box) {
      box.addEventListener('change', update);
    });
    if (all) {
      all.hidden = false;
      all.addEventListener('change', function () {
        boxes.forEach(function (box) {
          box.checked = all.checked;
        });
        update();
      });
    }
    update();
  }

  function init(doc) {
    Array.prototype.forEach.call(
      doc.querySelectorAll('[data-maint-form]'),
      bind
    );
  }

  return {
    formatSize: formatSize,
    confirmLabel: confirmLabel,
    guardSubmit: guardSubmit,
    init: init
  };
});
