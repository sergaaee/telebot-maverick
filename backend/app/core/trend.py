"""Фильтр «торговля только по тренду»: лонг — цена выше всех выбранных EMA, шорт — ниже всех.

EMA считается по закрытым свечам таймфрейма tf (минутки агрегируются). На момент сигнала берётся последняя уже
закрытая свеча: данных из будущего нет. Пока свечей меньше периода EMA, тренд неизвестен и сделка не открывается."""
from bisect import bisect_right

from .backtest import tf_to_ms


def build_trend(candles, tf, periods):
    """-> функция allow(ts_мс, d), d=+1 лонг / -1 шорт: можно ли входить по тренду."""
    step = tf_to_ms(tf)
    starts, closes = [], []
    for t, _o, _h, _l, c in candles:
        b = t // step * step
        if starts and starts[-1] == b:
            closes[-1] = c
        else:
            starts.append(b)
            closes.append(c)
    ema = {}
    for p in periods:
        k, out, v = 2 / (p + 1), [], None
        for c in closes:
            v = c if v is None else v + k * (c - v)
            out.append(v)
        ema[p] = out
    warm = max(periods)
    ends = [b + step for b in starts]

    def allow(ts, d):
        i = bisect_right(ends, ts) - 1            # последняя свеча, закрывшаяся не позже сигнала
        if i < warm:
            return False
        c = closes[i]
        return all(c > ema[p][i] for p in periods) if d == 1 else all(c < ema[p][i] for p in periods)

    return allow
