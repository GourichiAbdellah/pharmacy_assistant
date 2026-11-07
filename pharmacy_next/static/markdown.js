/* Only assistant answers are interpreted as Markdown. User/OCR text stays literal. */
(function () {
  'use strict';

  window.renderAssistantMessage = function (container, value) {
    const text = String(value ?? '');
    container.classList.remove('markdown-body');
    container.textContent = text; // Safe fallback if an asset fails to load.
    if (!window.marked?.parse || !window.DOMPurify?.isSupported) return;

    try {
      const html = window.marked.parse(text, {gfm: true, breaks: true, async: false});
      const fragment = window.DOMPurify.sanitize(html, {
        RETURN_DOM_FRAGMENT: true,
        ALLOWED_TAGS: [
          'p', 'br', 'strong', 'b', 'em', 'i', 'del', 's', 'ul', 'ol', 'li',
          'blockquote', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr',
          'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'a'
        ],
        ALLOWED_ATTR: ['href', 'title', 'start'],
        ALLOWED_URI_REGEXP: /^https?:\/\//i,
        ALLOW_DATA_ATTR: false,
        ALLOW_ARIA_ATTR: false,
        SANITIZE_NAMED_PROPS: true
      });

      // Model-supplied images, styles, scripts, forms and event handlers are excluded.
      // Only absolute HTTP(S) links may be clickable; never resolve relative URLs
      // against the API origin. Do not turn the cleaned fragment back into HTML.
      for (const link of fragment.querySelectorAll('a')) {
        const href = link.getAttribute('href') || '';
        let allowed = false;
        try {
          const url = new URL(href);
          allowed = /^https?:\/\//i.test(href) && ['http:', 'https:'].includes(url.protocol);
        } catch {}
        if (!allowed) {
          link.replaceWith(...link.childNodes);
          continue;
        }
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.referrerPolicy = 'no-referrer';
      }
      for (const table of fragment.querySelectorAll('table')) {
        const wrapper = document.createElement('div');
        wrapper.className = 'table-wrap';
        wrapper.tabIndex = 0;
        wrapper.setAttribute('role', 'region');
        wrapper.setAttribute('aria-label', 'Tableau comparatif — défilement horizontal');
        table.replaceWith(wrapper);
        wrapper.append(table);
        for (const cell of table.querySelectorAll('th,td')) cell.dir = 'auto';
      }
      container.replaceChildren(fragment);
      container.classList.add('markdown-body');
    } catch {
      container.textContent = text;
      container.classList.remove('markdown-body');
    }
  };
})();
