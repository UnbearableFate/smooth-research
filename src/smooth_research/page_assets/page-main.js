
var HT = (function () {
  'use strict';
  var EV = {supported: ['■', '支持'], challenged: ['▨', '受挑战'], inconclusive: ['◧', '无定论'], untested: ['□', '未检验']};
  var DISP = {active: ['●', '活跃'], paused: ['‖', '暂停'], retired: ['×', '已关闭'], candidate: ['◌', '候选']};
  var STAGE = {E0: '实现', E1: '机制', E2: '真实模型', E3: '确认'};
  var RUN = {done: ['✓', '已完成'], running: ['◐', '进行中'], notstarted: ['○', '未开始']};
  var CARET = '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" focusable="false"><path d="M4 2.5 8 6 4 9.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var ESC = {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'};
  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return ESC[c]; }); }
  function badge(cls, glyph, word, title) {
    return '<span class="b ' + cls + '" title="' + esc(title) + '"><span class="g" aria-hidden="true">' + glyph + '</span>' + esc(word) + '</span>';
  }
  function badgesHTML(n) {
    if (n.kind === 'H') {
      return badge('ev ev-' + n.ev, EV[n.ev][0], EV[n.ev][1], '证据状态：' + n.ev) +
        badge('disp disp-' + n.disp, DISP[n.disp][0], DISP[n.disp][1], '处置：' + n.disp);
    }
    var st = n.stage && STAGE[n.stage] ? '<span class="b stage" title="实验阶段"><span class="mono">' + esc(n.stage) + '</span>' + esc(STAGE[n.stage]) + '</span>' : '';
    return st + badge('run run-' + n.run, RUN[n.run][0], RUN[n.run][1], '进度');
  }
  function walk(list, fn) { list.forEach(function (it) { fn(it[0]); walk(it[1], fn); }); }
  function lineIds(line) {
    var seen = {}, out = [];
    walk(line.tree, function (id) { if (!seen[id]) { seen[id] = 1; out.push(id); } });
    return out;
  }
  function countLine(line) {
    var ids = lineIds(line), h = 0, e = 0;
    ids.forEach(function (id) { if (id.charAt(0) === 'H') { h++; } else { e++; } });
    return [h, e];
  }
  function nodeHTML(data, item, path) {
    var id = item[0], kids = item[1], n = data.nodes[id], gid = 'g-' + path;
    var s = '<li class="node ' + (n.kind === 'H' ? 'h' : 'e') + '" data-id="' + id + '" id="r-' + path + '"><div class="row">';
    if (kids.length) {
      s += '<button type="button" class="tog" aria-expanded="false" aria-controls="' + gid + '" aria-label="展开或折叠 ' + id + '">' + CARET + '</button>';
    } else {
      s += '<span class="spacer" aria-hidden="true"></span>';
    }
    s += '<button type="button" class="sel" data-id="' + id + '"><span class="nid">' + id + '</span><span class="ttl">' + esc(n.title) +
      '</span><span class="badges">' + badgesHTML(n) + '</span></button></div>';
    if (kids.length) {
      s += '<ul class="kids" id="' + gid + '" hidden>';
      kids.forEach(function (k, i) { s += nodeHTML(data, k, path + '-' + i); });
      s += '</ul>';
    }
    return s + '</li>';
  }
  function treeHTML(data) {
    return data.lines.map(function (line) {
      var c = countLine(line), gid = 'g-' + line.key;
      var s = '<li class="line" data-line="' + line.key + '"><button type="button" class="line-tog" aria-expanded="true" aria-controls="' + gid + '">' +
        CARET + '<span class="lname">' + esc(line.name) + '</span><span class="count">' + c[0] + ' 个假说 · ' + c[1] + ' 个实验</span></button>' +
        '<p class="line-desc">' + esc(line.desc) + '</p><ul class="kids top" id="' + gid + '">';
      line.tree.forEach(function (it, i) { s += nodeHTML(data, it, line.key + '-' + i); });
      return s + '</ul></li>';
    }).join('');
  }
  function overviewHTML(data) {
    return data.lines.map(function (line) {
      var chips = lineIds(line).filter(function (id) { return id.charAt(0) === 'H'; }).map(function (id) {
        var n = data.nodes[id];
        var label = id + ' ' + EV[n.ev][1] + '，' + DISP[n.disp][1] + '：' + n.title;
        return '<button type="button" class="ov-chip" data-id="' + id + '" aria-label="' + esc(label) + '" title="' + esc(label) + '">' +
          '<span class="sw ' + n.ev + (n.disp === 'candidate' ? ' cand' : '') + '" aria-hidden="true"></span>' + id + '</button>';
      }).join('');
      return '<div class="ov-row"><div class="ov-name">' + esc(line.name) + '</div><div class="ov-chips">' + chips + '</div></div>';
    }).join('');
  }
  function lineNames(data) {
    var m = {};
    data.lines.forEach(function (line) { lineIds(line).forEach(function (id) { m[id] = line.name; }); });
    return m;
  }
  return {esc: esc, badgesHTML: badgesHTML, treeHTML: treeHTML, overviewHTML: overviewHTML,
    countLine: countLine, lineIds: lineIds, lineNames: lineNames, EV: EV, DISP: DISP, STAGE: STAGE, RUN: RUN};
})();

function htInit() {
  'use strict';
  var data = JSON.parse(document.getElementById('tree-data').textContent);
  var tree = document.getElementById('tree');
  var ov = document.getElementById('ov-grid');
  var card = document.getElementById('detail-card');
  var empty = document.getElementById('detail-empty');
  var dId = document.getElementById('d-id');
  var dTitle = document.getElementById('d-title');
  var dBadges = document.getElementById('d-badges');
  var dRel = document.getElementById('d-rel');
  var noMatch = document.getElementById('no-match');
  var filter = document.getElementById('filter');
  var names = HT.lineNames(data);
  var mqNarrow = window.matchMedia('(max-width: 899px)');
  var mqReduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  var lastRow = null, saved = null;
  tree.innerHTML = HT.treeHTML(data);
  ov.innerHTML = HT.overviewHTML(data);

  function behavior() { return mqReduce.matches ? 'auto' : 'smooth'; }
  function setExpanded(btn, open) {
    if (!btn) { return; }
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    var g = document.getElementById(btn.getAttribute('aria-controls'));
    if (g) { g.hidden = !open; }
  }
  function toggles() { return tree.querySelectorAll('.tog, .line-tog'); }
  function childList(li) {
    for (var c = li.firstElementChild; c; c = c.nextElementSibling) { if (c.tagName === 'UL') { return c; } }
    return null;
  }
  function ownToggle(li) {
    if (li.classList.contains('line')) { return li.firstElementChild; }
    var row = li.firstElementChild, b = row && row.firstElementChild;
    return b && b.classList.contains('tog') ? b : null;
  }
  function reveal(li) {
    var p = li.parentElement;
    while (p && p !== tree) {
      if (p.tagName === 'UL' && p.id) { setExpanded(tree.querySelector('[aria-controls="' + p.id + '"]'), true); }
      p = p.parentElement;
    }
  }
  function rowFor(id) { return tree.querySelector('li[data-id="' + id + '"]'); }
  function relButtons(label, ids) {
    var frag = document.createDocumentFragment();
    frag.appendChild(document.createTextNode(label));
    ids.forEach(function (rid) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = rid;
      b.setAttribute('data-rel', rid);
      frag.appendChild(b);
    });
    return frag;
  }
  function select(id, opt) {
    var n = data.nodes[id];
    if (!n) { return; }
    opt = opt || {};
    dId.textContent = id + ' · ' + (n.kind === 'H' ? '假说' : '实验') + ' · ' + (names[id] || '');
    dTitle.textContent = n.title;
    dBadges.innerHTML = HT.badgesHTML(n);
    dRel.textContent = '';
    if (n.kind === 'H' && n.derived.length) { dRel.appendChild(relButtons('源自：', n.derived)); }
    if (n.kind === 'E' && n.tests.length) { dRel.appendChild(relButtons('检验：', n.tests)); }
    dRel.hidden = !dRel.firstChild;
    document.getElementById('d-purpose').textContent = n.purpose;
    document.getElementById('d-did').textContent = n.did;
    document.getElementById('d-result').textContent = n.result;
    empty.hidden = true;
    card.hidden = false;
    tree.querySelectorAll('.sel[aria-current]').forEach(function (b) { b.removeAttribute('aria-current'); });
    tree.querySelectorAll('.sel[data-id="' + id + '"]').forEach(function (b) { b.setAttribute('aria-current', 'true'); });
    ov.querySelectorAll('.ov-chip[aria-current]').forEach(function (b) { b.removeAttribute('aria-current'); });
    var chip = ov.querySelector('.ov-chip[data-id="' + id + '"]');
    if (chip) { chip.setAttribute('aria-current', 'true'); }
    var row = opt.row || rowFor(id);
    if (opt.reveal && row) { reveal(row); }
    lastRow = row;
    document.getElementById('sr-status').textContent = '已显示 ' + id + ' 的详情';
    try { history.replaceState(null, '', '#' + id); } catch (e) { }
    if (!opt.from) { return; }
    if (mqNarrow.matches) {
      card.scrollIntoView({block: 'start', behavior: behavior()});
      dTitle.focus({preventScroll: true});
    } else if (opt.from !== 'tree' && row && !row.hidden) {
      row.scrollIntoView({block: 'center', behavior: behavior()});
    }
  }

  tree.addEventListener('click', function (ev) {
    var b = ev.target.closest('button');
    if (!b || !tree.contains(b)) { return; }
    if (b.classList.contains('tog') || b.classList.contains('line-tog')) {
      setExpanded(b, b.getAttribute('aria-expanded') !== 'true');
    } else if (b.classList.contains('sel')) {
      select(b.getAttribute('data-id'), {from: 'tree', row: b.closest('li')});
    }
  });
  ov.addEventListener('click', function (ev) {
    var b = ev.target.closest('.ov-chip');
    if (b) { select(b.getAttribute('data-id'), {from: 'overview', reveal: true}); }
  });
  dRel.addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-rel]');
    if (b) { select(b.getAttribute('data-rel'), {from: 'rel', reveal: true}); }
  });
  document.getElementById('back-to-tree').addEventListener('click', function () {
    if (!lastRow) { return; }
    reveal(lastRow);
    lastRow.scrollIntoView({block: 'center', behavior: behavior()});
    var s = lastRow.querySelector('.sel');
    if (s) { s.focus({preventScroll: true}); }
  });
  document.getElementById('expand-all').addEventListener('click', function () {
    toggles().forEach(function (b) { setExpanded(b, true); });
  });
  document.getElementById('collapse-all').addEventListener('click', function () {
    toggles().forEach(function (b) { setExpanded(b, false); });
  });

  function matchNode(li, q) {
    var n = data.nodes[li.getAttribute('data-id')];
    var self = (li.getAttribute('data-id') + ' ' + n.title).toLowerCase().indexOf(q) >= 0;
    var any = false, ul = childList(li);
    if (ul) {
      for (var k = ul.firstElementChild; k; k = k.nextElementSibling) { if (matchNode(k, q)) { any = true; } }
      setExpanded(ownToggle(li), any);
    }
    li.hidden = !(self || any);
    if (self) { li.setAttribute('data-hit', ''); } else { li.removeAttribute('data-hit'); }
    return self || any;
  }
  function applyFilter() {
    var q = filter.value.trim().toLowerCase();
    if (!q) {
      tree.querySelectorAll('li').forEach(function (li) { li.hidden = false; li.removeAttribute('data-hit'); });
      if (saved) { saved.forEach(function (st) { setExpanded(st[0], st[1]); }); saved = null; }
      noMatch.hidden = true;
      return;
    }
    if (!saved) {
      saved = [];
      toggles().forEach(function (b) { saved.push([b, b.getAttribute('aria-expanded') === 'true']); });
    }
    var hits = 0;
    for (var line = tree.firstElementChild; line; line = line.nextElementSibling) {
      var ul = childList(line), any = false;
      if (line.querySelector('.lname').textContent.toLowerCase().indexOf(q) >= 0) {
        line.querySelectorAll('li').forEach(function (li) { li.hidden = false; });
        any = true;
      } else {
        for (var k = ul.firstElementChild; k; k = k.nextElementSibling) { if (matchNode(k, q)) { any = true; } }
      }
      line.hidden = !any;
      setExpanded(ownToggle(line), any);
      if (any) { hits++; }
    }
    noMatch.hidden = hits > 0;
  }
  filter.addEventListener('input', applyFilter);

  var themeBtn = document.getElementById('theme');
  var modes = ['auto', 'light', 'dark'], modeName = {auto: '自动', light: '浅色', dark: '深色'};
  function curMode() {
    var t = document.documentElement.getAttribute('data-theme');
    return t === 'light' || t === 'dark' ? t : 'auto';
  }
  function showMode() { themeBtn.textContent = '配色：' + modeName[curMode()]; }
  themeBtn.addEventListener('click', function () {
    var m = modes[(modes.indexOf(curMode()) + 1) % modes.length];
    if (m === 'auto') { document.documentElement.removeAttribute('data-theme'); } else { document.documentElement.setAttribute('data-theme', m); }
    try { if (m === 'auto') { localStorage.removeItem('ht-theme'); } else { localStorage.setItem('ht-theme', m); } } catch (e) { }
    showMode();
  });
  showMode();

  var h = decodeURIComponent((location.hash || '').slice(1));
  if (data.nodes[h]) { select(h, {reveal: true}); }
}
if (typeof document !== 'undefined' && document.getElementById) {
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', htInit); } else { htInit(); }
}
