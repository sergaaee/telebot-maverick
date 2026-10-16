import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getConfig, getUiState, putUiState, refreshCandles, runBacktest } from './api';
import EquityChart from './components/EquityChart';
import Help from './components/Help';
import ParamsPanel from './components/ParamsPanel';
import PriceChart from './components/PriceChart';
import StatsPanel from './components/StatsPanel';
import TradesTable from './components/TradesTable';
import { fmtDate, fmtPrice, fmtTs, fmtUsd, signClass, tfLabel, tfSeconds } from './format';
import { HELP } from './helpTexts';
import type { AppConfig, BacktestResult, Params, Signal, TfMode, Trade, UiState } from './types';

const profileKey = (source: string, symbol: string) => `${source}|${symbol}`;

/** Что запоминаем для связки «источник + актив»: все параметры, кроме самих source/symbol, и выбранный ТФ графика. */
function splitProfile(p: Params): Partial<Params> {
  const { source: _s, symbol: _y, ...rest } = p;
  void _s;
  void _y;
  return rest;
}

/** Параметры для связки: умолчания из env, поверх них сохранённый профиль (с отбраковкой исчезнувших источников). */
function loadProfile(cfg: AppConfig, source: string, symbol: string, st: UiState): { params: Params; tf: TfMode } {
  const prof = st.profiles?.[profileKey(source, symbol)];
  const saved: Partial<Params> & { tp_pct?: number } = { ...(prof?.params ?? {}) };
  // профили старого формата хранили один тейк в tp_pct
  if (saved.tp_pct != null && !saved.tps) saved.tps = [{ pct: saved.tp_pct, share: 100 }];
  delete saved.tp_pct;
  if (saved.liq_source && !cfg.data.sources.some((s) => s.id === saved.liq_source)) delete saved.liq_source;
  return { params: { ...cfg.defaults, ...saved, source, symbol }, tf: prof?.tf ?? 'auto' };
}

const NO_TRADES: Trade[] = [];
const NO_SIGNALS: Signal[] = [];

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [params, setParams] = useState<Params | null>(null);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chartError, setChartError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [focusKey, setFocusKey] = useState(0); // растёт при каждом явном выборе сделки: график возвращается к ней, даже если она уже выбрана
  const [showLevels, setShowLevels] = useState(true);
  const [showSkipped, setShowSkipped] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [tfMode, setTfMode] = useState<TfMode>('auto');
  const [effTf, setEffTf] = useState<{ tf: number; truncated: boolean } | null>(null);

  const uiState = useRef<UiState>({}); // последнее известное состояние, хранящееся на сервере
  const ready = useRef(false);
  const runSeq = useRef(0);
  const leaving = useRef(false); // страница закрывается: прерванные запросы сохранения — не ошибка

  // ---------- сохранение выбора на сервере ----------
  const buildState = useCallback((p: Params, tf: TfMode): UiState => {
    const st = uiState.current;
    return {
      version: 1,
      last: { source: p.source, symbol: p.symbol },
      profiles: { ...(st.profiles ?? {}), [profileKey(p.source, p.symbol)]: { params: splitProfile(p), tf } },
    };
  }, []);

  const saveNow = useCallback(
    (p: Params, tf: TfMode, keepalive = false) => {
      const st = buildState(p, tf);
      uiState.current = st;
      putUiState(st, keepalive).catch((e) => {
        if (!leaving.current) console.warn('не удалось сохранить состояние', e);
      });
    },
    [buildState],
  );

  useEffect(() => {
    if (!params || !ready.current) return;
    const t = setTimeout(() => saveNow(params, tfMode), 600);
    return () => clearTimeout(t);
  }, [params, tfMode, saveNow]);

  useEffect(() => {
    // не потерять последние правки при закрытии вкладки
    const flush = () => {
      leaving.current = true;
      if (params && ready.current) saveNow(params, tfMode, true);
    };
    window.addEventListener('pagehide', flush);
    return () => window.removeEventListener('pagehide', flush);
  }, [params, tfMode, saveNow]);

  // ---------- расчёт ----------
  const run = useCallback(async (p: Params) => {
    const seq = ++runSeq.current;
    setLoading(true);
    setError(null);
    try {
      const r = await runBacktest(p);
      if (seq !== runSeq.current) return; // пока считали, пользователь переключил источник/актив
      setResult(r);
      setSelected(null);
    } catch (e) {
      if (seq === runSeq.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (seq === runSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    Promise.all([getConfig(), getUiState().catch(() => ({}) as UiState)])
      .then(([c, st]) => {
        uiState.current = st;
        setConfig(c);
        // последний выбор: источник и актив, если они всё ещё есть в конфиге
        const source = c.data.sources.some((s) => s.id === st.last?.source) ? st.last!.source! : c.defaults.source;
        const symbol = c.symbols.includes(st.last?.symbol ?? '') ? st.last!.symbol! : c.defaults.symbol;
        const { params: p, tf } = loadProfile(c, source, symbol, st);
        setParams(p);
        setTfMode(tf);
        ready.current = true;
        void run(p);
      })
      .catch((e) => setError(`Не удалось связаться с бэкендом: ${e instanceof Error ? e.message : e}`));
  }, [run]);

  const switchTo = (t: { source?: string; symbol?: string }) => {
    if (!params || !config) return;
    saveNow(params, tfMode); // текущие настройки остаются за прежней связкой
    const { params: np, tf } = loadProfile(config, t.source ?? params.source, t.symbol ?? params.symbol, uiState.current);
    setParams(np);
    setTfMode(tf);
    setResult(null);
    setSelected(null);
    setEffTf(null);
    setChartError(null);
    void run(np);
  };

  const resetParams = () => {
    if (!params || !config) return;
    const np = { ...config.defaults, source: params.source, symbol: params.symbol };
    setParams(np);
    setTfMode('auto');
    void run(np);
  };

  const onRefresh = async () => {
    if (!params) return;
    setRefreshing(true);
    setError(null);
    try {
      const data = await refreshCandles(params.symbol);
      setConfig((c) => (c ? { ...c, data } : c));
      setReloadKey((k) => k + 1);
      void run(params);
    } catch (e) {
      setError(`Не удалось докачать свечи: ${e instanceof Error ? e.message : e}`);
    } finally {
      setRefreshing(false);
    }
  };

  // ---------- сделки: выбор и навигация ----------
  const selectTrade = useCallback((n: number) => {
    setSelected(n);
    setFocusKey((k) => k + 1);
  }, []);

  const trades = result?.trades ?? NO_TRADES;
  const selIdx = trades.findIndex((t) => t.n === selected);
  const selTrade = selIdx >= 0 ? trades[selIdx] : null;
  const step = useCallback(
    (d: number) => {
      if (!trades.length) return;
      const i = selIdx < 0 ? (d > 0 ? 0 : trades.length - 1) : Math.min(trades.length - 1, Math.max(0, selIdx + d));
      selectTrade(trades[i].n);
    },
    [trades, selIdx, selectTrade],
  );

  // ← / → переключают сделки (если фокус не в поле ввода)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName)) return;
      if (e.key === 'ArrowRight') step(1);
      else if (e.key === 'ArrowLeft') step(-1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [step]);

  // ---------- производные ----------
  const symbol = params?.symbol ?? config?.symbol ?? '';
  const asset = config?.data.assets.find((a) => a.symbol === symbol);
  const dataRange = useMemo(
    () => (asset && asset.count > 0 && config ? { first: asset.first_ts! / 1000, last: asset.last_ts! / 1000, nativeTf: tfSeconds(config.tf) } : null),
    [asset, config],
  );
  const curSrc = config?.data.sources.find((s) => s.id === (params?.source ?? config.defaults.source));
  const tfOptions = (config && config.timeframes[symbol]) || [];

  return (
    <div className="app">
      <header>
        <h1>Maverick <span>· бэктест</span></h1>
        {config && (
          <div className="chips">
            <span className="chip">{config.exchange} · {symbol} · {config.tf}</span>
            {curSrc && curSrc.count > 0 && <span className="chip">{curSrc.label} · отчётов {curSrc.count.toLocaleString('ru-RU')} · {fmtDate(curSrc.first_ts!)} … {fmtDate(curSrc.last_ts!)}</span>}
            {asset && asset.count > 0 && <span className="chip">свечей {asset.count.toLocaleString('ru-RU')} · до {fmtDate(asset.last_ts!)}</span>}
            <button type="button" className="ghost" disabled={refreshing} onClick={onRefresh}>
              {refreshing ? 'Качаю свечи…' : 'Обновить свечи'}
            </button>
            <Help text={HELP.refresh} />
          </div>
        )}
      </header>

      {error && <div className="banner error">{error}</div>}
      {(result?.warnings ?? []).map((w) => <div className="banner warn" key={w}>{w}</div>)}
      {config && !config.data.sources.some((s) => s.exists) && <div className="banner error">Нет ни одного файла истории — выгрузите историю (python cli.py export …) и проверьте SOURCE_*_FILE в backend/.env</div>}
      {(chartError || asset?.error) && (
        <div className="banner warn">
          {chartError ?? asset?.error}
          <button type="button" className="ghost" disabled={refreshing} onClick={onRefresh}>
            {refreshing ? 'Качаю…' : 'Скачать свечи'}
          </button>
        </div>
      )}

      <main>
        {params && config ? (
          <ParamsPanel
            params={params}
            sources={config.data.sources}
            assets={config.data.assets}
            loading={loading}
            onChange={setParams}
            onRun={() => void run(params)}
            onSwitch={switchTo}
            onReset={resetParams}
          />
        ) : (
          <aside className="params"><div className="empty">{error ? '' : 'Загрузка…'}</div></aside>
        )}

        <section className="center">
          <div className="chart-bar">
            <div className="seg">
              <button type="button" className={tfMode === 'auto' ? 'on' : ''} onClick={() => setTfMode('auto')}>
                Авто{tfMode === 'auto' && effTf ? ` · ${tfLabel(effTf.tf)}` : ''}
              </button>
              {tfOptions.map((t) => (
                <button type="button" key={t} className={tfMode === t ? 'on' : ''} onClick={() => setTfMode(t)}>
                  {tfLabel(t)}
                </button>
              ))}
            </div>
            <Help text={HELP.tf} />
            <div className="nav">
              <button type="button" className="ghost" onClick={() => step(-1)} disabled={!trades.length} title="Предыдущая сделка (←)">◀</button>
              <button type="button" className="ghost" onClick={() => step(1)} disabled={!trades.length} title="Следующая сделка (→)">▶</button>
            </div>
            <Help text={HELP.nav} />
            <label><input type="checkbox" checked={showLevels} onChange={(e) => setShowLevels(e.target.checked)} /> уровни TP / SL<Help text={HELP.levels} /></label>
            <label><input type="checkbox" checked={showSkipped} onChange={(e) => setShowSkipped(e.target.checked)} /> пропущенные сигналы<Help text={HELP.skipped} /></label>
            <span className="legend">
              <i className="dot up" /> лонг ▲ <i className="dot down" /> шорт ▼ · выход: <b className="pos">TP</b> <b className="neg">SL</b> <b className="rev">REV</b> <b className="muted">OPEN</b>
            </span>
            {effTf?.truncated && <span className="chip warn">окно слишком широкое для {tfLabel(effTf.tf)} — показана последняя часть, приблизьте или возьмите ТФ крупнее</span>}
            {result?.signals_truncated && <span className="chip warn">сигналов слишком много, показаны первые 5000</span>}
          </div>
          {selTrade && (
            <div className="sel-info">
              <b>#{selTrade.n}</b> <span className={selTrade.side === 'long' ? 'pos' : 'neg'}>{selTrade.side === 'long' ? 'лонг' : 'шорт'}</span>
              {' · '}{fmtTs(selTrade.entry_ts)} → {fmtTs(selTrade.exit_ts)} · вход {fmtPrice(selTrade.entry)} · TP {selTrade.tps.map(fmtPrice).join(' / ')} · SL {fmtPrice(selTrade.sl)}{selTrade.be ? ' (БУ после TP1)' : ''}
              {' · '}<span className="fills">{selTrade.fills.map((f) => `${f.label} ${Math.round(f.share * 100)}% @ ${fmtPrice(f.price)}`).join(' → ')}</span>
              {' · '}<b className={signClass(selTrade.pnl_usd)}>{fmtUsd(selTrade.pnl_usd)}</b>
              {selTrade.local != null && <span className="muted"> · Local {selTrade.local.toFixed(2)} / Scalp {selTrade.scalp?.toFixed(2)}</span>}
            </div>
          )}
          <div className="price">
            {/* график создаём, когда известен актив: иначе первый запрос свечей уходит с пустым символом */}
            {symbol ? <PriceChart
              trades={trades}
              signals={result?.signals ?? NO_SIGNALS}
              showLevels={showLevels}
              showSkipped={showSkipped}
              selected={selected}
              onSelect={selectTrade}
              focusKey={focusKey}
              symbol={symbol}
              tfMode={tfMode}
              dataRange={dataRange}
              reloadKey={reloadKey}
              onLoadError={setChartError}
              onTfInfo={(tf, truncated) => setEffTf({ tf, truncated })}
            /> : <div className="empty">Загрузка…</div>}
          </div>
          <div className="equity-title">Капитал, $ (накопленный результат по закрытым сделкам)</div>
          <div className="equity">{result && result.equity.length > 1 ? <EquityChart equity={result.equity} splitTs={result.range.split_ts} /> : <div className="empty">нет данных</div>}</div>
          <TradesTable trades={trades} selected={selected} onSelect={selectTrade} />
        </section>

        <section className="right">{result ? <StatsPanel result={result} /> : <div className="empty">{loading ? 'Считаю…' : 'Нет результата'}</div>}</section>
      </main>
    </div>
  );
}
