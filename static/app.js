// Анализатор пиков Dota 2: интерфейс. Вся статистика и расчёты — на сервере (/api).
const POS_NAMES = { 1: 'Керри', 2: 'Мид', 3: 'Оффлейн', 4: 'Роумер', 5: 'Саппорт' };
const RANKS = { 1: 'Herald', 2: 'Guardian', 3: 'Crusader', 4: 'Archon', 5: 'Legend', 6: 'Ancient', 7: 'Divine', 8: 'Immortal' };

let HEROES = [], heroById = new Map();
const state = { ally: [], enemy: [], bans: [], mode: 'ally', side: 'ally', pos: 0, search: '', replace: null };

const $ = (s) => document.querySelector(s);
const pp = (x, d = 1) => (x >= 0 ? '+' : '') + (x * 100).toFixed(d);
const cls = (x) => (x >= 0 ? 'p' : 'n');
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

function heroImg(h, extra = '') {
  return `<img src="${h.img}" alt="${esc(h.name)}" title="${esc(h.name)}" loading="lazy" ${extra}>`;
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
    el.appendChild(div);
  }
}

function addHero(id, mode = state.mode, pos) {
  if (mode === 'ban') { state.bans.push(id); render(); return; }
  if (state[mode].length >= 5) return;
  const h = heroById.get(id);
  const used = new Set(state[mode].map((s) => s.pos));
  const p = pos || positionsOf(h).find((x) => !used.has(x)) || positionsOf(h)[0];
  state[mode].push({ id, pos: p });
  render();
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
  el.querySelectorAll('.rec-top[data-i]').forEach((n) => n.addEventListener('click', () => {
    const r = list[+n.dataset.i];
    if (rep && state[rep.team][rep.index]) {
      state[rep.team][rep.index] = { id: r.id, pos: r.pos };
      state.replace = null;
      render();
    } else addHero(r.id, state.side, r.pos);
  }));
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
    replace: state.replace, limit: 15,
    weights: { counter: +$('#wCounter').value, synergy: +$('#wSyn').value, meta: +$('#wMeta').value },
  };
  $('#recs').classList.add('loading');
  try {
    const res = await api('/api/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (seq !== analyzeSeq) return;
    renderRecs(res);
    renderAdvantage(res.advantage);
  } catch (e) {
    if (seq === analyzeSeq) $('#recs').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
  } finally {
    if (seq === analyzeSeq) $('#recs').classList.remove('loading');
  }
}

function renderGrid() {
  const taken = new Set([...state.ally, ...state.enemy].map((s) => s.id).concat(state.bans));
  const q = state.search.trim().toLowerCase();
  $('#grid').innerHTML = HEROES.map((h) => {
    const match = !q || h.name.toLowerCase().includes(q) || h.npc.includes(q);
    return `<div class="hero ${taken.has(h.id) ? 'used' : ''} ${match ? '' : 'dim'}" data-id="${h.id}">${heroImg(h)}<div class="nm">${esc(h.name)}</div></div>`;
  }).join('');
}

function render() {
  const rp = state.replace;
  if (rp && !state[rp.team][rp.index]) state.replace = null;
  renderSlots('ally');
  renderSlots('enemy');
  $('#bans').innerHTML = '<span class="lbl">Баны:</span>' + (state.bans.length
    ? state.bans.map((id, i) => heroImg(heroById.get(id), `data-ban="${i}"`)).join('')
    : '<span class="lbl">нет</span>');
  $('#bans').querySelectorAll('[data-ban]').forEach((n) => n.addEventListener('click', () => { state.bans.splice(+n.dataset.ban, 1); render(); }));
  $('#side').hidden = $('#posTabs').hidden = !!state.replace;
  $('#posTabs').innerHTML = [0, 1, 2, 3, 4, 5].map((p) =>
    `<button data-pos="${p}" class="${state.pos === p ? 'on' : ''}">${p ? `${p} · ${POS_NAMES[p]}` : 'Любая'}</button>`).join('');
  for (const k of ['wCounter', 'wSyn', 'wMeta']) $('#' + k + 'V').textContent = '×' + (+$('#' + k).value).toFixed(1);
  renderGrid();
  scheduleAnalyze();
}

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
  prev.hidden = false;
  $('#shotHint').hidden = true;
  $('#shotRun').disabled = false;
  setShotStatus('Скриншот загружен');
}

async function recognize() {
  $('#shotRun').disabled = true;
  setShotStatus('Ищу героев на скриншоте…');
  const slow = setTimeout(() => setShotStatus('Верхняя панель не найдена, распознаю фото с помощью Claude, это до минуты…'), 2500);
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
    const how = r.method === 'claude' ? 'с помощью Claude' : 'по портретам верхней панели';
    const total = ours.length + theirs.length;
    setShotStatus(total
      ? `Найдено ${how}: ${ours.length} у нас, ${theirs.length} у противников${r.bans.length ? `, банов ${r.bans.length}` : ''}. Если что-то не так, поправь драфт вручную.`
      : 'Героев на снимке не нашлось. Нужен скриншот всего экрана игры, где видна верхняя панель с портретами.', !total);
  } catch (e) {
    setShotStatus(e.message, true);
  } finally {
    clearTimeout(slow);
    $('#shotRun').disabled = !shotBlob;
  }
}

// --- События ---
$('#grid').addEventListener('click', (e) => {
  const n = e.target.closest('.hero');
  if (n) { addHero(+n.dataset.id); $('#search').value = ''; state.search = ''; renderGrid(); }
});
$('#search').addEventListener('input', (e) => { state.search = e.target.value; renderGrid(); });
$('#search').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  document.querySelector('.hero:not(.used):not(.dim)')?.click();
});
$('#mode').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  state.mode = b.dataset.mode;
  $('#mode').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
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
  } catch (e) {
    $('#recs').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    $('#meta').textContent = e.status === 503 ? 'Сервер загружает статистику…' : 'Нет связи с сервером';
    setTimeout(boot, 5000);
  }
}
boot();
