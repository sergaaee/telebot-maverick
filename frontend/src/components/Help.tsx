import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';

/** Значок «?» с подсказкой по наведению (и по фокусу с клавиатуры). Подсказка рисуется поверх всего через портал,
 *  поэтому не обрезается прокручиваемыми панелями. */
export default function Help({ text }: { text: string }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [pos, setPos] = useState<{ left?: number; right?: number; top: number } | null>(null);

  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const W = 320;
    const top = Math.max(8, Math.min(r.top - 6, window.innerHeight - 280));
    // справа от значка, а если не помещается — слева
    if (r.right + 10 + W < window.innerWidth) setPos({ left: r.right + 10, top });
    else setPos({ right: window.innerWidth - r.left + 10, top });
  };

  return (
    <>
      <span
        ref={ref}
        className="help"
        tabIndex={0}
        role="img"
        aria-label="Подсказка"
        onMouseEnter={show}
        onMouseLeave={() => setPos(null)}
        onFocus={show}
        onBlur={() => setPos(null)}
        onClick={(e) => e.preventDefault()}
      >
        ?
      </span>
      {pos && createPortal(<div className="tooltip" role="tooltip" style={pos}>{text}</div>, document.body)}
    </>
  );
}
