(() => {
  'use strict';

  const config = window.PAPER_CONFIG || {};
  const paperNode = document.getElementById('paper');
  const tocNode = document.getElementById('toc');
  const loadingNode = document.getElementById('loading');
  const summaryText = document.getElementById('summaryText');
  const chatButton = document.getElementById('chatButton');
  const chatAvailability = document.getElementById('chatAvailability');
  const chatDialog = document.getElementById('chatDialog');
  const chatMessages = document.getElementById('chatMessages');
  const chatForm = document.getElementById('chatForm');
  const chatInput = document.getElementById('chatInput');
  const sendButton = document.getElementById('sendButton');
  const sidebar = document.getElementById('paperSidebar');
  const mobileTocButton = document.getElementById('mobileTocButton');
  const history = [];

  if (config.isDraft) document.getElementById('draftBanner').hidden = false;
  document.title = `${config.title || 'Live paper'} — Alessandro Veneri`;

  function safeAsset(value) {
    if (!value || /^(?:javascript|data):/i.test(value) || value.includes('..')) return '';
    return value;
  }

  function addText(parent, value) {
    if (value) parent.appendChild(document.createTextNode(value));
  }

  function showNote(title, text) {
    document.getElementById('noteTitle').textContent = title;
    document.getElementById('noteText').textContent = text;
    document.getElementById('noteDialog').showModal();
  }

  function renderMixed(xmlParent, htmlParent) {
    addText(htmlParent, xmlParent.firstChild && xmlParent.firstChild.nodeType === Node.TEXT_NODE ? xmlParent.firstChild.nodeValue : '');
    for (const child of xmlParent.children) {
      const tag = child.tagName.toUpperCase();
      let node;
      if (tag === 'MATH') {
        node = document.createElement('span');
        node.textContent = `\\(${child.textContent.trim()}\\)`;
      } else if (tag === 'CITATION') {
        node = document.createElement('button');
        node.type = 'button';
        node.className = 'citation';
        node.textContent = child.textContent;
        node.addEventListener('click', () => showNote(`Citation: ${child.getAttribute('key') || ''}`, child.textContent));
      } else if (tag === 'XREF') {
        node = document.createElement('a');
        node.href = `#${child.getAttribute('target') || ''}`;
        node.textContent = child.textContent;
        node.className = 'xref';
      } else if (tag === 'FOOTNOTE') {
        node = document.createElement('button');
        node.type = 'button';
        node.className = 'footnote-ref';
        node.textContent = 'note';
        node.addEventListener('click', () => showNote('Footnote', child.textContent));
      } else if (['B', 'STRONG', 'I', 'EM', 'SUP', 'SUB'].includes(tag)) {
        node = document.createElement(tag.toLowerCase());
        renderMixed(child, node);
      } else {
        node = document.createElement('span');
        renderMixed(child, node);
      }
      htmlParent.appendChild(node);
      addText(htmlParent, child.nextSibling && child.nextSibling.nodeType === Node.TEXT_NODE ? child.nextSibling.nodeValue : '');
    }
  }

  function sectionLevel(tag) {
    return tag === 'SECTION' ? 2 : tag === 'SUBSECTION' ? 3 : 4;
  }

  function renderElement(xml, parent) {
    const tag = xml.tagName.toUpperCase();
    if (['SECTION', 'SUBSECTION', 'SUBSUBSECTION'].includes(tag)) {
      const section = document.createElement('section');
      section.className = 'section';
      section.id = xml.getAttribute('ref') || `section-${tocNode.children.length + 1}`;
      const heading = document.createElement(`h${sectionLevel(tag)}`);
      heading.textContent = xml.getAttribute('title') || 'Untitled section';
      section.appendChild(heading);
      const item = document.createElement('li');
      if (tag !== 'SECTION') item.className = 'subsection';
      const link = document.createElement('a');
      link.href = `#${section.id}`;
      link.textContent = heading.textContent;
      item.appendChild(link);
      tocNode.appendChild(item);
      for (const child of xml.children) renderElement(child, section);
      parent.appendChild(section);
      return;
    }
    if (tag === 'P') {
      const paragraph = document.createElement('p');
      paragraph.className = 'paper-paragraph';
      renderMixed(xml, paragraph);
      parent.appendChild(paragraph);
      return;
    }
    if (tag === 'EQUATION') {
      const equation = document.createElement('div');
      equation.className = 'equation';
      equation.id = xml.getAttribute('ref') || '';
      equation.textContent = `\\[${xml.textContent.trim()}\\]`;
      parent.appendChild(equation);
      return;
    }
    if (['THEOREM', 'PROPOSITION', 'LEMMA', 'COROLLARY', 'DEFINITION', 'ASSUMPTION', 'REMARK', 'PROOF'].includes(tag)) {
      const block = document.createElement('div');
      block.className = `result-block ${tag.toLowerCase()}`;
      block.id = xml.getAttribute('ref') || '';
      const label = document.createElement('span');
      label.className = 'result-label';
      label.textContent = tag === 'PROOF' ? 'Proof' : tag.toLowerCase();
      block.appendChild(label);
      renderMixed(xml, block);
      parent.appendChild(block);
      return;
    }
    if (tag === 'FIGURE') {
      const figure = document.createElement('figure');
      figure.id = xml.getAttribute('ref') || '';
      const src = safeAsset(xml.getAttribute('src'));
      if (src) {
        const image = document.createElement('img');
        image.src = src;
        image.alt = xml.querySelector(':scope > CAPTION')?.textContent || '';
        figure.appendChild(image);
      }
      const captionXml = xml.querySelector(':scope > CAPTION');
      if (captionXml) {
        const caption = document.createElement('figcaption');
        caption.textContent = captionXml.textContent;
        figure.appendChild(caption);
      }
      parent.appendChild(figure);
      return;
    }
    if (tag === 'TABLE') {
      const wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      wrap.id = xml.getAttribute('ref') || '';
      const table = document.createElement('table');
      for (const rowXml of xml.querySelectorAll('TR')) {
        const row = document.createElement('tr');
        for (const cellXml of rowXml.children) {
          if (!['TH', 'TD'].includes(cellXml.tagName.toUpperCase())) continue;
          const cell = document.createElement(cellXml.tagName.toLowerCase());
          renderMixed(cellXml, cell);
          row.appendChild(cell);
        }
        table.appendChild(row);
      }
      wrap.appendChild(table);
      const captionXml = xml.querySelector(':scope > CAPTION');
      if (captionXml) {
        const caption = document.createElement('div');
        caption.className = 'table-caption';
        caption.textContent = captionXml.textContent;
        wrap.appendChild(caption);
      }
      parent.appendChild(wrap);
      return;
    }
    if (tag === 'ABSTRACT') {
      const abstract = document.createElement('div');
      abstract.className = 'abstract';
      for (const child of xml.children) renderElement(child, abstract);
      parent.appendChild(abstract);
      return;
    }
    if (tag === 'REFERENCES') {
      const section = document.createElement('section');
      section.className = 'section references';
      section.id = 'references';
      const heading = document.createElement('h2');
      heading.textContent = 'References';
      section.appendChild(heading);
      for (const reference of xml.children) {
        const paragraph = document.createElement('p');
        paragraph.textContent = reference.textContent;
        section.appendChild(paragraph);
      }
      parent.appendChild(section);
      return;
    }
    for (const child of xml.children) renderElement(child, parent);
  }

  async function loadPaper() {
    const response = await fetch(config.xmlUrl, { credentials: 'same-origin' });
    if (!response.ok) throw new Error(`Paper data returned ${response.status}`);
    const xmlText = await response.text();
    const documentXml = new DOMParser().parseFromString(xmlText, 'application/xml');
    const parserError = documentXml.querySelector('parsererror');
    if (parserError) throw new Error(parserError.textContent.slice(0, 300));
    const root = documentXml.documentElement;
    if (root.tagName.toUpperCase() !== 'PAPER') throw new Error('Paper XML has an invalid root element');

    const header = document.createElement('header');
    header.className = 'paper-header';
    const title = document.createElement('h1');
    title.className = 'paper-title';
    title.textContent = root.querySelector(':scope > TITLE')?.textContent || config.title;
    header.appendChild(title);
    const authors = document.createElement('div');
    authors.className = 'paper-authors';
    authors.textContent = Array.from(root.querySelectorAll(':scope > AUTHORS > AUTHOR')).map((author) => author.textContent).join(' and ');
    header.appendChild(authors);
    const date = document.createElement('div');
    date.className = 'paper-date';
    date.textContent = root.querySelector(':scope > PUBLICATION_DATE')?.textContent || '';
    header.appendChild(date);
    const pdf = safeAsset(root.querySelector(':scope > PDF')?.textContent || '');
    if (pdf) {
      const links = document.createElement('div');
      links.className = 'paper-links';
      const anchor = document.createElement('a');
      anchor.href = pdf;
      anchor.target = '_blank';
      anchor.rel = 'noopener';
      anchor.textContent = 'Open PDF';
      links.appendChild(anchor);
      header.appendChild(links);
    }
    paperNode.appendChild(header);

    const summary = root.querySelector(':scope > PLAINTEXT')?.textContent || 'No plain-language summary is available.';
    summaryText.textContent = summary;
    const skip = new Set(['TITLE', 'AUTHORS', 'PUBLICATION_DATE', 'PDF', 'PLAINTEXT']);
    for (const child of root.children) {
      if (!skip.has(child.tagName.toUpperCase())) renderElement(child, paperNode);
    }
    loadingNode.hidden = true;
    if (window.MathJax?.typesetPromise) await window.MathJax.typesetPromise([paperNode]);
  }

  function resolvedChatEndpoint() {
    const override = new URLSearchParams(location.search).get('chatEndpoint');
    const isLocal = ['localhost', '127.0.0.1'].includes(location.hostname);
    return isLocal && override ? override : config.chatEndpoint;
  }

  function configureChat() {
    const endpoint = resolvedChatEndpoint();
    if (!config.chatEnabled || !endpoint) {
      chatButton.disabled = true;
      chatAvailability.textContent = config.isDraft ? 'Start the local Worker to test chat.' : 'Chat is not enabled yet.';
      return;
    }
    chatButton.addEventListener('click', () => chatDialog.showModal());
  }

  function addChatMessage(role, text, source) {
    const message = document.createElement('div');
    message.className = `chat-message ${role}`;
    message.textContent = text;
    if (source?.sourceQuote) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'source-button';
      button.textContent = 'View source passage';
      button.addEventListener('click', () => {
        chatDialog.close();
        highlightSource(source.sourceAnchor, source.sourceQuote);
      });
      message.appendChild(button);
    }
    chatMessages.appendChild(message);
    chatMessages.scrollTop = chatMessages.scrollHeight;
  }

  function highlightSource(anchor, quote) {
    document.querySelectorAll('.source-highlight').forEach((node) => node.classList.remove('source-highlight'));
    let target = anchor ? document.getElementById(anchor) : null;
    if (!target || (quote && !target.textContent.includes(quote))) {
      target = Array.from(paperNode.querySelectorAll('p, .result-block, .abstract')).find((node) => node.textContent.includes(quote));
    }
    if (target) {
      target.classList.add('source-highlight');
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setTimeout(() => target.classList.remove('source-highlight'), 3000);
    }
  }

  chatForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const question = chatInput.value.trim();
    if (!question) return;
    addChatMessage('user', question);
    chatInput.value = '';
    sendButton.disabled = true;
    sendButton.textContent = 'Thinking…';
    try {
      const response = await fetch(resolvedChatEndpoint(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paperId: config.paperId, question, history: history.slice(-6) })
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || 'Questions are temporarily unavailable.');
      addChatMessage('assistant', data.answer, data);
      history.push({ role: 'user', text: question }, { role: 'assistant', text: data.answer });
      if (history.length > 6) history.splice(0, history.length - 6);
    } catch (error) {
      addChatMessage('assistant', error.message || 'Questions are temporarily unavailable.');
    } finally {
      sendButton.disabled = false;
      sendButton.textContent = 'Ask';
    }
  });

  document.getElementById('summaryButton').addEventListener('click', () => document.getElementById('summaryDialog').showModal());
  document.querySelectorAll('[data-close]').forEach((button) => button.addEventListener('click', () => document.getElementById(button.dataset.close).close()));
  mobileTocButton.addEventListener('click', () => {
    const open = sidebar.classList.toggle('open');
    mobileTocButton.setAttribute('aria-expanded', String(open));
  });
  tocNode.addEventListener('click', () => {
    sidebar.classList.remove('open');
    mobileTocButton.setAttribute('aria-expanded', 'false');
  });

  configureChat();
  loadPaper().catch((error) => {
    loadingNode.textContent = `This paper could not be loaded: ${error.message}`;
  });
})();
