/** Confirm only an activation from the setting loaded by the server. */
document.querySelectorAll('[data-rental-selection-form]').forEach(form => {
  const enabled = form.elements.namedItem('enabled');
  form.addEventListener('submit', event => {
    if (form.dataset.enabled === 'false' && enabled.value === 'true'
        && !window.confirm(form.dataset.confirmEnable)) {
      event.preventDefault();
      enabled.value = 'false';
    }
  });
});
