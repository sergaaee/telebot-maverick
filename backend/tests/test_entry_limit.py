"""Вход лимитным ордером на X % движения цены вместо входа по рынку: откат / по ходу, срок жизни ордера, отмена, тейки в свече исполнения."""
from app.core import backtest
from app.core.backtest import LIMIT_PENDING, normalize_tps, run_events

M = 60_000
LV = normalize_tps([(2.0, 100)])                 # TP +2%
SL = 1.0


def cs(rows):
    """rows: [(open, high, low)] по минутам; close = open."""
    return [(i * M, o, h, l, o) for i, (o, h, l) in enumerate(rows)]


def run(rows, events, pct=0.5, mode="pullback", ttl_h=1.0, slip=0.0, **kw):
    skipped = []
    tr = run_events(events, cs(rows), LV, SL, fee=0.0, slip=slip, skipped=skipped, entry_limit={"pct": pct, "dir": mode, "ttl_h": ttl_h}, **kw)
    return tr, skipped


FLAT = (100.0, 100.2, 99.9)


def test_pullback_long_fills_at_level_when_price_comes_back():
    tr, sk = run([FLAT, (99.9, 100.0, 99.4), (99.6, 101.6, 99.5), (101, 101, 101)], [(0, 1)])
    t = tr[0]
    assert len(tr) == 1 and not sk and abs(t["entry"] - 99.5) < 1e-9 and t["entry_ts"] == 1 * M and t["signal_ts"] == 0       # уровень 100 × (1 − 0.5%) = 99.5, исполнен на второй свече
    assert t["reason"] == "TP" and abs(t["limit"]["level"] - 99.5) < 1e-9 and abs(t["limit"]["wait_h"] - 1 / 60) < 1e-9 and t["limit"]["dir"] == "pullback"
    assert abs(t["tp"] - 99.5 * 1.02) < 1e-9                                                                                   # тейк и стоп — от цены исполнения, а не от цены сигнала


def test_gap_through_the_level_fills_at_the_better_open_price():
    tr, _ = run([FLAT, (99.0, 99.2, 98.9), (99, 99, 99)], [(0, 1)])
    assert abs(tr[0]["entry"] - 99.0) < 1e-9


def test_pullback_short_sits_above_the_signal_price():
    tr, _ = run([FLAT, (100.3, 100.6, 100.2), (100, 100, 100)], [(0, -1)])
    assert abs(tr[0]["entry"] - 100.5) < 1e-9 and tr[0]["side"] == "short"


def test_order_that_never_fills_is_skipped_after_ttl():
    rows = [FLAT] + [(100.0, 100.3, 99.8)] * 10                                                                                # откат до 99.5 не случился
    tr, sk = run(rows, [(0, 1)], ttl_h=3 / 60)                                                                                # живёт 3 минуты
    assert tr == [] and sk and "лимитка не исполнилась за 0.05 ч" in sk[0][2]
    tr2, sk2 = run(rows, [(0, 1)], ttl_h=1.0)                                                                                  # данные кончились раньше срока — ордер ещё ждёт
    assert tr2 == [] and sk2[0][2] == LIMIT_PENDING


def test_takes_are_not_counted_in_the_fill_candle_but_the_stop_is():
    tr, _ = run([FLAT, (99.9, 101.6, 99.4), (99.9, 100.1, 99.7), (99.8, 99.9, 99.7)], [(0, 1)])
    t = tr[0]
    assert t["reason"] == "OPEN" and t["tp_hits"] == 0                                                                         # high свечи исполнения (101.6) мог быть ДО касания уровня — тейк не засчитан
    tr2, _ = run([FLAT, (99.9, 100.0, 98.4), (99, 99, 99)], [(0, 1)])                                                           # а стоп в этой свече засчитывается (худший случай)
    assert tr2[0]["reason"] == "SL" and tr2[0]["exit_ts"] == 1 * M and abs(tr2[0]["exit"] - 99.5 * 0.99) < 1e-9


def test_breakout_enters_with_the_move_and_pays_slippage():
    tr, _ = run([FLAT, (100.2, 100.7, 100.1), (100.6, 100.6, 100.6)], [(0, 1)], mode="breakout", slip=0.001)
    assert abs(tr[0]["entry"] - 100.5 * 1.001) < 1e-9 and tr[0]["entry_ts"] == 1 * M and tr[0]["limit"]["dir"] == "breakout"
    tr2, _ = run([FLAT, (99.8, 99.9, 99.4), (99.5, 99.5, 99.5)], [(0, -1)], mode="breakout", slip=0.0)                         # шорт по ходу — ниже цены сигнала
    assert abs(tr2[0]["entry"] - 99.5) < 1e-9
    tr3, _ = run([FLAT, (101.0, 101.2, 100.9), (101, 101, 101)], [(0, 1)], mode="breakout")                                    # гэп вверх — исполнение по open (хуже уровня)
    assert abs(tr3[0]["entry"] - 101.0) < 1e-9


def test_repeated_signal_is_ignored_and_opposite_cancels_a_standing_order():
    rows = [FLAT, FLAT, FLAT, (99.9, 100.0, 99.4), (99.6, 99.7, 99.5), (99.6, 99.7, 99.5)]
    tr, sk = run(rows, [(0, 1), (1 * M, 1)])
    assert len(tr) == 1 and tr[0]["signal_ts"] == 0 and any(r == "лимитка уже стоит" for _, _, r in sk)                           # второй такой же сигнал, пока ордер стоит, ничего не меняет
    tr2, sk2 = run(rows, [(0, 1), (1 * M, -1)], pct=0.5)
    assert all(t["side"] == "short" or t["signal_ts"] != 0 for t in tr2)                                                       # лонговая лимитка отменена обратным сигналом
    assert any("отменена: пришёл обратный сигнал" in r for _, _, r in sk2) and not any(t["side"] == "long" for t in tr2)
    assert all("_i" in t for t in tr2)


def test_cancelled_order_does_not_leave_cooldown_or_gap_state_behind():
    rows = [FLAT, FLAT, FLAT, FLAT, (100.0, 100.0, 99.0)]
    # лонговая лимитка (0.5% вниз) исполнится бы на последней свече и сразу уйти в стоп; обратный сигнал её отменил -> паузы после стопа быть не должно
    tr, sk = run(rows, [(0, 1), (1 * M, -1), (2 * M, -1)], cooldown_h=5.0)
    assert not any(r == "пауза после стопа" for _, _, r in sk)


def test_without_limit_behaviour_is_unchanged():
    rows = [FLAT, (99.9, 102.5, 99.95), (100, 100, 100)]
    a = run_events([(0, 1)], cs(rows), LV, SL, fee=0.0)
    b = run_events([(0, 1)], cs(rows), LV, SL, fee=0.0, entry_limit={"pct": 0, "dir": "pullback", "ttl_h": 4})
    assert a[0]["entry"] == b[0]["entry"] == 100.0 and a[0]["reason"] == b[0]["reason"] == "TP" and "limit" not in a[0] and "limit" not in b[0]       # 0% — обычный вход по рынку


def test_limit_entry_with_averaging_position_uses_fill_price_and_skips_takes_in_fill_candle():
    pyr = {"max": 2, "gap_h": 0.0, "move_pct": 0.0, "move_dir": "any", "avg": True, "w0": 100.0, "w": 100.0}
    tr, _ = run([FLAT, (99.9, 101.6, 99.4), (99.9, 100.1, 99.7), (99.8, 99.9, 99.7)], [(0, 1)], pyramid=pyr)
    t = tr[0]
    assert t["avg"] and abs(t["entry"] - 99.5) < 1e-9 and t["entry_ts"] == 1 * M and t["tp_hits"] == 0 and t["limit"]["wait_h"] > 0


def test_service_reports_limit_fills_and_skip_reasons(tmp_path):
    from app.config import load_settings
    from app.schemas import BacktestParams
    from app.service import run_backtest
    from test_runcache import FakeStore, make
    sigs, candles = make(seed=3, n_rep=3000)
    base = {"top_enabled": False, "side": "both", "confirm": 1, "short_local_max": 1.1, "short_scalp_max": 0.7, "sl_pct": 0.5, "tps": [{"pct": 0.5, "share": 100}]}
    d = load_settings().defaults
    mk = run_backtest(FakeStore(tmp_path / "m", sigs, candles), d, BacktestParams(**base), now_ms=0)
    lm = run_backtest(FakeStore(tmp_path / "l", sigs, candles), d, BacktestParams(**base, entry_limit_pct=0.3, entry_limit_ttl_h=1.0), now_ms=0)
    assert all("limit" not in t for t in mk["trades"])
    filled = [t for t in lm["trades"] if "limit" in t]
    assert filled and all(t["entry_ts"] >= t["signal_ts"] and t["limit"]["wait_h"] >= 0 for t in filled)
    assert any("лимитка не исполнилась" in k for k in lm["skipped_reasons"])                                  # часть ордеров не дошла до уровня за час
    assert len(lm["trades"]) < len(mk["trades"])
    # лонг: цена исполнения не выше цены сигнала (лимитка на откат), шорт — не ниже
    assert all((t["entry"] <= t["limit"]["ref"] + 1e-9) if t["side"] == "long" else (t["entry"] >= t["limit"]["ref"] - 1e-9) for t in filled)
