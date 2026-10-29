import { useCallback, useEffect, useRef, useState } from 'react';
import { autoOffAll, closeTestPosition, panicStop, setCatchup, setSizing, deleteConfig, disconnectExchange, exportBotConfig, getExchangeAccount, getMonitor, getMonitorDetail, openTestPosition, pauseConfig, renameConfig, setAuto, setExchangeOver } from '../api';
import { fmtPct, fmtPf, fmtPrice, fmtTs, fmtUsd, signClass } from '../format';
import { downloadText } from '../tradesCsv';
import { HELP } from '../helpTexts';
import type { AutoLogItem, ExchangeAccount, ExchangeInfo, ExchangeName, ExchangeOverrides, MonitorEntry, MonitorLeg, MonitorResponse } from '../types';
import { Card as StatCard } from './StatsPanel';
import Help from './Help';

const KIND_NAMES: Record<string, string> = {
  open: 'вход', add: 'добор', move_stop: 'стоп → БУ', close_weekend: 'закрытие к концу недели', close_reverse: 'закрытие по развороту',
  test_position: 'тестовая позиция', test_close: 'закрытие тестовой', auto_toggle: 'переключение автомода', auto_off_all: 'автомод выключен везде', pause: 'пауза конфига', resume: 'конфиг возобновлён', config_removed: 'конфиг удалён', exchange_cfg: 'настройки конфига для биржи', exchange_disconnected: 'счёт биржи отключён', exchange_connected: 'счёт биржи подключён', limit_entry: 'лимитный вход', cancel_entry: 'снят ожидающий вход', protect: 'стоп и тейки',
};
const STATUS_NAMES: Record<string, string> = { dry_run: 'симуляция', sent: 'отправлено', error: 'ошибка', info: '' };

const ago = (ms: number | null | undefined) => {
  if (!ms) return '–';
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000));
  return m < 1 ? 'только что' : m < 90 ? `${m} мин назад` : m < 48 * 60 ? `${(m / 60).toFixed(1)} ч назад` : `${Math.round(m / 1440)} дн назад`;
};
const held = (h: number) => (h < 1 ? `${Math.round(h * 60)} мин` : h < 48 ? `${h.toFixed(1)} ч` : `${(h / 24).toFixed(1)} сут`);

function BotStatus({ bot, anyAuto }: { bot: MonitorResponse['bot']; anyAuto: boolean }) {
  if (!bot || bot.state === 'ok' || (bot.state === 'unknown' && !anyAuto)) return null;
  const mins = (s: number | null) => (s == null ? '?' : Math.round(s / 60));
  const text = bot.state === 'dead' ? `Бот не отвечает уже ${mins(bot.alive_age_s)} мин` : bot.state === 'stalled' ? `Бот завис: цикл проверки не заканчивается ${mins(bot.cycle_age_s)} мин` : 'Бот ещё ни разу не запускался (нет пульса)';
  return (
    <div className="banner error" data-testid="bot-status">
      ⚠️ {text}. {anyAuto ? 'Автоторговля не работает: новые сигналы не открываются, стопы в безубыток не переставляются (стопы и тейки на бирже работают).' : ''} <Help text={HELP.bot_status} />
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="mon-kv"><span className="muted">{label}</span><span>{children}</span></div>;
}

function Leg({ l, side }: { l: MonitorLeg; side: 'long' | 'short' }) {
  return (
    <tr>
      <td>{l.kind}{l.adds_n ? ` ×${l.adds_n + 1}` : l.leg > 1 ? ` #${l.leg - 1}` : ''}</td>
      <td title={fmtTs(l.entry_ts)}>{fmtPrice(l.entry)}</td>
      <td title={`маржа ${fmtUsd(l.margin)} × плечо ${l.leverage.toFixed(1)}`}>{fmtUsd(l.notional)}</td>
      <td title={l.stop_kind === 'БУ' ? 'стоп перенесён в безубыток' : 'стоп-лосс'}>{l.stop_kind} {fmtPrice(l.stop)}<span className="muted"> {l.to_stop_pct != null ? `(${l.to_stop_pct.toFixed(2)}%)` : ''}</span></td>
      <td>
        {l.tps.map((t) => <span key={t.n} className={t.hit ? 'pos' : ''} title={`${t.share}% позиции${t.hit ? ' — исполнен' : ''}`}>{t.hit ? '✓' : ''}TP{t.n} {fmtPrice(t.price)}{' '}</span>)}
        {l.to_next_tp_pct != null && <span className="muted">(до ближайшего {l.to_next_tp_pct.toFixed(2)}%)</span>}
      </td>
      <td title={side === 'long' ? 'оценка цены ликвидации лонга (изолированная маржа)' : 'оценка цены ликвидации шорта'}>{l.liq_price ? fmtPrice(l.liq_price) : '–'}</td>
      <td className={signClass(l.pnl_usd)}>{fmtUsd(l.pnl_usd)}<span className="muted"> {fmtPct(l.pnl_pct, 2)}</span></td>
      <td className="muted" title="метрики отчёта на входе">{l.metrics_at_entry.dl != null ? `DG ${l.metrics_at_entry.dg?.toFixed(2) ?? '–'} · DL ${l.metrics_at_entry.dl.toFixed(2)} · DS ${l.metrics_at_entry.ds?.toFixed(2) ?? '–'}` : '–'}</td>
    </tr>
  );
}

const PAGE = 20;
type Tab = 'pos' | 'hist' | 'stats' | 'params' | 'log';
const TABS: [Tab, string][] = [['pos', 'Позиция'], ['hist', 'История'], ['stats', 'Статистика'], ['params', 'Параметры'], ['log', 'Журнал']];

function PositionTab({ e }: { e: MonitorEntry }) {
  const pos = e.position;
  if (!pos) {
    return (
      <div>
        <p className="note">Позиции сейчас нет.</p>
        {e.ready_now && e.ready_now.length > 0 && <div className="ready-info">{e.ready_now.map((r) => <div key={r.side}><b>⏳ Готов к {r.side === 'long' ? 'ЛОНГУ' : 'ШОРТУ'}, ждёт:</b><ul>{r.lines.map((l, i) => <li key={i}>{l}</li>)}</ul></div>)}</div>}
        {e.report && <p className="muted small">Метрики сейчас: DG {e.report.dg?.toFixed(2) ?? '–'} · DL {e.report.dl.toFixed(2)} · DS {e.report.ds.toFixed(2)} ({ago(e.report.ts)})</p>}
      </div>
    );
  }
  return (
    <>
      <div className="mon-grid">
        <Row label="Открыта">{fmtTs(pos.opened_ts)} · {held(pos.hold_h)}</Row>
        <Row label="Цена сейчас">{e.price ? fmtPrice(e.price) : '–'} <span className="muted">({ago(e.candle_ts)})</span></Row>
        <Row label="Средний вход">{fmtPrice(pos.avg_entry)} <span className={signClass(pos.side === 'long' ? (e.price ?? 0) - pos.avg_entry : pos.avg_entry - (e.price ?? 0))}>{e.price ? fmtPct(((e.price / pos.avg_entry - 1) * (pos.side === 'long' ? 1 : -1)) * 100, 2) : ''}</span></Row>
        <Row label="Объём / маржа">{fmtUsd(pos.notional)} / {fmtUsd(pos.margin)}</Row>
        <Row label="Результат"><b className={signClass(pos.pnl_usd)}>{fmtUsd(pos.pnl_usd)}</b> <span className="muted">{pos.pnl_pct_deposit != null ? `${fmtPct(pos.pnl_pct_deposit, 2)} депозита` : ''}{pos.pnl_pct_margin != null ? ` · ${fmtPct(pos.pnl_pct_margin, 1)} маржи` : ''}</span></Row>
        {pos.deadline_ts && <Row label="Закрытие к концу недели">{fmtTs(pos.deadline_ts)} UTC</Row>}
        {e.report && <Row label="Метрики сейчас">DG {e.report.dg?.toFixed(2) ?? '–'} · DL {e.report.dl.toFixed(2)} · DS {e.report.ds.toFixed(2)} <span className="muted">({ago(e.report.ts)})</span></Row>}
      </div>
      <div className="table-wrap mon-legs">
        <table className="trades">
          <thead><tr><th>нога</th><th>вход</th><th>объём</th><th>стоп (запас)</th><th>тейки</th><th>ликвид.</th><th>результат</th><th>метрики на входе</th></tr></thead>
          <tbody>{(pos.legs ?? []).map((l, i) => <Leg key={i} l={l} side={pos.side} />)}</tbody>
        </table>
      </div>
      {e.settings && <div className="muted small">План: SL {e.settings.sl_pct}% · TP {e.settings.tps.map((t) => `${t.pct}%×${t.share}`).join(' / ')}{e.settings.be_after_tp1 ? ' · БУ после TP1' : ''} · плечо {e.settings.leverage}×{e.settings.pyramid_leverage ? ` (доборы ${e.settings.pyramid_leverage}×)` : ''} · {e.settings.trade_pct}% депозита {fmtUsd(e.settings.deposit)}</div>}
    </>
  );
}

function HistoryTab({ e }: { e: MonitorEntry }) {
  const [page, setPage] = useState(0);
  const h = e.history ?? [];
  if (!h.length) return <p className="note">Закрытых сделок пока нет.</p>;
  const pages = Math.ceil(h.length / PAGE);
  return (
    <div data-testid="mon-history">
      <div className="table-wrap mon-log">
        <table className="trades">
          <thead><tr><th>#</th><th>сторона</th><th>вход (UTC)</th><th>цена</th><th>выход</th><th>цена</th><th>причина</th><th>тейки</th><th>держал</th><th>PnL</th><th>PnL $</th></tr></thead>
          <tbody>
            {h.slice(page * PAGE, (page + 1) * PAGE).map((t) => (
              <tr key={t.n} title={t.fills.map((f) => `${f.label} ${Math.round(f.share * 100)}% @ ${fmtPrice(f.price)}`).join(' → ')}>
                <td>{t.n}</td><td className={t.side === 'long' ? 'pos' : 'neg'}>{t.side === 'long' ? 'лонг' : 'шорт'}{t.leg > 1 ? ` +${t.leg - 1}` : ''}</td>
                <td>{fmtTs(t.entry_ts)}</td><td>{fmtPrice(t.entry)}</td><td>{fmtTs(t.exit_ts)}</td><td>{fmtPrice(t.exit)}</td>
                <td><span className={`tag tag-${t.reason}`}>{t.reason}</span></td><td>{t.tp_hits || '–'}</td><td>{held(t.hold_h)}</td>
                <td className={signClass(t.pnl_pct)}>{fmtPct(t.pnl_pct, 2)}</td><td className={signClass(t.pnl_usd)}>{fmtUsd(t.pnl_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mon-pager">
        <button type="button" className="ghost" disabled={page === 0} onClick={() => setPage(page - 1)}>←</button>
        <span className="muted small">{page + 1} / {pages} · последние {h.length} закрытых</span>
        <button type="button" className="ghost" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>→</button>
      </div>
    </div>
  );
}

function StatsTab({ e }: { e: MonitorEntry }) {
  const t = e.stats;
  if (!t || !t.trades) return <p className="note">Сделок по этому конфигу в истории пока нет.</p>;
  const n = (k: string) => (t[k] ?? 0) as number;
  return (
    <div className="cards mon-cards" data-testid="mon-stats">
      <StatCard label="Итого" value={fmtUsd(n('total_usd'))} cls={signClass(n('total_usd'))} sub={t.return_pct != null ? `${fmtPct(t.return_pct as number, 1)} от депозита` : undefined} />
      <StatCard label="Сделок" value={String(n('trades'))} sub={`TP ${n('tp')} · БУ ${n('be')} · SL ${n('sl')} · разворот ${n('rev')}${n('liq') ? ` · ликвид. ${n('liq')}` : ''}${n('wknd') ? ` · пятница ${n('wknd')}` : ''}`} />
      <StatCard label="Winrate" value={`${n('winrate').toFixed(1)}%`} />
      {!!t.funding_usd && <StatCard label="Фандинг" value={fmtUsd(-(t.funding_usd as number))} />}
      <StatCard label="Profit factor" value={fmtPf((t.profit_factor as number | null) ?? null)} />
      <StatCard label="Коэф. Шарпа" value={t.sharpe == null ? '–' : (t.sharpe as number).toFixed(2)} cls={t.sharpe == null ? '' : signClass(t.sharpe as number)} />
      <StatCard label="Макс. просадка" value={fmtUsd(-n('max_dd_usd'))} cls="neg" sub={t.max_dd_pct != null ? `${(t.max_dd_pct as number).toFixed(1)}% по закрытым` : undefined} />
      {t.max_dd_real_usd != null && <StatCard label="Реальная просадка" value={fmtUsd(-n('max_dd_real_usd'))} cls="neg" sub={`${n('max_dd_real_pct').toFixed(1)}% с открытыми позициями`} />}
      <StatCard label="Серия убытков" value={String(n('max_loss_streak'))} sub="макс. подряд" />
      <StatCard label="В позиции" value={`${n('exposure_pct').toFixed(0)}%`} sub="доля времени" />
    </div>
  );
}

function ParamsTab({ e }: { e: MonitorEntry }) {
  const p = e.params as Record<string, unknown> | undefined;
  if (!p) return null;
  const on = (k: string) => (p[k] ? 'да' : 'нет');
  const rows: [string, string][] = [
    ['Источник · актив · ТФ графика', `${p.source} · ${p.symbol} · ${e.tf ?? 'auto'}`], ['Сторона', String(p.side)], ['Метрика', p.only ? String(p.only) : 'Local + Scalp'],
    ['Пороги лонга', `DL ≥ ${p.local_min} · DS ≥ ${p.scalp_min}${p.global_min != null ? ` · DG ≥ ${p.global_min}` : ''}`], ['Пороги шорта', `DL ≤ ${p.short_local_max} · DS ≤ ${p.short_scalp_max}${p.short_global_max != null ? ` · DG ≤ ${p.short_global_max}` : ''}`],
    ['Подтверждение', `${p.confirm} отчётов подряд`], ['Остывание', p.cool_drop != null ? `откат ${p.cool_drop} (${p.cool_metric})` : p.cool_declines != null ? `${p.cool_declines} отчётов подряд (${p.cool_metric})` : 'выключено'],
    ['Ликвидации (фильтр)', p.liq_hours != null ? `окно ${p.liq_hours} ч, ≤ ${p.liq_frac} пика, перцентиль ${p.liq_pct}` : 'выключено'], ['Тренд', p.trend_enabled ? `EMA ${(p.trend_emas as number[]).join('/')} на ${p.trend_tf}` : 'выключен'],
    ['Выход', `SL ${p.sl_pct}% · TP ${(p.tps as { pct: number; share: number }[]).map((t) => `${t.pct}%×${t.share}`).join(' / ')}${p.be_after_tp1 ? ' · БУ после TP1' : ''}`],
    ['Вход', p.entry_limit_pct ? `${p.entry_limit_dir === 'breakout' ? 'по ходу цены' : 'лимиткой на откат'} ${p.entry_limit_pct}% · ордер живёт ${p.entry_limit_ttl_h ?? 4} ч` : 'по рынку'], ['Фандинг', p.funding_apr ? `${p.funding_apr}% годовых · период ${p.funding_period_h ?? 8} ч${p.funding_mode === 'cost' ? ' · платят обе стороны' : ''}` : 'не учитывается'], ['Размер', `депозит ${fmtUsd(p.deposit as number)} · ${p.trade_pct}% · плечо ${p.leverage}× · ${p.compound ? 'сложный %' : 'фикс. сумма'}`],
    ['Пирамидинг', p.pyramid_enabled ? `до ${p.pyramid_max} × ${p.pyramid_pct}%${p.pyramid_avg ? ' с усреднением' : ''}${p.pyramid_leverage ? `, плечо ${p.pyramid_leverage}×` : ''}${p.pyramid_move_pct ? `, движение ≥ ${p.pyramid_move_pct}%` : ''}` : 'выключен'],
    ['Только будни', on('weekdays_only')], ['Моделировать ликвидацию', on('liquidation_sim')], ['Не ставить SL, пока метрики сильны', on('sl_hold_strong')],
    ['Пауза после стопа / между сигналами', `${p.cooldown_h} ч / ${p.signal_gap_h} ч`], ['Топ-коины', on('top_enabled')], ['Создан', e.created ? fmtTs(e.created * 1000) : '–'], ['Уведомлений', String(e.alerts ?? 0)],
  ];
  return <div className="mon-params" data-testid="mon-params">{rows.map(([k, v]) => <Row key={k} label={k}>{v}</Row>)}</div>;
}

/** Подробности стратегии под её строкой: вкладки позиция / история / статистика / параметры / журнал и действия управления. */
function Details({ id, onChanged, exchange }: { id: number; onChanged: () => void; exchange: MonitorResponse['exchange'] }) {
  const [e, setE] = useState<MonitorEntry | null>(null);
  const [tab, setTab] = useState<Tab>('pos');
  const [err, setErr] = useState<string | null>(null);
  const alive = useRef(true);
  const load = useCallback(async () => {
    try {
      const d = await getMonitorDetail(id);
      if (alive.current) {
        setE(d);
        setErr(null);
      }
    } catch (x) {
      if (alive.current) setErr(x instanceof Error ? x.message : String(x));
    }
  }, [id]);
  useEffect(() => {
    alive.current = true;
    void load();
    const t = setInterval(() => void load(), 30000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [load]);

  const act = async (fn: () => Promise<unknown>, gone = false) => {
    try {
      await fn();
      if (!gone) await load();                                  // после удаления конфига подробности перечитывать нельзя (404): строка исчезнет после обновления списка
      onChanged();
    } catch (x) {
      setErr(x instanceof Error ? x.message : String(x));
    }
  };
  if (err && !e) return <div className="bad-text small">{err}</div>;
  if (!e) return <div className="muted small">Загрузка подробностей…</div>;
  return (
    <div className="mon-details" data-testid="mon-details">
      <div className="mon-actions">
        <button type="button" className="ghost" onClick={() => { const n = window.prompt('Новое имя конфига', e.title ?? ''); if (n && n.trim()) void act(() => renameConfig(id, n.trim())); }}>Переименовать</button>
        <button type="button" className="ghost" onClick={() => void exportBotConfig(id).then((c) => downloadText(`maverick-config_${id}.json`, JSON.stringify(c, null, 2), 'application/json')).catch((x) => setErr(String(x)))}>⬇ Скачать конфиг</button>
        <button type="button" className="ghost bad-text" onClick={() => { if (window.confirm(`Удалить конфиг «${e.title}»? Бот перестанет по нему считать и присылать сигналы. Позиция, уже открытая на бирже, останется со своими стопом и тейками, но бот её больше не ведёт (безубыток, закрытие по развороту, уведомления) — закройте её сами, если нужно. Стоящий лимитный ордер на вход будет снят.`)) void act(() => deleteConfig(id), true); }}>Удалить</button>
        {exchange.real === false && <span className="muted small">автомод — симуляция</span>}
        {err && <span className="bad-text small"> {err}</span>}
      </div>
      <div className="seg-mini mon-tabs" role="tablist">
        {TABS.map(([k, name]) => <button key={k} type="button" role="tab" aria-selected={tab === k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{name}</button>)}
      </div>
      {e.error && <div className="bad-text small">Не удалось посчитать: {e.error}</div>}
      {tab === 'pos' && <PositionTab e={e} />}
      {tab === 'hist' && <HistoryTab e={e} />}
      {tab === 'stats' && <StatsTab e={e} />}
      {tab === 'params' && <ParamsTab e={e} />}
      {tab === 'log' && <Journal log={e.log ?? []} />}
    </div>
  );
}

function statusOf(e: MonitorEntry): { cls: string; text: string } {
  if (e.error) return { cls: 'bad', text: 'ошибка расчёта' };
  if (e.paused) return { cls: 'paused', text: 'на паузе' };
  if (e.position) return { cls: 'open', text: e.position.side === 'long' ? 'в лонге' : 'в шорте' };
  if (e.ready_now && e.ready_now.length) return { cls: 'ready', text: 'ждёт подтверждения' };
  return { cls: 'idle', text: 'позиции нет' };
}

/** Строка стратегии в списке: статус, позиция, автомод, пауза и «Подробнее». */
function SizingEditor({ e, exchange, onSaved, onError }: { e: MonitorEntry; exchange: MonitorResponse['exchange']; onSaved: () => void; onError: (m: string) => void }) {
  const st = e.settings;
  const [pct, setPct] = useState('');
  const [lev, setLev] = useState('');
  const [pp, setPp] = useState('');
  const [pl, setPl] = useState('');
  useEffect(() => {
    if (!st) return;
    setPct(String(st.trade_pct));
    setLev(String(st.leverage));
    setPp(st.pyramid_pct != null ? String(st.pyramid_pct) : '');
    setPl(st.pyramid_leverage != null ? String(st.pyramid_leverage) : '');
  }, [st?.trade_pct, st?.leverage, st?.pyramid_pct, st?.pyramid_leverage]);                       // eslint-disable-line react-hooks/exhaustive-deps
  if (!st || e.config_id == null) return null;
  const n = (v: string) => Number(v.replace(',', '.'));
  const patch: Parameters<typeof setSizing>[1] = {};
  if (pct.trim() !== '' && n(pct) !== st.trade_pct) patch.trade_pct = n(pct);
  if (lev.trim() !== '' && n(lev) !== st.leverage) patch.leverage = n(lev);
  if (st.pyramid_enabled) {
    if (pp.trim() !== '' && n(pp) !== (st.pyramid_pct ?? NaN)) patch.pyramid_pct = n(pp);
    if (pl.trim() !== '' && n(pl) !== (st.pyramid_leverage ?? NaN)) patch.pyramid_leverage = n(pl);
  }
  const dirty = Object.keys(patch).length > 0;
  const save = async () => {
    const real = exchange.real && e.auto?.open;
    if (real && !window.confirm(`Новые входы по автомоду пойдут с размером ${pct}% депозита × ${lev}× на ${exchange.name}. Позиции, уже открытые на бирже, не меняются. Продолжить?`)) return;
    try {
      await setSizing(e.config_id!, patch);
      onSaved();
    } catch (x) {
      onError(x instanceof Error ? x.message : String(x));
    }
  };
  const volume = n(pct) * n(lev);
  return (
    <div className="mon-sizing" data-testid="mon-sizing">
      <span className="muted small">Размер входа<Help text={HELP.sizing} />:</span>
      <input className="mon-num" inputMode="decimal" value={pct} onChange={(ev) => setPct(ev.target.value)} onKeyDown={(ev) => { if (ev.key === 'Enter' && dirty) void save(); }} aria-label="Доля депозита на вход, %" data-testid="mon-pct" /> <span className="small">% депозита ×</span>
      <input className="mon-num" inputMode="decimal" value={lev} onChange={(ev) => setLev(ev.target.value)} onKeyDown={(ev) => { if (ev.key === 'Enter' && dirty) void save(); }} aria-label="Плечо" data-testid="mon-lev" /> <span className="small">плечо</span>
      {Number.isFinite(volume) && volume > 0 && <span className="muted small">= объём {volume.toFixed(volume < 10 ? 1 : 0)}% депозита</span>}
      {st.pyramid_enabled && (
        <>
          <span className="muted small">· добор</span>
          <input className="mon-num" inputMode="decimal" value={pp} onChange={(ev) => setPp(ev.target.value)} aria-label="Доля депозита на добор, %" /> <span className="small">% ×</span>
          <input className="mon-num" inputMode="decimal" value={pl} placeholder={String(st.leverage)} onChange={(ev) => setPl(ev.target.value)} aria-label="Плечо добора" />
        </>
      )}
      <button type="button" className="ghost" disabled={!dirty} onClick={() => void save()} data-testid="mon-sizing-save">Применить</button>
    </div>
  );
}

const EX_NAMES: ExchangeName[] = ['hyperliquid', 'bybit'];
const EX_LABEL: Record<ExchangeName, string> = { hyperliquid: 'Hyperliquid', bybit: 'Bybit' };
const OVER_NUM = ['trade_pct', 'leverage', 'pyramid_pct', 'pyramid_leverage', 'entry_limit_pct', 'entry_limit_ttl_h'] as const;

/** Свои настройки конфига для одной биржи (поверх параметров конфига): доля и плечо входа / добора, вход лимитным ордером. Пусто — как в конфиге (значение конфига показано серым). */
function ExchangeOverEditor({ e, name, onSaved, onError }: { e: MonitorEntry; name: ExchangeName; onSaved: () => void; onError: (m: string) => void }) {
  const st = e.settings;
  const over: ExchangeOverrides = e.ex?.[name]?.over ?? {};
  const [f, setF] = useState<Record<string, string>>({});
  const key = JSON.stringify(over);
  useEffect(() => {
    const o = JSON.parse(key) as ExchangeOverrides;
    setF({ ...Object.fromEntries(OVER_NUM.map((k) => [k, o[k] != null ? String(o[k]) : ''])), entry_limit_dir: o.entry_limit_dir ?? '' });
  }, [key]);
  if (!st || e.config_id == null) return null;
  const n = (v: string) => Number(v.replace(',', '.'));
  const patch: { [K in keyof ExchangeOverrides]?: ExchangeOverrides[K] | null } = {};
  for (const k of OVER_NUM) {
    const t = (f[k] ?? '').trim();
    if (t === '' && over[k] != null) patch[k] = null;
    else if (t !== '' && n(t) !== over[k]) patch[k] = n(t);
  }
  const dir = f.entry_limit_dir ?? '';
  if (dir === '' && over.entry_limit_dir != null) patch.entry_limit_dir = null;
  else if (dir !== '' && dir !== over.entry_limit_dir) patch.entry_limit_dir = dir as 'pullback' | 'breakout';
  const dirty = Object.keys(patch).length > 0;
  const own = Object.keys(over).length > 0;
  const call = async (p: typeof patch) => {
    try {
      await setExchangeOver(e.config_id!, name, p);
      onSaved();
    } catch (x) {
      onError(x instanceof Error ? x.message : String(x));
    }
  };
  const num = (k: typeof OVER_NUM[number], ph: number | null | undefined, label: string, w?: string) => (
    <input className="mon-num" inputMode="decimal" value={f[k] ?? ''} placeholder={ph != null ? String(ph) : ''} onChange={(ev) => setF({ ...f, [k]: ev.target.value })}
      onKeyDown={(ev) => { if (ev.key === 'Enter' && dirty) void call(patch); }} aria-label={`${EX_LABEL[name]}: ${label}`} data-testid={`mon-ex-${name}-${w ?? k}`} />
  );
  const limitCfg = st.entry_limit_pct ?? 0;
  return (
    <div className="mon-sizing" data-testid={`mon-ex-${name}`}>
      <b className="small">{EX_LABEL[name]}</b><Help text={HELP.ex_over} />
      {num('trade_pct', st.trade_pct, 'доля депозита, %', 'pct')} <span className="small">% ×</span> {num('leverage', st.leverage, 'плечо', 'lev')} <span className="small">плечо</span>
      {st.pyramid_enabled && (
        <>
          <span className="muted small">· добор</span> {num('pyramid_pct', st.pyramid_pct, 'доля добора, %', 'ppct')} <span className="small">% ×</span> {num('pyramid_leverage', st.pyramid_leverage ?? st.leverage, 'плечо добора', 'plev')}
        </>
      )}
      <span className="muted small">· лимитный вход</span><Help text={HELP.ex_over_limit} /> {num('entry_limit_pct', limitCfg, 'вход лимиткой, % (0 — по рынку)', 'limit')} <span className="small">%</span>
      <select value={dir} onChange={(ev) => setF({ ...f, entry_limit_dir: ev.target.value })} aria-label={`${EX_LABEL[name]}: направление лимитного входа`} data-testid={`mon-ex-${name}-dir`}>
        <option value="">как в конфиге ({(st.entry_limit_dir ?? 'pullback') === 'breakout' ? 'по ходу' : 'на откат'})</option><option value="pullback">на откат</option><option value="breakout">по ходу</option>
      </select>
      <span className="small">живёт</span> {num('entry_limit_ttl_h', st.entry_limit_ttl_h ?? 4, 'жизнь ордера, часов', 'ttl')} <span className="small">ч</span>
      <button type="button" className="ghost" disabled={!dirty} onClick={() => void call(patch)} data-testid={`mon-ex-${name}-save`}>Применить</button>
      <button type="button" className="ghost" disabled={!own} onClick={() => void call(Object.fromEntries(Object.keys(over).map((k) => [k, null])))} data-testid={`mon-ex-${name}-reset`}>как в конфиге</button>
    </div>
  );
}

function CatchupEditor({ e, onSaved, onError }: { e: MonitorEntry; onSaved: () => void; onError: (m: string) => void }) {
  const cu = e.catchup;
  const [age, setAge] = useState('');
  const [mv, setMv] = useState('');
  useEffect(() => {
    if (!cu) return;
    setAge(String(cu.age_min));
    setMv(String(cu.move_pct));
  }, [cu?.age_min, cu?.move_pct]);                                  // eslint-disable-line react-hooks/exhaustive-deps
  if (!cu || e.config_id == null) return null;
  const n = (v: string) => Number(v.replace(',', '.'));
  const dirty = n(age) !== cu.age_min || n(mv) !== cu.move_pct;
  const save = async (patch: { on?: boolean; age_min?: number; move_pct?: number }) => {
    try {
      await setCatchup(e.config_id!, patch);
      onSaved();
    } catch (x) {
      onError(x instanceof Error ? x.message : String(x));
    }
  };
  return (
    <div className="mon-sizing" data-testid="mon-catchup">
      <label title={HELP.catchup}><input type="checkbox" checked={cu.on} disabled={e.paused} onChange={(ev) => void save({ on: ev.target.checked, age_min: n(age), move_pct: n(mv) })} data-testid="mon-catchup-on" /> догнать свежий сигнал<Help text={HELP.catchup} /></label>
      <span className="muted small">не старше</span>
      <input className="mon-num" inputMode="decimal" value={age} onChange={(ev) => setAge(ev.target.value)} aria-label="Не старше, минут" data-testid="mon-catchup-age" /> <span className="small">мин, цена не ушла в прибыль больше</span>
      <input className="mon-num" inputMode="decimal" value={mv} onChange={(ev) => setMv(ev.target.value)} aria-label="Допустимое движение цены в прибыль, %" data-testid="mon-catchup-move" /> <span className="small">%</span>
      {dirty && <button type="button" className="ghost" onClick={() => void save({ age_min: n(age), move_pct: n(mv) })} data-testid="mon-catchup-save">Применить</button>}
      {cu.armed && <span className="muted small">проверю сигнал при ближайшей проверке…</span>}
    </div>
  );
}

function Row2({ e, open, exchange, exchanges, account, accounts, onToggleOpen, onAuto, onPause, onSaved, onError }: {
  e: MonitorEntry; open: boolean; exchange: MonitorResponse['exchange']; exchanges?: MonitorResponse['exchanges']; account?: MonitorResponse['account']; accounts?: MonitorResponse['accounts'];
  onToggleOpen: () => void; onAuto: (patch: { open?: boolean; manage?: boolean; base?: 'config' | 'account' }, exchange: ExchangeName) => void; onPause: () => void; onSaved: () => void; onError: (m: string) => void;
}) {
  const st = statusOf(e);
  const pos = e.position;
  const stale = e.candle_ts ? (Date.now() - e.candle_ts) / 60000 : null;
  const dep = e.settings?.deposit;
  const infoOf = (n: ExchangeName): ExchangeInfo | undefined => (n === 'hyperliquid' ? exchanges?.hyperliquid ?? exchange : exchanges?.[n]);
  const accOf = (n: ExchangeName) => (n === 'hyperliquid' ? accounts?.hyperliquid ?? account : accounts?.[n]);
  const cfgOf = (n: ExchangeName) => e.ex?.[n] ?? (n === 'hyperliquid' ? { open: e.auto?.open ?? false, manage: e.auto?.manage ?? false, base: e.auto?.base ?? 'config', over: {} } : { open: false, manage: false, base: 'config' as const, over: {} });
  // Hyperliquid показывается всегда, остальные биржи — если счёт подключён или у конфига для неё что-то включено / настроено
  const shown = EX_NAMES.filter((n) => n === 'hyperliquid' || infoOf(n)?.real || cfgOf(n).open || cfgOf(n).manage || Object.keys(cfgOf(n).over).length > 0);
  const multi = shown.length > 1;
  const pend = { ...(e.pending_entries ?? {}), ...(e.pending_entries?.hyperliquid || !e.pending_entry ? {} : { hyperliquid: e.pending_entry }) };
  return (
    <div className="mon-row" data-status={st.cls}>
      <span className={`mon-dot ${st.cls}`} title={st.text} />
      <div className="mon-name"><b>{e.title ?? e.name}</b><span className="muted small"> #{e.config_id} · {e.symbol?.split('/')[0] ?? '–'}{e.source ? ` · ${e.source}` : ''}</span></div>
      <div className="mon-state">
        <span className={st.cls === 'open' ? (pos?.side === 'long' ? 'pos' : 'neg') : 'muted'}>{st.text}</span>
        {pos && <> · <b className={signClass(pos.pnl_usd)}>{fmtUsd(pos.pnl_usd)}</b> <span className="muted small">{pos.pnl_pct_deposit != null ? fmtPct(pos.pnl_pct_deposit, 2) : ''} · {held(pos.hold_h)}{pos.legs_n && pos.legs_n > 1 ? ` · ног ${pos.legs_n}` : ''}</span></>}
        {pos?.danger && <span className="bad-text small" title="цена за оценочной ценой ликвидации"> ⚠ ликвидация</span>}
        {EX_NAMES.map((n) => { const pe = pend[n]; return pe ? <span key={n} className="warn-text small" data-testid="mon-pending" title={pe.order_id ? `лимитный ордер ${pe.order_id} на бирже` : 'бот следит за ценой и войдёт по рынку при касании уровня'}> · ⏳ {multi ? `${EX_LABEL[n]}: ` : ''}ждёт вход {pe.side === 'long' ? 'лонг' : 'шорт'} по {fmtPrice(pe.level)}</span> : null; })}
        {!pos && e.ready_now && e.ready_now[0] && <span className="muted small"> · {e.ready_now[0].waiting.map((w) => ({ cool: 'откат', liq: 'ликвидации', trend: 'тренд' })[w]).join(' + ')}</span>}
        {e.baseline === false && !e.paused && <span className="muted small" title="Бот ещё не обработал этот конфиг (первый расчёт идёт или бот не запущен)"> · бот ещё не посчитал</span>}
        {stale != null && stale > 20 && <span className="warn-text small"> · свечи {ago(e.candle_ts)}</span>}
      </div>
      {shown.map((n) => {
        const c = cfgOf(n), info = infoOf(n), acc = accOf(n);
        const sfx = n === 'hyperliquid' ? '' : `-${n}`;
        const sizeWarn = !!(info?.real && acc?.equity && dep && dep > acc.equity * 1.5 && c.open && c.base === 'config');
        return (
          <div key={n} className="mon-auto-wrap">
            <span className={`mon-auto${n === 'hyperliquid' ? '' : ' mon-auto-extra'}`} data-testid={`mon-auto${sfx}`}>
              {multi && <b className="small" title={HELP.auto_exchange}>{EX_LABEL[n]}<Help text={HELP.auto_exchange} /></b>}
              <label title={HELP.auto_open}><input type="checkbox" checked={c.open} disabled={e.paused} onChange={(ev) => onAuto({ open: ev.target.checked }, n)} /> открывать<Help text={HELP.auto_open} /></label>
              <label title={HELP.auto_manage}><input type="checkbox" checked={c.manage} disabled={e.paused} onChange={(ev) => onAuto({ manage: ev.target.checked }, n)} /> управлять<Help text={HELP.auto_manage} /></label>
              <label className="mon-base" title={HELP.auto_base}>размер от <select value={c.base} disabled={e.paused} onChange={(ev) => onAuto({ base: ev.target.value as 'config' | 'account' }, n)} aria-label={`База размера входа${multi ? `, ${EX_LABEL[n]}` : ''}`} data-testid={`mon-base${sfx}`}>
                <option value="config">депозита конфига</option><option value="account">баланса счёта</option>
              </select><Help text={HELP.auto_base} /></label>
              {(c.open || c.manage) && <span className={info?.real ? 'neg small' : 'muted small'}>{info?.real ? '● биржа' : '● симуляция'}</span>}
            </span>
            {sizeWarn && <div className="warn-text small mon-sizewarn" data-testid={`mon-sizewarn${sfx}`}>⚠️ {multi ? `${EX_LABEL[n]}: д` : 'Д'}епозит конфига {fmtUsd(dep ?? 0)} намного больше капитала на бирже {fmtUsd(acc!.equity ?? 0)}: объём входа считается от {fmtUsd(dep ?? 0)} и, скорее всего, не пройдёт. Выберите «размер от баланса счёта».</div>}
          </div>
        );
      })}
      <SizingEditor e={e} exchange={exchange} onSaved={onSaved} onError={onError} />
      <details className="mon-exover" data-testid="mon-exover" open={shown.some((n) => Object.keys(cfgOf(n).over).length > 0)}>
        <summary className="small">Свои настройки по биржам (размер и вход лимиткой){multi || Object.keys(cfgOf('hyperliquid').over).length ? '' : ' — по умолчанию как в конфиге'}<Help text={HELP.ex_over} /></summary>
        {shown.map((n) => <ExchangeOverEditor key={n} e={e} name={n} onSaved={onSaved} onError={onError} />)}
      </details>
      <CatchupEditor e={e} onSaved={onSaved} onError={onError} />
      <span className="mon-btns">
        <button type="button" className="ghost" onClick={onPause} data-testid="mon-pause">{e.paused ? '▶ Возобновить' : '⏸ Пауза'}</button>
        <button type="button" className="ghost" onClick={onToggleOpen} aria-expanded={open} data-testid="mon-more">{open ? 'Свернуть ▴' : 'Подробнее ▾'}</button>
      </span>
    </div>
  );
}

function Journal({ log }: { log: AutoLogItem[] }) {
  if (!log.length) return <p className="note">Журнал действий пока пуст.</p>;
  return (
    <div className="table-wrap mon-log" data-testid="mon-log">
      <table className="trades">
        <thead><tr><th>время</th><th>действие</th><th>актив</th><th>статус</th><th>детали</th></tr></thead>
        <tbody>
          {log.map((x) => (
            <tr key={x.id}>
              <td>{fmtTs(x.ts * 1000)}</td><td>{KIND_NAMES[x.kind] ?? x.kind}</td><td>{x.symbol?.split('/')[0] ?? ''} {x.side === 'long' ? 'лонг' : x.side === 'short' ? 'шорт' : ''}</td>
              <td className={x.status === 'error' ? 'neg' : x.status === 'dry_run' ? 'muted' : ''}>{STATUS_NAMES[x.status] ?? x.status}</td>
              <td className="muted small">{x.details.error ? String(x.details.error) : x.details.note ? String(x.details.note) : x.details.margin != null && x.kind === 'test_position' ? `маржа ${x.details.margin} $ · объём ${fmtUsd(Number(x.details.notional ?? 0))}` : x.details.notional != null ? `объём ${fmtUsd(Number(x.details.notional))}` : x.details.auto ? `открывать: ${(x.details.auto as { open: boolean }).open ? 'да' : 'нет'}, управлять: ${(x.details.auto as { manage: boolean }).manage ? 'да' : 'нет'}` : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const exchangeLabel = (x: ExchangeInfo) => {
  if (x.name !== 'hyperliquid' && x.name !== 'bybit') return x.name;
  const net = x.net === 'demo' ? 'демо-торговля' : x.testnet ? 'тестовая сеть' : 'БОЕВАЯ сеть';
  return `${x.name === 'bybit' ? 'Bybit' : 'Hyperliquid'} (${net})`;
};

/** Что именно сейчас происходит с ордерами: симуляция, тестовая сеть или боевая (по каждой подключённой бирже). */
function ExchangeBanner({ info, all }: { info?: ExchangeInfo; all?: MonitorResponse['exchanges'] }) {
  const reals = (all ? EX_NAMES.map((n) => all[n]).filter((x): x is ExchangeInfo => !!x?.real) : info?.real ? [info] : []);
  const live = reals.some((x) => !x.testnet);
  return (
    <div className={`banner ${live ? 'error' : 'warn'}`} data-testid="mon-banner">
      <span>
        {reals.length > 0 ? (
          <>Торговля: <b>{reals.map(exchangeLabel).join(' · ')}</b> — автомод {live ? <b>отправляет реальные ордера на настоящие деньги</b> : 'отправляет ордера в тестовую сеть'}. </>
        ) : (
          <>Биржа для торговли не подключена — автомод работает как <b>симуляция</b> (действия пишутся в журнал, ордера не отправляются; чтобы торговать, подключите счёт в Telegram-боте: /hl — Hyperliquid, /bybit — Bybit). </>
        )}
        Позиции в списке ниже — расчёт стратегий по свечам и отчётам Binance, а не позиции на бирже{reals.length > 0 ? ': реальные позиции бирж — в отдельных блоках' : ''}.
      </span>
    </div>
  );
}

/** Подключённый счёт на бирже: подключение, баланс, позиции, открытые ордера, последние исполнения (реальные данные биржи, не расчёт стратегий). */
function ExchangeBlock({ name = 'hyperliquid', state, error, onDisconnect }: { name?: ExchangeName; state: ExchangeAccount | null; error: string | null; onDisconnect: () => void }) {
  const bybit = name === 'bybit';
  const tid = bybit ? 'mon-exchange-bybit' : 'mon-exchange';
  if (!state) return error ? <section className="mon-card"><div className="bad-text small">{EX_LABEL[name]}: {error}</div></section> : null;
  if (!state.connected) {
    return (
      <section className="mon-card" data-testid={tid}>
        <div className="mon-head"><b>{bybit ? 'Биржа: Bybit (USDT-фьючерсы)' : 'Биржа'}</b><Help text={bybit ? HELP.bybit_block : HELP.exchange_block} /></div>
        {bybit
          ? <div className="muted small">Счёт Bybit не подключён — автомод на Bybit работает как симуляция. Подключить: в Telegram-боте команда <b>/bybit</b> (API-ключ и секрет присылаются боту, который проверит права ключа — торговля контрактами да, вывод средств нет, — сохранит секрет зашифрованным и удалит ваши сообщения). Только USDT-фьючерсы. Работает только после привязки Telegram к этому аккаунту.</div>
          : <div className="muted small">Счёт Hyperliquid не подключён — автомод работает как симуляция. Подключить: в Telegram-боте команда <b>/hl</b> (адрес кошелька и ключ API-кошелька присылаются боту, который проверит их, сохранит зашифрованными и удалит ваши сообщения). Работает только после привязки Telegram к этому аккаунту.</div>}
      </section>
    );
  }
  const net = state.exchange.net === 'demo' ? 'демо-торговля' : state.exchange.testnet ? 'тестовая сеть' : 'БОЕВАЯ сеть';
  const acc = state.account;
  return (
    <section className="mon-card" data-testid={tid}>
      <div className="mon-head">
        <b>Биржа: {exchangeLabel(state.exchange)}</b><Help text={bybit ? HELP.bybit_block : HELP.exchange_block} />
        <span className="muted small"> · {bybit ? `API-ключ ${state.wallet}` : `кошелёк ${state.wallet} · API-кошелёк ${state.agent ?? '–'}`} · {net} · проверен {state.verified ? fmtTs(state.verified * 1000) : '–'}</span>
        <span className="mon-spacer" />
        <button type="button" className="ghost bad-text" onClick={onDisconnect} data-testid={bybit ? 'exchange-disconnect-bybit' : 'exchange-disconnect'}>Отключить счёт</button>
      </div>
      {state.error && <div className="bad-text small">Биржа: {state.error}</div>}
      {!state.error && (
        <>
          <div className="mon-grid" data-testid={bybit ? 'exchange-account-bybit' : 'exchange-account'}>
            <Row label="Капитал">{acc.equity != null ? fmtUsd(Number(acc.equity), 2) : '–'}</Row>
            <Row label="Свободно">{acc.free != null ? fmtUsd(Number(acc.free), 2) : '–'}</Row>
            <Row label="В марже">{acc.used != null ? fmtUsd(Number(acc.used), 2) : '–'}</Row>
            <Row label="Позиций / ордеров">{state.positions.length} / {state.orders.length}</Row>
          </div>
          {state.positions.length === 0 ? <div className="muted small">Открытых позиций на бирже нет.</div> : (
            <div className="table-wrap mon-legs">
              <table className="trades">
                <thead><tr><th>контракт</th><th>сторона</th><th>размер</th><th>вход</th><th>цена</th><th>ликвид.</th><th>плечо</th><th>маржа</th><th>PnL</th></tr></thead>
                <tbody>{state.positions.map((p, i) => (
                  <tr key={i}><td>{p.symbol}</td><td className={p.side === 'long' ? 'pos' : 'neg'}>{p.side === 'long' ? 'лонг' : 'шорт'}</td><td>{p.size}</td>
                    <td>{p.entry != null ? fmtPrice(p.entry) : '–'}</td><td>{p.mark != null ? fmtPrice(p.mark) : '–'}</td><td>{p.liq != null ? fmtPrice(p.liq) : '–'}</td>
                    <td>{p.leverage ?? '–'}×</td><td>{p.margin != null ? fmtUsd(p.margin, 2) : '–'}</td><td className={signClass(p.pnl_usd ?? 0)}>{p.pnl_usd != null ? fmtUsd(p.pnl_usd, 2) : '–'}</td></tr>
                ))}</tbody>
              </table>
            </div>
          )}
          {state.orders.length > 0 && (
            <details open><summary>Открытые ордера ({state.orders.length})</summary>
              <div className="table-wrap mon-legs"><table className="trades">
                <thead><tr><th>контракт</th><th>сторона</th><th>тип</th><th>размер</th><th>цена</th><th>триггер</th><th>reduce-only</th></tr></thead>
                <tbody>{state.orders.map((o, i) => <tr key={i}><td>{o.symbol}</td><td>{o.side}</td><td>{o.type}</td><td>{o.amount ?? '–'}</td><td>{o.price != null ? fmtPrice(o.price) : '–'}</td><td>{o.trigger != null ? fmtPrice(o.trigger) : '–'}</td><td>{o.reduce_only ? 'да' : 'нет'}</td></tr>)}</tbody>
              </table></div>
            </details>
          )}
          {state.fills.length > 0 && (
            <details><summary>Последние исполнения ({state.fills.length})</summary>
              <div className="table-wrap mon-legs"><table className="trades">
                <thead><tr><th>время</th><th>контракт</th><th>сторона</th><th>размер</th><th>цена</th><th>комиссия</th><th>закрытый PnL</th></tr></thead>
                <tbody>{state.fills.map((f, i) => <tr key={i}><td>{f.ts ? fmtTs(f.ts) : '–'}</td><td>{f.symbol}</td><td>{f.side}</td><td>{f.amount ?? '–'}</td><td>{f.price != null ? fmtPrice(f.price) : '–'}</td><td>{f.fee ?? '–'}</td><td>{f.pnl ?? '–'}</td></tr>)}</tbody>
              </table></div>
            </details>
          )}
        </>
      )}
    </section>
  );
}

/** Страница мониторинга: ВСЕ стратегии пользователя (конфиги, отправленные боту), по клику «Подробнее» — позиция, история, статистика, параметры и журнал; управление (автомод, пауза, имя, удаление). От бэктеста не зависит. */
export default function MonitorPage() {
  const [data, setData] = useState<MonitorResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testSide, setTestSide] = useState<'long' | 'short'>('long');
  const [testEx, setTestEx] = useState<ExchangeName>('hyperliquid');
  const [openIds, setOpenIds] = useState<Set<number>>(new Set());
  const [ex, setEx] = useState<Record<ExchangeName, ExchangeAccount | null>>({ hyperliquid: null, bybit: null });
  const [exErr, setExErr] = useState<Record<ExchangeName, string | null>>({ hyperliquid: null, bybit: null });
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const r = await getMonitor();
      if (alive.current) {
        setData(r);
        setErr(null);
      }
      // подключённый счёт на бирже (реальные позиции, баланс, ордера, исполнения); сбой биржи не мешает остальной странице
      for (const n of EX_NAMES) {
        getExchangeAccount(n).then((x) => alive.current && (setEx((s) => ({ ...s, [n]: x })), setExErr((s) => ({ ...s, [n]: null })))).catch((e) => alive.current && setExErr((s) => ({ ...s, [n]: e instanceof Error ? e.message : String(e) })));
      }
    } catch (e) {
      if (alive.current) setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void load();
    const t = setInterval(() => void load(), 30000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [load]);

  const toggleOpen = (id: number) => setOpenIds((s) => {
    const n = new Set(s);
    if (n.has(id)) n.delete(id);
    else n.add(id);
    return n;
  });

  const infoOf = (n: ExchangeName): ExchangeInfo | undefined => data?.exchanges?.[n] ?? (n === 'hyperliquid' ? data?.exchange : undefined);
  const auto = async (id: number, patch: { open?: boolean; manage?: boolean; base?: 'config' | 'account' }, exchange: ExchangeName = 'hyperliquid') => {
    const turningOn = patch.open === true || patch.manage === true;
    const info = infoOf(exchange);
    if (turningOn && !window.confirm(info?.real
      ? `Включить автомод? Бот будет сам отправлять ордера на ${exchangeLabel(info)} по этому конфигу.`
      : `Включить автомод? Счёт ${EX_LABEL[exchange]} не подключён — действия будут только записываться в журнал (симуляция).`)) return;
    try {
      await setAuto(id, patch, exchange);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const pause = async (e: MonitorEntry) => {
    if (e.config_id == null) return;
    if (!e.paused && !window.confirm('Поставить конфиг на паузу? Бот перестанет по нему считать и присылать сигналы, автомод будет выключен.')) return;
    try {
      await pauseConfig(e.config_id, !e.paused);
      await load();
    } catch (x) {
      setErr(x instanceof Error ? x.message : String(x));
    }
  };

  const offAll = async () => {
    if (!window.confirm('Выключить автомод у всех ваших конфигов? Уже открытые позиции на бирже это не закрывает.')) return;
    const r = await autoOffAll().catch((e) => { setErr(String(e)); return null; });
    if (r) setMsg(`Автомод выключен у конфигов: ${r.disabled}`);
    await load();
  };

  const panic = async () => {
    const reals = EX_NAMES.map((n) => infoOf(n)).filter((x): x is ExchangeInfo => !!x?.real);
    if (!window.confirm(`🚨 СТОП ВСЁ\nАвтомод выключится у ВСЕХ конфигов на всех биржах, все позиции ${reals.length ? `на ${reals.map(exchangeLabel).join(' и ')}` : '(счёт не подключён — закрывать нечего)'} закроются по рынку, все ордера (стопы, тейки) снимутся. Продолжить?`)) return;
    try {
      const r = await panicStop();
      setMsg(`СТОП ВСЁ: автомод выключен у ${r.auto_off}, закрыто позиций ${r.closed.length}, снято ордеров ${r.cancelled}${r.errors.length ? `. ОШИБКИ: ${r.errors.join('; ')} — проверьте счёт на бирже вручную` : ''}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    await load();
  };

  const testPos = async () => {
    const info = infoOf(testEx);
    const mn = info?.min_notional ?? 0;
    const lev = mn ? Math.ceil(mn * 1.1) : 1;
    if (!window.confirm(`Открыть тестовую позицию по BTC (${testSide === 'long' ? 'лонг' : 'шорт'}): маржа 1 $ × ${lev}× = объём ${lev} $?${info?.real ? ` Это реальный ордер на ${exchangeLabel(info)}.` : ` Счёт ${EX_LABEL[testEx]} не подключён — будет симуляция.`}`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await openTestPosition(testSide, testEx);
      setMsg(r.status === 'dry_run' ? 'Симуляция: ордер не отправлялся (биржа не подключена). Запись добавлена в журнал.' : `Открыта: маржа ${r.margin} $ × ${r.leverage}× = объём ${r.notional} $. Закрыть — кнопкой «Закрыть тестовую».`);
    } catch (e) {
      setMsg(`Не удалось: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
      await load();
    }
  };

  const disconnect = async (n: ExchangeName) => {
    if (!window.confirm(`Отключить счёт ${EX_LABEL[n]}? Сохранённый ключ будет удалён, автомод на ${EX_LABEL[n]} выключится у всех конфигов. Открытые на бирже позиции это не закрывает. Подключить заново можно через бота: ${n === 'bybit' ? '/bybit' : '/hl'}.`)) return;
    try {
      await disconnectExchange(n);
      setMsg(`Счёт ${EX_LABEL[n]} отключён, автомод на нём выключен.`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
    await load();
  };

  const closeTest = async () => {
    if (!window.confirm('Закрыть позицию по BTC на бирже целиком (рыночным ордером) и снять висящие ордера?')) return;
    setBusy(true);
    try {
      const r = await closeTestPosition(testEx);
      setMsg(r.status === 'dry_run' ? 'Симуляция: закрывать нечего.' : r.status === 'skipped' ? 'На бирже нет позиции по BTC — закрывать нечего.' : 'Позиция по BTC закрыта.');
    } catch (e) {
      setMsg(`Не удалось: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
      await load();
    }
  };

  const entries = data?.entries ?? [];
  const anyAuto = entries.some((e) => (e.ex ? Object.values(e.ex).some((c) => c.open || c.manage) : e.auto && (e.auto.open || e.auto.manage)));
  const live = entries.filter((e) => e.position && !e.paused);                 // позиции конфигов на паузе в сводку не входят
  const open = live.length;
  const total = live.reduce((a, e) => a + (e.position?.pnl_usd ?? 0), 0);
  return (
    <div className="monitor" data-testid="monitor-page">
      <div className="mon-top">
        <h2>Стратегии и позиции <Help text={HELP.monitor_page} /></h2>
        <button type="button" className="ghost" onClick={() => void load()}>Обновить</button>
        {data && <span className="muted small" data-testid="mon-summary">Стратегий: {entries.length} · с позицией: {open}{open > 0 && <> · открытый результат <b className={signClass(total)}>{fmtUsd(total)}</b></>}</span>}
        <span className="mon-spacer" />
        <button type="button" className="ghost" disabled={!anyAuto} onClick={() => void offAll()}>⛔ Выключить автомод везде</button><Help text={HELP.auto_off_all} />
        <button type="button" className="danger" data-testid="panic" onClick={() => void panic()}>🚨 СТОП ВСЁ</button><Help text={HELP.panic} />
      </div>
      <BotStatus bot={data?.bot} anyAuto={anyAuto} />
      <ExchangeBanner info={data?.exchange} all={data?.exchanges} />
      {err && <div className="banner error">{err}</div>}
      <div className="mon-test">
        <b>Тестовая позиция $1 по BTC</b><Help text={HELP.test_position} />
        <select value={testEx} onChange={(e) => setTestEx(e.target.value as ExchangeName)} aria-label="Биржа тестовой позиции" data-testid="test-exchange">
          <option value="hyperliquid">Hyperliquid</option><option value="bybit">Bybit</option>
        </select>
        <select value={testSide} onChange={(e) => setTestSide(e.target.value as 'long' | 'short')} aria-label="Сторона тестовой позиции">
          <option value="long">лонг</option><option value="short">шорт</option>
        </select>
        <button type="button" className="ghost" disabled={busy} onClick={() => void testPos()}>{busy ? 'Открываю…' : 'Открыть'}</button>
        {infoOf(testEx)?.real && <button type="button" className="ghost" disabled={busy} onClick={() => void closeTest()}>Закрыть тестовую</button>}
        {msg && <span className="muted small" data-testid="mon-msg"> {msg}</span>}
      </div>
      <ExchangeBlock name="hyperliquid" state={ex.hyperliquid} error={exErr.hyperliquid} onDisconnect={() => void disconnect('hyperliquid')} />
      <ExchangeBlock name="bybit" state={ex.bybit} error={exErr.bybit} onDisconnect={() => void disconnect('bybit')} />
      {!data && !err && <div className="empty">Загрузка…</div>}
      {data && entries.length === 0 && <div className="empty">У вас пока нет конфигов бота. Отправьте конфиг боту в Telegram (файлом или текстом) — он появится здесь.</div>}
      <div className="mon-list">
        {entries.map((e) => (
          <section key={e.config_id ?? e.name} className="mon-card" data-testid="mon-card" data-name={e.title ?? e.name}>
            <Row2 e={e} open={e.config_id != null && openIds.has(e.config_id)} exchange={data!.exchange} exchanges={data!.exchanges} account={data!.account} accounts={data!.accounts}
              onToggleOpen={() => e.config_id != null && toggleOpen(e.config_id)} onAuto={(p, x) => e.config_id != null && void auto(e.config_id, p, x)} onPause={() => void pause(e)} onSaved={() => void load()} onError={(m) => setErr(m)} />
            {e.error && <div className="bad-text small">Не удалось посчитать: {e.error}</div>}
            {e.config_id != null && openIds.has(e.config_id) && <Details id={e.config_id} exchange={data!.exchange} onChanged={() => void load()} />}
          </section>
        ))}
      </div>
      {data && (
        <>
          <h3>Общий журнал действий</h3>
          <Journal log={data.log} />
        </>
      )}
    </div>
  );
}
