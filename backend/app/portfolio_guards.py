"""Защита портфеля: два правила, общие для расчёта портфеля (страница «Портфель») и для автоторговли (бот).

  * лимит занятой маржи (margin_cap_pct, % баланса): новый вход (и добор) не выполняется, если сумма маржи открытых позиций вместе с ним превысила бы этот процент баланса.
    В расчёте баланс — депозит плюс закрытые результаты портфеля на момент входа, на бирже — капитал счёта;
  * пауза стороны после серии убытков (side_pause): если в момент нового входа за последние window_h часов закрылось не меньше losses убыточных позиций той же стороны
    (лонг / шорт — по всему портфелю, по всем активам), новый вход пропускается, а эта сторона ставится на паузу на pause_h часов. Доборы к уже открытой позиции не блокируются.

Оба правила по умолчанию выключены, значения задаёт пользователь (страница «Портфель»)."""
HOUR_MS = 3_600_000
MARGIN_CAP_MAX = 1000.0
PAUSE_MAX_H = 24 * 30


def normalize(raw):
    """Правила из сохранённых настроек пользователя -> {"margin_cap_pct": число | None, "side_pause": {"losses", "window_h", "pause_h"} | None}. Мусор считается «выключено»."""
    raw = raw if isinstance(raw, dict) else {}
    cap = raw.get("margin_cap_pct")
    cap = float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and 0 < cap <= MARGIN_CAP_MAX else None
    sp = raw.get("side_pause")
    pause = None
    if isinstance(sp, dict):
        try:
            n, w, p = int(sp["losses"]), float(sp["window_h"]), float(sp["pause_h"])
            if n >= 1 and 0 < w <= PAUSE_MAX_H and 0 < p <= PAUSE_MAX_H:
                pause = {"losses": n, "window_h": w, "pause_h": p}
        except (KeyError, TypeError, ValueError):
            pause = None
    return {"margin_cap_pct": cap, "side_pause": pause}


def validate_cap(v):
    """Лимит маржи из запроса: число (0, 1000] или None (выключено). Иначе ValueError с понятным текстом."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= MARGIN_CAP_MAX:
        raise ValueError(f"Лимит занятой маржи — число больше 0 и не больше {MARGIN_CAP_MAX:g} (% баланса) или null (выключено)")
    return float(v)


def validate_side_pause(v):
    """Пауза стороны из запроса: {"losses": целое ≥ 1, "window_h": (0, 720], "pause_h": (0, 720]} или None (выключено)."""
    if v is None:
        return None
    msg = f"Пауза стороны: losses — целое от 1, window_h и pause_h — часы больше 0 и не больше {PAUSE_MAX_H}"
    if not isinstance(v, dict):
        raise ValueError(msg)
    try:
        n, w, p = v["losses"], v["window_h"], v["pause_h"]
    except KeyError:
        raise ValueError(msg)
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in (n, w, p)) or int(n) != n or n < 1 or not 0 < w <= PAUSE_MAX_H or not 0 < p <= PAUSE_MAX_H:
        raise ValueError(msg)
    return {"losses": int(n), "window_h": float(w), "pause_h": float(p)}


def active(guards):
    return bool(guards and (guards.get("margin_cap_pct") or guards.get("side_pause")))


def positions_closed(trades_by_key):
    """{ключ позиции: [ноги]} -> [(время закрытия, сторона, результат)] только для позиций, у которых все ноги закрыты (нет OPEN)."""
    out = []
    for legs in trades_by_key.values():
        if any(t["reason"] == "OPEN" for t in legs):
            continue
        out.append((max(t["exit_ts"] for t in legs), legs[0]["side"], sum(t["pnl_usd"] for t in legs)))
    return out


def losing_closes(closed, side, now_ms, window_h):
    """Сколько убыточных позиций стороны side закрылось за последние window_h часов до момента now_ms (включительно). closed: [(время закрытия мс, сторона, результат)]."""
    lo = now_ms - window_h * HOUR_MS
    return sum(1 for ts, s, pnl in closed if s == side and pnl < 0 and lo < ts <= now_ms)


class SidePause:
    """Пауза стороны в расчёте портфеля: allow() — можно ли открывать новую позицию стороны в момент ts; при срабатывании запоминает паузу."""

    def __init__(self, rule):
        self.rule, self.until, self.periods = rule, {}, []

    def allow(self, side, ts, closed):
        if ts < self.until.get(side, 0):
            return False
        if losing_closes(closed, side, ts, self.rule["window_h"]) >= self.rule["losses"]:
            end = ts + self.rule["pause_h"] * HOUR_MS
            self.until[side] = end
            self.periods.append({"side": side, "from_ts": int(ts), "until_ts": int(end)})
            return False
        return True
