import { ColorType, createChart, CrosshairMode, LineType, type IChartApi, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts';
import { useEffect, useRef, useState } from 'react';
import { fmtTs } from '../format';

export interface CurveLine { name: string; color: string; points: [number, number][]; bold?: boolean }
interface Tip { x: number; time: number; items: { name: string; color: string; y: number; text: string; neg: boolean; bold: boolean }[] }

export const PALETTE = ['#4f8cff', '#e0a030', '#2fbf71', '#c26cff', '#ff6b6b', '#40c4d8', '#b0b84a', '#ff8fb1'];
const money = (v: number) => `${v >= 0 ? '+' : '−'}${Math.round(Math.abs(v)).toLocaleString('ru-RU')} $`;

/** Значение ступенчатой кривой на момент t (секунды): последняя точка не позже t; до первой точки — нет значения. */
function valueAt(points: [number, number][], t: number): number | null {
  let lo = 0;
  let hi = points.length - 1;
  let ans = -1;
  while (lo <= hi) {
    const m = (lo + hi) >> 1;
    if (points[m][0] / 1000 <= t) { ans = m; lo = m + 1; } else hi = m - 1;
  }
  return ans < 0 ? null : points[ans][1];
}

/** Кривые прибыли стратегий и портфеля: график двигается и масштабируется мышью, при наведении над каждой кривой подписано её состояние ($) на этот момент. */
export default function PortfolioChart({ lines }: { lines: CurveLine[] }) {
  const host = useRef<HTMLDivElement>(null);
  const range = useRef<{ from: number; to: number } | null>(null);
  const [tip, setTip] = useState<Tip | null>(null);

  useEffect(() => {
    const el = host.current!;
    const ch: IChartApi = createChart(el, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#0f131a' }, textColor: '#9aa4b5', fontSize: 11 },
      grid: { vertLines: { color: '#1a212c' }, horzLines: { color: '#1a212c' } },
      rightPriceScale: { borderColor: '#262e3b' },
      timeScale: { borderColor: '#262e3b', timeVisible: false },
      crosshair: { mode: CrosshairMode.Magnet },
      handleScroll: true,
      handleScale: true,
    });
    const series: { line: CurveLine; s: ISeriesApi<'Line'> }[] = lines.filter((l) => l.points.length).map((line) => {
      const s = ch.addLineSeries({
        color: line.color, lineWidth: line.bold ? 3 : 2, lineType: LineType.WithSteps, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
        priceFormat: { type: 'custom', formatter: (v: number) => `${Math.round(v).toLocaleString('ru-RU')} $` },
      });
      let last = -Infinity;
      s.setData(line.points.map(([ms, v]) => {
        const t = Math.max(Math.floor(ms / 1000), last + 1);
        last = t;
        return { time: t as UTCTimestamp, value: v };
      }));
      return { line, s };
    });
    if (series[0]) series[0].s.createPriceLine({ price: 0, color: '#3a4558', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
    if (range.current) ch.timeScale().setVisibleRange({ from: range.current.from as UTCTimestamp, to: range.current.to as UTCTimestamp });
    else ch.timeScale().fitContent();
    const onRange = () => {
      const r = ch.timeScale().getVisibleRange();
      if (r) range.current = { from: r.from as number, to: r.to as number };
    };
    ch.timeScale().subscribeVisibleTimeRangeChange(onRange);
    ch.subscribeCrosshairMove((param) => {
      if (!param.point || param.point.x < 0 || param.point.y < 0) { setTip(null); return; }
      const raw = ch.timeScale().coordinateToTime(param.point.x) ?? param.time;
      const t = typeof raw === 'number' ? raw : null;
      if (t == null) { setTip(null); return; }
      const items = series.flatMap(({ line, s }) => {
        const v = valueAt(line.points, t);
        const y = v == null ? null : s.priceToCoordinate(v);
        return v == null || y == null ? [] : [{ name: line.name, color: line.color, y: y as number, text: money(v), neg: v < 0, bold: !!line.bold }];
      });
      items.sort((a, b) => a.y - b.y);
      for (let i = 1; i < items.length; i += 1) if (items[i].y - items[i - 1].y < 17) items[i].y = items[i - 1].y + 17;     // подписи не налезают друг на друга
      setTip(items.length ? { x: param.point.x, time: t, items } : null);
    });
    return () => { ch.timeScale().unsubscribeVisibleTimeRangeChange(onRange); ch.remove(); };
  }, [lines]);

  const w = host.current?.clientWidth ?? 0;
  const flip = tip ? tip.x > w - 230 : false;
  return (
    <div className="pf-chart-wrap">
      <div ref={host} className="pf-canvas" data-testid="pf-chart" />
      {tip && (
        <div className="pf-tips" aria-hidden="true">
          <div className="pf-tip-date" style={{ left: Math.min(Math.max(tip.x - 40, 4), Math.max(w - 100, 4)) }}>{fmtTs(tip.time * 1000).slice(0, 16)}</div>
          {tip.items.map((it) => (
            <div key={it.name} className={`pf-tip ${it.bold ? 'bold' : ''}`} data-testid="pf-tip" style={{ top: it.y - 9, [flip ? 'right' : 'left']: flip ? w - tip.x + 12 : tip.x + 12, borderColor: it.color }}>
              <i style={{ background: it.color }} />{it.name}: <b className={it.neg ? 'neg' : 'pos'}>{it.text}</b>
            </div>
          ))}
        </div>
      )}
      <div className="pf-legend small">{lines.map((l) => <span key={l.name}><i style={{ background: l.color }} />{l.name}</span>)}</div>
      <div className="muted small">Колесо — масштаб, перетаскивание — сдвиг, двойной щелчок по шкале — сбросить. Подписи у кривых — результат на выбранный момент.</div>
    </div>
  );
}
