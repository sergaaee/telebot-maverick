"""Управление стратегиями и автомодом — общая логика для веб-интерфейса и Telegram-бота, чтобы оба вели себя одинаково."""
import math
import threading
import time

from .db import EXCHANGES, EXCHANGE_OVERRIDES, DbError
from .tradelog import trade_log
from .trading import EXCHANGE_LABELS, ExchangeError, NotConfigured, TEST_MAX_USD, forget_adapter, mask_address

TEST_SYMBOL = "BTC/USDT:USDT"


class ManageError(RuntimeError):
    def __init__(self, msg, code=422):
        super().__init__(msg)
        self.code = code


def own_config(db, uid, config_id):
    c = db.find_config(uid, str(config_id))
    if not c:
        raise ManageError("Конфиг не найден", 404)
    return c


def exchange_name(exchange):
    exchange = (exchange or "hyperliquid").strip().lower()
    if exchange not in EXCHANGES:
        raise ManageError("Неизвестная биржа: " + ", ".join(EXCHANGES))
    return exchange


def effective_params(params, over):
    """Параметры конфига с настройками биржи поверх (размер входа и вход лимиткой); сигналы и правила выхода остаются из конфига."""
    return {**params, **{k: v for k, v in (over or {}).items() if k in EXCHANGE_OVERRIDES}}


def set_auto(db, adapter, uid, config_id, open_=None, manage=None, base=None, exchange="hyperliquid"):
    """Автомод конфига на конкретной бирже (по умолчанию Hyperliquid). adapter — адаптер ЭТОЙ биржи (для журнала)."""
    exchange = exchange_name(exchange)
    own_config(db, uid, config_id)
    was = db.find_config(uid, str(config_id))
    try:
        new = db.set_auto(uid, config_id, open_, manage, base, exchange=exchange)
    except DbError as e:
        raise ManageError(str(e))
    now = db.find_config(uid, str(config_id))
    if new and new["open"] and not was["ex"][exchange]["open"] and was["catchup"]["on"]:       # автомод «открывать» включили при включённом «догнать сигнал» — взводим один заход
        db.set_catchup(uid, config_id, armed=True)
    elif new and not new["open"] and not now["any_open"]:
        db.set_catchup(uid, config_id, armed=False)
    db.add_auto_log(uid, config_id, "auto_toggle", None, None, "info", {"auto": new, "exchange": adapter.name if adapter.real else exchange, "target": exchange})
    trade_log("auto_toggle", user=uid, config=config_id, auto=new, exchange=exchange, real=adapter.real, testnet=adapter.testnet)
    return new


def set_ex_over(db, adapter, uid, config_id, exchange, **patch):
    """Настройки конфига ТОЛЬКО для одной биржи, поверх параметров конфига: доля депозита и плечо (первого входа и доборов), вход лимитным ордером.
    Значение None — убрать настройку биржи (брать из конфига). Применяется к НОВЫМ входам на этой бирже; сигналы и правила выхода (стоп, тейки, БУ) — общие, из конфига.
    -> {ключ: значение} действующих переопределений."""
    exchange = exchange_name(exchange)
    c = own_config(db, uid, config_id)
    bad = [k for k in patch if k not in EXCHANGE_OVERRIDES]
    if bad:
        raise ManageError("Для биржи нельзя менять: " + ", ".join(bad))
    if not patch:
        raise ManageError("Нечего менять")
    vals = {k: v for k, v in patch.items() if v is not None}
    for k, v in vals.items():
        if k == "entry_limit_dir":
            if v not in ("pullback", "breakout"):
                raise ManageError("entry_limit_dir: pullback или breakout")
        elif isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ManageError(f"{k}: нужно число")
    from pydantic import ValidationError
    from .schemas import BacktestParams
    try:
        BacktestParams(**vals)
    except ValidationError as e:
        err = e.errors()[0]
        raise ManageError(f"{err['loc'][0]}: {err['msg']}")
    old = dict(c["ex"][exchange]["over"])
    merged = {**old, **vals}
    for k, v in patch.items():
        if v is None:
            merged.pop(k, None)
    eff = effective_params(c["params"], merged)
    if eff.get("pyramid_enabled"):
        tp, pp = eff.get("trade_pct") or 0, eff.get("pyramid_pct") or 0
        if pp and int((100 - tp) // pp) < 1:
            raise ManageError(f"Пирамидинг: первая нога {tp:g}% депозита, на добор ({pp:g}%) места не остаётся — уменьшите доли")
    try:
        new = db.set_ex_over(uid, config_id, exchange, patch)
    except DbError as e:
        raise ManageError(str(e))
    db.add_auto_log(uid, config_id, "exchange_cfg", None, None, "info", {"exchange": exchange, "from": old, "to": new})
    trade_log("exchange_cfg", user=uid, config=config_id, name=c["name"], exchange=exchange, old=old, new=new)
    return new


SIZING_KEYS = ("trade_pct", "leverage", "pyramid_pct", "pyramid_leverage")


def set_sizing(db, adapter, uid, config_id, **new):
    """Размер входа конфига: trade_pct — доля депозита (маржа) на первый вход, leverage — плечо; для пирамидинга ещё pyramid_pct и pyramid_leverage.
    Применяется к НОВЫМ входам и ко всем расчётам по истории; позиции, уже открытые на бирже, не меняются. Проверяются те же границы, что у параметров бэктеста."""
    c = own_config(db, uid, config_id)
    upd = {k: v for k, v in new.items() if k in SIZING_KEYS and (v is not None or k == "pyramid_leverage") and k in new}
    if not upd:
        raise ManageError("Нечего менять: укажите trade_pct и/или leverage")
    for k, v in upd.items():
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise ManageError(f"{k}: нужно число")
    from pydantic import ValidationError
    from .schemas import BacktestParams
    try:
        BacktestParams(**{k: v for k, v in upd.items()})
    except ValidationError as e:
        err = e.errors()[0]
        raise ManageError(f"{err['loc'][0]}: {err['msg']}")
    old = {k: c["params"].get(k) for k in upd}
    params = {**c["params"], **upd}
    if params.get("pyramid_enabled"):
        tp, pp = params.get("trade_pct") or 0, params.get("pyramid_pct") or 0
        if pp and int((100 - tp) // pp) < 1:
            raise ManageError(f"Пирамидинг: первая нога {tp:g}% депозита, на добор ({pp:g}%) места не остаётся — уменьшите доли")
    db.update_params(c["id"], params)
    db.add_auto_log(uid, c["id"], "sizing", None, None, "info", {"from": old, "to": upd, "exchange": adapter.name})
    trade_log("sizing", user=uid, config=c["id"], name=c["name"], old=old, new=upd)
    return {k: params.get(k) for k in SIZING_KEYS}


MAX_CATCHUP_AGE_MIN = 1440.0           # «догнать сигнал»: не старше суток


def set_catchup(db, adapter, uid, config_id, on=None, age_min=None, move_pct=None):
    """«Догнать свежий сигнал»: при включении автомода (или самой этой опции при уже включённом автомоде) бот один раз входит по последнему сигналу, если он не старше age_min минут
    и цена не ушла в сторону прибыли больше чем на move_pct % от цены сигнала (то есть вход не хуже сигнального). Выключено по умолчанию."""
    c = own_config(db, uid, config_id)
    for name, v, lo, hi in (("age_min", age_min, 0.0, MAX_CATCHUP_AGE_MIN), ("move_pct", move_pct, 0.0, 5.0)):
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= v <= hi) or (name == "age_min" and v <= 0)):
            raise ManageError(f"Возраст сигнала — от 1 до {MAX_CATCHUP_AGE_MIN:g} минут (24 часа), допустимое движение цены в прибыль — от 0 до 5 %")
    cur = c["catchup"]
    new = db.set_catchup(uid, config_id, on, age_min, move_pct)
    if on and not cur["on"] and c["any_open"]:
        new = db.set_catchup(uid, config_id, armed=True)
    elif on is False:
        new = db.set_catchup(uid, config_id, armed=False)
    db.add_auto_log(uid, config_id, "catchup_cfg", None, None, "info", {"catchup": new, "exchange": adapter.name})
    trade_log("catchup_cfg", user=uid, config=config_id, catchup=new)
    return new


def auto_off_all(db, uid):
    n = db.disable_all_auto(uid)
    db.add_auto_log(uid, None, "auto_off_all", None, None, "info", {"configs": n})
    trade_log("auto_off_all", "warn", user=uid, configs=n)
    return n


def set_paused(db, uid, config_id, paused):
    """Пауза / возобновление конфига (как /pause и /resume в боте): на паузе автомод выключается; при возобновлении расчёт идёт с нуля, пропущенные сигналы не накапливаются."""
    c = own_config(db, uid, config_id)
    if paused:
        db.update_config(c["id"], paused=True)
        for name in EXCHANGES:
            db.set_auto(uid, c["id"], False, False, exchange=name)
    else:
        db.update_config(c["id"], paused=False, baseline=False, last_ts=0)
    db.add_auto_log(uid, c["id"], "pause" if paused else "resume", None, None, "info", {})
    trade_log("pause" if paused else "resume", user=uid, config=c["id"], name=c["name"])
    return bool(paused)


def remove_config(db, uid, config_id, adapter=None):
    """Удаляет конфиг. Позиция, уже открытая на бирже, остаётся (вместе со своими стопом и тейками на бирже), но бот её больше не ведёт. Лимитный ордер на вход, который ещё стоит
    на бирже, снимается: после удаления некому было бы поставить стоп и тейки, если он исполнится. Не удалось снять — ошибка, конфиг остаётся."""
    c = own_config(db, uid, config_id)
    cancelled = []
    for pend in db.pending_of(c["id"]):
        ad = adapter.get(pend["exchange"]) if isinstance(adapter, dict) else (adapter if pend["exchange"] == "hyperliquid" else None)      # adapter — один адаптер Hyperliquid или словарь {биржа: адаптер}
        if pend["order_id"] and ad is not None and ad.real:
            try:
                ad.cancel_order_id(pend["order_id"], pend["symbol"])
                cancelled.append(f"{pend['exchange']}:{pend['order_id']}")
            except (NotConfigured, ExchangeError) as e:
                raise ManageError(f"Не удалось снять лимитный ордер {pend['order_id']} на {EXCHANGE_LABELS.get(pend['exchange'], pend['exchange'])}: {e}. Конфиг не удалён — снимите ордер вручную или повторите.", 502)
    db.remove_config(c["id"])
    db.add_auto_log(uid, None, "config_removed", None, None, "info", {"name": c["name"], "cancelled_order": cancelled[0].split(":", 1)[1] if len(cancelled) == 1 else None, "cancelled_orders": cancelled})
    trade_log("config_removed", "warn", user=uid, config=c["id"], name=c["name"], cancelled_order=cancelled[0].split(":", 1)[1] if len(cancelled) == 1 else None, cancelled_orders=cancelled)
    return c["name"]


def _fail(db, uid, kind, side, e):
    db.add_auto_log(uid, None, kind, TEST_SYMBOL, side, "error", {"error": str(e)})
    trade_log(kind + "_fail", "error", user=uid, side=side, error=str(e), error_type=type(e).__name__)
    raise ManageError(str(e), 503 if isinstance(e, NotConfigured) else 502)


TEST_MAX_LEVERAGE = 25                  # потолок плеча тестовой позиции: если минимум биржи велик, растёт маржа (до TEST_MAX_USD), а не плечо


def test_size(adapter, margin):
    """(маржа, плечо) тестовой позиции: плечо подбирается так, чтобы объём прошёл минимум биржи с запасом 10 % (округление идёт вниз, а биржа проверяет объём по цене с проскальзыванием).
    Минимум берётся у самой биржи, если она его знает (Bybit: наибольшее из 5 $ и минимального количества × цена — у BTC это порядка сотни долларов); если с маржой
    margin плечо выходит больше TEST_MAX_LEVERAGE, маржа поднимается, но не выше TEST_MAX_USD. Не помещается — ManageError с цифрами."""
    need = float(adapter.min_notional or 0)
    mov = getattr(adapter, "min_order_value", None)
    if adapter.real and mov:
        try:
            need = max(need, float(mov(TEST_SYMBOL)))
        except (NotConfigured, ExchangeError):
            raise
        except Exception:
            pass
    need *= 1.1
    lev = max(1, math.ceil(round(need / margin, 6))) if need else 1
    if lev > TEST_MAX_LEVERAGE and margin < TEST_MAX_USD:
        margin = min(TEST_MAX_USD, math.ceil(round(need / TEST_MAX_LEVERAGE * 100, 6)) / 100)
        lev = max(1, math.ceil(round(need / margin, 6)))
    if lev > TEST_MAX_LEVERAGE * 2:
        raise ManageError(f"Минимальный ордер по BTC на {getattr(adapter, 'label', adapter.name)} ≈ {need / 1.1:.0f} $ объёма: тестовая позиция с маржой до {TEST_MAX_USD:g} $ не проходит "
                          f"(потребовалось бы плечо {lev}×). Проверьте работу биржи входом по сигналу или откройте тестовую позицию вручную.")
    return margin, float(lev)


def test_open(db, adapter, uid, side="long", margin=1.0):
    """Тестовая позиция по BTC: маржа 1 $ (потолок TEST_MAX_USD), плечо подбирается так, чтобы объём прошёл минимум биржи (Hyperliquid: ордер от 10 $ с запасом → 11×;
    Bybit: минимум зависит от цены монеты — если на 1 $ маржи нужно слишком большое плечо, маржа вырастает до TEST_MAX_USD)."""
    if side not in ("long", "short") or not (0 < margin <= TEST_MAX_USD):
        raise ManageError(f"Сторона long/short и маржа от 0 до {TEST_MAX_USD:g} $")
    try:
        margin, lev = test_size(adapter, margin)
    except (NotConfigured, ExchangeError) as e:
        _fail(db, uid, "test_position", side, e)
    lev = int(lev)
    notional = margin * lev
    try:
        res = adapter.open_market(TEST_SYMBOL, side, notional, float(lev))
    except (NotConfigured, ExchangeError) as e:
        _fail(db, uid, "test_position", side, e)
    status = res.get("status", "sent")
    db.add_auto_log(uid, None, "test_position", TEST_SYMBOL, side, status, {"margin": margin, **res})
    trade_log("test_position", user=uid, side=side, margin=margin, leverage=lev, notional=notional, status=status, exchange=adapter.name, testnet=adapter.testnet)
    return {"status": status, "result": res, "margin": margin, "leverage": lev, "notional": notional, "exchange": adapter.describe()}


def test_close(db, adapter, uid):
    try:
        res = adapter.close_market(TEST_SYMBOL, None, 1.0)
    except (NotConfigured, ExchangeError) as e:
        _fail(db, uid, "test_close", None, e)
    db.add_auto_log(uid, None, "test_close", TEST_SYMBOL, None, res.get("status", "sent"), res)
    trade_log("test_close", user=uid, status=res.get("status", "sent"), exchange=adapter.name)
    return {"status": res.get("status", "sent"), "result": res, "exchange": adapter.describe()}


def exchange_state(adapter, detail=False):
    """Состояние счёта на бирже: баланс и позиции (detail — ещё открытые ордера и последние исполнения). В симуляции — пусто."""
    d = adapter.describe()
    out = {"exchange": d, "positions": [], "account": {}, "orders": [], "fills": []}
    if not adapter.real:
        return out
    try:
        out["positions"], out["account"] = adapter.positions(), adapter.account()
        if detail:
            out["orders"], out["fills"] = adapter.open_orders(), adapter.fills(20)
    except (NotConfigured, ExchangeError) as e:
        raise ManageError(str(e), 503 if isinstance(e, NotConfigured) else 502)
    return out


def account_info(db, adapter, uid, exchange="hyperliquid"):
    """Подключённый счёт пользователя на бирже для веб-интерфейса: кто и где подключён (без ключа!) и всё, что видно на бирже. Сбой биржи не прячет сведения о подключении — он в поле error.
    wallet — адрес кошелька (Hyperliquid) или API-ключ (Bybit), всегда в усечённом виде."""
    exchange = exchange_name(exchange)
    acc = db.get_exchange_account(uid, exchange)
    info = {"connected": bool(acc), "exchange": adapter.describe(), "wallet": None, "agent": None, "created": None, "verified": None, "error": None, "net": None,
            "positions": [], "account": {}, "orders": [], "fills": []}
    if not acc:
        return info
    info.update(wallet=mask_address(acc["wallet"]), agent=mask_address(acc.get("agent_address")), created=acc["created"], verified=acc["verified"], net=acc.get("net"))
    try:
        info.update({k: v for k, v in exchange_state(adapter, detail=True).items() if k != "exchange"})
    except ManageError as e:
        info["error"] = str(e)
    return info


def disconnect_account(db, uid, exchange="hyperliquid"):
    """Отключает счёт на бирже: ключ удаляется из базы, автомод на этой бирже у всех конфигов выключается (иначе он молча перешёл бы в симуляцию), ожидающие входы снимаются.
    Позиции и ордера на самой бирже остаются — их закрывает пользователь или «аварийная остановка» (до отключения)."""
    exchange = exchange_name(exchange)
    removed = db.delete_exchange_account(uid, exchange)
    forget_adapter(uid, exchange)
    n = db.disable_all_auto(uid, exchange)
    for row in [r for r in db.list_pending() if r["user_id"] == uid and r["exchange"] == exchange]:
        db.clear_pending(row["config_id"], exchange)
    for row in [r for r in db.list_retries() if r["exchange"] == exchange and (db.get_config(r["config_id"]) or {}).get("user_id") == uid]:
        db.clear_retry(row["config_id"], exchange)
    db.add_auto_log(uid, None, "exchange_disconnected", None, None, "info", {"removed": removed, "auto_off": n, "exchange": exchange})
    trade_log("account_disconnected", "warn", user=uid, removed=removed, auto_off=n, exchange=exchange)
    return removed


def preflight_open(adapter, symbol, side, add):
    """Сверка с реальной биржей перед входом / добором (решение бота опирается на симуляцию, а позиция на бирже могла измениться: закрыта вручную, не закрылась и т.п.).
    -> {"skip": причина} (ордер не отправлять), {"close_first": сторона} (сначала закрыть встречную позицию) или {} (можно). Не удалось проверить — не торгуем."""
    if not adapter.real:
        return {}
    try:
        positions = adapter.positions()
    except (NotConfigured, ExchangeError) as e:
        return {"skip": f"не удалось проверить позиции на бирже ({e}) — ордер не отправлен", "retry": True}
    base = symbol.split("/")[0]
    cur = next((p for p in positions if (p.get("symbol") or "").split("/")[0] == base), None)
    if add:
        if not cur or cur["side"] != side:
            return {"skip": f"добор {side}: на бирже нет позиции {side} по {base} — добирать нечего"}
        return {}
    if cur and cur["side"] == side:
        return {"skip": f"на бирже уже есть позиция {side} по {base} ({cur['size']:g}) — повторный вход пропущен"}
    if cur:
        return {"close_first": cur["side"]}
    return {}


def panic(db, adapter, uid):
    """Аварийная остановка: автомод выключается у всех конфигов пользователя на всех биржах (сначала — чтобы бот не открыл позицию заново), затем закрываются все позиции
    и снимаются все ордера на каждой подключённой бирже. adapter — один адаптер или словарь {биржа: адаптер}. Ошибки не прерывают процесс (у одной биржи сбой не мешает другой):
    что не удалось — в errors."""
    adapters = adapter if isinstance(adapter, dict) else {adapter.name: adapter}
    multi = len(adapters) > 1
    off = db.disable_all_auto(uid)
    db.clear_pending_user(uid)                              # ожидающие входы (лимитки и «вход по ходу») снимаются; сами ордера на бирже закрывает close_all
    trade_log("panic_start", "warn", user=uid, auto_off=off, exchanges=list(adapters), real=[a.real for a in adapters.values()])
    errors, closed, cancelled = [], [], 0
    for name, ad in adapters.items():
        pref = f"{EXCHANGE_LABELS.get(name, name)}: " if multi and ad.real else ""
        try:
            r = ad.close_all()
            closed += [{**x, "exchange": name} for x in r.get("closed", [])] if multi else r.get("closed", [])
            cancelled += r.get("cancelled", 0)
            errors += [pref + str(e) for e in r.get("errors", [])]
        except (NotConfigured, ExchangeError) as e:
            errors.append(pref + str(e))
    first = next(iter(adapters.values()))
    real = next((a for a in adapters.values() if a.real), first)
    out = {"auto_off": off, "closed": closed, "cancelled": cancelled, "errors": errors, "exchange": real.describe(), "exchanges": [a.describe() for a in adapters.values()]}
    db.add_auto_log(uid, None, "panic", None, None, "error" if errors else "ok", out)
    trade_log("panic_done", "error" if errors else "warn", user=uid, **{k: v for k, v in out.items() if k not in ("exchange", "exchanges")})
    return out


_acc, _acc_lock = {}, threading.Lock()


def account_quick(adapter, uid, ttl=15.0):
    """Капитал на бирже ({"equity", "free"}) с короткой памятью, чтобы страницы с автообновлением не дёргали биржу каждый раз. Нет счёта / ошибка -> None."""
    if not adapter.real:
        return None
    now = time.monotonic()
    key = (uid, adapter.name)
    with _acc_lock:
        hit = _acc.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        a = adapter.account()
        val = {"equity": a.get("equity"), "free": a.get("free")}
    except (NotConfigured, ExchangeError):
        val = None
    with _acc_lock:
        _acc[key] = (now, val)
    return val


def sizing_warning(auto, deposit, acc):
    """Предупреждение, если размеры считаются от депозита конфига, а на бирже капитала заметно меньше (или заметно больше)."""
    if not acc or not acc.get("equity") or not auto or not auto.get("open") or auto.get("base") == "account":
        return None
    eq = acc["equity"]
    if deposit and deposit > eq * 1.5:
        return (f"Депозит конфига {deposit:,.0f} $ намного больше капитала на бирже {eq:,.0f} $: объём входа считается от {deposit:,.0f} $ и, скорее всего, не пройдёт "
                f"(лимит TRADE_MAX_USD или нехватка маржи). Выберите базу размера «баланс счёта».").replace(",", " ")
    return None


def size_order(engine_order_size, result, add, adapter, base, acc, over=None):
    """Размер ордера автомода -> {"notional", "margin", "note", "skip"}. base='account': маржа = доля конфига × реальный капитал; иначе — от депозита конфига.
    over — настройки конфига для этой биржи (доля, плечо): поверх параметров конфига. Если свободной маржи на бирже не хватает, ордер не отправляется (skip с причиной), а не ждёт отказа биржи."""
    kw = {"over": over} if over else {}
    if base == "account" and adapter.real:
        eq = (acc or {}).get("equity")
        if not eq or eq <= 0:
            return {"skip": "размер от баланса счёта: не удалось получить капитал на бирже — ордер не отправлен", "retry": True}
        notional, margin = engine_order_size(result, add, equity=eq, **kw)
        note = f"от баланса счёта {eq:,.2f} $".replace(",", " ")
    else:
        notional, margin = engine_order_size(result, add, **kw)
        note = "от депозита конфига" if base != "account" else "от депозита конфига (счёт не подключён — симуляция)"
    free = (acc or {}).get("free") if adapter.real else None
    if free is not None and margin > free * 0.98 + 1e-9:
        hint = "" if base == "account" else " Поставьте базу размера «баланс счёта» или уменьшите долю входа."
        return {"skip": f"не хватает свободной маржи: нужно {margin:,.2f} $, свободно {free:,.2f} $.{hint}".replace(",", " "), "retry": True}
    return {"notional": notional, "margin": margin, "note": note}
