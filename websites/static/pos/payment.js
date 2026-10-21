'use strict';
const form = document.getElementById('pos-payment');
const method = document.getElementById('id_method');
function showPaymentFields() {
  const cash = method.value === 'cash';
  for (const name of ['reference', 'note', 'confirmed']) {
    const field = document.getElementById('id_' + name);
    field.closest('p').hidden = cash;
    field.disabled = cash;
  }
  const tendered = document.getElementById('id_tendered');
  tendered.closest('p').hidden = !cash;
  tendered.disabled = !cash;
}
method.addEventListener('change', showPaymentFields);
showPaymentFields();
form.addEventListener('submit', () => {
  document.getElementById('pos-confirm').disabled = true;
});

window.addEventListener('pageshow', () => { document.getElementById('pos-confirm').disabled = false; });
