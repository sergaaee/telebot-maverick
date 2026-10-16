"""FastAPI-бэкенд интерактивного просмотра бэктеста. Запуск: python cli.py serve (из папки backend/)."""
import json

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .config import Settings, load_settings
from .core import backtest
from .data import DataStore
from .jobs import JobManager
from .markets import MarketsCache, MarketsError, ccxt_swap_symbols, normalize_symbol
from .schemas import BacktestParams
from .service import ParamError, run_backtest
from .state import read_state, write_state


class AssetIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40, description="актив: ETH, ETHUSDT, ETH/USDT или ETH/USDT:USDT")


def create_app(settings: "Settings | None" = None, markets: "MarketsCache | None" = None, runner=None, top_runner=None) -> FastAPI:
    """markets, runner и top_runner подменяются в тестах (без обращения к бирже)."""
    s = settings or load_settings()
    store = DataStore(s)
    markets = markets or MarketsCache(lambda: ccxt_swap_symbols(s.exchange))

    def default_runner(symbol, progress):
        backtest.fetch_candles(s.exchange, symbol, s.candle_tf, store.first_signal_ts(), str(s.cache_dir), progress=progress)

    def default_top_runner(job, progress):
        """Качает окна свечей топ-коинов; сбой по одной монете не останавливает остальные (итог — в job.warning)."""
        windows = job.payload["windows"]
        job.total, failed, count = len(windows), [], 0
        for sym, ws in windows.items():
            try:
                for a, b in ws:
                    count += backtest.fetch_window(s.exchange, sym, s.candle_tf, a, b, str(s.cache_dir),
                                                   progress=lambda n, ts, base=count: progress(base + n, ts))
            except BaseException as e:
                failed.append(f"{sym}: {str(e) or type(e).__name__}")
            job.done += 1
        if failed and len(failed) == len(windows):
            raise RuntimeError("; ".join(failed))
        if failed:
            job.warning = "Не удалось скачать: " + "; ".join(failed)

    def on_done(job):
        if job.kind == "top":
            for sym in job.payload.get("windows", {}):
                try:
                    store.reload_candles(sym)
                except KeyError:
                    pass
        else:
            store.reload_candles(job.symbol)

    def available_fn():
        return markets.symbols()

    jobs = JobManager(runner or default_runner, on_done=on_done, top_runner=top_runner or default_top_runner)
    app = FastAPI(title="telebot-maverick backtest viewer")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.state.settings, app.state.store, app.state.jobs = s, store, jobs

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/config")
    def config():
        tfs = {}
        for sym in store.symbols():
            try:
                tfs[sym] = store.timeframes(sym)
            except FileNotFoundError:
                tfs[sym] = []
        return {"defaults": s.defaults, "timeframes": tfs, "exchange": s.exchange, "symbols": store.symbols(),
                "user_symbols": store.user_symbols(), "symbol": s.symbol, "tf": s.candle_tf, "max_bars": s.max_bars,
                "data": store.info()}

    # ---------------- свечи ----------------
    @app.get("/api/candles")
    def candles(start: "int | None" = Query(None, description="мс, UTC"), end: "int | None" = Query(None, description="мс, UTC"),
                max_bars: "int | None" = Query(None, ge=100, le=20000),
                tf: "int | None" = Query(None, ge=1, description="таймфрейм, сек; без него подбирается автоматически"),
                symbol: "str | None" = Query(None, description="актив; по умолчанию SYMBOL_DEFAULT")):
        try:
            tf_, bars, truncated = store.window(start, end, max_bars or s.max_bars, tf, symbol)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except KeyError as e:
            raise HTTPException(422, str(e.args[0]))
        return {"tf": tf_, "bars": bars, "truncated": truncated}

    @app.post("/api/candles/refresh")
    def refresh(symbol: "str | None" = Query(None)):
        """Ставит в фон загрузку/докачку свечей актива и сразу возвращает задачу (ход — GET /api/jobs)."""
        try:
            sym = store._symbol(symbol)
        except KeyError as e:
            raise HTTPException(422, str(e.args[0]))
        job, created = jobs.submit(sym)
        return {"job": job.to_dict(), "created": created}

    # ---------------- активы ----------------
    @app.post("/api/assets")
    def add_asset(body: AssetIn):
        """Добавляет актив: проверяет символ на бирже и запускает в фоне загрузку свечей.
        Если актив уже есть, а свечей нет — тоже запускает загрузку."""
        try:
            sym = normalize_symbol(body.symbol)
        except ValueError as e:
            raise HTTPException(422, str(e))
        added = False
        if sym not in store.symbols():
            try:
                available = markets.symbols()
            except MarketsError as e:
                raise HTTPException(503, f"{e}. Проверить символ сейчас не получилось, попробуйте позже.")
            if sym not in available:
                near = markets.suggest(sym)
                raise HTTPException(422, f"Актива {sym} нет на {s.exchange}." + (f" Похожие: {', '.join(near)}" if near else ""))
            added = store.add_symbol(sym)
        job = None
        if added or not store.has_candles(sym):
            job, _ = jobs.submit(sym)
        return {"symbol": sym, "added": added, "job": job.to_dict() if job else None}

    @app.delete("/api/assets")
    def remove_asset(symbol: str = Query(...)):
        """Убирает из списка актив, добавленный из интерфейса (файл со свечами остаётся на диске)."""
        if symbol == s.symbol:
            raise HTTPException(422, "Актив по умолчанию убрать нельзя")
        if jobs.active(symbol, kind="candles"):
            raise HTTPException(409, "По активу идёт загрузка свечей — дождитесь её завершения")
        if not store.remove_symbol(symbol):
            raise HTTPException(404, "Такого добавленного актива нет (активы из SYMBOLS в .env убираются только в .env)")
        return {"ok": True}

    # ---------------- фоновые задачи ----------------
    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": [j.to_dict() for j in jobs.list()]}

    @app.post("/api/jobs/{job_id}/ack")
    def ack_job(job_id: int):
        j = jobs.ack(job_id)
        if j is None:
            raise HTTPException(404, "Задача не найдена")
        return j.to_dict()

    # ---------------- бэктест ----------------
    @app.post("/api/backtest")
    def run(params: BacktestParams):
        try:
            return run_backtest(store, s.defaults, params, available_fn=available_fn)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ParamError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/top/download")
    def top_download(params: BacktestParams):
        """Ставит в фон загрузку недостающих окон свечей топ-коинов (момент сигнала + TOP_DAYS) для текущих параметров."""
        try:
            res = run_backtest(store, s.defaults, params, available_fn=available_fn)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ParamError as e:
            raise HTTPException(422, str(e))
        items = (res["top"].get("pending") or {}).get("items") or []
        if not items:
            return {"job": None, "created": False, "windows": 0}
        windows = {}
        for it in items:
            windows.setdefault(it["symbol"], []).append([it["from"], it["to"]])
        job, created = jobs.submit("top", kind="top", label=f"Топ-коины: {len(windows)} монет", payload={"windows": windows}, total=len(windows))
        return {"job": job.to_dict(), "created": created, "windows": len(items)}

    # ---- состояние интерфейса (выбранные источник/актив/ТФ и параметры по каждой связке) хранится на сервере ----
    @app.get("/api/ui-state")
    def get_ui_state():
        return read_state(s.state_file)

    @app.put("/api/ui-state")
    def put_ui_state(state: dict = Body(...)):
        if len(json.dumps(state)) > 500_000:
            raise HTTPException(413, "Слишком большое состояние")
        write_state(s.state_file, state)
        return {"ok": True}

    return app


app = create_app()
