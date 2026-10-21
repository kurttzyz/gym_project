'use strict';
const addRow = document.getElementById('cart-add');
const totalForms = document.getElementById('id_cart-TOTAL_FORMS');
addRow.addEventListener('click', () => {
  const count = Number(totalForms.value);
  if (count >= 20) return;
  document.getElementById('cart-rows').insertAdjacentHTML('beforeend',
    document.getElementById('cart-empty').innerHTML.replaceAll('__prefix__', String(count)));
  totalForms.value = String(count + 1);
  if (count + 1 >= 20) addRow.disabled = true;
});
