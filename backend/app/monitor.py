"""Мониторинг открытых позиций: что сейчас «держит» каждая стратегия по расчёту бэктеста (свечи и отчёты Binance), со всеми уровнями и цифрами.

Это РАСЧЁТНАЯ позиция стратегии, а не позиция на бирже: пока нет коннектора к бирже для торговли, реальные позиции здесь не показываются."""
import json
import threading
import time

from .core import tradingweek
from .legacy_off import LEGACY_OFF
from .schemas import BacktestParams
from .service import ParamError, run_backtest

_cache, _lock = {}, threading.Lock()
TTL = 20.0                                              # секунд: страница опрашивает раз в ~30 с, повторные расчёты одинаковых параметров не нужны


def _view(res, price, latest, candle_ts, name, config_id, auto, paused):
    p = res["params"]
    legs = sorted([t for t in res["trades"] if t["reason"] == "OPEN"], key=lambda t: (t["entry_ts"], t.get("leg", 1)))
    out = {"name": name, "config_id": config_id, "symbol": res["symbol"], "source": res["source"]["label"], "auto": auto, "paused": paused,
           "price": price, "candle_ts": candle_ts, "report": latest, "ready_now": res.get("ready_now", []), "position": None,
           "recent": [{"side": t["side"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"], "reason": t["reason"], "pnl_usd": t["pnl_usd"], "pnl_pct": t["pnl_pct"],
                       "hold_h": t["hold_h"]} for t in [t for t in res["trades"] if t["reason"] != "OPEN"][-5:]][::-1],
           "settings": {"sl_pct": p["sl_pct"], "tps": p["tps"], "be_after_tp1": p["be_after_tp1"], "leverage": p["leverage"], "pyramid_leverage": p.get("pyramid_leverage"),
                        "trade_pct": p["trade_pct"], "pyramid_enabled": p["pyramid_enabled"], "deposit": p["deposit"], "liquidation_sim": p.get("liquidation_sim", False),
                        "weekdays_only": p.get("weekdays_only", False), "side": p["side"]}}
    if not legs:
        return out
    side = legs[0]["side"]
    d = 1 if side == "long" else -1
    mmr = (p.get("maint_margin_pct") or 0.5) / 100
    rows = []
    for t in legs:
        be_on = bool(t["be"] and t["tp_hits"] >= 1)
        stop = t["entry"] if be_on else t["sl"]
        lev = (t["notional"] / t["margin"]) if t.get("margin") else p["leverage"]
        nxt = next((x for i, x in enumerate(t["tps"], 1) if i > t["tp_hits"]), None)
        rows.append({
            "leg": t.get("leg", 1), "kind": "усреднённая" if t.get("avg") else ("добор" if t.get("pyramid") else "вход"), "adds_n": t.get("adds_n", 0),
            "entry_ts": t["entry_ts"], "entry": t["entry"], "notional": t["notional"], "margin": t["margin"], "leverage": lev,
            "tps": [{"n": i, "price": x, "hit": i <= t["tp_hits"], "share": sh} for i, (x, sh) in enumerate(zip(t["tps"], t["tp_shares"]), 1)],
            "stop": stop, "stop_kind": "БУ" if be_on else "SL", "tp_hits": t["tp_hits"],
            "liq_price": t["entry"] * (1 - d * (1 / lev - mmr)) if lev else None,
            "to_stop_pct": (d * (price / stop - 1) * 100) if price else None,                 # запас до стопа, % от цены (+ — стоп ещё далеко)
            "to_next_tp_pct": (d * (nxt / price - 1) * 100) if (price and nxt) else None,
            "move_pct": d * (price / t["entry"] - 1) * 100 if price else None,
            "pnl_usd": t["pnl_usd"], "pnl_pct": t["pnl_pct"], "signal_ts": t["signal_ts"],
            "fills": t["fills"], "metrics_at_entry": {"dg": t.get("global"), "dl": t.get("local"), "ds": t.get("scalp")},
        })
    notional = sum(r["notional"] for r in rows)
    margin = sum(r["margin"] for r in rows)
    pnl = sum(r["pnl_usd"] for r in rows)
    avg_entry = sum(r["entry"] * r["notional"] for r in rows) / notional if notional else rows[0]["entry"]
    first = min(r["entry_ts"] for r in rows)
    out["position"] = {
        "side": side, "opened_ts": first, "hold_h": ((candle_ts or first) - first) / 3.6e6, "legs": rows, "notional": notional, "margin": margin, "avg_entry": avg_entry,
        "pnl_usd": pnl, "pnl_pct_deposit": pnl / p["deposit"] * 100 if p["deposit"] else None, "pnl_pct_margin": pnl / margin * 100 if margin else None,
        "deadline_ts": tradingweek.window_close(first, p.get("friday_close_before_min") or 0) if p.get("weekdays_only") else None,
    }
    return out


def evaluate_entry(store, defaults, name, config_id, params, auto=None, paused=False, available_fn=None):
    """Один блок мониторинга; ошибка расчёта превращается в поле error (одна сломанная стратегия не роняет страницу)."""
    key = json.dumps(params, sort_keys=True, default=str)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            res = hit[1]
        else:
            res = None
    err = None
    if res is None:
        try:
            res = run_backtest(store, defaults, BacktestParams(**{**LEGACY_OFF, **params}), available_fn=available_fn)
            with _lock:
                _cache[key] = (now, res)
                if len(_cache) > 64:
                    _cache.pop(next(iter(_cache)))
        except (ParamError, FileNotFoundError, KeyError, ValueError) as e:
            err = str(e.args[0]) if e.args else str(e)
    if res is None:
        return {"name": name, "config_id": config_id, "symbol": params.get("symbol"), "auto": auto, "paused": paused, "error": err, "position": None}
    sym = res["symbol"]
    price = candle_ts = None
    try:
        last = store.candles(sym)[-1]
        candle_ts, price = last[0], last[4]
    except (KeyError, IndexError, FileNotFoundError):
        pass
    try:
        latest = store.latest(res["params"]["source"])
    except (KeyError, FileNotFoundError):
        latest = None
    return _view(res, price, latest, candle_ts, name, config_id, auto, paused)
