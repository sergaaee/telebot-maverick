import {
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LineData,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
  type WhitespaceData,
} from 'lightweight-charts';
import { useEffect, useRef } from 'react';
import { getCandles } from '../api';
import type { Signal, TfMode, Trade } from '../types';

interface Props {
  trades: Trade[];
  signals: Signal[];
  showLevels: boolean;
  showSkipped: boolean;
  selected: number | null;
  onSelect: (n: number) => void;
  /** меняется при каждом явном выборе сделки (в том числе повторном) — график приближается к ней */
  focusKey: number;
  /** актив, свечи которого показываем */
  symbol: string;
  /** 'auto' — бэкенд подбирает ТФ под масштаб; число — фиксированный ТФ (сек) */
  tfMode: TfMode;
  /** первая/последняя свеча в кеше (сек) и родной таймфрейм (сек) — чтобы не запрашивать лишнее */
  dataRange: { first: number; last: number; nativeTf: number } | null;
  /** меняйте значение, чтобы принудительно перезагрузить свечи (после докачки) */
  reloadKey: number;
  onLoadError: (msg: string | null) => void;
  /** какой ТФ сейчас реально на графике и обрезано ли окно */
  onTfInfo: (tf: number, truncated: boolean) => void;
}

type Range = { from: number; to: number };

const COLORS = { up: '#26a69a', down: '#ef5350', rev: '#f0a030', open: '#8a94a8', be: '#6ea0ff', sl: '#ef5350', entry: '#8a94a8' };
const TP_COLORS = ['#26a69a', '#35c9b8', '#7be0d2'];
/** Цвет маркера закрытия по метке исполнения: TP1..TP3, SL, BE, REV, LIQ, OPEN. */
const labelColor = (l: string) => (l.startsWith('TP') ? COLORS.up : l === 'SL' ? COLORS.down : l === 'BE' ? COLORS.be : l === 'REV' ? COLORS.rev : l === 'LIQ' ? '#d81b60' : COLORS.open);

/** Для фиксированного ТФ: сколько баров запрашиваем за раз и сколько максимум показываем в видимой области. */
const FIXED_MAX_BARS = 6000;
const FIXED_VISIBLE_BARS = FIXED_MAX_BARS / 2;

export default function PriceChart(props: Props) {
  const host = useRef<HTMLDivElement>(null);
  const propsRef = useRef(props);
  propsRef.current = props;

  const chart = useRef<IChartApi | null>(null);
  const candle = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const levels = useRef<Record<string, ISeriesApi<'Line'>>>({});
  const priceLines = useRef<IPriceLine[]>([]);
  const tf = useRef(60);
  const coverage = useRef<Range | null>(null);   // фактически загруженные свечи
  const barTimes = useRef<number[]>([]);         // времена загруженных баров — для перевода логического индекса во время
  const programmatic = useRef(0);                // >0: идёт программная смена диапазона — события шкалы времени игнорируем
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const loadSeq = useRef(0);

  const snap = (sec: number) => Math.floor(sec / tf.current) * tf.current;
  const fixedTf = () => (propsRef.current.tfMode === 'auto' ? null : propsRef.current.tfMode);

  /** Маркеры и линии уровней пересобираются при смене данных, флагов или таймфрейма свечей. */
  const redraw = () => {
    const c = candle.current;
    if (!c || !chart.current) return;
    const { trades, signals, showLevels, showSkipped } = propsRef.current;
    const t = tf.current;
    // Всё рисуем только внутри загруженного окна свечей: времена всех серий входят в общую шкалу времени графика,
    // и точки линий далеко за пределами окна ломают масштабирование (диапазон «прыгает»).
    const cov = coverage.current;
    const lo = cov?.from ?? Infinity;
    const hi = cov?.to ?? -Infinity;
    const inWin = (sec: number) => sec >= lo && sec <= hi;

    const markers: SeriesMarker<Time>[] = [];
    const labels = trades.length <= 60; // при сотнях сделок подписи сливаются — детали видны при зуме и в таблице
    for (const tr of trades) {
      const long = tr.side === 'long';
      if (inWin(snap(tr.entry_ts / 1000)))
        markers.push({
          time: snap(tr.entry_ts / 1000) as UTCTimestamp,
          position: long ? 'belowBar' : 'aboveBar',
          shape: long ? 'arrowUp' : 'arrowDown',
          color: long ? COLORS.up : COLORS.down,
          text: labels ? (tr.pyramid ? `+${(tr.leg ?? 2) - 1}` : `${long ? 'L' : 'S'}${tr.n}`) : '',
          size: 1.4,
        });
      // доборы усреднённой позиции
      (tr.adds ?? []).slice(1).forEach((a, k) => {
        const at = snap(a.ts / 1000);
        if (!inWin(at)) return;
        markers.push({ time: at as UTCTimestamp, position: long ? 'belowBar' : 'aboveBar', shape: long ? 'arrowUp' : 'arrowDown', color: '#e0b050', text: labels ? `+${k + 1}` : '', size: 0.9 });
      });
      // каждое исполнение (частичные тейки и финальное закрытие) — свой маркер
      tr.fills.forEach((f, k) => {
        const ft = snap(f.ts / 1000);
        if (!inWin(ft)) return;
        markers.push({
          time: ft as UTCTimestamp,
          position: long ? 'aboveBar' : 'belowBar',
          shape: 'circle',
          color: labelColor(f.label),
          text: labels ? f.label : '',
          size: k < tr.fills.length - 1 ? 0.8 : 1,
        });
      });
    }
    if (showSkipped) {
      for (const s of signals.filter((x) => !x.taken && inWin(snap(x.ts / 1000)))) {
        markers.push({ time: snap(s.ts / 1000) as UTCTimestamp, position: 'inBar', shape: 'square', color: '#5d6677', size: 0.4 });
      }
    }
    markers.sort((a, b) => (a.time as number) - (b.time as number));
    c.setMarkers(markers);

    // Уровни: по две серии на вид (чётные/нечётные сделки), чтобы соседние сегменты не пересекались по времени.
    // Линия каждого тейка идёт до его исполнения, стоп — до переноса в безубыток (дальше остаётся линия входа), вход — до закрытия.
    type Kind = { name: string; color: string; style: LineStyle; segment: (tr: Trade) => { price: number; end: number } | null };
    const fillTs = (tr: Trade, label: string) => tr.fills.find((f) => f.label === label)?.ts;
    const kinds: Kind[] = [
      ...[0, 1, 2].map((i): Kind => ({
        name: `tp${i}`, color: TP_COLORS[i], style: LineStyle.Dashed,
        segment: (tr) => (i < tr.tps.length ? { price: tr.tps[i], end: fillTs(tr, `TP${i + 1}`) ?? tr.exit_ts } : null),
      })),
      { name: 'sl', color: COLORS.sl, style: LineStyle.Dashed, segment: (tr) => ({ price: tr.sl, end: tr.be_ts ?? tr.exit_ts }) },
      { name: 'entry', color: COLORS.entry, style: LineStyle.Dotted, segment: (tr) => ({ price: tr.entry, end: tr.exit_ts }) },
    ];
    for (const kind of kinds) {
      for (const parity of [0, 1]) {
        const key = `${kind.name}-${parity}`;
        let s = levels.current[key];
        if (!s) {
          s = chart.current.addLineSeries({
            color: kind.color, lineWidth: 1, lineStyle: kind.style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
          });
          levels.current[key] = s;
        }
        const data: (LineData | WhitespaceData)[] = [];
        let last = -Infinity;
        const push = (time: number, value?: number) => {
          const tt = Math.max(time, last + 1);
          last = tt;
          data.push(value == null ? { time: tt as UTCTimestamp } : { time: tt as UTCTimestamp, value });
        };
        if (showLevels) {
          for (const tr of trades.filter((x) => x.n % 2 === parity)) {
            const seg = kind.segment(tr);
            if (!seg) continue;
            const a0 = snap(tr.entry_ts / 1000);
            const b0 = Math.max(snap(seg.end / 1000), a0 + t);
            if (b0 < lo || a0 > hi) continue; // сделка целиком вне окна
            const a = Math.max(a0, lo);
            const b = Math.min(b0, hi); // обрезаем по окну
            if (b <= a || a <= last) continue;
            push(a, seg.price);
            push(b, seg.price);
            if (b + t <= hi) push(b + t); // разрыв между сегментами
          }
        }
        s.setData(data);
      }
    }
  };

  /**
   * Применяет видимый диапазон после того, как шкала времени переварила новые данные: setData обновляет индексы
   * асинхронно, поэтому диапазон выставляем, проверяем, и после паузы проверяем ещё раз (шкала может «доехать» позже).
   */
  const applyRange = async (range: Range, seq: number) => {
    const cov = coverage.current;
    const want = { from: Math.max(range.from, cov?.from ?? -Infinity), to: Math.min(range.to, cov?.to ?? Infinity) };
    const tol = Math.max((want.to - want.from) * 0.05, tf.current);
    const ok = () => {
      const v = chart.current?.timeScale().getVisibleRange();
      return !!v && Math.abs((v.from as number) - want.from) <= tol && Math.abs((v.to as number) - want.to) <= tol;
    };
    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
    for (let i = 0; i < 6; i++) {
      await new Promise((r) => requestAnimationFrame(r));
      if (seq !== loadSeq.current || !chart.current) return;
      chart.current.timeScale().setVisibleRange({ from: range.from as UTCTimestamp, to: range.to as UTCTimestamp });
      await sleep(120);
      if (ok()) {
        await sleep(250);
        if (seq !== loadSeq.current) return;
        if (ok()) return;
      }
    }
  };

  /** Грузит свечи окна `win` и затем показывает диапазон `keep` (или всё, если keep не задан). */
  const load = async (win?: Range, keep?: Range) => {
    const seq = ++loadSeq.current;
    const fixed = fixedTf();
    try {
      const r = await getCandles(propsRef.current.symbol, win?.from, win?.to, fixed ?? undefined, fixed ? FIXED_MAX_BARS : undefined);
      if (seq !== loadSeq.current || !candle.current || !chart.current) return;
      tf.current = r.tf;
      candle.current.setData(r.bars.map((b) => ({ time: b[0] as UTCTimestamp, open: b[1], high: b[2], low: b[3], close: b[4] })));
      coverage.current = r.bars.length ? { from: r.bars[0][0], to: r.bars[r.bars.length - 1][0] } : null;
      barTimes.current = r.bars.map((b) => b[0]);
      redraw();
      propsRef.current.onTfInfo(r.tf, r.truncated);
      programmatic.current++;
      try {
        if (keep) await applyRange(keep, seq);
        else chart.current.timeScale().fitContent();
      } finally {
        setTimeout(() => (programmatic.current = Math.max(0, programmatic.current - 1)), 250);
      }
      propsRef.current.onLoadError(null);
      // верхняя цена шкалы — для автотеста (и отладки): видно, подобрала ли шкала диапазон под актив
      const top = candle.current.coordinateToPrice(1);
      if (host.current && top != null) host.current.dataset.priceTop = String(Math.round(top));
    } catch (e) {
      propsRef.current.onLoadError(e instanceof Error ? e.message : String(e));
    }
  };

  /** Показать диапазон: загрузить окно с запасом по краям и выставить диапазон. В фиксированном ТФ слишком широкий вид сужаем. */
  const focus = (range: Range) => {
    const dr = propsRef.current.dataRange;
    let { from, to } = range;
    const fixed = fixedTf();
    if (fixed && (to - from) / fixed > FIXED_VISIBLE_BARS) {
      const c = (from + to) / 2;
      from = c - (FIXED_VISIBLE_BARS * fixed) / 2;
      to = c + (FIXED_VISIBLE_BARS * fixed) / 2;
    }
    const span = to - from;
    const winFrom = Math.max(from - span * 0.5, dr?.first ?? -Infinity);
    const winTo = Math.min(to + span * 0.5, dr?.last ?? Infinity);
    return load({ from: winFrom, to: winTo }, { from, to });
  };

  const focusTrade = (tr: Trade) => {
    const a = tr.entry_ts / 1000;
    const b = tr.exit_ts / 1000;
    const pad = Math.max((b - a) * 0.6, 6 * 3600);
    return focus({ from: a - pad, to: b + pad });
  };

  const initialLoad = () => {
    const fixed = fixedTf();
    const dr = propsRef.current.dataRange;
    if (fixed && dr) return focus({ from: dr.last - FIXED_VISIBLE_BARS * 0.4 * fixed, to: dr.last });
    return load();
  };

  // создание графика
  useEffect(() => {
    const ch = createChart(host.current!, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#0f131a' }, textColor: '#9aa4b5', fontSize: 12 },
      grid: { vertLines: { color: '#1a212c' }, horzLines: { color: '#1a212c' } },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: '#262e3b' },
      timeScale: { borderColor: '#262e3b', timeVisible: true, secondsVisible: false },
    });
    chart.current = ch;
    // хук для e2e-проверок (открыть интерфейс с ?debug): window.__chart даёт доступ к диапазону графика
    if (new URLSearchParams(window.location.search).has('debug')) (window as unknown as { __chart?: IChartApi }).__chart = ch;
    candle.current = ch.addCandlestickSeries({
      upColor: COLORS.up, downColor: COLORS.down, borderVisible: false, wickUpColor: COLORS.up, wickDownColor: COLORS.down,
    });

    // листание и зум: смотрим на логический диапазон (сколько баров осталось слева/справа от видимой области);
    // диапазон по времени библиотека обрезает по границам данных, поэтому по нему выход за край не определить
    const timeAtIndex = (i: number): number | null => {
      const bt = barTimes.current;
      if (!bt.length) return null;
      if (i <= 0) return bt[0] + i * tf.current;
      if (i >= bt.length - 1) return bt[bt.length - 1] + (i - (bt.length - 1)) * tf.current;
      const k = Math.floor(i);
      return bt[k] + (i - k) * (bt[k + 1] - bt[k]);
    };
    ch.timeScale().subscribeVisibleLogicalRangeChange(() => {
      if (programmatic.current > 0) return;
      clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        if (programmatic.current > 0) return;
        const lr = ch.timeScale().getVisibleLogicalRange();
        const cov = coverage.current;
        const info = lr && candle.current ? candle.current.barsInLogicalRange(lr) : null;
        if (!lr || !cov || !info) return;
        const dr = propsRef.current.dataRange;
        const t = tf.current;
        const n = lr.to - lr.from;
        if (n <= 0) return;
        const fixed = fixedTf();
        const nearLeft = info.barsBefore < n * 0.25 && cov.from > (dr?.first ?? -Infinity) + t;
        const nearRight = info.barsAfter < n * 0.25 && cov.to < (dr?.last ?? Infinity) - t;
        const tooCoarse = !fixed && n < 80 && t > (dr?.nativeTf ?? 60);
        const tooWide = !!fixed && n > FIXED_VISIBLE_BARS;
        if (!(nearLeft || nearRight || tooCoarse || tooWide)) return;
        const mid = timeAtIndex((lr.from + lr.to) / 2);
        if (mid == null) return;
        void focus({ from: mid - (n * t) / 2, to: mid + (n * t) / 2 });
      }, 250);
    });

    // клик по графику внутри сделки выбирает её
    ch.subscribeClick((p) => {
      if (!p.time) return;
      const t = p.time as number;
      const hit = propsRef.current.trades.find((tr) => t >= snap(tr.entry_ts / 1000) && t <= snap(tr.exit_ts / 1000));
      if (hit) propsRef.current.onSelect(hit.n);
    });

    void initialLoad();
    return () => {
      clearTimeout(timer.current);
      ch.remove();
      chart.current = null;
      candle.current = null;
      levels.current = {};
      priceLines.current = [];
    };
  }, []);

  // перезагрузка свечей после докачки
  const first = useRef(true);
  useEffect(() => {
    if (props.reloadKey > 0 && candle.current) void initialLoad();
  }, [props.reloadKey]);

  // смена актива: другие свечи — начинаем с обзора всего периода
  const firstSymbol = useRef(true);
  useEffect(() => {
    if (firstSymbol.current) {
      firstSymbol.current = false;
      return;
    }
    // ручное масштабирование по вертикали отключает автошкалу цены; у другого актива цена в другом диапазоне — возвращаем автоподбор
    chart.current?.priceScale('right').applyOptions({ autoScale: true });
    if (candle.current) void initialLoad();
  }, [props.symbol]);

  // смена таймфрейма: остаёмся на выбранной сделке, а если её нет — на текущем участке времени
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const tr = propsRef.current.trades.find((x) => x.n === propsRef.current.selected);
    if (tr) {
      void focusTrade(tr);
      return;
    }
    const v = chart.current?.timeScale().getVisibleRange();
    if (v) void focus({ from: v.from as number, to: v.to as number });
    else void initialLoad();
  }, [props.tfMode]);

  // новые сделки / флаги отображения
  useEffect(() => {
    redraw();
  }, [props.trades, props.signals, props.showLevels, props.showSkipped]);

  // выбранная сделка: линии входа/TP/SL на шкале цены
  useEffect(() => {
    const c = candle.current;
    if (!c) return;
    priceLines.current.forEach((l) => c.removePriceLine(l));
    priceLines.current = [];
    const tr = props.trades.find((x) => x.n === props.selected);
    if (!tr) return;
    const mk = (price: number, color: string, title: string, style: LineStyle) =>
      c.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title });
    priceLines.current = [
      mk(tr.entry, '#cfd6e4', `вход #${tr.n}`, LineStyle.Solid),
      ...tr.tps.map((price, i) => mk(price, TP_COLORS[i], tr.tps.length > 1 ? `TP${i + 1} · ${Math.round(tr.tp_shares[i] * 100)}%` : 'TP', LineStyle.Dashed)),
      mk(tr.sl, COLORS.sl, 'SL', LineStyle.Dashed),
    ];
  }, [props.selected, props.trades]);

  // выбор сделки: приблизить к ней (в текущем ТФ)
  useEffect(() => {
    const tr = propsRef.current.trades.find((x) => x.n === propsRef.current.selected);
    if (tr) void focusTrade(tr);
  }, [props.focusKey]);

  return <div ref={host} className="chart-host" />;
}
