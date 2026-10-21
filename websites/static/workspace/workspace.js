(() => {
  'use strict';
  document.documentElement.classList.add('js');
  const sidebar = document.getElementById('workspace-sidebar');
  const toggle = document.getElementById('sidebar-toggle');
  const shade = document.getElementById('sidebar-shade');
  const closeSidebar = document.getElementById('sidebar-close');
  let previousFocus;
  function setSidebar(open) {
    if (!sidebar || !toggle || !shade) return;
    document.body.classList.toggle('sidebar-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    shade.hidden = !open;
    if (open) { previousFocus = document.activeElement; (closeSidebar || sidebar.querySelector('a')).focus(); }
    else if (previousFocus) { previousFocus.focus(); previousFocus = null; }
  }
  toggle?.addEventListener('click', () => setSidebar(!document.body.classList.contains('sidebar-open')));
  closeSidebar?.addEventListener('click', () => setSidebar(false));
  shade?.addEventListener('click', () => setSidebar(false));
  const media = matchMedia('(min-width: 761px)');
  media.addEventListener('change', event => { if (event.matches) setSidebar(false); });
  document.addEventListener('keydown', event => {
    if (!document.body.classList.contains('sidebar-open')) return;
    if (event.key === 'Escape') { event.preventDefault(); setSidebar(false); }
    if (event.key === 'Tab') {
      const elements = [...sidebar.querySelectorAll('a, button')].filter(el => el.getClientRects().length);
      const first = elements[0], last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  const dialog = document.getElementById('navigation-dialog');
  const search = document.getElementById('navigation-search');
  const results = document.getElementById('navigation-results');
  const empty = document.getElementById('navigation-empty');
  const links = [...document.querySelectorAll('[data-nav-link]')];
  function renderResults() {
    const query = search.value.trim().toLocaleLowerCase();
    results.replaceChildren();
    links.filter(link => link.textContent.toLocaleLowerCase().includes(query)).forEach(link => {
      const clone = link.cloneNode(true);
      clone.className = '';
      clone.removeAttribute('data-nav-link');
      clone.querySelector('.nav-dot')?.remove();
      results.append(clone);
    });
    empty.hidden = results.children.length > 0;
  }
  function openDialog() {
    if (!dialog || typeof dialog.showModal !== 'function') return;
    if (document.body.classList.contains('sidebar-open')) setSidebar(false);
    search.value = ''; renderResults(); dialog.showModal(); search.focus();
  }
  document.getElementById('open-navigation')?.addEventListener('click', openDialog);
  document.getElementById('close-navigation')?.addEventListener('click', () => dialog.close());
  search?.addEventListener('input', renderResults);
  search?.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); results.querySelector('a')?.click(); }
    if (event.key === 'ArrowDown') { event.preventDefault(); results.querySelector('a')?.focus(); }
  });
  dialog?.addEventListener('click', event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && dialog?.open) { event.preventDefault(); dialog.close(); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k' && dialog) { event.preventDefault(); if (dialog.open) dialog.close(); else openDialog(); }
  });
})();
