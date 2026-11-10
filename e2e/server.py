"""Бэкенд для e2e: настоящий API, но «биржа» и загрузчик свечей поддельные — без сети и с управляемой задержкой.

Запуск: cd backend && python -m uvicorn server:app --app-dir ../e2e (так делает e2e/run.py)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import load_settings  # noqa: E402
from app.core import backtest  # noqa: E402
from app.main import create_app  # noqa: E402
from app.history_sync import HistorySync  # noqa: E402
from app.markets import MarketsCache  # noqa: E402

EXCHANGE_SYMBOLS = {"BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT", "FAIL/USDT:USDT", "ETHW/USDT:USDT", "TIA/USDT:USDT", "ARB/USDT:USDT"}
SCALE = {"SOL": 0.002, "DOGE": 0.00001}      # во сколько раз цена актива меньше «биткоина» из демо-данных
SECONDS = {"SOL": 3.0, "DOGE": 7.0}          # сколько «качается» актив


def runner(symbol, progress):
    s = load_settings()
    base = symbol.split("/")[0]
    if base == "FAIL":
        time.sleep(1.5)
        raise RuntimeError("биржа вернула ошибку 429")
    src = backtest.read_cache(backtest.cache_path(s.exchange, "BTC/USDT:USDT", s.candle_tf, str(s.cache_dir)))
    k = SCALE.get(base, 0.01)
    steps = int(SECONDS.get(base, 3.0) / 0.2)
    for i in range(steps):
        time.sleep(0.2)
        progress(int(len(src) * (i + 1) / steps), src[min(len(src) - 1, int(len(src) * (i + 1) / steps))][0])
    path = backtest.cache_path(s.exchange, symbol, s.candle_tf, str(s.cache_dir))
    import csv
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([(t, o * k, h * k, l * k, c * k) for t, o, h, l, c in src])


def top_runner(job, progress):
    """Окна свечей топ-коинов: берём минутки «биткоина» за окно и пересчитываем в цену монеты; карта окон ведётся как в боевой загрузке."""
    s = load_settings()
    tf_ms = backtest.tf_to_ms(s.candle_tf)
    src = backtest.read_cache(backtest.cache_path(s.exchange, "BTC/USDT:USDT", s.candle_tf, str(s.cache_dir)))
    job.total = len(job.payload["windows"])
    count = 0
    for sym, windows in job.payload["windows"].items():
        time.sleep(0.8)
        path = backtest.cache_path(s.exchange, sym, s.candle_tf, str(s.cache_dir))
        have = {c[0]: c for c in backtest.read_cache(path)}
        covered = backtest.covered_intervals(path, tf_ms)
        k = 0.001
        for a, b in windows:
            for t, o, h, l, c in src:
                if a <= t <= b:
                    have[t] = (t, o * k, h * k, l * k, c * k)
                    count += 1
            covered.append((a, b))
        backtest._save_window(path, have, covered, tf_ms)
        job.done += 1
        progress(count, windows[-1][1])


_calls = {}


def sync_exporter(path, chat):
    """Поддельная выгрузка Telegram: первый вызов (при запуске сервера) ничего не добавляет, каждый следующий для binance дописывает новый отчёт."""
    import csv
    from datetime import datetime, timedelta

    _calls[chat] = _calls.get(chat, 0) + 1
    if _calls[chat] == 1 or "bn" not in str(path):
        return 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    last = rows[-1]
    ts = datetime.fromisoformat(last["date_utc"]) + timedelta(minutes=30)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([int(last["id"]) + 1, ts.isoformat(), 0.8, 1.9, 1.3, last["liq_long_k"], last["liq_short_k"],
                                "Top Coin\n————————————————\nTIA-USD      | 8.80\n"])
    return 1


# Поддельный счёт Hyperliquid для проверки ручного закрытия позиций: для кошелька E2E_WALLET позиции и закрытия «живут» в памяти процесса, остальные счета работают как обычно.
E2E_WALLET = "0x" + "ee" * 20
E2E_POSITIONS = [
    {"symbol": "BTC/USDC:USDC", "side": "long", "size": 0.4, "entry": 100.0, "mark": 101.0, "liq": 60.0, "pnl_usd": 12.0, "leverage": 5, "margin": 8.0, "notional": 40.0},
    {"symbol": "kPEPE/USDC:USDC", "side": "short", "size": 1000.0, "entry": 0.01, "mark": 0.0103, "liq": 0.02, "pnl_usd": -3.0, "leverage": 3, "margin": 3.3, "notional": 10.0},
]
E2E_CLOSED = []


def _patch_fake_account():
    from app.trading import HyperliquidAdapter as HL
    real = {k: getattr(HL, k) for k in ("positions", "account", "open_orders", "fills", "close_market")}

    def mine(self):
        return self.wallet == E2E_WALLET

    def positions(self):
        return [dict(p) for p in E2E_POSITIONS] if mine(self) else real["positions"](self)

    def close_market(self, symbol, side, share=1.0):
        if not mine(self):
            return real["close_market"](self, symbol, side, share)
        for p in list(E2E_POSITIONS):
            if p["symbol"].split("/")[0] == symbol.split("/")[0] and p["side"] == side:
                E2E_CLOSED.append((p["symbol"], side, share))
                if share >= 0.999:
                    E2E_POSITIONS.remove(p)
                else:
                    p["size"] *= 1 - share
                return {"status": "sent", "symbol": symbol, "closed": p["size"] * share}
        return {"status": "skipped", "note": "позиции на бирже нет — закрывать нечего", "symbol": symbol}

    HL.positions = positions
    HL.close_market = close_market
    HL.account = lambda self: {"equity": 500.0, "free": 450.0, "used": 50.0} if mine(self) else real["account"](self)
    HL.open_orders = lambda self: [] if mine(self) else real["open_orders"](self)
    HL.fills = lambda self, limit=20: [] if mine(self) else real["fills"](self, limit)


_patch_fake_account()
_settings = load_settings()
app = create_app(_settings, markets=MarketsCache(lambda: EXCHANGE_SYMBOLS), runner=runner, top_runner=top_runner,
                 sync=HistorySync(list(_settings.sources.values()), _settings.sync_min, exporter=sync_exporter))
