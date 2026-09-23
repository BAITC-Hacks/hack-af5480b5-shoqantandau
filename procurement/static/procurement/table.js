/* Умная таблица без зависимостей: мгновенный поиск, фильтры, сортировка по клику, постраничный вывод.
   Данные — массив объектов (из json_script), строки рисуются только для текущей страницы. */
class SmartTable {
  constructor(opts) {
    Object.assign(this, {pageSize: 50, sortKey: null, sortDir: 1, filters: {}, query: ''}, opts);
    this.page = 1;
    this.table = document.querySelector(opts.table);
    this.tbody = this.table.querySelector('tbody');
    this.foot = document.querySelector(opts.foot);
    this.searchKeys = opts.searchKeys || [];
    this.data.forEach(r => r._s = this.searchKeys.map(k => String(r[k] ?? '')).join(' ').toLowerCase());
    this.table.querySelectorAll('th[data-sort]').forEach(th => {
      th.classList.add('sortable');
      th.addEventListener('click', () => {
        const k = th.dataset.sort;
        this.sortDir = this.sortKey === k ? -this.sortDir : (th.dataset.dir === 'desc' ? -1 : 1);
        this.sortKey = k;
        this.table.querySelectorAll('th').forEach(x => x.classList.remove('sorted-asc', 'sorted-desc'));
        th.classList.add(this.sortDir > 0 ? 'sorted-asc' : 'sorted-desc');
        this.render();
      });
    });
    if (opts.search) {
      const inp = document.querySelector(opts.search);
      inp.addEventListener('input', () => { this.query = inp.value.trim().toLowerCase(); this.page = 1; this.render(); });
      this.query = inp.value.trim().toLowerCase();
    }
    this.render();
  }
  setFilter(name, fn) { if (fn) this.filters[name] = fn; else delete this.filters[name]; this.page = 1; this.render(); }
  rows() {
    const words = this.query.split(/\s+/).filter(Boolean);
    let rows = this.data.filter(r => words.every(w => r._s.includes(w)) && Object.values(this.filters).every(f => f(r)));
    if (this.sortKey) {
      const k = this.sortKey, d = this.sortDir;
      rows = rows.slice().sort((a, b) => {
        const x = a[k], y = b[k];
        if (typeof x === 'number' || typeof y === 'number') return ((x ?? -1e18) - (y ?? -1e18)) * d;
        return String(x ?? '').localeCompare(String(y ?? ''), 'ru') * d;
      });
    }
    return rows;
  }
  render() {
    const rows = this.rows();
    const pages = Math.max(1, Math.ceil(rows.length / this.pageSize));
    this.page = Math.min(this.page, pages);
    const slice = rows.slice((this.page - 1) * this.pageSize, this.page * this.pageSize);
    this.tbody.innerHTML = slice.length ? slice.map(r => this.rowHtml(r)).join('')
      : `<tr><td colspan="20" class="text-center text-muted py-4">${this.emptyText || 'Ничего не найдено. Попробуйте другое слово или снимите фильтры.'}</td></tr>`;
    if (this.afterRender) this.afterRender(slice);
    if (this.foot) {
      this.foot.innerHTML = `<span>Найдено: <b>${rows.length.toLocaleString('ru')}</b>${rows.length !== this.data.length ? ` из ${this.data.length.toLocaleString('ru')}` : ''}</span>
        <span class="d-flex gap-2 align-items-center">
          <button type="button" class="btn btn-sm btn-outline-primary" data-p="-1" ${this.page <= 1 ? 'disabled' : ''}>← Назад</button>
          <span>стр. ${this.page} из ${pages}</span>
          <button type="button" class="btn btn-sm btn-outline-primary" data-p="1" ${this.page >= pages ? 'disabled' : ''}>Дальше →</button></span>`;
      this.foot.querySelectorAll('[data-p]').forEach(b => b.addEventListener('click', () => {
        this.page += +b.dataset.p; this.render(); this.table.scrollIntoView({behavior: 'smooth', block: 'start'});
      }));
    }
    if (this.onCount) this.onCount(rows.length);
  }
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const fmt = n => n == null ? '—' : Math.round(n).toLocaleString('ru');
function daysText(v) {
  if (v == null || v >= 9999) return 'нет продаж';
  if (v >= 365) return 'больше года';
  if (v < 1) return 'уже нет';
  const n = Math.round(v), m10 = n % 10, m100 = n % 100;
  const w = m10 === 1 && m100 !== 11 ? 'день' : (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'дня' : 'дней');
  return `${n} ${w}`;
}
