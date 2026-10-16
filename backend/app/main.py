"""FastAPI-бэкенд интерактивного просмотра бэктеста. Запуск: uvicorn app.main:app --reload (из папки backend/)."""
import threading

from .core import backtest
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, load_settings
from .data import DataStore
from .schemas import BacktestParams
from .service import ParamError, run_backtest


def create_app(settings: "Settings | None" = None) -> FastAPI:
    s = settings or load_settings()
    store = DataStore(s)
    refresh_lock = threading.Lock()
    app = FastAPI(title="telebot-maverick backtest viewer")
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.state.settings, app.state.store = s, store

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/config")
    def config():
        try:
            tfs = store.timeframes()
        except FileNotFoundError:
            tfs = []
        return {"defaults": s.defaults, "timeframes": tfs, "exchange": s.exchange, "symbol": s.symbol, "tf": s.candle_tf,
                "max_bars": s.max_bars, "data": store.info()}

    @app.get("/api/candles")
    def candles(start: "int | None" = Query(None, description="мс, UTC"), end: "int | None" = Query(None, description="мс, UTC"),
                max_bars: "int | None" = Query(None, ge=100, le=20000),
                tf: "int | None" = Query(None, ge=1, description="таймфрейм, сек; без него подбирается автоматически")):
        try:
            tf_, bars, truncated = store.window(start, end, max_bars or s.max_bars, tf)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        return {"tf": tf_, "bars": bars, "truncated": truncated}

    @app.post("/api/backtest")
    def run(params: BacktestParams):
        try:
            return run_backtest(store, s.defaults, params)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ParamError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/candles/refresh")
    def refresh():
        """Скачивает/докачивает свечи с биржи (может занять несколько минут при первом запуске)."""
        if not refresh_lock.acquire(blocking=False):
            raise HTTPException(409, "Загрузка свечей уже идёт")
        try:
            sigs, _ = store.signals()
            backtest.fetch_candles(s.exchange, s.symbol, s.candle_tf, sigs[0][0], str(s.cache_dir))
            store.reload_candles()
            return store.info()
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        finally:
            refresh_lock.release()

    return app


app = create_app()
