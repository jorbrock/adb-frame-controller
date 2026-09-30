const dialog = document.querySelector('#reboot-dialog');
let pendingForm = null;
let submitting = false;
function closeRebootMenus(except = null) {
  document.querySelectorAll('.reboot-form').forEach(form => {
    if (form === except) return;
    form.querySelector('.reboot-menu').hidden = true;
    form.querySelector('.reboot-toggle').setAttribute('aria-expanded', 'false');
  });
}
document.querySelectorAll('.reboot-form').forEach(form => {
  const toggle = form.querySelector('.reboot-toggle');
  const menu = form.querySelector('.reboot-menu');
  const items = [...menu.querySelectorAll('button:not(:disabled)')];
  function openMenu(last = false) {
    closeRebootMenus(form);
    menu.hidden = false;
    toggle.setAttribute('aria-expanded', 'true');
    (last ? items.at(-1) : items[0])?.focus();
  }
  toggle.addEventListener('click', () => {
    if (menu.hidden) openMenu();
    else closeRebootMenus();
  });
  toggle.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      openMenu(event.key === 'ArrowUp');
    }
  });
  form.addEventListener('keydown', event => {
    if (menu.hidden) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      closeRebootMenus();
      toggle.focus();
    } else if (menu.contains(event.target) && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const index = items.indexOf(document.activeElement);
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }
  });
  form.addEventListener('focusout', event => {
    if (!form.contains(event.relatedTarget)) closeRebootMenus();
  });
  form.addEventListener('submit', event => {
    if (submitting) return;
    event.preventDefault();
    const mode = event.submitter?.dataset.mode;
    if (!['soft', 'hard'].includes(mode)) return;
    form.elements.mode.value = mode;
    pendingForm = form;
    closeRebootMenus();
    // Return focus to the trigger when the confirmation dialog closes.
    toggle.focus();
    document.querySelector('#reboot-name').textContent = form.dataset.frame;
    document.querySelector('#reboot-method').textContent = mode === 'hard'
      ? 'Hard reboot: try to shut down Android, then switch the Wyze plug off for 30 seconds and restore power. If ADB is unavailable or shutdown fails three times, cut plug power directly.'
      : 'Soft reboot: restart Android using ADB.';
    dialog.showModal();
  });
});
document.addEventListener('click', event => {
  if (!event.target.closest('.reboot-form')) closeRebootMenus();
});
document.querySelector('#cancel-reboot')?.addEventListener('click', () => dialog.close());
document.querySelector('#confirm-reboot')?.addEventListener('click', () => {
  if (!pendingForm || submitting) return;
  submitting = true;
  document.querySelector('#confirm-reboot').disabled = true;
  pendingForm.submit();
});
document.querySelectorAll('.display-form').forEach(form => {
  form.addEventListener('submit', () => { submitting = true; });
});
document.querySelectorAll('time[datetime]').forEach(element => {
  const value = new Date(element.dateTime);
  if (!Number.isNaN(value.getTime())) element.textContent = value.toLocaleString();
});
if (document.querySelector('[data-refresh]')) {
  setInterval(() => {
    if (!dialog?.open && !document.querySelector('.reboot-menu:not([hidden])') &&
        !submitting && !document.hidden &&
        !['BUTTON', 'INPUT', 'SELECT'].includes(document.activeElement?.tagName)) {
      window.location.reload();
    }
  }, 10000);
}
