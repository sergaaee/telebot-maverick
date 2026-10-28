"""Фандинг (funding) бессрочных контрактов в симуляции.

Задаётся годовой ставкой в процентах (funding_apr) и периодом списания в часах (funding_period_h: 1, 2, 4, 8, 12, 24). Ставка за период = APR / 100 × период / 8760.
Списание происходит в моменты, кратные периоду от 00:00 UTC (как на биржах: 00 / 08 / 16 при 8 часах), и только если позиция в этот момент открыта:
вход строго раньше момента, закрытие в момент или позже (позиция, закрытая в свече, которая началась в момент списания, ещё держалась на нём).
Платится доля ОБЪЁМА позиции, оставшаяся на этот момент (закрытые тейками части уже не платят; у усреднённой позиции — только уже вошедшие доборы).

Режимы (funding_mode):
  signed — по знаку ставки, как на бирже: при положительной ставке лонг платит, шорт получает; при отрицательной — наоборот;
  cost   — консервативно: платят обе стороны |ставку| (всегда расход).
Результат: у сделки поле funding_pct (% от объёма, + расход, − доход), и из pnl_pct вычитается он же; funding_usd (в долларах) считается после расчёта размера."""

HOURS_PER_YEAR = 8760.0
MODES = ("signed", "cost")


def period_rate(apr_pct, period_h):
    """Ставка за один период в долях (0.0001 = 0.01%)."""
    return (apr_pct or 0.0) / 100.0 * period_h / HOURS_PER_YEAR


def _frac_remaining(fills, t):
    """Доля позиции, остающаяся на момент t: закрытия строго раньше t уже не платят."""
    total = sum(f["share"] for f in fills) or 1.0
    done = sum(f["share"] for f in fills if f["ts"] < t)
    return max(0.0, 1.0 - done / total)


def _frac_entered(trade, t):
    """Доля итогового объёма, вошедшая к моменту t: у обычной ноги — 1, у усреднённой позиции — по уже выполненным доборам."""
    adds = trade.get("adds")
    if not trade.get("avg") or not adds:
        return 1.0
    total = trade.get("w_total") or sum(a["weight"] for a in adds) or 1.0
    return min(1.0, sum(a["weight"] for a in adds if a["ts"] < t) / total)


def trade_funding(trade, apr_pct, period_h, mode="signed"):
    """Фандинг сделки в долях объёма (+ расход, − доход)."""
    rate = period_rate(apr_pct, period_h)
    if not rate:
        return 0.0
    d = 1 if trade["side"] == "long" else -1
    sign, r = (d, rate) if mode == "signed" else (1, abs(rate))             # cost: всегда расход
    p = int(period_h * 3_600_000)
    entry, exit_ = trade["entry_ts"], trade["exit_ts"]
    t = (entry // p + 1) * p                           # первый момент списания строго после входа
    fills = trade.get("fills") or []
    cost = 0.0
    while t <= exit_:
        cost += sign * r * _frac_remaining(fills, t) * _frac_entered(trade, t)
        t += p
    return cost


def apply_funding(trades, apr_pct, period_h, mode="signed", notional=None):
    """Учитывает фандинг в сделках (на месте): pnl_pct уменьшается на funding_pct; notional (USD, если размер сделки известен — как у топ-коинов) — заодно pnl_usd и funding_usd.
    Для основной стратегии размер ещё не посчитан: funding_usd считается после apply_sizing (см. fill_funding_usd). apr = 0 — ничего не меняет."""
    if not apr_pct:
        return trades
    for t in trades:
        f = trade_funding(t, apr_pct, period_h, mode)
        t["funding_pct"] = 100.0 * f
        t["pnl_pct"] = t["pnl_pct"] - 100.0 * f
        if notional:
            t["funding_usd"] = f * notional
            t["pnl_usd"] = t["pnl_usd"] - f * notional
    return trades


def fill_funding_usd(trades):
    """После расчёта размера позиции: funding_usd = объём × funding_pct."""
    for t in trades:
        if "funding_pct" in t and t.get("notional") is not None:
            t["funding_usd"] = t["notional"] * t["funding_pct"] / 100.0
    return trades
