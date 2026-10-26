"""Сводки в Telegram и выгрузки CSV."""
import csv
import io
from datetime import datetime, timezone

from app import digest, exporter
from app.config import load_settings
from app.db import Database
from app.schemas import BacktestParams
from app.service import run_backtest
from app.trading import DryRunAdapter
from test_runcache import FakeStore, make


def ts(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp()


def test_digest_schedule():
    mon, tue = ts(2026, 10, 26, 9), ts(2026, 10, 27, 9)               # 26 октября 2026 — понедельник
    assert not digest.due("off", None, mon)
    assert digest.due("daily", None, mon) and not digest.due("daily", None, ts(2026, 10, 26, 7))      # до 8:00 UTC рано
    assert not digest.due("daily", ts(2026, 10, 26, 8, 5), mon)                                            # сегодня уже слали
    assert digest.due("daily", ts(2026, 10, 26, 8, 5), tue)
    assert digest.due("weekly", None, mon) and not digest.due("weekly", None, tue)                          # по понедельникам
    assert not digest.due("weekly", ts(2026, 10, 26, 8, 1), mon)


def setup(tmp_path):
    sigs, candles = make(seed=3, n_rep=900)
    store = FakeStore(tmp_path, sigs, candles)
    params = run_backtest(store, load_settings().defaults, BacktestParams(top_enabled=False, side="both", sl_pct=0.3, tps=[{"pct": 0.3, "share": 100}]), now_ms=0)["params"]
    db = Database(tmp_path / "d.db")
    u = db.add_user("alice", "alice-password")
    db.link_telegram("alice", 777)
    db.add_config(u["id"], "Моя <стратегия>", params)
    return db, store, db.get_user(u["id"]), candles[-1][0] / 1000


def test_digest_text_and_user_settings(tmp_path):
    db, store, user, end = setup(tmp_path)
    assert user["digest"] == "off"
    db.set_digest(user["id"], "weekly")
    assert db.get_user(user["id"])["digest"] == "weekly" and db.get_user(user["id"])["digest_ts"]
    db.add_auto_log(user["id"], 1, "open", "BTC/USDT:USDT", "long", "dry_run", {})
    db.add_auto_log(user["id"], 1, "open", "BTC/USDT:USDT", "long", "error", {"error": "x"})
    text = digest.build_digest(db, store, load_settings().defaults, user, DryRunAdapter(), None, "weekly", now=end + 60)
    assert "Недельная сводка" in text and "Моя &lt;стратегия&gt;" in text and "Итого по стратегиям" in text and "Автомод за период" in text and "Ошибок биржи за период: 1" in text
    db.mark_digest_sent(user["id"], 123.0)
    assert db.get_user(user["id"])["digest_ts"] == 123.0


def test_csv_exports(tmp_path):
    db, store, user, end = setup(tmp_path)
    cfg = db.list_configs(user["id"])[0]
    from app.monitor import cached_result
    res = cached_result(store, load_settings().defaults, cfg["params"])
    body = exporter.trades_csv([("=HYPERLINK(1)", res)])
    rows = list(csv.reader(io.StringIO(body.lstrip("﻿"))))
    assert rows[0][0] == "strategy" and len(rows) == len(res["trades"]) + 1 and rows[1][0] == "'=HYPERLINK(1)"      # формулы в ячейках экранируются
    assert rows[1][3] in ("long", "short") and float(rows[1][12]) == round(res["trades"][0]["pnl_usd"], 4)
    db.add_auto_log(user["id"], cfg["id"], "open", "BTC/USDT:USDT", "long", "sent", {"note": "тест"})
    log = list(csv.reader(io.StringIO(exporter.autolog_csv(db.list_auto_log(user["id"], 10)).lstrip("﻿"))))
    assert log[1][2] == "open" and "тест" in log[1][6]
    fills = list(csv.reader(io.StringIO(exporter.fills_csv([{"ts": 1700000000000, "symbol": "BTC/USDC:USDC", "side": "buy", "amount": 0.001, "price": 100.0, "fee": 0.01, "pnl": "1.5"}]).lstrip("﻿"))))
    assert fills[1][:3] == ["2023-11-14 22:13:20", "BTC/USDC:USDC", "buy"] and fills[1][6] == "1.5"
