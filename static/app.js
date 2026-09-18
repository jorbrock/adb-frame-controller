const dialog = document.querySelector('#reboot-dialog');
let pendingForm = null;
let submitting = false;
document.querySelectorAll('.reboot-form').forEach(form => {
  form.addEventListener('submit', event => {
    if (submitting) return;
    event.preventDefault();
    pendingForm = form;
    document.querySelector('#reboot-name').textContent = form.dataset.frame;
    dialog.showModal();
  });
});
document.querySelector('#cancel-reboot')?.addEventListener('click', () => dialog.close());
document.querySelector('#confirm-reboot')?.addEventListener('click', () => {
  if (!pendingForm || submitting) return;
  submitting = true;
  document.querySelector('#confirm-reboot').disabled = true;
  pendingForm.submit();
});
document.querySelectorAll('time[datetime]').forEach(element => {
  const value = new Date(element.dateTime);
  if (!Number.isNaN(value.getTime())) element.textContent = value.toLocaleString();
});
if (document.querySelector('[data-refresh]')) {
  setInterval(() => {
    if (!dialog?.open && !submitting && !document.hidden &&
        !['BUTTON', 'INPUT'].includes(document.activeElement?.tagName)) {
      window.location.reload();
    }
  }, 10000);
}
