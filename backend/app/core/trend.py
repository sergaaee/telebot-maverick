"""Фильтр «торговля только по тренду»: лонг — цена выше всех выбранных EMA, шорт — ниже всех.

EMA считается по закрытым свечам таймфрейма tf (минутки агрегируются). На момент сигнала берётся последняя уже
закрытая свеча: данных из будущего нет. Пока свечей меньше периода EMA, тренд неизвестен и сделка не открывается."""
from bisect import bisect_right

from .backtest import tf_to_ms


def build_trend(candles, tf, periods, state=None):
    """-> функция allow(ts_мс, d), d=+1 лонг / -1 шорт: можно ли входить по тренду.
    state — необязательный словарь для докладывания новых свечей: на выходе в нём лежит снимок (сколько свечей обработано, свечи таймфрейма и EMA);
    если на входе в нём уже есть снимок тех же tf/periods (и свечи до state["n"] не менялись — это проверяет вызывающий), пересчитываются только новые свечи
    и последняя, возможно незакрытая, свеча таймфрейма. Результат тот же, что при расчёте с нуля."""
    step = tf_to_ms(tf)
    periods = list(periods)
    starts, closes, ema, begin = [], [], {p: [] for p in periods}, 0
    if state and state.get("n") and state.get("tf") == tf and state.get("periods") == periods and 0 < state["n"] <= len(candles):
        starts, closes = list(state["starts"]), list(state["closes"])
        ema = {p: list(v) for p, v in state["ema"].items()}
        if starts:
            last = starts.pop()                      # последняя свеча таймфрейма могла быть неполной — считаем её заново
            closes.pop()
            for p in periods:
                ema[p].pop()
            begin = state["n"]
            while begin > 0 and candles[begin - 1][0] >= last:
                begin -= 1
    n_done = len(candles)
    for t, _o, _h, _l, c in candles[begin:]:
        b = t // step * step
        if starts and starts[-1] == b:
            closes[-1] = c
        else:
            starts.append(b)
            closes.append(c)
    for p in periods:
        k, out = 2 / (p + 1), ema[p]
        v = out[-1] if out else None
        for c in closes[len(out):]:
            v = c if v is None else v + k * (c - v)
            out.append(v)
    warm = max(periods)
    ends = [b + step for b in starts]

    def allow(ts, d):
        i = bisect_right(ends, ts) - 1            # последняя свеча, закрывшаяся не позже сигнала
        if i < warm:
            return False
        c = closes[i]
        return all(c > ema[p][i] for p in periods) if d == 1 else all(c < ema[p][i] for p in periods)

    if state is not None:
        state.clear()
        state.update(n=n_done, tf=tf, periods=periods, starts=starts, closes=closes, ema=ema)
    return allow
