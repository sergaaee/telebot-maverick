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


def _view(res, price, latest, candle_ts, name, config_id, auto, paused, detail=False):
    p = res["params"]
    legs = sorted([t for t in res["trades"] if t["reason"] == "OPEN"], key=lambda t: (t["entry_ts"], t.get("leg", 1)))
    out = {"name": name, "config_id": config_id, "symbol": res["symbol"], "source": res["source"]["label"], "auto": auto, "paused": paused,
           "price": price, "candle_ts": candle_ts, "report": latest, "ready_now": res.get("ready_now", []), "position": None,
           "recent": [{"side": t["side"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"], "reason": t["reason"], "pnl_usd": t["pnl_usd"], "pnl_pct": t["pnl_pct"],
                       "hold_h": t["hold_h"]} for t in [t for t in res["trades"] if t["reason"] != "OPEN"][-5:]][::-1],
           "settings": {"sl_pct": p["sl_pct"], "tps": p["tps"], "be_after_tp1": p["be_after_tp1"], "leverage": p["leverage"], "pyramid_leverage": p.get("pyramid_leverage"),
                        "funding_apr": p.get("funding_apr") or 0.0, "funding_period_h": p.get("funding_period_h") or 8, "trade_pct": p["trade_pct"], "pyramid_enabled": p["pyramid_enabled"], "pyramid_pct": p.get("pyramid_pct"), "deposit": p["deposit"], "liquidation_sim": p.get("liquidation_sim", False),
                        "weekdays_only": p.get("weekdays_only", False), "side": p["side"]}}
    if detail:                                             # подробности: статистика, вся история закрытых сделок (последние 300) и полные параметры
        t = res["stats"]["total"]
        keys = ("trades", "winrate", "profit_factor", "total_usd", "return_pct", "sharpe", "max_dd_usd", "max_dd_pct", "max_dd_real_usd", "max_dd_real_pct", "avg_pct",
                "tp", "sl", "be", "rev", "liq", "wknd", "max_loss_streak", "exposure_pct", "positions", "adds", "funding_usd")
        out["stats"] = {k: t.get(k) for k in keys}
        out["range"] = res["range"]
        out["params"] = p
        out["history"] = [{"n": i, "side": x["side"], "leg": x.get("leg", 1), "entry_ts": x["entry_ts"], "entry": x["entry"], "exit_ts": x["exit_ts"], "exit": x["exit_avg"],
                           "reason": x["reason"], "tp_hits": x["tp_hits"], "pnl_pct": x["pnl_pct"], "pnl_usd": x["pnl_usd"], "hold_h": x["hold_h"],
                           "fills": x["fills"]} for i, x in reversed(list(enumerate(res["trades"], 1))) if x["reason"] != "OPEN"][:300]
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
            "pnl_usd": t["pnl_usd"], "pnl_pct": t["pnl_pct"], "funding_usd": t.get("funding_usd") or 0.0, "signal_ts": t["signal_ts"],
            "fills": t["fills"], "metrics_at_entry": {"dg": t.get("global"), "dl": t.get("local"), "ds": t.get("scalp")},
        })
    notional = sum(r["notional"] for r in rows)
    margin = sum(r["margin"] for r in rows)
    pnl = sum(r["pnl_usd"] for r in rows)
    avg_entry = sum(r["entry"] * r["notional"] for r in rows) / notional if notional else rows[0]["entry"]
    first = min(r["entry_ts"] for r in rows)
    out["position"] = {
        "side": side, "opened_ts": first, "hold_h": ((candle_ts or first) - first) / 3.6e6, "legs": rows, "notional": notional, "margin": margin, "avg_entry": avg_entry,
        "pnl_usd": pnl, "funding_usd": sum(r["funding_usd"] for r in rows), "pnl_pct_deposit": pnl / p["deposit"] * 100 if p["deposit"] else None, "pnl_pct_margin": pnl / margin * 100 if margin else None,
        "deadline_ts": tradingweek.window_close(first, p.get("friday_close_before_min") or 0) if p.get("weekdays_only") else None,
    }
    return out


def compact(view):
    """Краткий вид для списка стратегий: без ног, исполнений и истории (они — в подробностях)."""
    v = dict(view)
    v.pop("recent", None)
    pos = v.get("position")
    if pos:
        v["position"] = {**{k: pos[k] for k in ("side", "opened_ts", "hold_h", "notional", "margin", "avg_entry", "pnl_usd", "funding_usd", "pnl_pct_deposit", "pnl_pct_margin", "deadline_ts")}, "legs_n": len(pos["legs"]),
                         "danger": any(l["liq_price"] is not None and v.get("price") and ((v["price"] <= l["liq_price"]) if pos["side"] == "long" else (v["price"] >= l["liq_price"])) for l in pos["legs"])}
    return v


def cached_result(store, defaults, params, available_fn=None):
    """Результат расчёта стратегии с краткой (TTL) памятью: страница мониторинга, портфель и бот не считают одно и то же дважды подряд."""
    key = json.dumps(params, sort_keys=True, default=str)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < TTL:
            return hit[1]
    res = run_backtest(store, defaults, BacktestParams(**{**LEGACY_OFF, **params}), available_fn=available_fn)
    with _lock:
        _cache[key] = (now, res)
        if len(_cache) > 64:
            _cache.pop(next(iter(_cache)))
    return res


def evaluate_entry(store, defaults, name, config_id, params, auto=None, paused=False, available_fn=None, detail=False):
    """Один блок мониторинга; ошибка расчёта превращается в поле error (одна сломанная стратегия не роняет страницу)."""
    err, res = None, None
    try:
        res = cached_result(store, defaults, params, available_fn)
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
    v = _view(res, price, latest, candle_ts, name, config_id, auto, paused, detail)
    return v if detail else compact(v)
