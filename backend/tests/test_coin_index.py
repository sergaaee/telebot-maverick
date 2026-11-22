"""Режим входа Coin Index (только бэктест): лонг по выбранной монете в отчётах, где она есть в блоке Top Coin со значением не ниже порога."""
import csv
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.core import backtest, topcoins
from app.core.entries import entries
from app.core.signal_parser import parse_top_coins
from app.main import create_app
from app.runcache import make_key
from test_api import H, M, T0

SYM = "BTC/USDT:USDT"


def test_parse_top_coins_reads_every_row_of_the_block():
    t = "Deep Local: 1\nTop Coin\n————————————————\nTIA-USD      | 10.12\nPAXG-USD | 6.42\nBTCUSDT | 3,5\n\nпосторонний текст | 9\n"
    assert parse_top_coins(t) == [{"coin": "TIA-USD", "value": 10.12}, {"coin": "PAXG-USD", "value": 6.42}, {"coin": "BTCUSDT", "value": 3.5}]
    assert parse_top_coins("Coin Index\nsol-usd | 4")[0] == {"coin": "SOL-USD", "value": 4.0}                          # заголовок «Coin Index» тоже
    assert parse_top_coins("Top Coin\nWBETHUSDT | 12.40")[0]["value"] == 12.4 and len(parse_top_coins("Top Coin\nWBETHUSDT | 12.40")) == 1
    assert parse_top_coins("нет такого блока\nTIA-USD | 3") == [] and parse_top_coins("") == [] and parse_top_coins(None) == []
    assert parse_top_coins("Top Coin\n———\nнет строк монет\n") == []
    assert len(parse_top_coins("Top Coin\n" + "\n".join(f"C{i} | {i}" for i in range(50)))) == 30                      # не больше 30 строк


def test_coin_key_matches_report_tickers_and_exchange_symbols():
    assert topcoins.coin_key("TIA-USD") == topcoins.coin_key("TIA/USDT:USDT") == topcoins.coin_key("TIAUSDT") == "TIA"
    assert topcoins.coin_key("1000PEPE/USDT:USDT") == topcoins.coin_key("PEPE-USD") == topcoins.coin_key("PEPEUSDT") == topcoins.coin_key("1000PEPEUSDT") == "PEPE"
    assert topcoins.coin_key("1000000MOG/USDT:USDT") == "MOG" and topcoins.coin_key("1INCH/USDT:USDT") == "1INCH" and topcoins.coin_key("") == ""


def test_entries_with_a_custom_long_condition_ignore_deep_thresholds():
    sigs = [(T0 + i * H // 2, 0.1, 0.1, 10.0, 20.0) for i in range(6)]                                                 # Deep везде «плохой»
    idx = {sigs[1][0]: 12.0, sigs[2][0]: 3.0, sigs[4][0]: 15.0, sigs[5][0]: 11.0}
    pred = lambda s: idx.get(s["ts"], -1e9) >= 10
    assert entries(sigs, 1.0, 2.0, confirm=1) == []                                                                    # обычное условие — входов нет
    assert [t for t, _ in entries(sigs, 1.0, 2.0, confirm=1, long_pred=pred)] == [sigs[1][0], sigs[4][0], sigs[5][0]]
    assert [t for t, _ in entries(sigs, 1.0, 2.0, confirm=2, long_pred=pred)] == [sigs[5][0]]                          # подтверждение: два отчёта подряд


# ---------------------------------------------------------------- API

def _candles(n, step=0.0004, base=100.0):
    price, rows = base, []
    for i in range(n):
        price *= 1 + step
        rows.append((T0 + i * M, price, price * 1.0002, price * 0.9998, price))
    return rows


def _history(path, btc):
    """Отчёты каждые 30 минут; Deep везде «плохой» (лонг по порогам не откроется). btc: {номер отчёта: Coin Index BTC}; в каждом отчёте есть ещё SOL."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "liq_long_k", "liq_short_k", "text"])
        for i in range(40):
            text = "Top Coin\n————————————————\nSOL-USD      | 9.00\n" + (f"BTC-USD | {btc[i]}\n" if i in btc else "")
            ts = T0 + i * H // 2
            w.writerow([i + 1, datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), 1.0, 0.5, 0.4, 10.0, 20.0, text])


def make_client(tmp_path, monkeypatch, btc):
    _history(tmp_path / "history.csv", btc)
    cache = tmp_path / "cache"
    cache.mkdir()
    with open(backtest.cache_path("binanceusdm", SYM, "1m", str(cache)), "w", newline="") as f:
        csv.writer(f).writerows(_candles(3000))
    for k, v in dict(HISTORY_FILE=str(tmp_path / "history.csv"), CACHE_DIR=str(cache), SOURCES="", LIQ_FILE="", LIQ_SOURCE="", SYMBOLS=SYM, SYMBOL_DEFAULT=SYM, SYMBOL="",
                     LOCAL_MIN="1.65", SCALP_MIN="2.0", SHORT_LOCAL_MAX="0.05", SHORT_SCALP_MAX="0.05", SIDE="both", ONLY="", CONFIRM="1", TP_LEVELS="", TP_PCT="2", SL_PCT="3",
                     BE_AFTER_TP1="", STATE_FILE=str(tmp_path / "s.json"), ASSETS_FILE=str(tmp_path / "a.json"), COOL_DROP="", COOL_DECLINES="", LIQ_HOURS="", COOLDOWN_H="0",
                     ORDER_SIZE="0.1", FEE="0", SLIPPAGE="0", TOP_ENABLED="false", GLOBAL_MIN="", SHORT_GLOBAL_MAX="", ENTRY_MODE="", COIN_INDEX_MIN="").items():
        monkeypatch.setenv(k, v)
    return TestClient(create_app(load_settings()))


def post(c, **kw):
    r = c.post("/api/backtest", json=kw)
    return r.status_code, r.json()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # BTC в блоке: отчёты 3 (12.0), 4 (4.0), 10 (8.0), 20 и 21 подряд (15.0, 11.0), 30 (10.0 — ровно на пороге)
    return make_client(tmp_path, monkeypatch, {3: 12.0, 4: 4.0, 10: 8.0, 20: 15.0, 21: 11.0, 30: 10.0})


def test_deep_mode_is_unchanged_and_finds_nothing_on_this_data(client):
    s, deep = post(client)
    assert s == 200 and deep["trades"] == [] and deep["coin_index"] is None and deep["params"]["entry_mode"] == "deep" and deep["params"]["coin_index_min"] is None


def test_coin_index_enters_long_on_reports_where_the_coin_reaches_the_value(client):
    s, r = post(client, entry_mode="coin_index", coin_index_min=10, confirm=1, side="short")                           # сторона и пороги Deep в этом режиме не используются
    assert s == 200
    assert [t["signal_ts"] for t in r["trades"]] == [T0 + i * H // 2 for i in (3, 20, 30)]                              # отчёт 21 тоже проходит порог, но позиция по 20-му ещё открыта
    assert r["skipped_reasons"] == {"позиция уже открыта": 1} and all(t["side"] == "long" for t in r["trades"]) and r["stats"]["short"]["trades"] == 0
    ci = r["coin_index"]
    assert ci["coin"] == "BTC" and ci["min"] == 10 and ci["with_coin"] == 6 and ci["passing"] == 4 and ci["max"] == 15.0 and ci["min_seen"] == 4.0 and ci["reports"] == 40
    assert r["params"]["side"] == "short" and r["params"]["entry_mode"] == "coin_index"                                 # в ответе — как прислал пользователь
    assert [x["side"] for x in r["signals"]] == ["long"] * len(r["signals"]) and len(r["signals"]) == 4


def test_threshold_is_inclusive_and_confirmation_needs_consecutive_reports(client):
    s, hi = post(client, entry_mode="coin_index", coin_index_min=10.01, confirm=1)
    assert s == 200 and hi["coin_index"]["passing"] == 3 and len(hi["signals"]) == 3                                  # 10.0 на пороге уже не проходит при 10.01
    s, eq = post(client, entry_mode="coin_index", coin_index_min=10, confirm=1)
    assert len(eq["signals"]) == 4                                                                                      # порог включается
    s, two = post(client, entry_mode="coin_index", coin_index_min=10, confirm=2)
    assert s == 200 and [x["ts"] for x in two["signals"]] == [T0 + 21 * H // 2]                                         # два отчёта подряд — только 20 и 21 (вход на 21-м)


def test_other_filters_and_exit_rules_work_as_in_the_main_scenario(client):
    s, base = post(client, entry_mode="coin_index", coin_index_min=10, confirm=1)
    s, ds = post(client, entry_mode="coin_index", coin_index_min=10, confirm=1, exit_long_ds=5.0)                       # выход по DS (0.4 ≤ 5): закрытие на следующем отчёте
    assert s == 200 and ds["stats"]["total"]["ds"] > 0 and ds["stats"]["total"]["ds"] > base["stats"]["total"]["ds"]
    s, tr = post(client, entry_mode="coin_index", coin_index_min=10, confirm=1, trend_enabled=True, trend_emas=[5, 10], trend_tf="5m")
    assert s == 200 and tr["trend"]["enabled"] and tr["trend"]["confirmed"] == 4 and len(tr["trades"]) <= len(base["trades"])
    s, wk = post(client, entry_mode="coin_index", coin_index_min=10, confirm=1, weekend_no_entry=True)
    assert s == 200 and len(wk["trades"]) <= len(base["trades"])
    s, tp = post(client, entry_mode="coin_index", coin_index_min=10, confirm=1, tps=[{"pct": 0.5, "share": 100}])
    assert s == 200 and tp["stats"]["total"]["tp"] > 0 and tp["stats"]["total"]["trades"] == len(tp["trades"])


def test_validation_and_info_warnings(client):
    s, err = post(client, entry_mode="coin_index")
    assert s == 422 and "Coin Index" in err["detail"]                                                                  # порог не задан
    assert post(client, entry_mode="what")[0] == 422 and post(client, entry_mode="coin_index", coin_index_min=5000)[0] == 422
    s, none = post(client, entry_mode="coin_index", coin_index_min=100)
    assert s == 200 and none["trades"] == [] and any("ни разу не достигает" in w for w in none["warnings"]) and none["coin_index"]["passing"] == 0
    s, tops = post(client, entry_mode="coin_index", coin_index_min=10, top_enabled=True)
    assert s == 200 and any("«Топ-коины» отключены" in w for w in tops["warnings"]) and tops["top"]["enabled"] is False


def test_coin_that_never_appears_gives_a_warning_not_an_error(tmp_path, monkeypatch):
    c = make_client(tmp_path, monkeypatch, {})
    s, r = post(c, entry_mode="coin_index", coin_index_min=1)
    assert s == 200 and r["trades"] == [] and r["coin_index"]["with_coin"] == 0 and r["coin_index"]["max"] is None and any("ни разу не встречается" in w for w in r["warnings"])


def test_monte_carlo_works_and_walk_forward_is_refused_in_this_mode(client):
    r = client.post("/api/montecarlo", json={"entry_mode": "coin_index", "coin_index_min": 10, "confirm": 1, "runs": 100, "mode": "shuffle"})
    assert r.status_code == 200 and r.json()["symbol"] == SYM                                                           # три сделки есть — Монте-Карло считается
    w = client.post("/api/walkforward", json={"entry_mode": "coin_index", "coin_index_min": 10, "grid": [{"param": "sl_pct", "values": [1, 2]}], "train_days": 14, "test_days": 7})
    assert w.status_code == 422 and "Coin Index" in w.json()["detail"]


def test_cache_key_changes_only_in_the_new_mode_and_bot_import_is_refused(client):
    eff = {"symbol": SYM, "sl_pct": 2.0}
    key = make_key(eff, "src", None, SYM)
    assert make_key({**eff, "entry_mode": "deep", "coin_index_min": 5.0}, "src", None, SYM) == key                     # обычный режим: ключ прежний (старые снимки годны)
    assert make_key({**eff, "entry_mode": "coin_index", "coin_index_min": 5.0}, "src", None, SYM) != key
    assert make_key({**eff, "entry_mode": "coin_index", "coin_index_min": 5.0}, "src", None, SYM) != make_key({**eff, "entry_mode": "coin_index", "coin_index_min": 6.0}, "src", None, SYM)
    from app.botconfigs import ConfigError, validate
    d = load_settings().defaults
    assert validate({"symbol": SYM, "entry_mode": "deep"}, d)[0]["entry_mode"] == "deep"
    with pytest.raises(ConfigError, match="только в бэктесте"):
        validate({"symbol": SYM, "entry_mode": "coin_index", "coin_index_min": 5}, d)
    from app.legacy_off import LEGACY_OFF
    assert LEGACY_OFF["entry_mode"] == "deep" and LEGACY_OFF["coin_index_min"] is None
