import { ColorType, createChart, type UTCTimestamp } from 'lightweight-charts';
import { useEffect, useRef } from 'react';

/** Кривая капитала: накопленный результат в $ по закрытым сделкам. */
export default function EquityChart({ equity, splitTs }: { equity: [number, number][]; splitTs: number | null }) {
  const host = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const ch = createChart(host.current!, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: '#0f131a' }, textColor: '#9aa4b5', fontSize: 11 },
      grid: { vertLines: { color: '#1a212c' }, horzLines: { color: '#1a212c' } },
      rightPriceScale: { borderColor: '#262e3b' },
      timeScale: { borderColor: '#262e3b', timeVisible: false },
    });
    const s = ch.addAreaSeries({
      lineColor: '#4f8cff', topColor: 'rgba(79,140,255,0.35)', bottomColor: 'rgba(79,140,255,0.02)', lineWidth: 2,
      priceFormat: { type: 'custom', formatter: (v: number) => `${Math.round(v)} $` },
    });
    let last = -Infinity;
    s.setData(
      equity.map(([ms, v]) => {
        const t = Math.max(Math.floor(ms / 1000), last + 1);
        last = t;
        return { time: t as UTCTimestamp, value: v };
      }),
    );
    if (splitTs != null) {
      s.createPriceLine({ price: 0, color: '#3a4558', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
    }
    ch.timeScale().fitContent();
    return () => ch.remove();
  }, [equity, splitTs]);

  return <div ref={host} className="chart-host" />;
}
