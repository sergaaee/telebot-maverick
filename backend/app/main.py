"""FastAPI-бэкенд интерактивного просмотра бэктеста. Запуск: python cli.py serve (из папки backend/)."""
import json
import time
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .auth import COOKIE, LoginLimiter, make_dependency
from .config import Settings, load_settings
from .db import Database, DbError
from .core import backtest
from .data import DataStore
from .candle_sync import CandleSync
from .history_sync import HistorySync
from .jobs import JobManager
from .markets import MarketsCache, MarketsError, ccxt_swap_symbols, normalize_symbol
from .schemas import BacktestParams, MonteCarloRequest
from .monitor import evaluate_entry
from .service import ParamError, run_backtest, run_montecarlo
from .trading import NotConfigured, TEST_MAX_USD, get_adapter


class LoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AssetIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=40, description="актив: ETH, ETHUSDT, ETH/USDT или ETH/USDT:USDT")


def create_app(settings: "Settings | None" = None, markets: "MarketsCache | None" = None, runner=None, top_runner=None, sync: "HistorySync | None" = None, candle_sync: "CandleSync | None" = None) -> FastAPI:
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
    if sync is None:
        sync = HistorySync(list(s.sources.values()), s.sync_min, enabled=s.sync_enabled,
                           disabled_reason=None if s.sync_enabled else "выключено: нет TG_API_ID/TG_API_HASH в .env (или HISTORY_SYNC=false)")

    if candle_sync is None:
        def sync_symbols():
            return [x for x in (s.candles_sync_symbols or s.symbols) if x in store.symbols()]

        def fetch_tail(symbol):
            backtest.fetch_candles(s.exchange, symbol, s.candle_tf, store.first_signal_ts(), str(s.cache_dir))

        candle_sync = CandleSync(sync_symbols, fetch_tail, s.candles_sync_min, enabled=s.candles_sync,
                                 skip_fn=lambda sym: bool(jobs.active(sym, kind="candles")), on_updated=store.reload_candles,
                                 disabled_reason="выключено (CANDLES_SYNC=false)")

    @asynccontextmanager
    async def lifespan(_app):
        candle_sync.start()                # свечи: при запуске и затем каждые CANDLES_SYNC_MIN минут
        sync.start()                       # подгрузка при запуске сервера и затем каждые HISTORY_SYNC_MIN минут
        yield
        sync.stop()
        candle_sync.stop()

    db = Database(s.db_file)
    limiter = LoginLimiter()
    app = FastAPI(title="telebot-maverick backtest viewer", lifespan=lifespan, dependencies=[Depends(make_dependency(db, s))])
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.state.settings, app.state.store, app.state.jobs, app.state.sync = s, store, jobs, sync

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

    # ---------------- вход, выход, профиль ----------------
    def public_user(u):
        return {"login": u["login"], "is_admin": u["is_admin"], "telegram": bool(u["telegram_id"]), "telegram_name": u["telegram_name"]}

    @app.get("/api/auth/status")
    def auth_status():
        return {"auth_enabled": s.auth_enabled, "users_exist": db.user_count() > 0}

    @app.get("/api/auth/me")
    def auth_me(request: Request):
        if not s.auth_enabled:
            return {"auth_enabled": False, "user": public_user(request.state.user), "bot_username": s.bot_username}
        user = db.session_user(request.cookies.get(COOKIE))
        if not user:
            raise HTTPException(401, "Требуется вход")
        return {"auth_enabled": True, "user": public_user(user), "bot_username": s.bot_username}

    @app.post("/api/auth/login")
    def auth_login(body: LoginIn, request: Request, response: Response):
        key = ((request.client.host if request.client else "?"), body.login.lower())
        limiter.check(key)
        user = db.check_login(body.login, body.password)
        if not user:
            limiter.fail(key)
            raise HTTPException(401, "Неверный логин или пароль")
        limiter.ok(key)
        token = db.create_session(user["id"], s.session_days)
        response.set_cookie(COOKIE, token, max_age=int(s.session_days * 86400), httponly=True, samesite="lax", secure=s.cookie_secure, path="/")
        return {"user": public_user(user), "bot_username": s.bot_username}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response):
        db.delete_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    @app.post("/api/me/telegram/link-code")
    def telegram_link_code(request: Request):
        """Одноразовый код (10 минут): отправьте его боту командой /link КОД — Telegram привяжется к этому пользователю."""
        return {"code": db.create_link_code(request.state.user["id"]), "ttl_min": 10, "bot_username": s.bot_username}

    @app.delete("/api/me/telegram")
    def telegram_unlink(request: Request):
        db.unlink_telegram(request.state.user["id"])
        return {"ok": True}

    # ---------------- текущие метрики и подгрузка истории ----------------
    @app.get("/api/latest")
    def latest():
        """Последний отчёт каждого источника: DG / DL / DS, топ-коин, ликвидации."""
        out = []
        for src in s.sources.values():
            try:
                row = store.latest(src.id)
            except FileNotFoundError:
                row = None
            out.append({"id": src.id, "label": src.label, "report": row})
        return {"sources": out}

    @app.get("/api/sync")
    def sync_status():
        return {**sync.snapshot(), "candles": candle_sync.snapshot()}

    @app.post("/api/sync/run")
    def sync_run():
        """Подгрузить новые сообщения прямо сейчас (не дожидаясь очередного срока)."""
        if not sync.enabled:
            raise HTTPException(409, sync.disabled_reason or "Подгрузка выключена")
        return {"started": sync.trigger(), **sync.snapshot()}

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
    def refresh(request: Request, symbol: "str | None" = Query(None)):
        """Ставит в фон загрузку/докачку свечей актива и сразу возвращает задачу (ход — GET /api/jobs)."""
        try:
            sym = store._symbol(symbol)
        except KeyError as e:
            raise HTTPException(422, str(e.args[0]))
        job, created = jobs.submit(sym)
        return {"job": job.to_dict(request.state.user["id"]), "created": created}

    # ---------------- активы ----------------
    @app.post("/api/assets")
    def add_asset(body: AssetIn, request: Request):
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
        return {"symbol": sym, "added": added, "job": job.to_dict(request.state.user["id"]) if job else None}

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
    def list_jobs(request: Request):
        uid = request.state.user["id"]
        return {"jobs": [j.to_dict(uid) for j in jobs.list()]}

    @app.post("/api/jobs/{job_id}/ack")
    def ack_job(job_id: int, request: Request):
        uid = request.state.user["id"]
        j = jobs.ack(job_id, uid)
        if j is None:
            raise HTTPException(404, "Задача не найдена")
        return j.to_dict(uid)

    # ---------------- бэктест ----------------
    @app.post("/api/backtest")
    def run(params: BacktestParams):
        try:
            return run_backtest(store, s.defaults, params, available_fn=available_fn)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ParamError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/montecarlo")
    def montecarlo_run(req: MonteCarloRequest):
        """Монте-Карло по сделкам стратегии: разброс итога и просадки при другом порядке/составе сделок."""
        try:
            return run_montecarlo(store, s.defaults, req, available_fn=available_fn)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ParamError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/top/download")
    def top_download(params: BacktestParams, request: Request):
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
        return {"job": job.to_dict(request.state.user["id"]), "created": created, "windows": len(items)}

    # ---- состояние интерфейса (выбранные источник/актив/ТФ и параметры по каждой связке) хранится на сервере ----
    @app.get("/api/ui-state")
    def get_ui_state(request: Request):
        return db.get_ui_state(request.state.user["id"])

    @app.put("/api/ui-state")
    def put_ui_state(request: Request, state: dict = Body(...)):
        if len(json.dumps(state)) > 500_000:
            raise HTTPException(413, "Слишком большое состояние")
        db.put_ui_state(request.state.user["id"], state)
        return {"ok": True}

    # ---- мониторинг открытых позиций и автомод ----
    adapter = get_adapter()

    @app.post("/api/monitor")
    def monitor(request: Request, body: dict = Body(default={})):
        """Открытые позиции по конфигам бота пользователя (и, если передан, по текущим параметрам интерфейса) со всеми уровнями и цифрами.
        Это расчёт стратегии по свечам, а не позиции на бирже."""
        uid = request.state.user["id"]
        entries = [evaluate_entry(store, s.defaults, f"#{c['id']} {c['name']}", c["id"], c["params"], c["auto"], c["paused"], available_fn) for c in db.list_configs(uid)]
        ui = body.get("params")
        if isinstance(ui, dict):
            entries.append(evaluate_entry(store, s.defaults, "Текущие параметры интерфейса", None, ui, None, False, available_fn))
        return {"entries": entries, "log": db.list_auto_log(uid, 50), "exchange": {"name": adapter.name, "real": adapter.real},
                "data": {"source": "Binance (свечи)", "now": int(time.time() * 1000)}}

    @app.put("/api/monitor/configs/{config_id}/auto")
    def set_auto(config_id: int, request: Request, body: dict = Body(...)):
        """Автомод конфига: open — открывать позиции и доборы по сигналу, manage — переставлять стоп (БУ), закрывать и т.д. Выключен, пока явно не включён."""
        new = db.set_auto(request.state.user["id"], config_id, body.get("open"), body.get("manage"))
        if new is None:
            raise HTTPException(404, "Конфиг не найден")
        db.add_auto_log(request.state.user["id"], config_id, "auto_toggle", None, None, "info", {"auto": new, "exchange": adapter.name})
        return {"auto": new, "exchange": {"name": adapter.name, "real": adapter.real}}

    @app.post("/api/monitor/auto/off")
    def auto_off(request: Request):
        n = db.disable_all_auto(request.state.user["id"])
        db.add_auto_log(request.state.user["id"], None, "auto_off_all", None, None, "info", {"configs": n})
        return {"disabled": n}

    @app.post("/api/trading/test-position")
    def test_position(request: Request, body: dict = Body(...)):
        """Тестовая позиция на $1 по BTC (проверка связи и исполнения). Нужно явное подтверждение; сумма жёстко ограничена. Без реального коннектора — симуляция."""
        if body.get("confirm") is not True:
            raise HTTPException(422, "Нужно подтверждение (confirm: true)")
        side = body.get("side", "long")
        usd = float(body.get("usd", 1.0))
        if side not in ("long", "short") or not (0 < usd <= TEST_MAX_USD):
            raise HTTPException(422, f"Сторона long/short и сумма от 0 до {TEST_MAX_USD:g} $")
        sym = "BTC/USDT:USDT"
        try:
            res = adapter.open_market(sym, side, usd, 1.0)
            status = res.get("status", "sent")
        except NotConfigured as e:
            db.add_auto_log(request.state.user["id"], None, "test_position", sym, side, "error", {"usd": usd, "error": str(e)})
            raise HTTPException(503, str(e))
        db.add_auto_log(request.state.user["id"], None, "test_position", sym, side, status, {"usd": usd, **res})
        return {"status": status, "result": res, "exchange": {"name": adapter.name, "real": adapter.real}}

    # ---- сохранённые прогоны: именованные снимки «параметры + ключевые цифры», у каждого пользователя свои ----
    @app.get("/api/runs")
    def list_runs(request: Request):
        return {"runs": db.list_runs(request.state.user["id"])}

    @app.post("/api/runs")
    def save_run(request: Request, body: dict = Body(...)):
        params, summary = body.get("params"), body.get("summary")
        if not isinstance(params, dict) or not isinstance(summary, dict) or len(json.dumps([params, summary])) > 100_000:
            raise HTTPException(422, "Нужны params и summary (объекты)")
        try:
            return db.save_run(request.state.user["id"], str(body.get("name") or ""), params, body.get("tf", "auto"), summary)
        except DbError as e:
            raise HTTPException(422, str(e))

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: int, request: Request):
        if not db.delete_run(request.state.user["id"], run_id):
            raise HTTPException(404, "Прогон не найден")
        return {"ok": True}

    return app


app = create_app()
