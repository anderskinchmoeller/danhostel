/* Side notes open without navigation; plain links remain the no-JS fallback. */
(function () {
  'use strict';
  const popup = document.getElementById('notes-popup');
  if (!popup || typeof popup.showModal !== 'function') return;
  const title = document.getElementById('notes-popup-title');
  const body = popup.querySelector('.notes-popup-body');
  const close = popup.querySelector('[data-close-notes]');
  const links = Array.from(document.querySelectorAll('.side-notes a'));
  let opener = null;
  let revision = 0;

  function message(text, href) {
    const paragraph = document.createElement('p');
    paragraph.textContent = text;
    body.replaceChildren(paragraph);
    if (href) {
      const fallback = document.createElement('a');
      fallback.href = href;
      fallback.textContent = 'Åbn listen som en side';
      body.appendChild(fallback);
    }
  }

  links.forEach(function (link) {
    link.addEventListener('click', async function (event) {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      opener = link;
      const current = ++revision;
      const href = link.getAttribute('href');
      title.textContent = link.textContent;
      links.forEach(function (item) { item.setAttribute('aria-expanded', String(item === link)); });
      message('Henter huskelisten …');
      body.setAttribute('aria-busy', 'true');
      body.scrollTop = 0;
      if (!popup.open) popup.showModal();
      close.focus();
      try {
        const response = await fetch(href, {credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw new Error('Request failed');
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const article = page.querySelector('.todo-document');
        if (!article) throw new Error('Missing note');
        if (current !== revision || !popup.open) return;
        const heading = article.querySelector('h1');
        if (heading) title.textContent = heading.textContent;
        body.replaceChildren(article);
        body.scrollTop = 0;
      } catch (_) {
        if (current === revision && popup.open) message('Listen kunne ikke hentes. Prøv igen eller åbn den som en side.', href);
      } finally {
        if (current === revision) body.removeAttribute('aria-busy');
      }
    });
  });
  close.addEventListener('click', function () { popup.close(); });
  popup.addEventListener('close', function () {
    revision += 1;
    body.removeAttribute('aria-busy');
    links.forEach(function (link) { link.setAttribute('aria-expanded', 'false'); });
    if (opener) opener.focus();
  });
  popup.addEventListener('click', function (event) {
    if (event.target !== popup) return;
    const rect = popup.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) popup.close();
  });
}());
