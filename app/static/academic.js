/* Render only explicitly authored formulae; user data is never interpreted as TeX. */
(function () {
  'use strict';
  if (!window.katex) return;
  document.querySelectorAll('[data-latex]').forEach(function (element) {
    const fallback = element.textContent;
    try {
      window.katex.render(element.getAttribute('data-latex'), element, {
        displayMode: element.getAttribute('data-display') === 'true',
        output: 'htmlAndMathml', throwOnError: true, trust: false,
        strict: 'error', maxSize: 12, maxExpand: 200, errorColor: '#171717'
      });
    } catch (_) {
      element.textContent = fallback;
    }
  });
}());

/* Plain anchors remain shareable. Expand any collapsed section they point to. */
(function () {
  'use strict';
  function revealHash() {
    if (!window.location.hash) return;
    let id;
    try { id = decodeURIComponent(window.location.hash.slice(1)); } catch (_) { return; }
    const target = document.getElementById(id);
    if (!target) return;
    for (let node = target; node; node = node.parentElement) {
      if (node.tagName === 'DETAILS') node.open = true;
    }
    target.scrollIntoView({block: 'start'});
  }
  window.addEventListener('hashchange', revealHash);
  document.querySelectorAll('a[href^="#"]').forEach(function (link) {
    link.addEventListener('click', function () {
      if (link.hash === window.location.hash) revealHash();
    });
  });
  const main = document.getElementById('content');
  if (main) {
    const headings = Array.from(main.querySelectorAll('h2')).filter(function (h) {
      return !h.closest('td') && !h.closest('details');
    });
    if (headings.length >= 3 && !main.querySelector('.link-index')) {
      const nav = document.createElement('nav');
      nav.className = 'page-index';
      nav.setAttribute('aria-label', 'På denne side');
      const label = document.createElement('span');
      label.className = 'index-label'; label.textContent = 'På denne side'; nav.appendChild(label);
      headings.forEach(function (heading, index) {
        if (!heading.id) heading.id = 'afsnit-' + (index + 1);
        const link = document.createElement('a');
        link.href = '#' + heading.id; link.textContent = heading.textContent;
        nav.appendChild(link);
      });
      const introduction = main.querySelector('h1');
      if (introduction) introduction.after(nav);
    }
  }
  revealHash();
}());
