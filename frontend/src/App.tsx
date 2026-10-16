import { useCallback, useEffect, useMemo, useState } from 'react';
import { getConfig, refreshCandles, runBacktest } from './api';
import EquityChart from './components/EquityChart';
import ParamsPanel from './components/ParamsPanel';
import PriceChart from './components/PriceChart';
import StatsPanel from './components/StatsPanel';
import TradesTable from './components/TradesTable';
import { fmtDate, fmtPrice, fmtTs, fmtUsd, signClass, tfLabel, tfSeconds } from './format';
import type { AppConfig, BacktestResult, Params, TfMode } from './types';

const STORAGE_KEY = 'maverick.params.v1';
const TF_KEY = 'maverick.tf.v1';

function loadSaved(): Partial<Params> | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Partial<Params>) : null;
  } catch {
    return null;
  }
}

function loadTf(): TfMode {
  try {
    const v = localStorage.getItem(TF_KEY);
    return v && v !== 'auto' && Number(v) > 0 ? Number(v) : 'auto';
  } catch {
    return 'auto';
  }
}

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
  const [tfMode, setTfMode] = useState<TfMode>(loadTf);
  const [effTf, setEffTf] = useState<{ tf: number; truncated: boolean } | null>(null);

  const run = useCallback(async (p: Params) => {
    setLoading(true);
    setError(null);
    try {
      const r = await runBacktest(p);
      setResult(r);
      setSelected(null);
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(p));
      } catch {
        /* localStorage может быть недоступен */
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    getConfig()
      .then((c) => {
        setConfig(c);
        const p = { ...c.defaults, ...(loadSaved() ?? {}) } as Params;
        setParams(p);
        void run(p);
      })
      .catch((e) => setError(`Не удалось связаться с бэкендом: ${e instanceof Error ? e.message : e}`));
  }, [run]);

  const onRefresh = async () => {
    setRefreshing(true);
    setError(null);
    try {
      const data = await refreshCandles();
      setConfig((c) => (c ? { ...c, data } : c));
      setReloadKey((k) => k + 1);
      if (params) void run(params);
    } catch (e) {
      setError(`Не удалось докачать свечи: ${e instanceof Error ? e.message : e}`);
    } finally {
      setRefreshing(false);
    }
  };

  const changeTf = (m: TfMode) => {
    setTfMode(m);
    try {
      localStorage.setItem(TF_KEY, String(m));
    } catch {
      /* ignore */
    }
  };

  const selectTrade = useCallback((n: number) => {
    setSelected(n);
    setFocusKey((k) => k + 1);
  }, []);

  const trades = result?.trades ?? [];
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

  const dataRange = useMemo(() => {
    const c = config?.data.candles;
    return c && config ? { first: c.first_ts / 1000, last: c.last_ts / 1000, nativeTf: tfSeconds(config.tf) } : null;
  }, [config]);

  const d = config?.data;

  return (
    <div className="app">
      <header>
        <h1>Maverick <span>· бэктест</span></h1>
        {config && (
          <div className="chips">
            <span className="chip">{config.exchange} · {config.symbol} · {config.tf}</span>
            {d?.signals && <span className="chip">отчётов {d.signals.count.toLocaleString('ru-RU')} · {fmtDate(d.signals.first_ts)} … {fmtDate(d.signals.last_ts)}</span>}
            {d?.candles && <span className="chip">свечей {d.candles.count.toLocaleString('ru-RU')} · до {fmtDate(d.candles.last_ts)}</span>}
            {d?.signals && !d.signals.has_liq && <span className="chip warn">нет данных о ликвидациях</span>}
            <button type="button" className="ghost" disabled={refreshing} onClick={onRefresh} title="Скачать/докачать свечи с биржи">
              {refreshing ? 'Качаю свечи…' : 'Обновить свечи'}
            </button>
          </div>
        )}
      </header>

      {error && <div className="banner error">{error}</div>}
      {d?.signals_error && <div className="banner error">{d.signals_error}</div>}
      {(chartError || d?.candles_error) && (
        <div className="banner warn">
          {chartError ?? d?.candles_error}
          <button type="button" className="ghost" disabled={refreshing} onClick={onRefresh}>
            {refreshing ? 'Качаю…' : 'Скачать свечи'}
          </button>
        </div>
      )}

      <main>
        {params && config ? (
          <ParamsPanel params={params} defaults={config.defaults} loading={loading} onChange={setParams} onRun={() => void run(params)} />
        ) : (
          <aside className="params"><div className="empty">{error ? '' : 'Загрузка…'}</div></aside>
        )}

        <section className="center">
          <div className="chart-bar">
            <div className="seg" title="Таймфрейм свечей">
              <button type="button" className={tfMode === 'auto' ? 'on' : ''} onClick={() => changeTf('auto')}>
                Авто{tfMode === 'auto' && effTf ? ` · ${tfLabel(effTf.tf)}` : ''}
              </button>
              {(config?.timeframes ?? []).map((t) => (
                <button type="button" key={t} className={tfMode === t ? 'on' : ''} onClick={() => changeTf(t)}>
                  {tfLabel(t)}
                </button>
              ))}
            </div>
            <div className="nav">
              <button type="button" className="ghost" onClick={() => step(-1)} disabled={!trades.length} title="Предыдущая сделка (←)">◀</button>
              <button type="button" className="ghost" onClick={() => step(1)} disabled={!trades.length} title="Следующая сделка (→)">▶</button>
            </div>
            <label><input type="checkbox" checked={showLevels} onChange={(e) => setShowLevels(e.target.checked)} /> уровни TP / SL</label>
            <label><input type="checkbox" checked={showSkipped} onChange={(e) => setShowSkipped(e.target.checked)} /> пропущенные сигналы</label>
            <span className="legend">
              <i className="dot up" /> лонг ▲ <i className="dot down" /> шорт ▼ · выход: <b className="pos">TP</b> <b className="neg">SL</b> <b className="rev">REV</b> <b className="muted">OPEN</b>
            </span>
            {effTf?.truncated && <span className="chip warn">окно слишком широкое для {tfLabel(effTf.tf)} — показана последняя часть, приблизьте или возьмите ТФ крупнее</span>}
            {result?.signals_truncated && <span className="chip warn">сигналов слишком много, показаны первые 5000</span>}
          </div>
          {selTrade && (
            <div className="sel-info">
              <b>#{selTrade.n}</b> <span className={selTrade.side === 'long' ? 'pos' : 'neg'}>{selTrade.side === 'long' ? 'лонг' : 'шорт'}</span>
              {' · '}{fmtTs(selTrade.entry_ts)} → {fmtTs(selTrade.exit_ts)} · вход {fmtPrice(selTrade.entry)} · TP {fmtPrice(selTrade.tp)} · SL {fmtPrice(selTrade.sl)} · выход {fmtPrice(selTrade.exit)} ({selTrade.reason})
              {' · '}<b className={signClass(selTrade.pnl_usd)}>{fmtUsd(selTrade.pnl_usd)}</b>
              {selTrade.local != null && <span className="muted"> · Local {selTrade.local.toFixed(2)} / Scalp {selTrade.scalp?.toFixed(2)}</span>}
            </div>
          )}
          <div className="price">
            <PriceChart
              trades={trades}
              signals={result?.signals ?? []}
              showLevels={showLevels}
              showSkipped={showSkipped}
              selected={selected}
              onSelect={selectTrade}
              focusKey={focusKey}
              tfMode={tfMode}
              dataRange={dataRange}
              reloadKey={reloadKey}
              onLoadError={setChartError}
              onTfInfo={(tf, truncated) => setEffTf({ tf, truncated })}
            />
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
