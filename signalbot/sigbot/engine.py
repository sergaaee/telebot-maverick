"""Расчёт по конфигу: бэктест (те же правила, что в интерфейсе), поиск НОВЫХ сигналов и план сделки от текущей цены."""
import os
import time

from app import service
from app.core import backtest, topcoins
from app.schemas import BacktestParams
from app.service import ParamError


# Конфиги, сохранённые до появления этих возможностей, не содержат полей: считаем их выключенными (а не берём умолчания из .env),
# чтобы результат старого конфига не менялся. Новые конфиги при загрузке фиксируют все поля явно.
LEGACY_OFF = {"pyramid_enabled": False, "pyramid_avg": False, "signal_gap_h": 0.0, "sl_hold_strong": False}


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
            return service.run_backtest(self.store, self.s.defaults, BacktestParams(**{**LEGACY_OFF, **params}), available_fn=(self.markets.symbols if self.markets else None))
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
    def pyramid_limit(p):
        """Сколько доборов реально разрешено (как в сервисе: первая нога + доборы не больше 100% депозита)."""
        if not p.get("pyramid_enabled"):
            return 0
        room = int((100 - p["trade_pct"]) // p["pyramid_pct"])
        return max(0, min(p["pyramid_max"], room))

    @staticmethod
    def new_signals(result, last_ts):
        """Сигналы после last_ts, по которым стратегия ОТКРЫЛА БЫ новую позицию или (при пирамидинге) сделала бы ДОБОР. Повторные подтверждения
        той же серии, пока позиция открыта и доборы не разрешены, сигналами не считаются. Внутри диапазона свечей это `taken` из бэктеста;
        свежие сигналы, для которых свечи входа ещё нет, определяются по состоянию позиции на конец данных и порядку самих сигналов
        (паузы, лимит и интервал доборов учитываются так же, как в расчёте). Отсечённые фильтром тренда пропускаются.
        У возвращаемого сигнала есть `pyramid` (добор или нет) и `leg` (номер ноги)."""
        p = result["params"]
        candles_last = result["range"]["candles_last_ts"]
        trades = result["trades"]
        by_signal = {t["signal_ts"]: t for t in trades}
        for t in trades:                                             # доборы усреднённой позиции: сигнал добора -> номер ноги и новая средняя
            for k, a in enumerate(t.get("adds", [])[1:], 2):
                sig_ts = a.get("signal_ts")
                last_add = k - 1 == t.get("adds_n")
                by_signal[sig_ts] = {"leg": k, "pyramid": True, **({"avg_entry": t["entry"], "avg_tps": t["tps"], "avg_sl": t["sl"]} if last_add else {})}
        max_adds = Engine.pyramid_limit(p)
        gap_sig = (p.get("signal_gap_h") or 0) * 3.6e6
        gap_pyr = (p.get("pyramid_gap_h") or 0) * 3.6e6
        # состояние на конец свечей: какая позиция ещё открыта (ноги с причиной OPEN), сколько в ней доборов и когда открывалась
        open_side = next((t["side"] for t in trades if t["reason"] == "OPEN"), None)
        group = [t for t in trades if t.get("group") == trades[-1].get("group")] if trades else []
        adds = max((t.get("leg", 1) for t in group), default=1) - 1 + sum(t.get("adds_n", 0) for t in group)
        last_open_ts = min((t["entry_ts"] for t in group), default=None)
        last_leg_ts = max((t["entry_ts"] for t in group), default=None)
        cooldown_until = 0
        if group and p.get("cooldown_h"):
            last = max(group, key=lambda t: t["exit_ts"])
            if last["reason"] == "SL":
                cooldown_until = last["exit_ts"] + p["cooldown_h"] * 3.6e6
        out = []
        for s in result["last_signals"]:
            if s["trend_blocked"] or s["ts"] <= last_ts:
                continue
            if s["ts"] <= candles_last:
                if s["taken"]:
                    t = by_signal.get(s["ts"], {})
                    out.append({**s, "leg": t.get("leg", 1), "pyramid": bool(t.get("pyramid")),
                                **{k: t[k] for k in ("avg_entry", "avg_tps", "avg_sl") if k in t}})
                continue
            if open_side == s["side"]:                               # позиция открыта в ту же сторону: добор или пропуск
                if adds >= max_adds or (gap_pyr and last_leg_ts is not None and s["ts"] < last_leg_ts + gap_pyr):
                    continue
                adds += 1
                last_leg_ts = s["ts"]
                out.append({**s, "leg": adds + 1, "pyramid": True})
                continue
            if open_side is None and s["ts"] < cooldown_until:      # пауза после стопа (только когда позиции нет)
                continue
            if gap_sig and last_open_ts is not None and s["ts"] < last_open_ts + gap_sig:
                continue
            open_side, adds, last_open_ts, last_leg_ts = s["side"], 0, s["ts"], s["ts"]
            out.append({**s, "leg": 1, "pyramid": False})
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
