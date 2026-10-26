"""Управление стратегиями и автомодом — общая логика для веб-интерфейса и Telegram-бота, чтобы оба вели себя одинаково."""
import math
import threading
import time

from .db import DbError
from .tradelog import trade_log
from .trading import ExchangeError, NotConfigured, TEST_MAX_USD, forget_adapter, mask_address

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


def set_auto(db, adapter, uid, config_id, open_=None, manage=None, base=None):
    own_config(db, uid, config_id)
    try:
        new = db.set_auto(uid, config_id, open_, manage, base)
    except DbError as e:
        raise ManageError(str(e))
    db.add_auto_log(uid, config_id, "auto_toggle", None, None, "info", {"auto": new, "exchange": adapter.name})
    trade_log("auto_toggle", user=uid, config=config_id, auto=new, exchange=adapter.name, real=adapter.real, testnet=adapter.testnet)
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
        db.set_auto(uid, c["id"], False, False)
    else:
        db.update_config(c["id"], paused=False, baseline=False, last_ts=0)
    db.add_auto_log(uid, c["id"], "pause" if paused else "resume", None, None, "info", {})
    trade_log("pause" if paused else "resume", user=uid, config=c["id"], name=c["name"])
    return bool(paused)


def remove_config(db, uid, config_id):
    c = own_config(db, uid, config_id)
    db.remove_config(c["id"])
    db.add_auto_log(uid, None, "config_removed", None, None, "info", {"name": c["name"]})
    trade_log("config_removed", "warn", user=uid, config=c["id"], name=c["name"])
    return c["name"]


def _fail(db, uid, kind, side, e):
    db.add_auto_log(uid, None, kind, TEST_SYMBOL, side, "error", {"error": str(e)})
    trade_log(kind + "_fail", "error", user=uid, side=side, error=str(e), error_type=type(e).__name__)
    raise ManageError(str(e), 503 if isinstance(e, NotConfigured) else 502)


def test_open(db, adapter, uid, side="long", margin=1.0):
    """Тестовая позиция по BTC: маржа 1 $ (потолок TEST_MAX_USD), плечо подбирается так, чтобы объём прошёл минимум биржи (Hyperliquid: ордер от 10 $ с запасом → 11×)."""
    if side not in ("long", "short") or not (0 < margin <= TEST_MAX_USD):
        raise ManageError(f"Сторона long/short и маржа от 0 до {TEST_MAX_USD:g} $")
    lev = max(1, math.ceil(adapter.min_notional * 1.1 / margin)) if adapter.min_notional else 1      # +10%: размер округляется вниз, а биржа проверяет объём по цене с проскальзыванием
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


def account_info(db, adapter, uid):
    """Подключённый счёт пользователя для веб-интерфейса: кто и где подключён (без ключа!) и всё, что видно на бирже. Сбой биржи не прячет сведения о подключении — он в поле error."""
    acc = db.get_exchange_account(uid)
    info = {"connected": bool(acc), "exchange": adapter.describe(), "wallet": None, "agent": None, "created": None, "verified": None, "error": None,
            "positions": [], "account": {}, "orders": [], "fills": []}
    if not acc:
        return info
    info.update(wallet=mask_address(acc["wallet"]), agent=mask_address(acc.get("agent_address")), created=acc["created"], verified=acc["verified"])
    try:
        info.update({k: v for k, v in exchange_state(adapter, detail=True).items() if k != "exchange"})
    except ManageError as e:
        info["error"] = str(e)
    return info


def disconnect_account(db, uid):
    """Отключает счёт: ключ удаляется из базы, автомод у всех конфигов выключается (иначе он молча перешёл бы в симуляцию)."""
    removed = db.delete_exchange_account(uid)
    forget_adapter(uid)
    n = db.disable_all_auto(uid)
    db.add_auto_log(uid, None, "exchange_disconnected", None, None, "info", {"removed": removed, "auto_off": n})
    trade_log("account_disconnected", "warn", user=uid, removed=removed, auto_off=n)
    return removed


def preflight_open(adapter, symbol, side, add):
    """Сверка с реальной биржей перед входом / добором (решение бота опирается на симуляцию, а позиция на бирже могла измениться: закрыта вручную, не закрылась и т.п.).
    -> {"skip": причина} (ордер не отправлять), {"close_first": сторона} (сначала закрыть встречную позицию) или {} (можно). Не удалось проверить — не торгуем."""
    if not adapter.real:
        return {}
    try:
        positions = adapter.positions()
    except (NotConfigured, ExchangeError) as e:
        return {"skip": f"не удалось проверить позиции на бирже ({e}) — ордер не отправлен"}
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
    """Аварийная остановка: автомод выключается у всех конфигов пользователя (сначала — чтобы бот не открыл позицию заново), затем закрываются все позиции
    на бирже и снимаются все ордера. Ошибки не прерывают процесс: что не удалось — в errors."""
    off = db.disable_all_auto(uid)
    trade_log("panic_start", "warn", user=uid, auto_off=off, exchange=adapter.name, real=adapter.real)
    errors, closed, cancelled = [], [], 0
    try:
        r = adapter.close_all()
        closed, cancelled, errors = r.get("closed", []), r.get("cancelled", 0), list(r.get("errors", []))
    except (NotConfigured, ExchangeError) as e:
        errors.append(str(e))
    out = {"auto_off": off, "closed": closed, "cancelled": cancelled, "errors": errors, "exchange": adapter.describe()}
    db.add_auto_log(uid, None, "panic", None, None, "error" if errors else "ok", out)
    trade_log("panic_done", "error" if errors else "warn", user=uid, **{k: v for k, v in out.items() if k != "exchange"})
    return out


_acc, _acc_lock = {}, threading.Lock()


def account_quick(adapter, uid, ttl=15.0):
    """Капитал на бирже ({"equity", "free"}) с короткой памятью, чтобы страницы с автообновлением не дёргали биржу каждый раз. Нет счёта / ошибка -> None."""
    if not adapter.real:
        return None
    now = time.monotonic()
    with _acc_lock:
        hit = _acc.get(uid)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        a = adapter.account()
        val = {"equity": a.get("equity"), "free": a.get("free")}
    except (NotConfigured, ExchangeError):
        val = None
    with _acc_lock:
        _acc[uid] = (now, val)
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


def size_order(engine_order_size, result, add, adapter, base, acc):
    """Размер ордера автомода -> {"notional", "margin", "note", "skip"}. base='account': маржа = доля конфига × реальный капитал; иначе — от депозита конфига.
    Если свободной маржи на бирже не хватает, ордер не отправляется (skip с причиной), а не ждёт отказа биржи."""
    if base == "account" and adapter.real:
        eq = (acc or {}).get("equity")
        if not eq or eq <= 0:
            return {"skip": "размер от баланса счёта: не удалось получить капитал на бирже — ордер не отправлен"}
        notional, margin = engine_order_size(result, add, equity=eq)
        note = f"от баланса счёта {eq:,.2f} $".replace(",", " ")
    else:
        notional, margin = engine_order_size(result, add)
        note = "от депозита конфига" if base != "account" else "от депозита конфига (счёт не подключён — симуляция)"
    free = (acc or {}).get("free") if adapter.real else None
    if free is not None and margin > free * 0.98 + 1e-9:
        hint = "" if base == "account" else " Поставьте базу размера «баланс счёта» или уменьшите долю входа."
        return {"skip": f"не хватает свободной маржи: нужно {margin:,.2f} $, свободно {free:,.2f} $.{hint}".replace(",", " ")}
    return {"notional": notional, "margin": margin, "note": note}
