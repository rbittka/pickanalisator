// «Мой разбор»: статистика игрока по его матчам. Данные — /api/me/analysis.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
const pct = (x, d = 1) => (x * 100).toFixed(d) + '%';
const pp = (x) => (x >= 0 ? '+' : '') + (x * 100).toFixed(1);
let heroById = new Map(), period = '180', seq = 0;

async function api(path) {
  const r = await fetch(path);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(body.detail || `Ошибка сервера (${r.status})`), { status: r.status });
  return body;
}

function hero(id) {
  const h = heroById.get(id);
  return h || { name: `Герой ${id}`, img: '' };
}
const img = (h) => (h.img ? `<img src="${esc(h.img)}" alt="" loading="lazy">` : '');

function item(x) {
  const h = hero(x.id);
  return `<div class="item">${img(h)}<div><div class="name">${esc(h.name)}</div>
    <div class="sub">${x.games} игр · винрейт ${pct(x.winrate)}</div></div>
    <div class="val ${x.delta >= 0 ? 'p' : 'n'}" title="Отличие от твоего обычного винрейта, со сглаживанием">${pp(x.delta)}</div></div>`;
}

function render(r) {
  const wr = r.games ? r.wins / r.games : 0;
  $('#summary').hidden = false;
  $('#summary').innerHTML = `<div class="summary">
    <div><b>${r.games.toLocaleString('ru-RU')}</b> <span>игр</span></div>
    <div><b>${pct(wr)}</b> <span>винрейт</span></div>
    <div><span>Чаще всего: ${r.heroes.slice(0, 3).map((h) => `${esc(hero(h.id).name)} (${h.games})`).join(', ') || '—'}</span></div>
  </div>`;

  $('#againstPanel').hidden = $('#withPanel').hidden = false;
  $('#against').innerHTML = r.against.map(item).join('') || '<div class="empty">Нет героев, против которых ты заметно проседаешь.</div>';
  $('#with').innerHTML = r.with.map(item).join('') || '<div class="empty">Пока мало данных.</div>';

  const p = r.patch;
  $('#patchPanel').hidden = !p.heroes.length;
  $('#patchTitle').textContent = `Твои герои в патче ${p.name}`;
  const cell = (g, w) => (g ? `${pct(w / g, 0)} <span class="sub">(${g})</span>` : '—');
  $('#patch').innerHTML = `<tr><th>Герой</th><th>${esc(p.name)}</th><th>${esc(p.prev_name || 'прошлый')}</th><th>Изменение</th></tr>`
    + p.heroes.map((x) => {
      const h = hero(x.id);
      const diff = x.games >= 5 && x.prev_games >= 5 ? x.wins / x.games - x.prev_wins / x.prev_games : null;
      return `<tr><td>${img(h)}${esc(h.name)}</td><td>${cell(x.games, x.wins)}</td><td>${cell(x.prev_games, x.prev_wins)}</td>
        <td class="${diff == null ? '' : diff >= 0 ? 'p' : 'n'}">${diff == null ? '—' : pp(diff)}</td></tr>`;
    }).join('');

  $('#benchPanel').hidden = !r.benchmarks.length;
  $('#bench').innerHTML = r.benchmarks.map((b) => {
    const h = hero(b.id);
    return `<div><div class="item" style="background:none;padding:0 0 6px">${img(h)}<div><div class="name">${esc(h.name)}</div>
      <div class="sub">${b.games} игр</div></div></div>`
      + b.metrics.map((m) => `<div class="metric"><span>${esc(m.label)}: <b>${m.value >= 10 ? Math.round(m.value) : m.value.toFixed(2)}</b></span>
        <div class="bar" title="Лучше, чем у ${pct(m.better_than, 0)} игроков"><div style="width:${Math.round(m.better_than * 100)}%"></div></div>
        <span class="${m.better_than >= 0.5 ? 'p' : 'n'}">лучше ${pct(m.better_than, 0)}</span></div>`).join('')
      + '</div>';
  }).join('');
}

async function load() {
  const my = ++seq;
  $('#state').hidden = false;
  $('#state').className = 'state';
  $('#state').textContent = 'Загружаю твои матчи… первый раз это может занять до 20 секунд.';
  try {
    const r = await api(`/api/me/analysis?period=${period}`);
    if (my !== seq) return;
    $('#state').hidden = true;
    render(r);
  } catch (e) {
    if (my !== seq) return;
    $('#state').className = 'state err';
    $('#state').innerHTML = e.status === 401
      ? 'Нужно войти через Steam. <a href="/auth/steam/login">Войти</a>'
      : esc(e.message);
  }
}

$('#period').addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  period = b.dataset.period;
  $('#period').querySelectorAll('button').forEach((x) => x.classList.toggle('on', x === b));
  load();
});

(async () => {
  try {
    const [me, heroes] = await Promise.all([api('/api/me'), api('/api/heroes')]);
    heroById = new Map(heroes.map((h) => [h.id, h]));
    if (me.logged_in) $('#who').textContent = `· ${me.name}`;
  } catch { /* имена героев не загрузились — покажем номера */ }
  load();
})();
