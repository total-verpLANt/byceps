/** Verify ticket-scoped chair information without navigating. */
onDomReady(() => {
  let saving = false;

  const loadInformation = async field => {
    const response = await fetch(window.location.pathname + window.location.search, {cache: 'no-store'});
    if (!response.ok || response.redirected) {
      throw new Error('Ticket refresh failed');
    }
    const page = new DOMParser().parseFromString(await response.text(), 'text/html');
    const ticketId = field.closest('[id^="ticket-"]').id;
    const updatedField = page.getElementById(ticketId)?.querySelector('.chair-information');
    const value = updatedField?.querySelector('.data-value');
    const selection = updatedField?.querySelector('.field-sub > span');
    const source = updatedField?.dataset.chairSource;
    if (!value || !selection || !['unknown', 'user', 'venue', 'rental'].includes(source)) {
      throw new Error('Ticket information missing');
    }
    return {field: updatedField, value: value.textContent, selection: selection.textContent, source};
  };

  const synchronize = (field, information) => {
    field.querySelector('.data-value').textContent = information.value;
    field.querySelector('.field-sub > span').textContent = information.selection;
    field.dataset.chairSource = information.source;
  };

  document.querySelectorAll('a[data-action="set-chair-source"]').forEach(choice => {
    choice.addEventListener('click', async event => {
      event.preventDefault();
      const field = choice.closest('.chair-information');
      if (saving) {
        return;
      }
      const scrollPosition = {top: window.scrollY, left: window.scrollX, behavior: 'instant'};
      const toggle = field.querySelector('.dropdown-toggle');
      const error = field.querySelector('.chair-update-error');
      let success = field.querySelector('.chair-update-success');
      // Static assets can arrive before the app container's new templates.
      if (!success) {
        success = document.createElement('p');
        success.className = 'chair-update-success';
        success.setAttribute('role', 'status');
        success.hidden = true;
        field.append(success);
      }
      const defaultError = error.dataset.defaultText || error.textContent;
      error.dataset.defaultText = defaultError;
      error.textContent = defaultError;
      saving = true;
      let saved = false;
      const closeMenu = () => {
        field.querySelector('.dropdown').classList.remove('open');
        toggle.disabled = false;
        toggle.focus({preventScroll: true});
      };

      try {
        toggle.disabled = true;
        error.hidden = true;
        success.hidden = true;
        // A displayed answer may have become stale in another tab or editor.
        if (field.dataset.chairSource === choice.dataset.chairSource) {
          const information = await loadInformation(field);
          synchronize(field, information);
          if (information.source === choice.dataset.chairSource) {
            closeMenu();
            return;
          }
        }
        const response = await fetch(choice.href, {method: 'POST'});
        if (response.status !== 204) {
          throw new Error('Chair update failed');
        }

        saved = true;
        // Session-wide flashes are not evidence of this ticket's saved state.
        const information = await loadInformation(field);
        synchronize(field, information);
        closeMenu();
        if (information.source !== choice.dataset.chairSource) {
          error.textContent = information.field.dataset.conflictText || defaultError;
          error.hidden = false;
          return;
        }
        success.textContent = information.field.querySelector('.chair-update-success')?.textContent
          || information.field.dataset.successText || field.dataset.successText || information.value;
        success.hidden = false;
      } catch {
        if (saved) {
          // The POST succeeded, but the old display is no longer verified.
          delete field.dataset.chairSource;
          error.textContent = field.dataset.refreshErrorText || defaultError;
        }
        error.hidden = false;
      } finally {
        saving = false;
        toggle.disabled = false;
        window.scrollTo(scrollPosition);
      }
    });
  });
});
