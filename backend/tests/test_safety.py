"""Безопасность автоторговли: сторож бота, сверка с биржей перед входом, аварийная кнопка."""
from app import manage, watchdog
from app.db import Database
from app.trading import DryRunAdapter, ExchangeAdapter, ExchangeError


def make(tmp_path):
    db = Database(tmp_path / "w.db")
    u = db.add_user("alice", "alice-password")
    db.link_telegram("alice", 555)
    cid = db.add_config(u["id"], "c", {"symbol": "BTC/USDT:USDT"})[0]["id"]
    return db, u, cid


def test_watchdog_notifies_once_and_on_recovery(tmp_path):
    db, u, cid = make(tmp_path)
    sent, state = [], {}
    notify = lambda chat, text: sent.append((chat, text))
    assert watchdog.check(db, state, notify, now=1000)["state"] == "unknown" and not sent          # бот ещё не запускался
    db.set_meta("bot_alive", 1000)
    db.set_meta("bot_cycle", 1000)
    assert watchdog.check(db, state, notify, now=1010)["state"] == "ok" and not sent
    db.set_meta("bot_alive", 1300)                                                                  # процесс жив, циклы встали
    db.set_meta("bot_alive", 1000 + 25 * 60)
    assert watchdog.check(db, state, notify, now=1000 + 26 * 60)["state"] == "stalled"
    assert not sent                                                                                  # автомод не включён ни у кого — писать некому
    db.set_auto(u["id"], cid, True, False)
    db.set_meta("bot_alive", 1000 + 27 * 60)
    watchdog.check(db, state, notify, now=1000 + 27 * 60)
    watchdog.check(db, state, notify, now=1000 + 28 * 60)
    assert len(sent) == 1 and sent[0][0] == 555 and "завис" in sent[0][1]                           # один раз за сбой
    db.set_meta("bot_alive", 1000 + 40 * 60)
    db.set_meta("bot_cycle", 1000 + 40 * 60)
    assert watchdog.check(db, state, notify, now=1000 + 40 * 60 + 5)["state"] == "ok"
    assert len(sent) == 2 and "снова работает" in sent[1][1]
    db.set_meta("bot_alive", 1000 + 40 * 60)
    watchdog.check(db, state, notify, now=1000 + 50 * 60)                                            # процесс замолчал совсем
    assert len(sent) == 3 and "не отвечает" in sent[2][1]


class Ex(ExchangeAdapter):
    real = True
    name = "fake"

    def __init__(self, positions=None, fail=False):
        self._p, self.fail, self.closed_all = positions or [], fail, 0

    def positions(self):
        if self.fail:
            raise ExchangeError("таймаут")
        return self._p

    def close_all(self):
        self.closed_all += 1
        return {"closed": [{"symbol": p["symbol"]} for p in self._p], "cancelled": 3, "errors": []}


def test_preflight_open_checks_real_positions():
    pos = [{"symbol": "BTC/USDC:USDC", "side": "long", "size": 0.01}]
    assert manage.preflight_open(DryRunAdapter(), "BTC/USDT:USDT", "long", False) == {}                         # симуляция — без сверки
    assert "уже есть" in manage.preflight_open(Ex(pos), "BTC/USDT:USDT", "long", False)["skip"]
    assert manage.preflight_open(Ex(pos), "BTC/USDT:USDT", "short", False) == {"close_first": "long"}          # разворот: сначала закрыть встречную
    assert manage.preflight_open(Ex([]), "BTC/USDT:USDT", "long", False) == {}
    assert manage.preflight_open(Ex(pos), "ETH/USDT:USDT", "long", False) == {}                                  # другой актив
    assert manage.preflight_open(Ex(pos), "BTC/USDT:USDT", "long", True) == {}                                   # добор к существующей позиции
    assert "нет позиции" in manage.preflight_open(Ex([]), "BTC/USDT:USDT", "long", True)["skip"]
    assert "нет позиции" in manage.preflight_open(Ex(pos), "BTC/USDT:USDT", "short", True)["skip"]
    assert "не удалось проверить" in manage.preflight_open(Ex(fail=True), "BTC/USDT:USDT", "long", False)["skip"]   # биржа не отвечает — не торгуем


def test_panic_turns_auto_off_first_and_closes_everything(tmp_path):
    db, u, cid = make(tmp_path)
    db.set_auto(u["id"], cid, True, True)
    ex = Ex([{"symbol": "BTC/USDC:USDC", "side": "long", "size": 1}])
    r = manage.panic(db, ex, u["id"])
    assert r["auto_off"] == 1 and ex.closed_all == 1 and len(r["closed"]) == 1 and r["cancelled"] == 3 and not r["errors"]
    assert db.get_config(cid)["auto"] == {"open": False, "manage": False, "base": "config"}
    assert db.list_auto_log(u["id"], 5)[0]["kind"] == "panic"
    r2 = manage.panic(db, DryRunAdapter(), u["id"])                                                              # без счёта — просто выключает автомод
    assert r2["closed"] == [] and not r2["errors"]
