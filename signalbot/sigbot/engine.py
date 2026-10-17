"""Расчёт по конфигу: бэктест (те же правила, что в интерфейсе), поиск НОВЫХ сигналов и план сделки от текущей цены."""
import os
import time

from app import service
from app.core import backtest, topcoins
from app.schemas import BacktestParams
from app.service import ParamError


class EvalError(RuntimeError):
    pass


class Cancelled(Exception):
    """Пользователь отменил подсчёт (/cancel)."""


def fingerprint(settings, params):
    """Меняется, когда изменились файлы истории, от которых зависит конфиг, — тогда нужен пересчёт."""
    out = []
    for sid in {params.get("source"), params.get("liq_source")} - {None}:
        src = settings.sources.get(sid)
        try:
            st = os.stat(src.file) if src else None
            out.append((sid, st.st_mtime_ns, st.st_size) if st else (sid, None))
        except OSError:
            out.append((sid, None))
    return tuple(sorted(out, key=str))


class Engine:
    def __init__(self, settings, store, markets=None, price_fn=None, candles_fn=None, windows_fn=None):
        self.s, self.store, self.markets = settings, store, markets
        self.price_fn = price_fn or (lambda symbol: None)
        self._candles_fn = candles_fn or self._download_candles
        self._windows_fn = windows_fn or self._download_windows

    # ---------- данные ----------
    def _download_candles(self, symbol, progress=None):
        backtest.fetch_candles(self.s.exchange, symbol, self.s.candle_tf, self.store.first_signal_ts(), str(self.s.cache_dir), progress=progress)

    def _download_windows(self, windows):
        for sym, a, b in windows:
            backtest.fetch_window(self.s.exchange, sym, self.s.candle_tf, a, b, str(self.s.cache_dir))

    def refresh_candles(self, symbol, progress=None):
        """Первичная загрузка или докачка хвоста свечей актива. Ошибка сети не фатальна, если свечи уже есть."""
        try:
            self._candles_fn(symbol, progress)
            self.store.reload_candles(symbol)
            return None
        except Cancelled:
            raise
        except BaseException as e:                      # fetch_candles завершается SystemExit при недоступной бирже
            if not self.store.has_candles(symbol):
                raise EvalError(f"Нет свечей {symbol} и не удалось их скачать: {e or type(e).__name__}") from None
            return f"свечи {symbol} не обновились: {e or type(e).__name__}"

    def available(self):
        try:
            return set(self.markets.symbols()) if self.markets else self.store.cached_symbols()
        except Exception:
            return self.store.cached_symbols()

    # ---------- расчёт ----------
    def evaluate(self, params):
        try:
            return service.run_backtest(self.store, self.s.defaults, BacktestParams(**params), available_fn=(self.markets.symbols if self.markets else None),
                                        available_token=(self.markets.version if self.markets else None))
        except (ParamError, FileNotFoundError) as e:
            raise EvalError(str(e)) from None

    def report(self, params, ts):
        """Метрики отчёта, по которому пришёл сигнал: DG/DL/DS, ликвидации, топ-коин."""
        try:
            rows, _ = self.store.signals(params["source"], params.get("liq_source"))
        except (KeyError, FileNotFoundError):
            return {}
        row = next((r for r in reversed(rows) if r[0] == ts), None)
        if not row:
            return {}
        out = {"dl": row[1], "ds": row[2], "liq_long_k": row[3], "liq_short_k": row[4], "top_coin": row[5], "top_value": row[6], "dg": None}
        try:
            last = self.store.latest(params["source"])
            if last and last["ts"] == ts:
                out["dg"] = last["dg"]
        except (KeyError, FileNotFoundError):
            pass
        return out

    @staticmethod
    def new_signals(result, last_ts):
        """Сигналы после last_ts, по которым стратегия ОТКРЫЛА БЫ новую позицию (повторные подтверждения той же серии, пока позиция
        открыта, сигналами не считаются). Внутри диапазона свечей это `taken` из бэктеста; свежие сигналы, для которых свечи входа ещё
        нет, определяются по состоянию последней сделки и порядку самих сигналов. Отсечённые фильтром тренда пропускаются."""
        candles_last = result["range"]["candles_last_ts"]
        trades = result["trades"]
        # состояние позиции на момент конца свечей
        cur_side = None
        cooldown_until = 0
        if trades:
            lt = trades[-1]
            if lt["reason"] == "OPEN":
                cur_side = lt["side"]
            elif lt["reason"] == "SL" and result["params"].get("cooldown_h"):
                cooldown_until = lt["exit_ts"] + result["params"]["cooldown_h"] * 3.6e6
        out = []
        for s in result["last_signals"]:
            if s["trend_blocked"] or s["ts"] <= last_ts:
                continue
            if s["ts"] <= candles_last:
                if s["taken"]:
                    out.append(s)
                continue
            if s["ts"] < cooldown_until or cur_side == s["side"]:
                continue
            cur_side = s["side"]
            out.append(s)
        return out

    # ---------- план сделки ----------
    def plan(self, side, entry, tps, sl_pct):
        d = 1 if side == "long" else -1
        return {"levels": [{"pct": l["pct"], "share": l["share"], "price": entry * (1 + d * l["pct"] / 100)} for l in tps],
                "sl": entry * (1 - d * sl_pct / 100), "sl_pct": sl_pct}

    def top_plan(self, result, sig, report):
        """Сделка по топ-коину отчёта для нового сигнала (или причина, почему её нет)."""
        p = result["params"]
        if not p.get("top_enabled"):
            return None
        if p["top_side"] != "both" and p["top_side"] != sig["side"]:
            return None
        coin, val = report.get("top_coin"), report.get("top_value")
        if not coin:
            return {"skip": "в отчёте нет топ-коина"}
        if p.get("top_min_value") and (val or 0) < p["top_min_value"]:
            return {"skip": f"{coin} {val}: значение ниже порога {p['top_min_value']}"}
        sym = topcoins.resolve_symbol(coin, self.available())
        if not sym:
            return {"skip": f"{coin}: нет бессрочного USDT-контракта на бирже"}
        price = self.price_fn(sym)
        out = {"coin": coin, "value": val, "symbol": sym, "price": price, "plan": None}
        if price:
            out["plan"] = self.plan(sig["side"], price, p["top_tps"], p["top_sl_pct"])
        return out

    def pending_windows(self, result, limit):
        top = result.get("top") or {}
        items = ((top.get("pending") or {}).get("items") or []) if top.get("enabled") else []
        return [(i["symbol"], i["from"], i["to"]) for i in items[:limit]], len(items)

    def download_windows(self, windows):
        try:
            self._windows_fn(windows)
            for sym in {w[0] for w in windows}:
                self.store.reload_candles(sym)
            return None
        except BaseException as e:
            return f"не удалось докачать свечи топ-коинов: {e or type(e).__name__}"


def make_price_fn(exchange_id, ttl=20):
    """Текущая цена (last) фьючерса через публичный API биржи; None при любой ошибке."""
    cache = {}
    ex = {}

    def price(symbol):
        hit = cache.get(symbol)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        try:
            if "ex" not in ex:
                import ccxt
                ex["ex"] = getattr(ccxt, exchange_id)({"enableRateLimit": True})
            v = float(ex["ex"].fetch_ticker(symbol)["last"])
        except Exception:
            v = None
        cache[symbol] = (time.time(), v)
        return v

    return price
