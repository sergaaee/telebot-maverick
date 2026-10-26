"""FastAPI-бэкенд интерактивного просмотра бэктеста. Запуск: python cli.py serve (из папки backend/)."""
import json
import os
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
from .schemas import BacktestParams, MonteCarloRequest, WalkForwardRequest
from . import exporter, portfolio, watchdog
from .watchdog import Watchdog
from .monitor import cached_result, evaluate_entry
from .service import ParamError, run_backtest, run_montecarlo
from .walkforward import run_walkforward
from .manage import (ManageError, account_info, auto_off_all, disconnect_account, own_config, remove_config, set_auto, set_paused, panic, test_close, test_open)


def _adapter(request, db):
    """Адаптер биржи ТЕКУЩЕГО пользователя (его подключённый счёт или симуляция)."""
    return adapter_for(db, request.state.user["id"])
from .trading import adapter_for


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
        wd = None
        if os.getenv("SIGNALBOT_TOKEN", "").strip() and os.getenv("WATCHDOG", "on").strip().lower() not in ("off", "0", "false"):
            wd = Watchdog(db, watchdog.telegram_notify(os.getenv("SIGNALBOT_TOKEN").strip()))        # сторож бота: пишет в Telegram, если бот молчит
            wd.start()
        candle_sync.start()                # свечи: при запуске и затем каждые CANDLES_SYNC_MIN минут
        sync.start()                       # подгрузка при запуске сервера и затем каждые HISTORY_SYNC_MIN минут
        yield
        if wd:
            wd.stop()
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

    @app.post("/api/walkforward")
    def walkforward_run(req: WalkForwardRequest):
        """Walk-forward: подбор параметров на окне и проверка на следующем, окна сдвигаются. Показывает, не подогнана ли стратегия под прошлое."""
        try:
            return run_walkforward(store, s.defaults, req, available_fn=available_fn)
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

    @app.post("/api/monitor")
    def monitor(request: Request, body: dict = Body(default={})):
        """Открытые позиции по конфигам бота пользователя (и, если runs=true, по его сохранённым прогонам) со всеми уровнями и цифрами.
        Это расчёт стратегии по свечам, а не позиции на бирже."""
        uid = request.state.user["id"]
        entries = [{**evaluate_entry(store, s.defaults, f"#{c['id']} {c['name']}", c["id"], c["params"], c["auto"], c["paused"], available_fn),
                    "title": c["name"], "baseline": c["baseline"], "alerts": c["alerts"], "tf": c["tf"]} for c in db.list_configs(uid)]
        if body.get("runs"):                                      # сохранённые прогоны пользователя — тоже в мониторинг (без автомода)
            entries += [evaluate_entry(store, s.defaults, f"Прогон: {r['name']}", None, r["params"], None, False, available_fn) for r in db.list_runs(uid)]
        return {"entries": entries, "log": db.list_auto_log(uid, 50), "exchange": _adapter(request, db).describe(), "bot": watchdog.bot_status(db),
                "data": {"source": "Binance (свечи)", "now": int(time.time() * 1000)}}

    def _guard(fn, *a, **k):
        try:
            return fn(*a, **k)
        except ManageError as e:
            raise HTTPException(e.code, str(e))

    def _own(request, config_id):
        return _guard(own_config, db, request.state.user["id"], config_id)

    @app.get("/api/monitor/configs/{config_id}")
    def monitor_detail(config_id: int, request: Request):
        """Подробности по одной стратегии: позиция по ногам, статистика, история закрытых сделок, параметры и её журнал действий."""
        c = _own(request, config_id)
        e = evaluate_entry(store, s.defaults, f"#{c['id']} {c['name']}", c["id"], c["params"], c["auto"], c["paused"], available_fn, detail=True)
        return {**e, "title": c["name"], "baseline": c["baseline"], "alerts": c["alerts"], "tf": c["tf"], "created": c["created"],
                "log": db.list_auto_log(request.state.user["id"], 30, config_id)}

    @app.post("/api/monitor/configs/{config_id}/pause")
    def monitor_pause(config_id: int, request: Request, body: dict = Body(...)):
        """Пауза / возобновление конфига бота: на паузе бот не считает и не шлёт сигналы, автомод выключен; при возобновлении сигналы, пропущенные на паузе, не накапливаются."""
        return {"paused": _guard(set_paused, db, request.state.user["id"], config_id, bool(body.get("paused")))}

    @app.put("/api/monitor/configs/{config_id}/name")
    def monitor_rename(config_id: int, request: Request, body: dict = Body(...)):
        c = _own(request, config_id)
        name = str(body.get("name") or "").strip()[:60]
        if not name:
            raise HTTPException(422, "Пустое имя")
        try:
            db.update_config(c["id"], name=name)
        except DbError as e:
            raise HTTPException(409, str(e))
        return {"name": name}

    @app.delete("/api/monitor/configs/{config_id}")
    def monitor_delete(config_id: int, request: Request):
        _guard(remove_config, db, request.state.user["id"], config_id)
        return {"ok": True}

    @app.get("/api/monitor/configs/{config_id}/export")
    def monitor_export(config_id: int, request: Request):
        c = _own(request, config_id)
        return {"format": "maverick-config", "version": 1, "name": c["name"], "tf": c["tf"], "params": c["params"]}

    @app.put("/api/monitor/configs/{config_id}/auto")
    def set_auto_ep(config_id: int, request: Request, body: dict = Body(...)):
        """Автомод конфига: open — открывать позиции и доборы по сигналу, manage — переставлять стоп (БУ), закрывать и т.д. Выключен, пока явно не включён."""
        new = _guard(set_auto, db, _adapter(request, db), request.state.user["id"], config_id, body.get("open"), body.get("manage"))
        return {"auto": new, "exchange": _adapter(request, db).describe()}

    @app.post("/api/monitor/auto/off")
    def auto_off(request: Request):
        return {"disabled": auto_off_all(db, request.state.user["id"])}

    @app.post("/api/portfolio")
    def portfolio_ep(request: Request):
        """Портфель всех стратегий пользователя: кривые капитала рядом, общая кривая, корреляция дневных результатов, суммарная экспозиция по активам и предупреждения о риске."""
        uid = request.state.user["id"]
        rows = portfolio.collect(store, s.defaults, db.list_configs(uid), available_fn)
        real = None
        ad = _adapter(request, db)
        if ad.real:
            try:
                real = {"equity": ad.account().get("equity"), "positions": ad.positions()}
            except Exception:                      # биржа недоступна — портфель показываем без блока «реальная биржа»
                real = None
        return portfolio.build_portfolio(rows, real)

    @app.get("/api/export/{kind}.csv")
    def export_csv(kind: str, request: Request, config_id: int | None = Query(None)):
        """CSV-выгрузки: trades — сделки стратегий (расчёт; config_id — одной, иначе всех), autolog — журнал автоторговли, fills — реальные исполнения на бирже."""
        uid = request.state.user["id"]
        if kind == "trades":
            cfgs = [_own(request, config_id)] if config_id is not None else db.list_configs(uid)
            items = []
            for c in cfgs:
                try:
                    items.append((c["name"], cached_result(store, s.defaults, c["params"], available_fn)))
                except (ParamError, FileNotFoundError, KeyError, ValueError):
                    continue
            body = exporter.trades_csv(items)
        elif kind == "autolog":
            body = exporter.autolog_csv(db.list_auto_log(uid, 500, config_id))
        elif kind == "fills":
            ad = _adapter(request, db)
            if not ad.real:
                raise HTTPException(409, "Счёт биржи не подключён — реальных исполнений нет")
            try:
                body = exporter.fills_csv(ad.fills(500))
            except Exception as e:
                raise HTTPException(502, str(e))
        else:
            raise HTTPException(404, "Неизвестная выгрузка")
        return Response(body, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="maverick-{kind}.csv"'})

    @app.get("/api/digest")
    def digest_get(request: Request):
        u = request.state.user
        return {"mode": u.get("digest") or "off", "telegram": bool(u["telegram_id"]), "hour_utc": int(os.getenv("DIGEST_HOUR_UTC", "8") or 8)}

    @app.put("/api/digest")
    def digest_put(request: Request, body: dict = Body(...)):
        """Регулярная сводка в Telegram: off / daily (каждый день) / weekly (по понедельникам). Нужен привязанный Telegram."""
        mode = str(body.get("mode") or "")
        u = request.state.user
        if mode != "off" and not u["telegram_id"]:
            raise HTTPException(409, "Сводка приходит в Telegram: сначала привяжите бота (/link)")
        try:
            db.set_digest(u["id"], mode)
        except DbError as e:
            raise HTTPException(422, str(e))
        return {"mode": mode}

    @app.post("/api/panic")
    def panic_ep(request: Request, body: dict = Body(default={})):
        """Аварийная кнопка: автомод выключается у всех конфигов, все позиции на бирже закрываются по рынку, все ордера снимаются. Нужно confirm: true."""
        if body.get("confirm") is not True:
            raise HTTPException(422, "Нужно подтверждение (confirm: true)")
        return panic(db, _adapter(request, db), request.state.user["id"])

    @app.post("/api/trading/test-position")
    def test_position(request: Request, body: dict = Body(...)):
        """Тестовая позиция по BTC: маржа 1 $ (потолок 5 $), плечо подбирается так, чтобы объём прошёл минимум биржи (на Hyperliquid ордер от 10 $ объёма → плечо 10×).
        Нужно явное подтверждение; без реального адаптера — симуляция. Закрывается кнопкой «Закрыть тестовую»."""
        if body.get("confirm") is not True:
            raise HTTPException(422, "Нужно подтверждение (confirm: true)")
        return _guard(test_open, db, _adapter(request, db), request.state.user["id"], body.get("side", "long"), float(body.get("usd", 1.0)))

    @app.post("/api/trading/test-position/close")
    def test_position_close(request: Request, body: dict = Body(default={})):
        """Закрыть позицию по BTC на бирже целиком (reduce-only по рынку) и снять висящие ордера."""
        return _guard(test_close, db, _adapter(request, db), request.state.user["id"])

    @app.get("/api/exchange/account")
    def exchange_account(request: Request):
        """Подключённый счёт Hyperliquid пользователя: кто подключён (адрес маскируется, ключ не отдаётся никогда), баланс, позиции, открытые ордера, последние исполнения."""
        return account_info(db, _adapter(request, db), request.state.user["id"])

    @app.delete("/api/exchange/account")
    def exchange_disconnect(request: Request):
        """Отключить счёт: ключ удаляется из базы, автомод выключается. Подключить заново можно только через бота (/hl)."""
        return {"removed": disconnect_account(db, request.state.user["id"])}

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
