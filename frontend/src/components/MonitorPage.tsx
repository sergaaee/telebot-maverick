import { useCallback, useEffect, useRef, useState } from 'react';
import { autoOffAll, getMonitor, openTestPosition, setAuto } from '../api';
import { fmtPct, fmtPrice, fmtTs, fmtUsd, signClass } from '../format';
import { HELP } from '../helpTexts';
import type { AutoLogItem, MonitorEntry, MonitorLeg, MonitorResponse, Params } from '../types';
import Help from './Help';

const KIND_NAMES: Record<string, string> = {
  open: 'вход', add: 'добор', move_stop: 'стоп → БУ', close_weekend: 'закрытие к концу недели', close_reverse: 'закрытие по развороту',
  test_position: 'тестовая позиция', auto_toggle: 'переключение автомода', auto_off_all: 'автомод выключен везде',
};
const STATUS_NAMES: Record<string, string> = { dry_run: 'симуляция', sent: 'отправлено', error: 'ошибка', info: '' };
const REASON_NAMES: Record<string, string> = { TP: 'тейк', SL: 'стоп', BE: 'БУ', REV: 'разворот', LIQ: 'ликвидация', WKND: 'закрытие пт', OPEN: 'открыта' };

const ago = (ms: number | null | undefined) => {
  if (!ms) return '–';
  const m = Math.max(0, Math.round((Date.now() - ms) / 60000));
  return m < 1 ? 'только что' : m < 90 ? `${m} мин назад` : m < 48 * 60 ? `${(m / 60).toFixed(1)} ч назад` : `${Math.round(m / 1440)} дн назад`;
};
const held = (h: number) => (h < 1 ? `${Math.round(h * 60)} мин` : h < 48 ? `${h.toFixed(1)} ч` : `${(h / 24).toFixed(1)} сут`);

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

function Card({ e, exchange, onToggle }: { e: MonitorEntry; exchange: MonitorResponse['exchange']; onToggle: (id: number, patch: { open?: boolean; manage?: boolean }) => void }) {
  const pos = e.position;
  const stale = e.candle_ts ? (Date.now() - e.candle_ts) / 60000 : null;
  return (
    <section className="mon-card" data-testid="mon-card" data-name={e.name}>
      <div className="mon-head">
        <b>{e.name}</b>
        <span className="muted"> · {e.symbol?.split('/')[0] ?? '–'}{e.source ? ` · ${e.source}` : ''}{e.paused ? ' · на паузе' : ''}</span>
        {pos && <span className={pos.side === 'long' ? 'pos' : 'neg'}> · {pos.side === 'long' ? 'ЛОНГ' : 'ШОРТ'}</span>}
        {!pos && !e.error && <span className="muted"> · позиции нет</span>}
        {e.config_id != null && e.auto && (
          <span className="mon-auto" data-testid="mon-auto">
            <label title={HELP.auto_open}><input type="checkbox" checked={e.auto.open} onChange={(ev) => onToggle(e.config_id!, { open: ev.target.checked })} /> открывать по сигналу<Help text={HELP.auto_open} /></label>
            <label title={HELP.auto_manage}><input type="checkbox" checked={e.auto.manage} onChange={(ev) => onToggle(e.config_id!, { manage: ev.target.checked })} /> управлять позицией<Help text={HELP.auto_manage} /></label>
            {(e.auto.open || e.auto.manage) && <span className={exchange.real ? 'neg' : 'muted'}>{exchange.real ? ' ● торгует на бирже' : ' ● симуляция'}</span>}
          </span>
        )}
      </div>
      {e.config_id == null && <div className="muted small">Автомод включается только у конфигов бота (по параметрам интерфейса действия не выполняются).</div>}
      {e.error && <div className="bad-text small">Не удалось посчитать: {e.error}</div>}
      {stale != null && stale > 20 && <div className="warn-text small">Свечи не обновлялись {ago(e.candle_ts)} — цена и уровни могут отставать.</div>}
      {pos && (
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
              <tbody>{pos.legs.map((l, i) => <Leg key={i} l={l} side={pos.side} />)}</tbody>
            </table>
          </div>
        </>
      )}
      {!pos && e.ready_now && e.ready_now.length > 0 && (
        <div className="ready-info">{e.ready_now.map((r) => <div key={r.side}><b>⏳ Готов к {r.side === 'long' ? 'ЛОНГУ' : 'ШОРТУ'}, ждёт:</b><ul>{r.lines.map((l, i) => <li key={i}>{l}</li>)}</ul></div>)}</div>
      )}
      {!pos && e.report && !e.error && <div className="muted small">Метрики сейчас: DG {e.report.dg?.toFixed(2) ?? '–'} · DL {e.report.dl.toFixed(2)} · DS {e.report.ds.toFixed(2)} ({ago(e.report.ts)})</div>}
      {e.settings && <div className="muted small">План: SL {e.settings.sl_pct}% · TP {e.settings.tps.map((t) => `${t.pct}%×${t.share}`).join(' / ')}{e.settings.be_after_tp1 ? ' · БУ после TP1' : ''} · плечо {e.settings.leverage}×{e.settings.pyramid_leverage ? ` (доборы ${e.settings.pyramid_leverage}×)` : ''} · {e.settings.trade_pct}% депозита {fmtUsd(e.settings.deposit)}</div>}
      {e.recent && e.recent.length > 0 && (
        <details className="mon-recent"><summary>Последние закрытые ({e.recent.length})</summary>
          <ul>{e.recent.map((t, i) => <li key={i}><span className={t.side === 'long' ? 'pos' : 'neg'}>{t.side === 'long' ? 'лонг' : 'шорт'}</span> {fmtTs(t.entry_ts)} → {fmtTs(t.exit_ts)} · {REASON_NAMES[t.reason] ?? t.reason} · {held(t.hold_h)} · <span className={signClass(t.pnl_usd)}>{fmtUsd(t.pnl_usd)}</span></li>)}</ul>
        </details>
      )}
    </section>
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
              <td className="muted small">{x.details.error ? String(x.details.error) : x.details.note ? String(x.details.note) : x.details.usd != null ? `${x.details.usd} $` : x.details.notional != null ? `объём ${fmtUsd(Number(x.details.notional))}` : x.details.auto ? `открывать: ${(x.details.auto as { open: boolean }).open ? 'да' : 'нет'}, управлять: ${(x.details.auto as { manage: boolean }).manage ? 'да' : 'нет'}` : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Страница мониторинга: открытые позиции стратегий (расчёт по свечам Binance), автомод по конфигам, тестовая позиция $1, журнал действий. */
export default function MonitorPage({ params, onClose }: { params: Params | null; onClose: () => void }) {
  const [data, setData] = useState<MonitorResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [testSide, setTestSide] = useState<'long' | 'short'>('long');
  const [withUi, setWithUi] = useState(false);
  const alive = useRef(true);

  const load = useCallback(async () => {
    try {
      const r = await getMonitor(withUi ? params : null);
      if (alive.current) {
        setData(r);
        setErr(null);
      }
    } catch (e) {
      if (alive.current) setErr(e instanceof Error ? e.message : String(e));
    }
  }, [params, withUi]);

  useEffect(() => {
    alive.current = true;
    void load();
    const t = setInterval(() => void load(), 30000);
    return () => {
      alive.current = false;
      clearInterval(t);
    };
  }, [load]);

  const toggle = async (id: number, patch: { open?: boolean; manage?: boolean }) => {
    const turningOn = patch.open === true || patch.manage === true;
    if (turningOn && !window.confirm(data?.exchange.real
      ? 'Включить автомод? Бот будет сам отправлять ордера на бирже по этому конфигу.'
      : 'Включить автомод? Биржа для торговли не подключена — действия будут только записываться в журнал (симуляция).')) return;
    try {
      await setAuto(id, patch);
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const offAll = async () => {
    if (!window.confirm('Выключить автомод у всех ваших конфигов? Уже открытые позиции на бирже это не закрывает.')) return;
    const r = await autoOffAll().catch((e) => { setErr(String(e)); return null; });
    if (r) setMsg(`Автомод выключен у конфигов: ${r.disabled}`);
    await load();
  };

  const testPos = async () => {
    if (!window.confirm(`Открыть тестовую позицию $1 по BTC (${testSide === 'long' ? 'лонг' : 'шорт'})?${data?.exchange.real ? ' Это реальный ордер на бирже.' : ' Биржа не подключена — будет симуляция.'}`)) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await openTestPosition(testSide);
      setMsg(r.status === 'dry_run' ? 'Симуляция: ордер не отправлялся (биржа не подключена). Запись добавлена в журнал.' : `Ордер отправлен: ${r.status}`);
    } catch (e) {
      setMsg(`Не удалось: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
      await load();
    }
  };

  const anyAuto = (data?.entries ?? []).some((e) => e.auto && (e.auto.open || e.auto.manage));
  return (
    <div className="monitor" data-testid="monitor-page">
      <div className="mon-top">
        <button type="button" className="ghost" onClick={onClose}>← К бэктесту</button>
        <h2>Мониторинг позиций <Help text={HELP.monitor_page} /></h2>
        <button type="button" className="ghost" onClick={() => void load()}>Обновить</button>
        <label className="muted small"><input type="checkbox" checked={withUi} onChange={(e) => setWithUi(e.target.checked)} /> показать и текущие параметры интерфейса</label>
        <span className="mon-spacer" />
        <button type="button" className="ghost" disabled={!anyAuto} onClick={() => void offAll()}>⛔ Выключить автомод везде</button><Help text={HELP.auto_off_all} />
      </div>
      <div className="banner warn" data-testid="mon-banner">
        Данные — расчёт стратегий по свечам и отчётам Binance, а не позиции на бирже. Биржа для торговли — Arcus: коннектор ещё не подключён, автомод работает как{' '}
        <b>симуляция</b> (действия пишутся в журнал, ордера не отправляются){data?.exchange.real ? ' — НО выбран реальный адаптер.' : '.'}
      </div>
      {err && <div className="banner error">{err}</div>}
      <div className="mon-test">
        <b>Тестовая позиция $1 по BTC</b><Help text={HELP.test_position} />
        <select value={testSide} onChange={(e) => setTestSide(e.target.value as 'long' | 'short')} aria-label="Сторона тестовой позиции">
          <option value="long">лонг</option><option value="short">шорт</option>
        </select>
        <button type="button" className="ghost" disabled={busy} onClick={() => void testPos()}>{busy ? 'Открываю…' : 'Открыть'}</button>
        {msg && <span className="muted small" data-testid="mon-msg"> {msg}</span>}
      </div>
      {!data && !err && <div className="empty">Загрузка…</div>}
      {data && data.entries.length === 0 && <div className="empty">Нет конфигов бота. Добавьте конфиг боту в Telegram или отметьте «показать и текущие параметры интерфейса».</div>}
      {data?.entries.map((e) => <Card key={e.name} e={e} exchange={data.exchange} onToggle={(id, patch) => void toggle(id, patch)} />)}
      {data && (
        <>
          <h3>Журнал действий</h3>
          <Journal log={data.log} />
        </>
      )}
    </div>
  );
}
