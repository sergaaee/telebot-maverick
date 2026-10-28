"""Симуляция фандинга: годовая ставка, период списания, режимы, учёт частичных закрытий и усреднённой позиции, во всех режимах расчёта прибыли."""
import pytest

from app.config import load_settings
from app.core import funding, topcoins
from app.schemas import BacktestParams
from app.service import run_backtest
from test_portfolio import build_portfolio, srow, leg
from test_runcache import FakeStore, make
from test_topcoins import _build, _candles, _p, DAY, M, T0

H = 3_600_000
APR_1BP = 10.95                                              # 10.95% годовых = 0.01% за 8 часов


def tr(side, entry_h, exit_h, fills=None, **kw):
    t = {"side": side, "entry_ts": int(entry_h * H), "exit_ts": int(exit_h * H), "pnl_pct": 1.0, "fills": fills or [{"ts": int(exit_h * H), "price": 1.0, "share": 1.0}]}
    return t | kw


def test_period_rate_and_signed_costs():
    assert abs(funding.period_rate(APR_1BP, 8) - 0.0001) < 1e-12 and abs(funding.period_rate(APR_1BP, 1) - 0.0001 / 8) < 1e-12
    long = tr("long", 1, 33)                                   # с 01:00 до 09:00 следующих суток: списания в 8, 16, 24, 32 часа -> 4 раза
    short = tr("short", 1, 33)
    assert abs(funding.trade_funding(long, APR_1BP, 8) - 0.0004) < 1e-12                    # лонг платит при положительной ставке
    assert abs(funding.trade_funding(short, APR_1BP, 8) + 0.0004) < 1e-12                   # шорт получает
    assert abs(funding.trade_funding(long, -APR_1BP, 8) + 0.0004) < 1e-12                   # отрицательная ставка — наоборот
    assert abs(funding.trade_funding(short, APR_1BP, 8, "cost") - 0.0004) < 1e-12           # «платят обе стороны» — всегда расход
    assert abs(funding.trade_funding(short, -APR_1BP, 8, "cost") - 0.0004) < 1e-12
    assert funding.trade_funding(long, 0, 8) == 0.0 and funding.trade_funding(long, None, 8) == 0.0
    assert abs(funding.trade_funding(long, APR_1BP, 1) - 32 * 0.0001 / 8) < 1e-12           # 32 часа по часовому периоду: та же ставка в годовых


def test_settlement_boundaries_and_partial_closes():
    assert funding.trade_funding(tr("long", 8, 15.99), APR_1BP, 8) == 0.0                   # вход ровно в момент списания его не оплачивает
    assert abs(funding.trade_funding(tr("long", 1, 16), APR_1BP, 8) - 0.0002) < 1e-12       # закрытие ровно в момент списания — ещё платит
    assert funding.trade_funding(tr("long", 1, 7.9), APR_1BP, 8) == 0.0                     # между списаниями — ничего
    # половина позиции закрыта тейком в 12:00: на 16:00 и 24:00 платит только оставшаяся половина
    fills = [{"ts": 12 * H, "price": 1.0, "share": 0.5}, {"ts": 26 * H, "price": 1.0, "share": 0.5}]
    assert abs(funding.trade_funding(tr("long", 1, 26, fills), APR_1BP, 8) - (0.0001 + 0.5 * 0.0001 + 0.5 * 0.0001)) < 1e-12      # 8:00 целиком, 16:00 и 24:00 по половине
    # закрытие внутри свечи списания (fill в момент 16:00) не освобождает от списания
    fills2 = [{"ts": 16 * H, "price": 1.0, "share": 1.0}]
    assert abs(funding.trade_funding(tr("long", 1, 16, fills2), APR_1BP, 8) - 0.0002) < 1e-12


def test_averaged_position_pays_only_for_size_already_entered():
    adds = [{"ts": 1 * H, "price": 1.0, "weight": 100.0, "signal_ts": None}, {"ts": 10 * H, "price": 1.0, "weight": 100.0, "signal_ts": 5}]
    t = tr("long", 1, 26, avg=True, adds=adds, w_total=200.0)
    # 8:00 — вошла половина, 16:00 и 24:00 — всё
    assert abs(funding.trade_funding(t, APR_1BP, 8) - (0.5 * 0.0001 + 0.0001 + 0.0001)) < 1e-12


def test_apply_funding_changes_pnl_and_fills_usd():
    trades = [tr("long", 1, 33, pnl_usd=10.0)]
    funding.apply_funding(trades, APR_1BP, 8, "signed", notional=1000.0)
    t = trades[0]
    assert abs(t["funding_pct"] - 0.04) < 1e-9 and abs(t["pnl_pct"] - (1.0 - 0.04)) < 1e-9 and abs(t["funding_usd"] - 0.4) < 1e-9 and abs(t["pnl_usd"] - 9.6) < 1e-9
    off = [tr("long", 1, 33)]
    funding.apply_funding(off, 0, 8)
    assert "funding_pct" not in off[0] and off[0]["pnl_pct"] == 1.0                           # выключено — сделки не тронуты
    sized = [{**tr("long", 1, 33), "funding_pct": 0.04, "notional": 5000.0}]
    funding.fill_funding_usd(sized)
    assert abs(sized[0]["funding_usd"] - 2.0) < 1e-9


BASE = {"top_enabled": False, "side": "both", "confirm": 1, "short_local_max": 1.1, "short_scalp_max": 0.7, "sl_pct": 0.5, "tps": [{"pct": 0.5, "share": 100}], "leverage": 3, "trade_pct": 20}


def run(tmp_path, **kw):
    sigs, candles = make(seed=3, n_rep=3000)
    return run_backtest(FakeStore(tmp_path, sigs, candles), load_settings().defaults, BacktestParams(**{**BASE, **kw}), now_ms=0)


def test_backtest_includes_funding_in_trades_stats_and_equity(tmp_path):
    off = run(tmp_path / "a")
    on = run(tmp_path / "b", funding_apr=50, funding_period_h=8)
    assert [t["entry_ts"] for t in on["trades"]] == [t["entry_ts"] for t in off["trades"]]            # сами сделки (входы, выходы) не меняются
    assert all("funding_usd" not in t for t in off["trades"]) and off["stats"]["total"]["funding_usd"] == 0
    fees = sum(t["funding_usd"] for t in on["trades"] if "funding_usd" in t)
    assert fees != 0 and abs(on["stats"]["total"]["funding_usd"] - fees) < 1e-6
    # итог уменьшился ровно на фандинг (размеры сделок без сложного процента не зависят от результата, считаем без компаундинга)
    off2, on2 = run(tmp_path / "c", compound=False), run(tmp_path / "d", compound=False, funding_apr=50)
    assert abs((off2["stats"]["total"]["total_usd"] - on2["stats"]["total"]["total_usd"]) - on2["stats"]["total"]["funding_usd"]) < 1e-6
    assert on2["equity"][-1][1] < off2["equity"][-1][1]
    # лонги платят, шорты получают при положительной ставке
    longs = [t["funding_usd"] for t in on2["trades"] if t["side"] == "long" and t.get("funding_usd")]
    shorts = [t["funding_usd"] for t in on2["trades"] if t["side"] == "short" and t.get("funding_usd")]
    assert longs and shorts and sum(longs) > 0 and sum(shorts) < 0
    cost = run(tmp_path / "e", compound=False, funding_apr=50, funding_mode="cost")
    assert all(t["funding_usd"] >= 0 for t in cost["trades"] if "funding_usd" in t)                     # режим «платят обе стороны»
    # период меняет только сетку списаний: годовая ставка та же, оба режима дают расход (на коротких сделках результат по сетке 8 часов «рваный»: платят только пересёкшие 00/08/16)
    h1, h8 = run(tmp_path / "f", compound=False, funding_apr=50, funding_period_h=1), run(tmp_path / "g", compound=False, funding_apr=50, funding_period_h=8)
    assert h1["stats"]["total"]["funding_usd"] != h8["stats"]["total"]["funding_usd"] and h1["trades"][0]["funding_pct"] >= 0 and h8["trades"][0]["funding_pct"] >= 0


def test_funding_works_with_pyramid_avg_and_changes_nothing_when_zero(tmp_path):
    pyr = dict(pyramid_enabled=True, pyramid_max=2, pyramid_pct=10, pyramid_avg=True, trade_pct=20)
    a, b = run(tmp_path / "a", **pyr), run(tmp_path / "b", funding_apr=80, **pyr)
    assert abs(b["stats"]["total"]["funding_usd"]) > 0 and b["stats"]["total"]["total_usd"] != a["stats"]["total"]["total_usd"]
    z = run(tmp_path / "c", funding_apr=0, **pyr)
    assert z["stats"]["total"]["total_usd"] == a["stats"]["total"]["total_usd"]
    leg_mode = run(tmp_path / "d", funding_apr=80, pyramid_enabled=True, pyramid_max=2, pyramid_pct=10, trade_pct=20)
    assert abs(leg_mode["stats"]["total"]["funding_usd"]) > 0


def test_funding_in_top_coin_trades_uses_same_settings():
    sym = "TIA/USDT:USDT"
    candles = {sym: _candles(T0, 3 * 1440 + 10)}
    covered = {sym: [(T0, T0 + (3 * 1440 + 9) * M)]}
    trig = [{"ts": T0 + 5 * M, "d": 1, "coin": "TIA-USD", "value": 10.0}]
    base = _build(trig, covered, candles, _p(top_tp=None))["trades"][0]
    with_f = _build(trig, covered, candles, _p(funding_apr=730.0, funding_period_h=8, funding_mode="signed"))["trades"][0]
    assert "funding_pct" not in base and with_f["funding_pct"] > 0 and abs(with_f["funding_usd"] - with_f["funding_pct"] / 100 * 100.0) < 1e-9       # размер сделки по монете 100 $
    assert abs((base["pnl_usd"] - with_f["pnl_usd"]) - with_f["funding_usd"]) < 1e-9


def test_portfolio_resizing_keeps_funding():
    t = {**leg(0, 24, 5.0), "funding_pct": 0.1}
    r = build_portfolio([srow(1, "A", [t], deposit=1000.0, trade_pct=20.0, leverage=2.0)], total_deposit=2000.0)
    assert abs(r["items"][0]["total_usd"] - 2000 * 0.20 * 2 * 0.05) < 1e-9                  # pnl_pct у сделки уже содержит фандинг — размер пересчитывается от него


def test_funding_params_are_validated_and_old_configs_have_it_off():
    from pydantic import ValidationError
    from app.legacy_off import LEGACY_OFF
    with pytest.raises(ValidationError):
        BacktestParams(funding_period_h=3)
    with pytest.raises(ValidationError):
        BacktestParams(funding_apr=5000)
    with pytest.raises(ValidationError):
        BacktestParams(funding_mode="both")
    assert LEGACY_OFF["funding_apr"] == 0.0 and LEGACY_OFF["funding_period_h"] == 8
    d = load_settings().defaults
    assert d["funding_apr"] == 0.0 and d["funding_period_h"] == 8 and d["funding_mode"] == "signed"          # по умолчанию выключен (FUNDING_APR в .env)
