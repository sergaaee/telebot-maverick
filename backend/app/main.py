"""FastAPI-бэкенд интерактивного просмотра бэктеста. Запуск: uvicorn app.main:app --reload (из папки backend/)."""
import json
import threading

from .core import backtest
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, load_settings
from .data import DataStore
from .schemas import BacktestParams
from .service import ParamError, run_backtest
from .state import read_state, write_state


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
        tfs = {}
        for sym in s.symbols:
            try:
                tfs[sym] = store.timeframes(sym)
            except FileNotFoundError:
                tfs[sym] = []
        return {"defaults": s.defaults, "timeframes": tfs, "exchange": s.exchange, "symbols": s.symbols, "symbol": s.symbol,
                "tf": s.candle_tf, "max_bars": s.max_bars, "data": store.info()}

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

    @app.post("/api/backtest")
    def run(params: BacktestParams):
        try:
            return run_backtest(store, s.defaults, params)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ParamError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/candles/refresh")
    def refresh(symbol: "str | None" = Query(None)):
        """Скачивает/докачивает свечи актива с биржи (может занять несколько минут при первом запуске)."""
        try:
            sym = store._symbol(symbol)
        except KeyError as e:
            raise HTTPException(422, str(e.args[0]))
        if not refresh_lock.acquire(blocking=False):
            raise HTTPException(409, "Загрузка свечей уже идёт")
        try:
            backtest.fetch_candles(s.exchange, sym, s.candle_tf, store.first_signal_ts(), str(s.cache_dir))
            store.reload_candles(sym)
            return store.info()
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        finally:
            refresh_lock.release()

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
