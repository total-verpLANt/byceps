/*
 * Image uploader and existing-image picker of the tournament creation wizard.
 *
 * This is convenience only. The server checks type, size, dimensions,
 * ownership and attachability again on upload and on the final POST. The
 * form keeps working without this file: the plain file input posts to the
 * server as before.
 *
 * What it does for `[data-wiz-image]`:
 *   - removes `name` from the file input, so the file never travels with the
 *     create POST; the hidden `image_id` carries the chosen image instead
 *   - uploads the chosen or dropped file through XHR with a progress bar
 *   - offers the images of this party or brand (`[data-wiz-lib-open]`)
 *   - deletes an image this page uploaded when it is replaced or removed
 *   - shows the chosen image as a row that replaces the drop zone, with the
 *     file input visually hidden behind a button-looking label
 *
 * Text comes only from the JSON config island (`strings`, keyed by the
 * English msgid) via `t(msgid, params)`. All DOM patching goes through
 * `textContent` and attribute setters; user- or server-derived strings never
 * reach a markup-parsing setter. Served image paths must be same-origin
 * absolute paths.
 *
 * The controller (`lan_tournament_create_wizard.js`) hears about every state
 * change through an event on `document`:
 *   - out: `lt-wizard:image-state`, detail
 *          {status: 'none'|'uploading'|'done'|'error', imageId: string|null,
 *           filename: string|null, url: string|null, width: number|null,
 *           height: number|null, byteSize: number|null, usedBy: string[],
 *           pct: number|null}
 *   - in:  `lt-wizard:image-restore`, same detail, fired once after a
 *          session restore
 */
(function () {
  'use strict';

  var ALLOWED_TYPES = ['image/jpeg', 'image/png', 'image/webp'];
  var ALLOWED_EXTENSIONS = /\.(jpe?g|png|webp)$/i;
  var UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  var SEARCH_DEBOUNCE_MS = 300;
  var DEFAULT_UPLOAD_BYTES = 5 * 1024 * 1024;
  var NO_FORM = 'lt-wiz-lib-void';

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
  }

  function qs(root, selector) {
    return root.querySelector(selector);
  }

  function setShown(node, shown) {
    node.hidden = !shown;
    node.style.display = shown ? '' : 'none';
  }

  function readConfig() {
    var island = document.getElementById('lt-create-wizard-config');
    if (!island) {
      return null;
    }
    try {
      return JSON.parse(island.textContent);
    } catch (e) {
      return null;
    }
  }

  function safeUrl(value) {
    if (
      typeof value !== 'string' || value.charAt(0) !== '/'
      || /[\u0000-\u001f\u007f]/.test(value)
    ) {
      return null;
    }
    var url;
    try {
      url = new URL(value, location.href);
    } catch (e) {
      return null;
    }
    return url.origin === location.origin && url.pathname.charAt(0) === '/'
      ? value
      : null;
  }

  function safeId(value) {
    return typeof value === 'string' && UUID_PATTERN.test(value) ? value : null;
  }

  function safeDimension(value) {
    var number = Number(value);
    return isFinite(number) && number > 0 ? Math.round(number) : null;
  }

  function safeBytes(value) {
    var number = Number(value);
    return isFinite(number) && number >= 0 ? number : null;
  }

  function safeNames(value) {
    return Array.isArray(value)
      ? value.map(function (name) { return String(name); })
      : [];
  }

  function formatMegabytes(bytes) {
    var value = bytes / 1048576;
    try {
      return value.toLocaleString(document.documentElement.lang || undefined, {
        minimumFractionDigits: 1,
        maximumFractionDigits: 1
      }) + ' MB';
    } catch (e) {
      return value.toFixed(1) + ' MB';
    }
  }

  function formatSize(bytes) {
    if (bytes === null) {
      return '';
    }
    return bytes < 1048576
      ? Math.max(1, Math.round(bytes / 1024)) + ' KB'
      : formatMegabytes(bytes);
  }

  function formatPercent(pct, locale) {
    try {
      return new Intl.NumberFormat(locale || undefined, {style: 'percent'})
        .format(pct / 100);
    } catch (e) {
      return pct + '%';
    }
  }

  function parseJson(text) {
    try {
      var data = JSON.parse(text);
      return data && typeof data === 'object' ? data : null;
    } catch (e) {
      return null;
    }
  }

  function init() {
    var root = qs(document, '[data-wiz-image]');
    var form = qs(document, 'form[data-lt-wizard]');
    var config = readConfig();
    if (!root || !form || !config) {
      return;
    }
    var urls = config.urls || {};
    var limits = config.limits || {};
    var strings = config.strings || {};
    var fileInput = qs(root, 'input[data-wiz-file]');
    var dropEl = qs(root, '[data-wiz-drop]');
    var libOpen = qs(root, '[data-wiz-lib-open]');
    var hiddenId = qs(form, 'input[name="image_id"]');
    if (!fileInput || !dropEl || !hiddenId || !urls.upload
        || typeof XMLHttpRequest !== 'function') {
      return;
    }
    var maxBytes = safeBytes(limits.uploadBytes) || DEFAULT_UPLOAD_BYTES;
    var announceEl = qs(document, '[data-wiz-announce]');
    var errImageServer = qs(root, '[data-wiz-err-for="image"]');
    var errImageId = qs(root, '[data-wiz-err-for="image_id"]');
    var stagedEl = qs(root, '[data-wiz-image-staged]');
    var altWrap = qs(root, '[data-wiz-field="image_alt_text"]');
    var altInput = qs(root, 'input[name="image_alt_text"]');
    var urlAlt = qs(root, 'details.lt-wiz-urlalt');

    var current = blankState();
    var upload = {seq: 0, xhr: null, previous: null};
    var lib = {
      open: false, scope: 'party', q: '', page: 1, hasNext: false, seq: 0,
      items: [], selected: null, loading: false, failed: false, timer: null
    };
    var live = {};
    var panel = {};
    var drop = {};
    var pick = {};
    var placed = '';

    function t(msgid, params) {
      var text = Object.prototype.hasOwnProperty.call(strings, msgid)
        ? strings[msgid]
        : msgid;
      return String(text).replace(/%\((\w+)\)[sd]/g, function (whole, key) {
        return params && params[key] !== undefined ? String(params[key]) : whole;
      });
    }

    function announce(text) {
      if (announceEl) {
        announceEl.textContent = text;
      }
    }

    function blankState() {
      return {
        status: 'none', imageId: null, filename: null, url: null,
        width: null, height: null, byteSize: null, usedBy: [], pct: null,
        source: null
      };
    }

    /* ---------- errors, state, events ---------- */

    function paintError(container, text) {
      if (!container) {
        return;
      }
      container.textContent = '';
      if (!text) {
        return;
      }
      var list = el('ol', 'form-errors');
      var item = el('li');
      item.appendChild(el('strong', null, t('Error') + ':'));
      item.appendChild(document.createTextNode(' '));
      item.appendChild(el('span', null, text));
      list.appendChild(item);
      container.appendChild(list);
    }

    function clearErrors() {
      paintError(errImageServer, null);
      paintError(live.error, null);
      paintError(errImageId, null);
    }

    function emit() {
      document.dispatchEvent(new CustomEvent('lt-wizard:image-state', {
        detail: {
          status: current.status,
          imageId: current.imageId,
          filename: current.filename,
          url: current.url,
          width: current.width,
          height: current.height,
          byteSize: current.byteSize,
          usedBy: current.usedBy,
          pct: current.pct
        }
      }));
    }

    function setState(next, silent) {
      var state = blankState();
      Object.keys(next).forEach(function (key) {
        state[key] = next[key];
      });
      if (state.status !== 'uploading') {
        dropPreview();
      }
      current = state;
      hiddenId.value = state.status === 'done' && state.imageId
        ? state.imageId
        : '';
      renderLive();
      if (!silent) {
        emit();
      }
    }

    /* ---------- image row ---------- */

    var previewUrl = null;

    function dropPreview() {
      if (previewUrl && window.URL && typeof URL.revokeObjectURL === 'function') {
        URL.revokeObjectURL(previewUrl);
      }
      previewUrl = null;
    }

    function makePreview(file) {
      dropPreview();
      if (
        ALLOWED_TYPES.indexOf(file.type) !== -1 && window.URL
        && typeof URL.createObjectURL === 'function'
      ) {
        try {
          previewUrl = URL.createObjectURL(file);
        } catch (e) {
          previewUrl = null;
        }
      }
      return previewUrl;
    }

    function buildLive() {
      live.root = el('div', 'lt-wiz-img');
      live.root.setAttribute('data-wiz-image-live', '');
      live.root.setAttribute('tabindex', '-1');
      live.thumb = el('div', 'lt-wiz-img__thumb');
      live.thumb.setAttribute('aria-hidden', 'true');
      live.img = el('img');
      live.img.setAttribute('alt', '');
      live.img.addEventListener('error', function () {
        setShown(live.img, false);
        setShown(live.thumbText, true);
      });
      live.thumbText = el('span');
      live.thumb.appendChild(live.img);
      live.thumb.appendChild(live.thumbText);
      var meta = el('div', 'lt-wiz-img__meta');
      live.name = el('strong', 'lt-wiz-img__name');
      live.name.id = 'wiz-image-name';
      live.dims = el('div', 'dimmed lt-wiz-img__dims');
      live.error = el('div', 'lt-wiz-img__error');
      live.error.setAttribute('data-wiz-err-for', 'image');
      live.bar = el('div', 'lt-wiz-img__pbar');
      live.bar.setAttribute('role', 'progressbar');
      live.bar.setAttribute('aria-labelledby', 'wiz-image-name');
      live.bar.setAttribute('aria-valuemin', '0');
      live.bar.setAttribute('aria-valuemax', '100');
      live.fill = el('i');
      live.bar.appendChild(live.fill);
      live.status = el('div', 'form-caption lt-wiz-img__status');
      live.status.id = 'wiz-image-status';
      live.actions = el('div', 'button-row lt-wiz-img__actions');
      live.button = el('button', 'button is-compact');
      live.button.type = 'button';
      live.button.addEventListener('click', onLiveAction);
      live.actions.appendChild(live.button);
      [live.name, live.dims, live.error, live.bar, live.status, live.actions]
        .forEach(function (node) { meta.appendChild(node); });
      if (altWrap) {
        meta.appendChild(altWrap);
      }
      live.root.appendChild(live.thumb);
      live.root.appendChild(meta);
      root.insertBefore(live.root, dropEl);
      var describedBy = fileInput.getAttribute('aria-describedby');
      fileInput.setAttribute(
        'aria-describedby',
        (describedBy ? describedBy + ' ' : '') + 'wiz-image-status'
      );
    }

    // The file input and the library button follow the visible actions.
    function placeControls(status) {
      var key = status === 'none' ? 'drop' : status === 'uploading' ? 'park' : 'row';
      if (key === placed) {
        return;
      }
      placed = key;
      var focused = document.activeElement;
      var hadFocus = focused === fileInput || focused === libOpen;
      var host = key === 'row' ? live.actions : drop.actions;
      host.appendChild(pick.label);
      host.appendChild(fileInput);
      if (libOpen) {
        host.appendChild(libOpen);
      }
      if (key === 'row') {
        host.appendChild(live.button);
      }
      if (hadFocus) {
        (key === 'park' ? live.root : focused).focus();
      }
    }

    function pickText(status) {
      return status === 'done'
        ? t('Upload new …')
        : status === 'error' ? t('Choose another file …') : t('Choose file …');
    }

    function statusText() {
      var status = current.status;
      if (status === 'uploading') {
        return t(
          'Uploading … %(pct)s · You can continue meanwhile.',
          {pct: formatPercent(current.pct || 0, config.locale)}
        );
      }
      if (status === 'error') {
        return t('Your other entries are kept.');
      }
      var text = current.source === 'library'
        ? (current.usedBy.length
          ? t('From existing images, also used by %(names)s.',
            {names: current.usedBy.join(', ')})
          : t('From existing images.')) + ' '
          + t('The preview shows the 16:9 crop from the tournament list.')
        : t('Uploaded and checked. The preview shows the 16:9 crop from the tournament list.');
      return '✓ ' + text;
    }

    function renderLive() {
      var status = current.status;
      var shown = status !== 'none';
      setShown(live.root, shown);
      setShown(dropEl, !shown);
      live.root.classList.toggle('is-error', status === 'error');
      live.root.classList.toggle('is-uploading', status === 'uploading');
      if (status === 'error') {
        live.root.setAttribute('role', 'alert');
      } else {
        live.root.removeAttribute('role');
      }
      pick.label.textContent = pickText(status);
      placeControls(status);
      if (altWrap) {
        setShown(altWrap, status === 'done');
      }
      if (!shown) {
        return;
      }
      var src = status === 'done'
        ? safeUrl(current.url)
        : status === 'uploading' ? previewUrl : null;
      live.thumb.classList.toggle('lt-wiz-img__thumb--err', status === 'error');
      setShown(live.img, !!src);
      if (src) {
        live.img.setAttribute('src', src);
      } else {
        live.img.removeAttribute('src');
      }
      live.thumbText.textContent = status === 'error'
        ? '✕'
        : status === 'uploading'
          ? t('Preview')
          : t('Tournament image') + ' 16:9';
      setShown(live.thumbText, !src);
      live.name.textContent = status === 'error'
        ? t('%(file)s was not accepted', {file: current.filename || ''})
        : (current.filename || '');
      var dims = [];
      if (status !== 'error' && current.byteSize !== null) {
        dims.push(formatSize(current.byteSize));
      }
      if (status === 'done' && current.width && current.height) {
        dims.push(current.width + ' × ' + current.height);
      }
      live.dims.textContent = dims.join(' · ');
      setShown(live.dims, dims.length > 0);
      setShown(live.error, status === 'error');
      setShown(live.bar, status === 'uploading');
      if (status === 'uploading') {
        var pct = current.pct || 0;
        live.bar.setAttribute('aria-valuenow', String(pct));
        live.fill.style.width = pct + '%';
      }
      live.status.textContent = statusText();
      live.button.textContent = status === 'uploading'
        ? t('Cancel upload')
        : (status === 'error' ? t('Continue without image') : t('Remove'));
    }

    function onLiveAction() {
      if (current.status === 'uploading' && upload.previous) {
        // Cancelling a replacement keeps the image that was staged before.
        var previous = upload.previous;
        upload.previous = null;
        abortUpload();
        clearErrors();
        setState(previous);
        fileInput.focus();
        return;
      }
      upload.previous = null;
      abortUpload();
      releaseCurrent();
      clearErrors();
      clearAlt();
      setState({status: 'none'});
      fileInput.focus();
      announce(t('Image removed.'));
    }

    function clearAlt() {
      if (altInput && altInput.value !== '') {
        altInput.value = '';
        altInput.dispatchEvent(new Event('input', {bubbles: true}));
      }
    }

    /* ---------- delete of images this page uploaded ---------- */

    function releaseCurrent() {
      releaseState(current);
    }

    function releaseState(state) {
      var id = state && state.source === 'upload' ? safeId(state.imageId) : null;
      if (!id || !urls.delete_template || typeof fetch !== 'function') {
        return;
      }
      try {
        fetch(urls.delete_template.replace('__ID__', encodeURIComponent(id)), {
          method: 'DELETE',
          credentials: 'same-origin',
          headers: {Accept: 'application/json'}
        }).catch(function () {});
      } catch (e) {
        // Best effort: unused uploads are cleaned up on the Wartung tab.
      }
    }

    /* ---------- upload ---------- */

    function abortUpload() {
      upload.seq += 1;
      if (upload.xhr) {
        try {
          upload.xhr.abort();
        } catch (e) {
          // Already finished.
        }
        upload.xhr = null;
      }
    }

    function precheck(file) {
      var typeOk = ALLOWED_TYPES.indexOf(file.type) !== -1
        || (!file.type && ALLOWED_EXTENSIONS.test(file.name));
      if (!typeOk) {
        return /svg/i.test(file.type)
          ? t('The file is not a supported image (SVG). Allowed are JPEG, PNG and WebP.')
          : t('The file is not a supported image. Allowed are JPEG, PNG and WebP.');
      }
      if (file.size > maxBytes) {
        return t('The file is %(size)s. The maximum is 5 MB.', {
          size: formatMegabytes(file.size)
        });
      }
      return null;
    }

    function fail(filename, reason) {
      abortUpload();
      clearAlt();
      setState({status: 'error', filename: filename});
      paintError(live.error, reason);
    }

    // A refused replacement puts the previously staged image back.
    function reject(filename, reason) {
      var previous = upload.previous;
      upload.previous = null;
      if (previous) {
        abortUpload();
        setState(previous);
        paintError(errImageServer, reason);
        return;
      }
      fail(filename, reason);
    }

    function startUpload(file) {
      var problem = precheck(file);
      if (problem) {
        clearErrors();
        if (current.status === 'done' || current.status === 'uploading') {
          paintError(errImageServer, problem);
        } else {
          fail(file.name, problem);
        }
        return;
      }
      var previous = current.status === 'uploading'
        ? upload.previous
        : (current.status === 'done' ? current : null);
      abortUpload();
      clearErrors();
      upload.previous = previous;
      var seq = upload.seq;
      var xhr = new XMLHttpRequest();
      upload.xhr = xhr;
      makePreview(file);
      setState({
        status: 'uploading', filename: file.name, byteSize: file.size, pct: 0
      });
      xhr.open('POST', urls.upload);
      xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.onprogress = function (event) {
        if (seq !== upload.seq || !event.lengthComputable || !event.total) {
          return;
        }
        var pct = Math.min(100, Math.round(event.loaded / event.total * 100));
        if (pct !== current.pct) {
          current.pct = pct;
          renderLive();
          emit();
        }
      };
      xhr.onload = function () {
        if (seq !== upload.seq) {
          return;
        }
        upload.xhr = null;
        onUploadResponse(file, xhr);
      };
      xhr.onerror = xhr.ontimeout = function () {
        if (seq !== upload.seq) {
          return;
        }
        upload.xhr = null;
        reject(file.name, t('Upload failed. Please try again. Your other entries are kept.'));
      };
      var body = new FormData();
      body.append('image', file, file.name);
      xhr.send(body);
    }

    function onUploadResponse(file, xhr) {
      var data = parseJson(xhr.responseText);
      var id = data ? safeId(data.image_id) : null;
      var url = data ? safeUrl(data.url) : null;
      if (xhr.status === 201 && id && url) {
        var replaced = upload.previous;
        upload.previous = null;
        setState({
          status: 'done',
          imageId: id,
          filename: typeof data.filename === 'string' && data.filename
            ? data.filename
            : file.name,
          url: url,
          width: safeDimension(data.width),
          height: safeDimension(data.height),
          byteSize: safeBytes(data.byte_size),
          source: 'upload'
        });
        if (replaced && replaced.imageId !== id) {
          releaseState(replaced);
        }
        announce(t('Uploaded and checked. The preview shows the 16:9 crop from the tournament list.'));
        return;
      }
      if (xhr.status === 201 && id) {
        // Unusable URL: give the image back rather than staging it blind.
        current.imageId = id;
        current.source = 'upload';
        releaseCurrent();
      }
      var serverError = data && typeof data.error === 'string' && data.error
        ? data.error
        : null;
      if (xhr.status >= 400 && xhr.status < 500 && serverError
          && xhr.status !== 403 && xhr.status !== 404) {
        reject(file.name, serverError);
      } else if (xhr.status === 413) {
        // nginx answers with an HTML page, not JSON.
        reject(file.name, t('The file is %(size)s. The maximum is 5 MB.', {
          size: formatMegabytes(file.size)
        }));
      } else {
        reject(file.name, t('Upload failed. Please try again. Your other entries are kept.'));
      }
    }

    /* ---------- file input and drop zone ---------- */

    function onFile(file) {
      if (file) {
        startUpload(file);
      }
    }

    fileInput.removeAttribute('name');
    fileInput.addEventListener('change', function () {
      var file = fileInput.files && fileInput.files[0];
      onFile(file);
      try {
        fileInput.value = '';
      } catch (e) {
        // Old browsers refuse to reset a file input.
      }
    });

    var hintEl = null;
    var coarse = typeof window.matchMedia === 'function'
      && window.matchMedia('(pointer: coarse)').matches;

    function buildDrop() {
      dropEl.classList.add('is-enhanced');
      if (!coarse) {
        var hintWrap = el('div');
        hintEl = el('strong', 'lt-wiz-drop__hint', t('Drag image here'));
        hintWrap.appendChild(hintEl);
        dropEl.insertBefore(hintWrap, dropEl.firstChild);
      }
      drop.actions = el('div', 'lt-wiz-drop__actions');
      drop.or = el('span', 'dimmed', t('or'));
      pick.label = el('label', 'button is-compact');
      pick.label.id = 'wiz-image-pick';
      pick.label.setAttribute('for', fileInput.id);
      fileInput.classList.add('lt-wiz-sr');
      fileInput.setAttribute('aria-labelledby', 'wiz-image-label wiz-image-pick');
      drop.actions.appendChild(drop.or);
      dropEl.appendChild(drop.actions);
      dropEl.appendChild(el(
        'div', 'form-caption lt-wiz-drop__note',
        t('On phones, the photo library opens.')
      ));
    }

    function dragHasFiles(event) {
      var types = event.dataTransfer && event.dataTransfer.types;
      return !!types && Array.prototype.indexOf.call(types, 'Files') !== -1;
    }

    function setOver(over) {
      dropEl.classList.toggle('is-over', over);
      if (hintEl) {
        hintEl.textContent = over
          ? t('Release to upload')
          : t('Drag image here');
      }
    }

    var dragDepth = 0;

    function bindDrop(zone) {
      zone.addEventListener('dragenter', function (event) {
        if (dragHasFiles(event)) {
          event.preventDefault();
          dragDepth += 1;
          setOver(zone === dropEl);
        }
      });
      zone.addEventListener('dragover', function (event) {
        if (dragHasFiles(event)) {
          event.preventDefault();
          event.dataTransfer.dropEffect = 'copy';
        }
      });
      zone.addEventListener('dragleave', function () {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) {
          setOver(false);
        }
      });
      zone.addEventListener('drop', function (event) {
        if (!dragHasFiles(event)) {
          return;
        }
        event.preventDefault();
        dragDepth = 0;
        setOver(false);
        onFile(event.dataTransfer.files && event.dataTransfer.files[0]);
      });
    }

    buildDrop();
    bindDrop(dropEl);

    /* ---------- picker ---------- */

    function buildPanel() {
      var stub = el('form');
      stub.id = NO_FORM;
      stub.hidden = true;
      stub.setAttribute('aria-hidden', 'true');
      stub.addEventListener('submit', function (event) {
        event.preventDefault();
      });

      panel.root = el('div', 'lt-wiz-lib');
      panel.root.setAttribute('role', 'region');
      panel.root.setAttribute('aria-labelledby', 'wiz-lib-heading');
      panel.root.setAttribute('data-wiz-lib', '');
      var head = el('div', 'lt-wiz-lib__head');
      panel.heading = el('strong', null, t('Choose existing image'));
      panel.heading.id = 'wiz-lib-heading';
      panel.heading.setAttribute('tabindex', '-1');
      var scopes = el('div', 'lt-wiz-lib__scopes');
      scopes.setAttribute('role', 'group');
      panel.scopeButtons = {};
      [['party', 'This party'], ['brand', 'All parties']].forEach(function (pair) {
        var button = el('button', 'button is-compact', t(pair[1]));
        button.type = 'button';
        button.addEventListener('click', function () {
          setScope(pair[0]);
        });
        panel.scopeButtons[pair[0]] = button;
        scopes.appendChild(button);
      });
      head.appendChild(panel.heading);
      head.appendChild(scopes);

      panel.searchLabel = el('label', 'lt-wiz-sr', t('Search by file name'));
      panel.searchLabel.setAttribute('for', 'wiz-lib-q');
      panel.search = el('input', 'form-control lt-wiz-lib__search');
      panel.search.id = 'wiz-lib-q';
      panel.search.type = 'text';
      panel.search.setAttribute('form', NO_FORM);
      panel.search.setAttribute('maxlength', '100');
      panel.search.setAttribute('autocomplete', 'off');
      panel.search.setAttribute('placeholder', t('Search by file name …'));
      panel.search.addEventListener('input', function () {
        clearTimeout(lib.timer);
        lib.timer = setTimeout(function () {
          lib.q = panel.search.value.trim();
          loadPage(1);
        }, SEARCH_DEBOUNCE_MS);
      });
      panel.search.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          event.stopPropagation();
          clearTimeout(lib.timer);
          lib.q = panel.search.value.trim();
          loadPage(1);
        }
      });

      panel.grid = el('fieldset', 'lt-wiz-lib__grid');
      panel.grid.appendChild(el('legend', 'lt-wiz-sr', t('Image')));
      panel.list = el('div', 'lt-wiz-lib__list');
      panel.grid.appendChild(panel.list);
      panel.more = el('button', 'button is-compact', t('Load more'));
      panel.more.type = 'button';
      panel.more.addEventListener('click', function () {
        loadPage(lib.page + 1);
      });

      var actions = el('div', 'button-row lt-wiz-lib__actions');
      panel.use = el('button', 'button is-compact color-primary', t('Use this image'));
      panel.use.type = 'button';
      panel.use.addEventListener('click', onUse);
      panel.cancel = el('button', 'button is-compact', t('Cancel'));
      panel.cancel.type = 'button';
      panel.cancel.addEventListener('click', closeLibrary);
      actions.appendChild(panel.use);
      actions.appendChild(panel.cancel);
      var note = el(
        'div', 'form-caption',
        t('The image is not copied. Removing or replacing it here does not change other tournaments.')
      );

      [head, panel.searchLabel, panel.search, panel.grid, panel.more,
       actions, note].forEach(function (node) {
        panel.root.appendChild(node);
      });
      panel.root.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          closeLibrary();
        }
      });
      root.appendChild(stub);
      root.insertBefore(panel.root, urlAlt);
      setShown(panel.root, false);
    }

    function renderScopes() {
      Object.keys(panel.scopeButtons).forEach(function (scope) {
        panel.scopeButtons[scope].setAttribute(
          'aria-pressed', String(lib.scope === scope)
        );
        panel.scopeButtons[scope].classList.toggle(
          'is-on', lib.scope === scope
        );
      });
    }

    function renderItems() {
      panel.list.textContent = '';
      panel.list.setAttribute('aria-busy', String(lib.loading));
      if (lib.failed) {
        var failed = el(
          'div', 'empty-hint lt-wiz-lib__empty',
          t('The images could not be loaded. Please try again.')
        );
        failed.setAttribute('role', 'alert');
        panel.list.appendChild(failed);
      } else if (!lib.items.length && !lib.loading) {
        var empty = el(
          'div', 'empty-hint lt-wiz-lib__empty',
          lib.q
            ? t('No image matches the search.')
            : t('No tournament images have been uploaded for this party yet.')
        );
        if (lib.scope === 'party') {
          var widen = el('button', 'button is-compact', t('Show all parties'));
          widen.type = 'button';
          widen.addEventListener('click', function () {
            setScope('brand');
          });
          empty.appendChild(document.createTextNode(' '));
          empty.appendChild(widen);
        }
        panel.list.appendChild(empty);
      }
      lib.items.forEach(function (item) {
        panel.list.appendChild(renderItem(item));
      });
      setShown(panel.more, lib.hasNext && !lib.failed);
      var chosen = lib.selected !== null;
      panel.use.disabled = !chosen;
      panel.use.setAttribute('aria-disabled', String(!chosen));
    }

    function renderItem(item) {
      var label = el('label', 'lt-wiz-lib__item');
      var radio = el('input');
      radio.type = 'radio';
      radio.name = 'wiz-lib';
      radio.value = item.imageId;
      radio.setAttribute('form', NO_FORM);
      radio.checked = lib.selected === item.imageId;
      radio.addEventListener('change', function () {
        lib.selected = item.imageId;
        renderItems();
        var again = qs(panel.list, 'input[value="' + item.imageId + '"]');
        if (again) {
          again.focus();
        }
      });
      label.classList.toggle('is-on', radio.checked);
      var thumb = el('span', 'lt-wiz-lib__thumb');
      var img = el('img');
      img.setAttribute('alt', '');
      img.setAttribute('loading', 'lazy');
      var thumbUrl = safeUrl(item.url);
      if (thumbUrl) {
        img.setAttribute('src', thumbUrl);
      }
      thumb.appendChild(img);
      var meta = el('span', 'lt-wiz-lib__meta');
      var name = el('span', 'lt-wiz-lib__name', item.filename);
      if (current.imageId && current.imageId === item.imageId) {
        name.appendChild(document.createTextNode(' '));
        name.appendChild(el('span', 'tag', t('current')));
      }
      var dims = item.width + ' × ' + item.height;
      var size = formatSize(item.byteSize);
      var line = dims + (size ? ' · ' + size : '');
      if (lib.scope === 'brand' && item.partyTitle) {
        line += ' · ' + item.partyTitle;
      }
      meta.appendChild(name);
      meta.appendChild(el('span', 'dimmed', line));
      meta.appendChild(el(
        'span', 'dimmed',
        item.usedBy.length
          ? t('Used by: %(names)s', {names: item.usedBy.join(', ')})
          : t('Not assigned to any tournament yet')
      ));
      label.appendChild(radio);
      label.appendChild(thumb);
      label.appendChild(meta);
      return label;
    }

    function parseItems(data) {
      var items = [];
      (data && Array.isArray(data.items) ? data.items : []).forEach(function (raw) {
        var id = raw ? safeId(raw.image_id) : null;
        var url = raw ? safeUrl(raw.url) : null;
        var width = raw ? safeDimension(raw.width) : null;
        var height = raw ? safeDimension(raw.height) : null;
        if (!id || !url || !width || !height) {
          return;
        }
        items.push({
          imageId: id,
          url: url,
          filename: String(raw.filename || ''),
          width: width,
          height: height,
          byteSize: safeBytes(raw.byte_size),
          partyTitle: raw.party_title ? String(raw.party_title) : '',
          usedBy: safeNames(raw.used_by)
        });
      });
      return items;
    }

    function loadPage(page) {
      if (!urls.images || typeof fetch !== 'function') {
        lib.failed = true;
        renderItems();
        return;
      }
      var seq = ++lib.seq;
      lib.loading = true;
      lib.failed = false;
      if (page === 1) {
        lib.items = [];
        lib.selected = null;
        lib.hasNext = false;
      }
      renderItems();
      var query = 'scope=' + encodeURIComponent(lib.scope)
        + '&q=' + encodeURIComponent(lib.q)
        + '&page=' + encodeURIComponent(page);
      var url = urls.images + (urls.images.indexOf('?') === -1 ? '?' : '&')
        + query;
      fetch(url, {
        credentials: 'same-origin',
        headers: {Accept: 'application/json'}
      }).then(function (response) {
        if (!response.ok) {
          throw new Error('list status ' + response.status);
        }
        return response.text();
      }).then(function (text) {
        var data = parseJson(text);
        if (!data) {
          throw new Error('list body');
        }
        if (seq !== lib.seq) {
          return;
        }
        lib.page = page;
        lib.hasNext = data.has_next === true;
        lib.items = lib.items.concat(parseItems(data));
        lib.loading = false;
        renderItems();
      }).catch(function () {
        if (seq !== lib.seq) {
          return;
        }
        lib.loading = false;
        lib.failed = true;
        renderItems();
      });
    }

    function setScope(scope) {
      lib.scope = scope;
      renderScopes();
      loadPage(1);
    }

    function openLibrary() {
      if (!panel.root) {
        buildPanel();
      }
      lib.open = true;
      libOpen.setAttribute('aria-expanded', 'true');
      setShown(panel.root, true);
      renderScopes();
      loadPage(1);
      panel.heading.focus();
    }

    function closeLibrary() {
      lib.open = false;
      clearTimeout(lib.timer);
      lib.seq += 1;
      libOpen.setAttribute('aria-expanded', 'false');
      if (panel.root) {
        setShown(panel.root, false);
      }
      libOpen.focus();
    }

    function onUse() {
      var chosen = lib.items.filter(function (item) {
        return item.imageId === lib.selected;
      })[0];
      if (!chosen) {
        return;
      }
      var base = current.status === 'uploading' ? upload.previous : current;
      upload.previous = null;
      abortUpload();
      clearErrors();
      if (base && base.status === 'done' && base.imageId === chosen.imageId) {
        if (current !== base) {
          setState(base);
        }
        closePicked();
        return;
      }
      setState({
        status: 'done',
        imageId: chosen.imageId,
        filename: chosen.filename,
        url: chosen.url,
        width: chosen.width,
        height: chosen.height,
        byteSize: chosen.byteSize,
        usedBy: chosen.usedBy,
        source: 'library'
      });
      releaseState(base);
      closePicked();
    }

    function closePicked() {
      lib.open = false;
      lib.seq += 1;
      libOpen.setAttribute('aria-expanded', 'false');
      setShown(panel.root, false);
      live.root.focus();
    }

    /* ---------- startup ---------- */

    buildLive();
    bindDrop(live.root);
    renderLive();
    if (stagedEl) {
      setShown(stagedEl, false);
    }
    if (libOpen) {
      libOpen.setAttribute('aria-expanded', 'false');
      libOpen.addEventListener('click', function () {
        if (lib.open) {
          closeLibrary();
        } else {
          openLibrary();
        }
      });
      libOpen.hidden = false;
      libOpen.style.display = '';
    }

    document.addEventListener('lt-wizard:image-restore', function (event) {
      var detail = event.detail || {};
      var id = safeId(detail.imageId);
      if (detail.status !== 'done' || !id || current.status !== 'none') {
        return;
      }
      setState({
        status: 'done',
        imageId: id,
        filename: detail.filename ? String(detail.filename) : null,
        url: safeUrl(detail.url),
        width: safeDimension(detail.width),
        height: safeDimension(detail.height),
        byteSize: safeBytes(detail.byteSize),
        usedBy: safeNames(detail.usedBy),
        source: 'restored'
      }, true);
    });

    var staged = config.stagedImage;
    var stagedId = staged ? safeId(staged.imageId) : null;
    if (stagedId) {
      setState({
        status: 'done',
        imageId: stagedId,
        filename: String(staged.filename || ''),
        url: safeUrl(staged.url),
        width: safeDimension(staged.width),
        height: safeDimension(staged.height),
        byteSize: safeBytes(staged.byteSize),
        source: 'staged'
      }, true);
      // The controller registers its listener after this script runs.
      setTimeout(emit, 0);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
