// Анализатор пиков Dota 2: интерфейс. Вся статистика и расчёты — на сервере (/api).
const POS_NAMES = { 1: 'Керри', 2: 'Мид', 3: 'Оффлейн', 4: 'Роумер', 5: 'Саппорт' };
const RANKS = { 1: 'Herald', 2: 'Guardian', 3: 'Crusader', 4: 'Archon', 5: 'Legend', 6: 'Ancient', 7: 'Divine', 8: 'Immortal' };

let HEROES = [], heroById = new Map();
const state = { ally: [], enemy: [], bans: [], mode: 'ally', side: 'ally', pos: 0, search: '', replace: null };

// Личный пул героев: [{id, level}] в localStorage этого браузера.
let POOL = [];
try { POOL = JSON.parse(localStorage.getItem('pool') || '[]').filter((p) => Number.isInteger(p.id)); } catch { POOL = []; }
function savePool() { try { localStorage.setItem('pool', JSON.stringify(POOL)); } catch { /* приватный режим */ } }
const poolLevel = (id) => POOL.find((p) => p.id === id)?.level || null;

// Вошедший через Steam игрок и его статистика по героям за полгода: id → {games, wins}.
let ME = null, MYSTATS = new Map();
const myStat = (id) => {
  const s = MYSTATS.get(id);
  return s ? `${s.games} игр · ${(100 * s.wins / s.games).toFixed(0)}%` : '';
};

const $ = (s) => document.querySelector(s);
const pp = (x, d = 1) => (x >= 0 ? '+' : '') + (x * 100).toFixed(d);
const cls = (x) => (x >= 0 ? 'p' : 'n');
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function heroImg(h, extra = '') {
  return `<img src="${esc(h.img)}" alt="${esc(h.name)}" title="${esc(h.name)}" loading="lazy" ${extra}>`;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(body.detail || `Ошибка сервера (${r.status})`), { status: r.status });
  return body;
}

// Позиции героя по убыванию доли игр; редкие (<3%) не показываем.
function positionsOf(h) {
  const ps = Object.entries(h.positions).filter(([, s]) => s >= 0.03).sort((a, b) => b[1] - a[1]).map(([p]) => +p);
  return ps.length ? ps : [1, 2, 3, 4, 5];
}

function renderSlots(key) {
  const el = $('#' + key);
  el.innerHTML = '';
  for (let i = 0; i < 5; i++) {
    const s = state[key][i];
    const div = document.createElement('div');
    div.className = 'slot';
    if (s) {
      const h = heroById.get(s.id);
      const ps = positionsOf(h);
      if (!ps.includes(s.pos)) ps.push(s.pos);
      const opts = ps.sort().map((p) => `<option value="${p}" ${p === s.pos ? 'selected' : ''}>${p} · ${POS_NAMES[p]}</option>`).join('');
      const replacing = state.replace && state.replace.team === key && state.replace.index === i;
      if (replacing) div.classList.add('replacing');
      div.innerHTML = heroImg(h, 'data-pick') + (replacing ? '<span class="tag">замена</span>' : '')
        + `<button class="x" type="button" title="Убрать из драфта" aria-label="Убрать ${esc(h.name)} из драфта">×</button><select aria-label="Позиция">${opts}</select>`;
      div.querySelector('[data-pick]').addEventListener('click', () => {
        state.replace = replacing ? null : { team: key, index: i };
        render();
      });
      div.querySelector('.x').addEventListener('click', () => { state[key].splice(i, 1); state.replace = null; render(); });
      div.querySelector('select').addEventListener('change', (e) => { s.pos = +e.target.value; render(); });
      div.title = replacing ? 'Клик — выйти из режима замены' : 'Клик — подобрать замену этому герою';
    }
    else {
      div.classList.add('empty');
      if (state.mode === key && i === state[key].length) div.classList.add('target');
      div.textContent = '+';
      div.title = key === 'ally' ? 'Следующий герой — в твою команду' : 'Следующий герой — противникам';
      div.addEventListener('click', () => setMode(key));
    }
    el.appendChild(div);
  }
}

function addHero(id, mode = state.mode, pos) {
  if (mode === 'ban') { state.bans.push(id); render(); return; }
  if (mode === 'pool') {
    const i = POOL.findIndex((p) => p.id === id);
    if (i >= 0) POOL.splice(i, 1); else POOL.push({ id, level: 1 });
    savePool();
    render();
    return;
  }
  if (state[mode].length >= 5) return;
  const h = heroById.get(id);
  const used = new Set(state[mode].map((s) => s.pos));
  const p = pos || positionsOf(h).find((x) => !used.has(x)) || positionsOf(h)[0];
  state[mode].push({ id, pos: p });
  // Команда заполнилась — следующие клики идут в другую, если там есть место.
  const other = mode === 'ally' ? 'enemy' : 'ally';
  if (state.mode === mode && state[mode].length >= 5 && state[other].length < 5) setMode(other, false);
  render();
}

function setMode(mode, redraw = true) {
  state.mode = mode;
  $('#mode').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x.dataset.mode === mode));
  if (redraw) { renderSlots('ally'); renderSlots('enemy'); }
}

function chip(x) {
  const h = heroById.get(x.id);
  return `<span class="chip" title="${esc(h.name)}: ${x.games} игр">${heroImg(h)}<span class="${cls(x.adv)}">${pp(x.adv)}</span></span>`;
}

function recCard(r, i, current) {
  const h = heroById.get(r.id);
  const delta = r.delta != null ? `<div class="delta ${cls(r.delta)}">${pp(r.delta)} к текущему</div>` : '';
  return `
    <div class="rec ${current ? 'current' : ''}">
      <div class="rec-top" ${current ? '' : `data-i="${i}" title="${state.replace ? 'Поставить вместо текущего героя' : 'Добавить в драфт'}"`}>
        ${heroImg(h)}
        <div class="info">
          <div class="name">${esc(r.name)}</div>
          <div class="sub">${r.pos} · ${POS_NAMES[r.pos]} · WR ${(r.winrate * 100).toFixed(1)}% (${r.games.toLocaleString('ru-RU')} игр)</div>
          <div class="sub">контр <span class="${cls(r.counter)}">${pp(r.counter)}</span> ·
            синергия <span class="${cls(r.synergy)}">${pp(r.synergy)}</span> ·
            мета <span class="${cls(r.meta)}">${pp(r.meta)}</span></div>
        </div>
        <div class="score ${cls(r.score)}">${pp(r.score)}${delta}</div>
      </div>
      ${r.vs.length ? `<div class="chips"><span class="k" style="font-size:11px">vs</span>${r.vs.map(chip).join('')}</div>` : ''}
      ${r.with.length ? `<div class="chips"><span class="k" style="font-size:11px">с</span>${r.with.map(chip).join('')}</div>` : ''}
    </div>`;
}

function renderRecs(res) {
  const rep = state.replace;
  const el = $('#recs');
  let head = '';
  if (rep) {
    const slot = state[rep.team][rep.index], h = heroById.get(slot.id);
    head = `<div class="rep-head"><span>Замена для <b>${esc(h.name)}</b> · ${slot.pos} · ${POS_NAMES[slot.pos]}</span>
      <button type="button" id="repCancel">Отмена</button></div>` + (res.current ? recCard(res.current, -1, true) : '');
  }
  const list = res.recommendations;
  el.innerHTML = head + (list.length ? list.map((r, i) => recCard(r, i, false)).join('') : '<div class="empty">Нет подходящих героев</div>');
  $('#repCancel')?.addEventListener('click', () => { state.replace = null; render(); });
  el.querySelectorAll('.rec-top[data-i]').forEach((n) => n.addEventListener('click', () => pickRecommendation(list[+n.dataset.i])));
}

// Взять рекомендованного героя: в режиме замены — вместо текущего, иначе в команду выбранной стороны.
function pickRecommendation(r) {
  const rep = state.replace;
  if (rep && state[rep.team][rep.index]) {
    state[rep.team][rep.index] = { id: r.id, pos: r.pos };
    state.replace = null;
    render();
  } else addHero(r.id, state.side, r.pos);
}

// Короткое объяснение по цифрам: против кого силён, с кем сочетается, где слабое место.
const STRONG = 0.01;
function reasons(r) {
  const named = (list) => list.map((x) => `${esc(heroById.get(x.id).name)} (${pp(x.adv)})`).join(', ');
  const plus = [], minus = [];
  const good = r.vs.filter((x) => x.adv >= STRONG).sort((a, b) => b.adv - a.adv).slice(0, 2);
  const syn = r.with.filter((x) => x.adv >= STRONG).sort((a, b) => b.adv - a.adv).slice(0, 2);
  const bad = r.vs.filter((x) => x.adv <= -STRONG).sort((a, b) => a.adv - b.adv).slice(0, 1);
  if (good.length) plus.push(`силён против ${named(good)}`);
  if (syn.length) plus.push(`сочетается с ${named(syn)}`);
  if (r.meta >= STRONG) plus.push(`винрейт ${(r.winrate * 100).toFixed(1)}% — силён в текущей мете`);
  if (!plus.length) plus.push(`надёжный вариант: без явных слабостей в этом драфте, винрейт ${(r.winrate * 100).toFixed(1)}%`);
  if (bad.length) minus.push(`осторожно с ${named(bad)}`);
  if (r.delta != null) (r.delta >= 0 ? plus : minus).push(`${pp(r.delta)} п.п. к текущему герою`);
  return plus.map((t) => `<li>${t}</li>`).join('') + minus.map((t) => `<li class="minus">${t}</li>`).join('');
}

function renderBest(res) {
  const list = res.recommendations.slice(0, 3);
  $('#best').hidden = !list.length;
  if (!list.length) return;
  const rep = state.replace;
  let title = state.side === 'ally' ? 'Лучший пик для нас' : 'Лучший пик для противников';
  if (rep) title = `Лучшая замена для ${heroById.get(state[rep.team][rep.index].id).name}`;
  else if (state.pos) title += ` · ${state.pos} · ${POS_NAMES[state.pos]}`;
  if (!state.ally.length && !state.enemy.length) title = 'Сильнейшие герои в мете сейчас';
  $('#bestTitle').textContent = title;
  fillPicks('#bestList', list, 'Лучший выбор');
  // Пул советуем только для своей команды.
  const pool = state.side === 'ally' ? (res.pool_recommendations || []).slice(0, 3) : [];
  $('#bestPoolTitle').hidden = !pool.length;
  fillPicks('#bestPool', pool, 'Лучший из твоих');
}

function fillPicks(sel, list, badge) {
  const rep = state.replace;
  $(sel).innerHTML = list.map((r, i) => {
    const lvl = poolLevel(r.id);
    return `
    <div class="pick ${i ? '' : 'top'}">
      <div class="pick-head">
        ${heroImg(heroById.get(r.id))}
        <div>
          ${i ? '' : `<div class="pick-badge">${badge}</div>`}
          <div class="name">${esc(r.name)}</div>
          <div class="sub">${r.pos} · ${POS_NAMES[r.pos]}</div>
          ${lvl || myStat(r.id) ? `<div class="pick-level">★ ${[lvl ? `уровень ${lvl}` : '', myStat(r.id)].filter(Boolean).join(' · ')}</div>` : ''}
        </div>
      </div>
      <ul>${reasons(r)}</ul>
      <div class="pick-foot">
        <span class="score ${cls(r.score)}">${pp(r.score)}</span>
        <button type="button" data-i="${i}">${rep ? 'Заменить' : 'Взять'}</button>
      </div>
    </div>`;
  }).join('');
  $(sel).querySelectorAll('button[data-i]').forEach((b) => b.addEventListener('click', () => pickRecommendation(list[+b.dataset.i])));
}

function renderPool() {
  const weight = (p) => (p.level || 0) * 1000 + (MYSTATS.get(p.id)?.games || 0);
  const items = [...POOL].sort((a, b) => weight(b) - weight(a)).filter((p) => heroById.has(p.id));
  $('#poolHint').hidden = items.length > 0;
  $('#poolList').innerHTML = items.map((p) => {
    const h = heroById.get(p.id);
    const stat = myStat(p.id);
    return `<div class="pool-item">${heroImg(h)}<span>${esc(h.name)}</span>${stat ? `<span class="stat">${stat}</span>` : ''}
      <label>ур. <input type="number" min="1" max="30" value="${p.level || ''}" placeholder="—" data-level="${p.id}" aria-label="Уровень ${esc(h.name)}"></label>
      <button type="button" class="x" data-drop="${p.id}" aria-label="Убрать ${esc(h.name)} из пула">×</button></div>`;
  }).join('');
}

function renderAdvantage(adv) {
  if (adv == null) { $('#adv').innerHTML = ''; return; }
  const pct = Math.max(5, Math.min(95, 50 + adv * 100 * 2.5));
  const even = state.ally.length === state.enemy.length;
  $('#adv').innerHTML = `<div class="bar"><div style="width:${pct}%"></div></div>
    <div class="txt"><span>Преимущество драфта: <b class="${cls(adv)}">${pp(adv)} п.п.</b></span>
    <span>${even ? 'по матчапам, синергиям и мете' : 'точно, когда героев в командах поровну'}</span></div>`;
}

// Запросы к /api/analyze: при быстрых изменениях учитываем только последний ответ.
let analyzeSeq = 0, analyzeTimer = null;
function scheduleAnalyze() {
  clearTimeout(analyzeTimer);
  analyzeTimer = setTimeout(runAnalyze, 120);
}
async function runAnalyze() {
  const seq = ++analyzeSeq;
  const body = {
    ally: state.ally, enemy: state.enemy, bans: state.bans, side: state.side, pos: state.pos,
    replace: state.replace, limit: 15, pool: POOL.map((p) => p.id),
    weights: { counter: +$('#wCounter').value, synergy: +$('#wSyn').value, meta: +$('#wMeta').value },
  };
  $('#recs').classList.add('loading');
  try {
    const res = await api('/api/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (seq !== analyzeSeq) return;
    renderRecs(res);
    renderBest(res);
    renderAdvantage(res.advantage);
  } catch (e) {
    if (seq === analyzeSeq) $('#recs').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  } finally {
    if (seq === analyzeSeq) $('#recs').classList.remove('loading');
  }
}

function renderGrid() {
  const picked = new Set([...state.ally, ...state.enemy].map((s) => s.id));
  const banned = new Set(state.bans);
  const q = state.search.trim().toLowerCase();
  $('#grid').innerHTML = HEROES.map((h) => {
    const match = !q || h.name.toLowerCase().includes(q) || h.npc.includes(q);
    const star = POOL.some((p) => p.id === h.id) ? '<span class="star" title="В твоём пуле">★</span>' : '';
    // Забаненного героя можно снять кликом; взятого в драфт — убрать крестиком в слоте.
    const cls = picked.has(h.id) ? 'used' : banned.has(h.id) ? 'banned' : '';
    const title = banned.has(h.id) ? ' title="В бане — нажми, чтобы снять бан"' : '';
    return `<div class="hero ${cls} ${match ? '' : 'dim'}" data-id="${h.id}"${title}>${heroImg(h)}${star}<div class="nm">${esc(h.name)}</div></div>`;
  }).join('');
}

function render() {
  const rp = state.replace;
  if (rp && !state[rp.team][rp.index]) state.replace = null;
  renderSlots('ally');
  renderSlots('enemy');
  $('#bans').innerHTML = '<span class="lbl">Баны:</span>' + (state.bans.length
    ? state.bans.map((id, i) => `<span class="ban" data-ban="${i}" title="Снять бан">${heroImg(heroById.get(id))}<b>×</b></span>`).join('')
    : '<span class="lbl">нет</span>');
  $('#bans').querySelectorAll('[data-ban]').forEach((n) => n.addEventListener('click', () => { state.bans.splice(+n.dataset.ban, 1); render(); }));
  $('#side').hidden = $('#posTabs').hidden = !!state.replace;
  $('#posTabs').innerHTML = [0, 1, 2, 3, 4, 5].map((p) =>
    `<button data-pos="${p}" class="${state.pos === p ? 'on' : ''}">${p ? `${p} · ${POS_NAMES[p]}` : 'Любая'}</button>`).join('');
  for (const k of ['wCounter', 'wSyn', 'wMeta']) $('#' + k + 'V').textContent = '×' + (+$('#' + k).value).toFixed(1);
  renderGrid();
  renderPool();
  scheduleAnalyze();
}

$('#poolList').addEventListener('change', (e) => {
  const id = +e.target.dataset.level;
  const p = POOL.find((x) => x.id === id);
  if (!p) return;
  p.level = e.target.value === '' ? null : Math.max(1, Math.min(30, Math.round(+e.target.value) || 1));
  savePool();
  render();
});
$('#poolList').addEventListener('click', (e) => {
  const id = +e.target.dataset.drop;
  if (!id) return;
  POOL = POOL.filter((p) => p.id !== id);
  savePool();
  render();
});

// --- Распознавание драфта по скриншоту (на сервере) ---
let shotBlob = null, ourSide = 'radiant';

function setShotStatus(text, err = false) {
  $('#shotStatus').textContent = text;
  $('#shotStatus').classList.toggle('err', err);
}
function setShot(blob) {
  if (!blob || !/^image\//.test(blob.type)) return;
  shotBlob = blob;
  const prev = $('#shotPreview');
  if (prev.src) URL.revokeObjectURL(prev.src);
  prev.src = URL.createObjectURL(blob);
  // Некоторые форматы (например, TIFF из буфера обмена) браузер не показывает — тогда просто пишем, что файл выбран.
  prev.onerror = () => { prev.hidden = true; $('#shotHint').hidden = false; $('#shotHint').innerHTML = '<b>Картинка загружена</b><br>Нажми «Распознать героев»'; };
  prev.hidden = false;
  $('#shotHint').hidden = true;
  $('#shotRun').disabled = false;
  setShotStatus('Скриншот загружен');
}

async function recognize() {
  $('#shotRun').disabled = true;
  setShotStatus('Распознаю героев…');
  // Честная подсказка при долгом ожидании, без догадок о том, что делает сервер.
  const slow = setTimeout(() => setShotStatus('Распознаю героев… сервер просыпается или разбирает фото, это может занять до минуты.'), 8000);
  try {
    const form = new FormData();
    form.append('image', shotBlob, 'draft');
    const r = await api('/api/recognize', { method: 'POST', body: form });
    const ids = (list) => list.filter((id) => heroById.has(id));
    const [ours, theirs] = ourSide === 'radiant' ? [ids(r.radiant), ids(r.dire)] : [ids(r.dire), ids(r.radiant)];
    state.ally = []; state.enemy = []; state.bans = []; state.replace = null;
    for (const id of ours.slice(0, 5)) addHero(id, 'ally');
    for (const id of theirs.slice(0, 5)) addHero(id, 'enemy');
    for (const id of ids(r.bans)) addHero(id, 'ban');
    render();
    const total = ours.length + theirs.length;
    setShotStatus(total
      ? `Найдено: ${ours.length} у нас, ${theirs.length} у противников${r.bans.length ? `, банов ${r.bans.length}` : ''}. Если что-то не так, поправь драфт вручную.`
      : 'Героев на снимке не нашлось. Нужен скриншот всего экрана игры, где видна верхняя панель с портретами.', !total);
  } catch (e) {
    // Сетевой сбой в браузере приходит без кода ответа («Load failed», «Failed to fetch»).
    setShotStatus(e.status ? e.message : 'Сервер не ответил. Проверь интернет и попробуй ещё раз через минуту.', true);
  } finally {
    clearTimeout(slow);
    $('#shotRun').disabled = !shotBlob;
  }
}

// --- События ---
$('#grid').addEventListener('click', (e) => {
  const n = e.target.closest('.hero');
  if (!n) return;
  const id = +n.dataset.id;
  if (e.altKey && !n.classList.contains('banned')) {
    addHero(id, 'ban');
    return;
  }
  if (n.classList.contains('banned') && state.mode !== 'pool') {
    state.bans = state.bans.filter((b) => b !== id);
    render();
    return;
  }
  addHero(id);
  $('#search').value = '';
  state.search = '';
  renderGrid();
});
$('#grid').addEventListener('contextmenu', (e) => {
  const n = e.target.closest('.hero');
  if (!n || n.classList.contains('banned')) return;
  e.preventDefault();
  addHero(+n.dataset.id, 'enemy');
});
$('#search').addEventListener('input', (e) => { state.search = e.target.value; renderGrid(); });
$('#search').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  document.querySelector('.hero:not(.used):not(.banned):not(.dim)')?.click();
});
$('#mode').addEventListener('click', (e) => {
  const b = e.target.closest('button');
  if (b) setMode(b.dataset.mode);
});
$('#side').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.side = b.dataset.side;
  $('#side').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
  scheduleAnalyze();
});
$('#posTabs').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.pos = +b.dataset.pos; render();
});
for (const k of ['wCounter', 'wSyn', 'wMeta']) $('#' + k).addEventListener('input', () => {
  $('#' + k + 'V').textContent = '×' + (+$('#' + k).value).toFixed(1);
  scheduleAnalyze();
});
$('#reset').addEventListener('click', () => { state.ally = []; state.enemy = []; state.bans = []; state.replace = null; render(); });

$('#shotDrop').addEventListener('click', () => $('#shotFile').click());
$('#shotDrop').addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('#shotFile').click(); } });
$('#shotFile').addEventListener('change', (e) => setShot(e.target.files[0]));
$('#shotDrop').addEventListener('dragover', (e) => { e.preventDefault(); $('#shotDrop').classList.add('over'); });
$('#shotDrop').addEventListener('dragleave', () => $('#shotDrop').classList.remove('over'));
$('#shotDrop').addEventListener('drop', (e) => {
  e.preventDefault();
  $('#shotDrop').classList.remove('over');
  setShot(e.dataTransfer.files[0]);
});
document.addEventListener('paste', (e) => {
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith('image/'));
  if (item) setShot(item.getAsFile());
});
$('#ourSide').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  ourSide = b.dataset.side;
  $('#ourSide').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
});
$('#shotRun').addEventListener('click', recognize);

// Подсказки под систему: на Mac — ⌥ и ⌘, на Windows — Alt и Ctrl, на телефоне — без клавиш.
const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const IS_TOUCH = matchMedia('(hover: none)').matches;
if (IS_TOUCH) {
  $('#gridHint').textContent = 'Нажми на пустой слот, чтобы выбрать команду, затем на героев. Кнопки выше — бан и мой пул.';
  $('#shotHint').innerHTML = '<b>Скриншот драфта</b><br>Нажми, чтобы выбрать картинку из галереи';
} else {
  if (IS_MAC) $('#gridHint').textContent = $('#gridHint').textContent.replace('Alt + клик', '⌥ + клик');
  $('#shotHint').innerHTML = IS_MAC
    ? '<b>Скриншот драфта</b><br>Сделай скриншот (⌘⇧3), затем вставь сюда через ⌘V, перетащи файл или нажми, чтобы выбрать'
    : '<b>Скриншот драфта</b><br>Нажми PrtScn или Win + Shift + S, затем вставь сюда через Ctrl + V, перетащи файл или нажми, чтобы выбрать';
}

// --- Вход через Steam ---
function renderAccount(error = '') {
  const el = $('#account');
  if (ME?.logged_in) {
    el.innerHTML = `${ME.avatar ? `<img src="${esc(ME.avatar)}" alt="">` : ''}<span>${esc(ME.name)}</span>
      <button type="button" id="logout">Выйти</button>`;
    $('#logout').addEventListener('click', async () => {
      await fetch('/auth/logout', { method: 'POST' }).catch(() => {});
      location.reload();
    });
  } else {
    el.innerHTML = (error ? `<span class="err">${esc(error)}</span>` : '')
      + '<a class="btn" href="/auth/steam/login">Войти через Steam</a>';
  }
  $('#poolImport').hidden = $('#poolPeriod').hidden = !ME?.logged_in;
}

async function loadMyStats() {
  try {
    const r = await api(`/api/me/heroes?period=${$('#poolPeriod').value}`);
    MYSTATS = new Map(r.heroes.map((h) => [h.id, h]));
    $('#poolStatus').textContent = MYSTATS.size ? ''
      : 'OpenDota не видит твоих матчей. Включи в настройках Dota «Общедоступная история матчей» и сыграй хотя бы одну игру.';
  } catch (e) {
    $('#poolStatus').textContent = e.message;
  }
  render();
}
$('#poolPeriod').addEventListener('change', loadMyStats);

async function loadMe() {
  let error = '';
  if (new URLSearchParams(location.search).get('login') === 'failed') {
    error = 'Не удалось войти через Steam';
    history.replaceState(null, '', '/');
  }
  try { ME = await api('/api/me'); } catch { ME = null; }
  renderAccount(error);
  if (ME?.logged_in) loadMyStats();
}

$('#poolImport').addEventListener('click', () => {
  // Герои, сыгранные хотя бы 5 раз за выбранный период, до 15 самых частых. Уровни, что уже стоят, сохраняем.
  const mine = [...MYSTATS.values()].filter((h) => h.games >= 5 && heroById.has(h.id)).slice(0, 15);
  if (!mine.length) {
    $('#poolStatus').textContent = 'Не нашлось героев, сыгранных хотя бы 5 раз за этот период.';
    return;
  }
  let added = 0;
  for (const h of mine) {
    if (!POOL.some((p) => p.id === h.id)) { POOL.push({ id: h.id, level: null }); added++; }
  }
  savePool();
  $('#poolStatus').textContent = added ? `Добавлено героев: ${added}. Уровни Dota Plus можно вписать вручную.` : 'Все твои частые герои уже в пуле.';
  render();
});

// --- Запуск: пока сервер грузит статистику (после пробуждения ~1 мин), ждём ---
async function boot() {
  try {
    const [meta, heroes] = await Promise.all([api('/api/meta'), api('/api/heroes')]);
    HEROES = heroes;
    heroById = new Map(heroes.map((h) => [h.id, h]));
    const rank = `${RANKS[Math.floor(meta.min_rank / 10)] || meta.min_rank} ${meta.min_rank % 10 || ''}`.trim();
    $('#meta').textContent = `Источник: OpenDota · ${meta.matches.toLocaleString('ru-RU')} публичных матчей рангом ${rank}+ за ${meta.window_days} дн. · обновлено ${new Date(meta.updated * 1000).toLocaleString('ru-RU')}`;
    setShotStatus(meta.photo_recognition
      ? 'Загрузи скриншот всего экрана игры или фото монитора: герои определятся автоматически.'
      : 'Загрузи скриншот всего экрана игры: герои определятся по верхней панели.');
    render();
    loadMe();
  } catch (e) {
    $('#recs').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    $('#meta').textContent = e.status === 503 ? 'Сервер загружает статистику…' : 'Нет связи с сервером';
    setTimeout(boot, 5000);
  }
}
boot();
