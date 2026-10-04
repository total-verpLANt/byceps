/** Render read-only chair-plan tooltips without interpreting data as HTML. */
function textElement(tagName, className, text) {
  const element = document.createElement(tagName);
  element.className = className;
  element.textContent = text;
  return element;
}

document.querySelectorAll('.seat-with-tooltip').forEach(seatContainer => {
  const removeTooltip = () => {
    seatContainer.querySelector('.seat-tooltip')?.remove();
  };

  seatContainer.addEventListener('mouseenter', () => {
    removeTooltip();
    const dataset = seatContainer.dataset;
    const tooltip = document.createElement('div');
    tooltip.className = 'seat-tooltip';
    tooltip.appendChild(textElement('div', 'seat-label', dataset.label));

    if (dataset.ticketId !== undefined && dataset.occupierName !== undefined) {
      const occupier = document.createElement('div');
      occupier.className = 'seat-occupier';
      const avatarContainer = document.createElement('div');
      avatarContainer.className = 'seat-occupier-avatar';
      const avatar = document.createElement('div');
      avatar.className = 'avatar size-48';
      const image = document.createElement('img');
      image.alt = '';
      if (dataset.occupierAvatar) {
        image.src = dataset.occupierAvatar;
      }
      avatar.appendChild(image);
      avatarContainer.appendChild(avatar);
      occupier.appendChild(avatarContainer);
      const name = document.createElement('div');
      name.className = 'seat-occupier-name';
      name.appendChild(textElement('strong', '', dataset.occupierName));
      occupier.appendChild(name);
      tooltip.appendChild(occupier);
    }

    if (dataset.tooltipNote !== undefined) {
      tooltip.appendChild(textElement('div', 'seat-tooltip-note', dataset.tooltipNote));
    }
    seatContainer.appendChild(tooltip);
  });

  seatContainer.addEventListener('mouseleave', removeTooltip);
});
