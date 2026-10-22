// Live Markdown rendering for streamed answers. A pure function (no DOM), so it can run on every update as words
// arrive and be tested under node. Supports: headings, bullet/numbered lists, pipe tables, **bold**, *italic*,
// `code`, and [n] citation links. Half-finished markup (an unclosed ** or a table row still being typed) renders
// sensibly instead of flashing raw asterisks.
'use strict';

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function inlineMd(t, sources, streaming) {
  // `t` is already HTML-escaped, so every tag below is ours.
  t = t.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  t = t.replace(/\*\*([^*\n]+?)\*\*/g, '<b>$1</b>');
  t = t.replace(/(^|[\s(])\*([^*\s][^*\n]*?)\*(?=$|[\s).,;:!?])/g, '$1<i>$2</i>');
  if (streaming) {
    // An opener whose closer has not arrived yet: show the text bold already, not as raw asterisks.
    const k = t.indexOf('**');
    if (k !== -1) t = t.slice(0, k) + '<b>' + t.slice(k + 2) + '</b>';
  }
  // [n] was cited by the model; [n*] was matched to its passage automatically afterwards (drawn dashed).
  return t.replace(/\[(\d+)(\*?)\]/g, (m, n, auto) => {
    const s = sources && sources[+n - 1];
    if (!s) return m;
    const tip = s.title + (s.section ? ' — ' + s.section : '') + (s.snippet ? '\n' + s.snippet : '') +
      (auto ? '\n(matched to this source automatically: the line closely overlaps it)' : '');
    return `<a class="cite${auto ? ' auto' : ''}" href="#" data-n="${n}" title="${escapeHtml(tip)}">${n}</a>`;
  }).replace(
    // Keep the word before a badge, the badge(s) and the punctuation after them on one line: a non-breaking space
    // does not glue to an inline badge, so the group goes in a nowrap span. The word is limited to plain text
    // (no tags) so the span can never straddle a <b> or <i>.
    /([^\s<>]+)\s+((?:<a class="cite[^>]*>[^<]*<\/a>)+)([.,;:!?)]*)/g,
    '<span class="nb">$1&nbsp;$2$3</span>');
}

function splitRow(line) {
  return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
}

function renderTable(rows, sources, streaming) {
  const sep = rows.findIndex(r => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(r));
  const cells = rows.filter((_, i) => i !== sep).map(splitRow);
  // The header row is the one before the separator. While streaming, a lone first row is almost surely the header.
  const hasHeader = sep === 1 || (sep === -1 && streaming && cells.length === 1);
  const cell = (tag, c) => `<${tag}>${inlineMd(c, sources, streaming)}</${tag}>`;
  let html = '<table>';
  cells.forEach((r, i) => {
    html += '<tr>' + r.map(c => cell(hasHeader && i === 0 ? 'th' : 'td', c)).join('') + '</tr>';
  });
  return html + '</table>';
}

const LIST_RE = /^(\s*)([-*•]|\d+[.)])\s+(.*)$/;

function renderMarkdown(src, sources, streaming) {
  const lines = escapeHtml(src).split('\n');
  const out = [];
  let i = 0;
  const startsBlock = (l) => /^\s*\|/.test(l) || /^#{1,4}\s/.test(l) || LIST_RE.test(l);
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      out.push(renderTable(rows, sources, streaming));
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      out.push(`<h4 class="md-h">${inlineMd(h[2], sources, streaming)}</h4>`);
      i++;
      continue;
    }
    if (LIST_RE.test(line)) {
      const ordered = /^\s*\d/.test(line);
      let html = ordered ? '<ol>' : '<ul>';
      while (i < lines.length && LIST_RE.test(lines[i])) {
        const m = LIST_RE.exec(lines[i++]);
        const level = Math.min(3, Math.floor(m[1].length / 2));
        html += `<li style="margin-left:${level * 1.2}em">${inlineMd(m[3], sources, streaming)}</li>`;
      }
      out.push(html + (ordered ? '</ol>' : '</ul>'));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i])) para.push(lines[i++]);
    out.push('<p>' + para.map(l => inlineMd(l, sources, streaming)).join('<br>') + '</p>');
  }
  return out.join('');
}

if (typeof module !== 'undefined') module.exports = { renderMarkdown, escapeHtml };
