import { fmtTs } from '../format';

export interface Line { name: string; color: string; points: [number, number][]; bold?: boolean }

const W = 640;
const H = 200;
export const PALETTE = ['#4f8cff', '#e0a030', '#2fbf71', '#c26cff', '#ff6b6b', '#40c4d8', '#b0b84a', '#ff8fb1'];

/** Простой график нескольких линий (время по X, значение по Y) на SVG; нулевая линия отмечена. */
export default function MiniLines({ lines, unit, testid }: { lines: Line[]; unit: string; testid?: string }) {
  const pts = lines.flatMap((l) => l.points);
  if (pts.length < 2) return <div className="empty small">Нет закрытых сделок для графика</div>;
  const t0 = Math.min(...pts.map((p) => p[0]));
  const t1 = Math.max(...pts.map((p) => p[0]));
  const lo = Math.min(0, ...pts.map((p) => p[1]));
  const hi = Math.max(0, ...pts.map((p) => p[1]));
  const x = (t: number) => ((t - t0) / (t1 - t0 || 1)) * (W - 8) + 4;
  const y = (v: number) => H - 14 - ((v - lo) / (hi - lo || 1)) * (H - 24);
  return (
    <div data-testid={testid}>
      <svg viewBox={`0 0 ${W} ${H}`} className="pf-chart" preserveAspectRatio="none" role="img" aria-label="Кривые капитала">
        <line x1="0" x2={W} y1={y(0)} y2={y(0)} stroke="#3a4558" strokeDasharray="4 4" />
        {lines.map((l) => (
          <path key={l.name} fill="none" stroke={l.color} strokeWidth={l.bold ? 2.6 : 1.4} opacity={l.bold ? 1 : 0.85} vectorEffect="non-scaling-stroke"
            d={l.points.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(' ')} />
        ))}
      </svg>
      <div className="pf-axis muted small"><span>{fmtTs(t0).slice(0, 10)}</span><span>от {lo.toFixed(1)}{unit} до {hi.toFixed(1)}{unit}</span><span>{fmtTs(t1).slice(0, 10)}</span></div>
      <div className="pf-legend small">{lines.map((l) => <span key={l.name}><i style={{ background: l.color }} />{l.name}</span>)}</div>
    </div>
  );
}
