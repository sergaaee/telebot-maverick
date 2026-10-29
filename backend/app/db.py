"""SQLite-база пользователей: логины и пароли, сессии сайта, привязка Telegram, сохранённое состояние интерфейса и конфиги бота.

Один файл (по умолчанию data/maverick.db) используют и backend, и бот сигналов. Каждое обращение открывает своё соединение
(WAL + таймаут ожидания блокировки), поэтому потоки и процессы друг другу не мешают. Данные рынка (свечи, история сообщений ботов)
общие для всех и лежат в data/ как раньше — в базе только то, что принадлежит пользователю."""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created REAL NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    disabled INTEGER NOT NULL DEFAULT 0,
    telegram_id INTEGER UNIQUE,
    telegram_name TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ui_state (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS link_codes (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    params TEXT NOT NULL,
    tf TEXT NOT NULL DEFAULT 'auto',
    paused INTEGER NOT NULL DEFAULT 0,
    baseline INTEGER NOT NULL DEFAULT 0,
    last_ts INTEGER NOT NULL DEFAULT 0,
    alerts INTEGER NOT NULL DEFAULT 0,
    fill_ts INTEGER,
    ready TEXT,
    auto TEXT,
    error TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS saved_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    params TEXT NOT NULL,
    tf TEXT NOT NULL DEFAULT 'auto',
    summary TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_accounts (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    exchange TEXT NOT NULL,
    wallet TEXT NOT NULL,
    key_enc TEXT NOT NULL,
    testnet INTEGER NOT NULL,
    agent_address TEXT,
    created REAL NOT NULL,
    verified REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    config_id INTEGER,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    symbol TEXT,
    side TEXT,
    status TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS pending_entries (
    config_id INTEGER PRIMARY KEY REFERENCES configs(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    signal_ts INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    mode TEXT NOT NULL,
    level REAL NOT NULL,
    ref REAL NOT NULL,
    notional REAL NOT NULL,
    margin REAL NOT NULL,
    leverage REAL NOT NULL,
    order_id TEXT,
    created REAL NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS auto_retry (
    config_id INTEGER PRIMARY KEY REFERENCES configs(id) ON DELETE CASCADE,
    signal_ts INTEGER NOT NULL,
    side TEXT NOT NULL,
    pyramid INTEGER NOT NULL DEFAULT 0,
    leg INTEGER,
    reason TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_autolog_user ON auto_log(user_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_user ON saved_runs(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""

LOGIN_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
MIN_PASSWORD = 8
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class DbError(ValueError):
    """Ошибка данных пользователя (понятный текст для CLI/интерфейса)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    n, r, p = 2 ** 14, 8, 1
    h = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        calc = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(h) // 2)
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _user(row):
    if row is None:
        return None
    return {"id": row["id"], "login": row["login"], "is_admin": bool(row["is_admin"]), "disabled": bool(row["disabled"]),
            "telegram_id": row["telegram_id"], "telegram_name": row["telegram_name"], "created": row["created"],
            "digest": (row["digest"] if "digest" in row.keys() else None) or "off", "digest_ts": row["digest_ts"] if "digest_ts" in row.keys() else None}


class Database:
    def __init__(self, path):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)
            ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
            if "digest" not in ucols:        # регулярная сводка в Telegram: off / daily / weekly
                c.execute("ALTER TABLE users ADD COLUMN digest TEXT")
            if "digest_ts" not in ucols:     # когда сводка последний раз отправлена
                c.execute("ALTER TABLE users ADD COLUMN digest_ts REAL")
            cols = {r["name"] for r in c.execute("PRAGMA table_info(configs)")}
            if "auto" not in cols:           # автомод: JSON {"open": bool, "manage": bool}; NULL = выключен
                c.execute("ALTER TABLE configs ADD COLUMN auto TEXT")
            if "ready" not in cols:          # состояние «ждёт подтверждения»: NULL = ещё не фиксировали (первое сообщение не шлём)
                c.execute("ALTER TABLE configs ADD COLUMN ready TEXT")
            if "catchup" not in cols:        # «догнать свежий сигнал при включении автомода»: JSON {"on", "age_min", "move_pct", "armed"}; NULL = выключено
                c.execute("ALTER TABLE configs ADD COLUMN catchup TEXT")
            if "fill_ts" not in cols:        # база от версии до уведомлений об исполнениях: NULL = «ещё не фиксировали», прошлые исполнения не присылаем
                c.execute("ALTER TABLE configs ADD COLUMN fill_ts INTEGER")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    # ---------------- пользователи ----------------
    def add_user(self, login, password, is_admin=None):
        login = (login or "").strip()
        if not LOGIN_RE.match(login):
            raise DbError("Логин: 3–32 символа, латиница, цифры и . _ -")
        if len(password or "") < MIN_PASSWORD:
            raise DbError(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
        with self.conn() as c:
            first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            try:
                cur = c.execute("INSERT INTO users(login, password_hash, created, is_admin) VALUES (?,?,?,?)",
                                (login, hash_password(password), time.time(), int(first if is_admin is None else is_admin)))
            except sqlite3.IntegrityError:
                raise DbError(f"Пользователь «{login}» уже существует") from None
            return self.get_user(cur.lastrowid, c)

    def get_user(self, ref, c=None):
        """По id (int) или логину."""
        def q(conn):
            if isinstance(ref, int):
                return _user(conn.execute("SELECT * FROM users WHERE id=?", (ref,)).fetchone())
            return _user(conn.execute("SELECT * FROM users WHERE login=?", (str(ref),)).fetchone())
        if c is not None:
            return q(c)
        with self.conn() as conn:
            return q(conn)

    def list_users(self):
        with self.conn() as c:
            return [_user(r) for r in c.execute("SELECT * FROM users ORDER BY id")]

    def user_count(self):
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def set_password(self, login, password):
        if len(password or "") < MIN_PASSWORD:
            raise DbError(f"Пароль должен быть не короче {MIN_PASSWORD} символов")
        u = self.get_user(login)
        if not u:
            raise DbError(f"Нет пользователя «{login}»")
        with self.conn() as c:
            c.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), u["id"]))
            c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))            # смена пароля разлогинивает везде

    def set_disabled(self, login, disabled):
        u = self.get_user(login)
        if not u:
            raise DbError(f"Нет пользователя «{login}»")
        with self.conn() as c:
            c.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), u["id"]))
            if disabled:
                c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))

    def remove_user(self, login):
        u = self.get_user(login)
        if not u:
            raise DbError(f"Нет пользователя «{login}»")
        with self.conn() as c:
            c.execute("DELETE FROM users WHERE id=?", (u["id"],))               # сессии, состояние и конфиги удаляются каскадом

    def check_login(self, login, password):
        """-> пользователь или None. Время проверки не зависит от того, существует ли логин."""
        with self.conn() as c:
            row = c.execute("SELECT * FROM users WHERE login=?", (login or "",)).fetchone()
        stored = row["password_hash"] if row else hash_password("dummy-password")
        ok = verify_password(password or "", stored)
        if row and ok and not row["disabled"]:
            return _user(row)
        return None

    # ---------------- сессии сайта ----------------
    def create_session(self, user_id, days=30):
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            c.execute("INSERT INTO sessions(token_hash, user_id, created, expires) VALUES (?,?,?,?)", (_hash_token(token), user_id, now, now + days * 86400))
        return token

    def session_user(self, token):
        if not token:
            return None
        with self.conn() as c:
            row = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=? AND s.expires>? AND u.disabled=0",
                            (_hash_token(token), time.time())).fetchone()
        return _user(row)

    def delete_session(self, token):
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_hash_token(token or ""),))

    # ---------------- состояние интерфейса ----------------
    def get_ui_state(self, user_id):
        with self.conn() as c:
            row = c.execute("SELECT data FROM ui_state WHERE user_id=?", (user_id,)).fetchone()
        try:
            return json.loads(row["data"]) if row else {}
        except ValueError:
            return {}

    def put_ui_state(self, user_id, state):
        with self.conn() as c:
            c.execute("INSERT INTO ui_state(user_id, data, updated) VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                      (user_id, json.dumps(state, ensure_ascii=False), time.time()))

    # ---------------- счёт на бирже (ключ API-кошелька хранится ЗАШИФРОВАННЫМ) ----------------
    def set_exchange_account(self, user_id, exchange, wallet, key_enc, testnet, agent_address=None):
        now = time.time()
        with self.conn() as c:
            old = c.execute("SELECT created FROM exchange_accounts WHERE user_id=?", (user_id,)).fetchone()
            c.execute("INSERT INTO exchange_accounts(user_id, exchange, wallet, key_enc, testnet, agent_address, created, verified) VALUES (?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(user_id) DO UPDATE SET exchange=excluded.exchange, wallet=excluded.wallet, key_enc=excluded.key_enc, testnet=excluded.testnet, "
                      "agent_address=excluded.agent_address, verified=excluded.verified",
                      (user_id, exchange, wallet, key_enc, int(bool(testnet)), agent_address, old["created"] if old else now, now))

    def get_exchange_account(self, user_id):
        with self.conn() as c:
            row = c.execute("SELECT * FROM exchange_accounts WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["testnet"] = bool(d["testnet"])
        return d

    def delete_exchange_account(self, user_id):
        with self.conn() as c:
            return c.execute("DELETE FROM exchange_accounts WHERE user_id=?", (user_id,)).rowcount > 0

    def set_digest(self, user_id, mode):
        if mode not in ("off", "daily", "weekly"):
            raise DbError("Сводка: off, daily или weekly")
        with self.conn() as c:
            c.execute("UPDATE users SET digest=?, digest_ts=? WHERE id=?", (mode, time.time() if mode != "off" else None, user_id))

    def mark_digest_sent(self, user_id, ts=None):
        with self.conn() as c:
            c.execute("UPDATE users SET digest_ts=? WHERE id=?", (time.time() if ts is None else ts, user_id))

    # ---------------- личные настройки пользователя (JSON по ключу) ----------------
    def get_pref(self, user_id, key, default=None):
        with self.conn() as c:
            r = c.execute("SELECT value FROM user_prefs WHERE user_id=? AND key=?", (user_id, key)).fetchone()
        try:
            return json.loads(r["value"]) if r else default
        except ValueError:
            return default

    def set_pref(self, user_id, key, value):
        with self.conn() as c:
            c.execute("INSERT INTO user_prefs(user_id, key, value) VALUES (?,?,?) ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value", (user_id, key, json.dumps(value, ensure_ascii=False)))

    # ---------------- ожидающие входы: лимитный ордер на вход / ожидание цены для входа по ходу ----------------
    def set_pending(self, config_id, user_id, signal_ts, symbol, side, mode, level, ref, notional, margin, leverage, order_id, expires):
        with self.conn() as c:
            c.execute("INSERT INTO pending_entries(config_id, user_id, signal_ts, symbol, side, mode, level, ref, notional, margin, leverage, order_id, created, expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(config_id) DO UPDATE SET user_id=excluded.user_id, signal_ts=excluded.signal_ts, symbol=excluded.symbol, side=excluded.side, mode=excluded.mode, level=excluded.level, "
                      "ref=excluded.ref, notional=excluded.notional, margin=excluded.margin, leverage=excluded.leverage, order_id=excluded.order_id, created=excluded.created, expires=excluded.expires",
                      (config_id, user_id, int(signal_ts), symbol, side, mode, level, ref, notional, margin, leverage, None if order_id is None else str(order_id), time.time(), expires))

    def get_pending(self, config_id):
        with self.conn() as c:
            r = c.execute("SELECT * FROM pending_entries WHERE config_id=?", (config_id,)).fetchone()
        return dict(r) if r else None

    def list_pending(self):
        with self.conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM pending_entries ORDER BY created")]

    def clear_pending(self, config_id):
        with self.conn() as c:
            c.execute("DELETE FROM pending_entries WHERE config_id=?", (config_id,))

    def clear_pending_user(self, user_id):
        with self.conn() as c:
            return c.execute("DELETE FROM pending_entries WHERE user_id=?", (user_id,)).rowcount

    # ---------------- вход, который не удался: повторить при перезапуске бота ----------------
    def set_retry(self, config_id, signal_ts, side, pyramid=False, leg=None, reason=None):
        with self.conn() as c:
            c.execute("INSERT INTO auto_retry(config_id, signal_ts, side, pyramid, leg, reason, created) VALUES (?,?,?,?,?,?,?) "
                      "ON CONFLICT(config_id) DO UPDATE SET signal_ts=excluded.signal_ts, side=excluded.side, pyramid=excluded.pyramid, leg=excluded.leg, reason=excluded.reason, created=excluded.created",
                      (config_id, int(signal_ts), side, int(bool(pyramid)), leg, (reason or "")[:300], time.time()))

    def list_retries(self):
        with self.conn() as c:
            return [dict(r) | {"pyramid": bool(r["pyramid"])} for r in c.execute("SELECT * FROM auto_retry ORDER BY created")]

    def clear_retry(self, config_id):
        with self.conn() as c:
            c.execute("DELETE FROM auto_retry WHERE config_id=?", (config_id,))

    # ---------------- служебные значения (пульс бота и т.п.) ----------------
    def set_meta(self, key, value):
        with self.conn() as c:
            c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def get_meta(self, key, default=None):
        with self.conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    # ---------------- автомод: действия (журнал) ----------------
    def set_auto(self, user_id, cfg_id, open_=None, manage=None, base=None):
        if base not in (None, "config", "account"):
            raise DbError("База размера: config или account")
        with self.conn() as c:
            row = c.execute("SELECT * FROM configs WHERE id=? AND user_id=?", (cfg_id, user_id)).fetchone()
            if not row:
                return None
            cur = self._cfg(row)["auto"]
            new = {"open": cur["open"] if open_ is None else bool(open_), "manage": cur["manage"] if manage is None else bool(manage), "base": base or cur["base"]}
            c.execute("UPDATE configs SET auto=? WHERE id=?", (json.dumps(new), cfg_id))
            return new

    def set_catchup(self, user_id, cfg_id, on=None, age_min=None, move_pct=None, armed=None):
        """Настройки «догнать свежий сигнал»: on — включено, age_min — не старше стольких минут, move_pct — цена не должна уйти в сторону прибыли больше чем на столько %,
        armed — «взведено»: бот при ближайшей проверке один раз попробует догнать сигнал и снимет отметку."""
        with self.conn() as c:
            row = c.execute("SELECT * FROM configs WHERE id=? AND user_id=?", (cfg_id, user_id)).fetchone()
            if not row:
                return None
            cur = self._cfg(row)["catchup"]
            new = {"on": cur["on"] if on is None else bool(on), "age_min": cur["age_min"] if age_min is None else float(age_min),
                   "move_pct": cur["move_pct"] if move_pct is None else float(move_pct), "armed": cur["armed"] if armed is None else bool(armed)}
            c.execute("UPDATE configs SET catchup=? WHERE id=?", (json.dumps(new), cfg_id))
            return new

    def disable_all_auto(self, user_id):
        """Выключает открытие и управление у всех конфигов пользователя (выбранная база размера сохраняется). -> у скольких конфигов автомод был включён."""
        n = 0
        with self.conn() as c:
            for row in c.execute("SELECT * FROM configs WHERE user_id=? AND auto IS NOT NULL", (user_id,)).fetchall():
                a = self._cfg(row)["auto"]
                if a["open"] or a["manage"]:
                    n += 1
                c.execute("UPDATE configs SET auto=? WHERE id=?", (json.dumps({"open": False, "manage": False, "base": a["base"]}) if a["base"] != "config" else None, row["id"]))
                c.execute("UPDATE configs SET catchup=json_set(COALESCE(catchup, '{}'), '$.armed', json('false')) WHERE id=? AND catchup IS NOT NULL", (row["id"],))
        return n

    def add_auto_log(self, user_id, config_id, kind, symbol, side, status, details):
        with self.conn() as c:
            c.execute("INSERT INTO auto_log(user_id, config_id, ts, kind, symbol, side, status, details) VALUES (?,?,?,?,?,?,?,?)",
                      (user_id, config_id, time.time(), kind, symbol, side, status, json.dumps(details, ensure_ascii=False)))
            c.execute("DELETE FROM auto_log WHERE user_id=? AND id NOT IN (SELECT id FROM auto_log WHERE user_id=? ORDER BY id DESC LIMIT 500)", (user_id, user_id))

    def list_auto_log(self, user_id, limit=50, config_id=None):
        with self.conn() as c:
            if config_id is None:
                rows = c.execute("SELECT * FROM auto_log WHERE user_id=? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM auto_log WHERE user_id=? AND config_id=? ORDER BY id DESC LIMIT ?", (user_id, config_id, limit)).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    # ---------------- сохранённые прогоны (у каждого пользователя свои) ----------------
    MAX_RUNS = 200

    @staticmethod
    def _run(row):
        d = dict(row)
        d["params"], d["summary"] = json.loads(d["params"]), json.loads(d["summary"])
        return d

    def list_runs(self, user_id):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM saved_runs WHERE user_id=? ORDER BY created DESC, id DESC", (user_id,)).fetchall()
        return [self._run(r) for r in rows]

    def save_run(self, user_id, name, params, tf, summary):
        """Новый прогон; имя уникально у пользователя без учёта регистра (в Python: SQLite NOCASE не знает кириллицу) — прогон с тем же именем перезаписывается."""
        name = (name or "").strip()
        if not name or len(name) > 80:
            raise DbError("Название прогона: от 1 до 80 символов")
        now = time.time()
        with self.conn() as c:
            same = next((r for r in c.execute("SELECT id, name FROM saved_runs WHERE user_id=?", (user_id,)) if r["name"].casefold() == name.casefold()), None)
            if same:
                c.execute("UPDATE saved_runs SET name=?, params=?, tf=?, summary=?, created=? WHERE id=?",
                          (name, json.dumps(params, ensure_ascii=False), str(tf), json.dumps(summary, ensure_ascii=False), now, same["id"]))
                rid = same["id"]
            else:
                if c.execute("SELECT COUNT(*) FROM saved_runs WHERE user_id=?", (user_id,)).fetchone()[0] >= self.MAX_RUNS:
                    raise DbError(f"Не больше {self.MAX_RUNS} сохранённых прогонов — удалите ненужные")
                rid = c.execute("INSERT INTO saved_runs(user_id, name, params, tf, summary, created) VALUES (?,?,?,?,?,?)",
                                (user_id, name, json.dumps(params, ensure_ascii=False), str(tf), json.dumps(summary, ensure_ascii=False), now)).lastrowid
            return self._run(c.execute("SELECT * FROM saved_runs WHERE id=?", (rid,)).fetchone())

    def delete_run(self, user_id, run_id):
        with self.conn() as c:
            return c.execute("DELETE FROM saved_runs WHERE id=? AND user_id=?", (run_id, user_id)).rowcount > 0

    # ---------------- привязка Telegram ----------------
    def create_link_code(self, user_id, ttl=600):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        now = time.time()
        with self.conn() as c:
            c.execute("DELETE FROM link_codes WHERE expires < ? OR user_id=?", (now, user_id))
            c.execute("INSERT INTO link_codes(code, user_id, expires) VALUES (?,?,?)", (code, user_id, now + ttl))
        return code

    def consume_link_code(self, code, telegram_id, telegram_name=None):
        """Привязывает Telegram-аккаунт к пользователю по одноразовому коду. -> пользователь. Бросает DbError."""
        code = (code or "").strip().upper()
        with self.conn() as c:
            row = c.execute("SELECT * FROM link_codes WHERE code=? AND expires>?", (code, time.time())).fetchone()
            if not row:
                raise DbError("Код не подошёл или устарел. Получите новый на сайте (меню пользователя → Telegram).")
            other = c.execute("SELECT login FROM users WHERE telegram_id=? AND id<>?", (telegram_id, row["user_id"])).fetchone()
            if other:
                raise DbError(f"Этот Telegram уже привязан к пользователю «{other['login']}». Сначала отвяжите его.")
            c.execute("UPDATE users SET telegram_id=?, telegram_name=? WHERE id=?", (telegram_id, telegram_name, row["user_id"]))
            c.execute("DELETE FROM link_codes WHERE code=?", (code,))
        return self.get_user(row["user_id"])

    def link_telegram(self, login, telegram_id):
        u = self.get_user(login)
        if not u:
            raise DbError(f"Нет пользователя «{login}»")
        with self.conn() as c:
            try:
                c.execute("UPDATE users SET telegram_id=? WHERE id=?", (telegram_id, u["id"]))
            except sqlite3.IntegrityError:
                raise DbError("Этот Telegram id уже привязан к другому пользователю") from None

    def unlink_telegram(self, user_id):
        with self.conn() as c:
            c.execute("UPDATE users SET telegram_id=NULL, telegram_name=NULL WHERE id=?", (user_id,))

    def user_by_telegram(self, telegram_id):
        with self.conn() as c:
            return _user(c.execute("SELECT * FROM users WHERE telegram_id=? AND disabled=0", (telegram_id,)).fetchone())

    # ---------------- конфиги бота (у каждого пользователя свои) ----------------
    @staticmethod
    def _cfg(row):
        if row is None:
            return None
        d = dict(row)
        d["params"] = json.loads(d["params"])
        d["paused"], d["baseline"] = bool(d["paused"]), bool(d["baseline"])
        try:
            a = json.loads(d.get("auto") or "{}")
        except ValueError:
            a = {}
        # автомод выключен, пока явно не включён; base — от чего считать размер входа: config (депозит из конфига, как в бэктесте) или account (реальный капитал на бирже)
        try:
            cu = json.loads(d.get("catchup") or "{}")
        except ValueError:
            cu = {}
        d["catchup"] = {"on": bool(cu.get("on")), "age_min": float(cu.get("age_min") or 15.0), "move_pct": float(cu["move_pct"]) if cu.get("move_pct") is not None else 0.1, "armed": bool(cu.get("armed"))}
        d["auto"] = {"open": bool(a.get("open")), "manage": bool(a.get("manage")), "base": a.get("base") if a.get("base") in ("config", "account") else "config"}
        return d

    def list_configs(self, user_id=None):
        with self.conn() as c:
            rows = c.execute("SELECT * FROM configs" + (" WHERE user_id=?" if user_id is not None else "") + " ORDER BY id",
                             (user_id,) if user_id is not None else ()).fetchall()
        return [self._cfg(r) for r in rows]

    @staticmethod
    def _by_name(c, user_id, name):
        """Имя без учёта регистра, в том числе кириллицы (NOCASE в SQLite понимает только латиницу)."""
        want = name.strip().casefold()
        return next((r for r in c.execute("SELECT * FROM configs WHERE user_id=?", (user_id,)) if r["name"].casefold() == want), None)

    def get_config(self, cfg_id):
        with self.conn() as c:
            return self._cfg(c.execute("SELECT * FROM configs WHERE id=?", (cfg_id,)).fetchone())

    def find_config(self, user_id, ref):
        """Конфиг пользователя по номеру (#3, 3) или имени. Чужие конфиги недоступны."""
        ref = (ref or "").strip().lstrip("#")
        with self.conn() as c:
            row = None
            if ref.isdigit():
                row = c.execute("SELECT * FROM configs WHERE user_id=? AND id=?", (user_id, int(ref))).fetchone()
            if row is None and ref:
                row = self._by_name(c, user_id, ref)
        return self._cfg(row)

    def add_config(self, user_id, name, params, tf="auto"):
        """Новый конфиг или обновление с тем же именем (расчёт и база сигналов начинаются заново). -> (конфиг, создан_ли)"""
        now = time.time()
        with self.conn() as c:
            old = self._by_name(c, user_id, name)
            if old:
                c.execute("UPDATE configs SET params=?, tf=?, baseline=0, last_ts=0, fill_ts=NULL, ready=NULL, auto=NULL, catchup=NULL, error=NULL, updated=? WHERE id=?",
                          (json.dumps(params, ensure_ascii=False), str(tf), now, old["id"]))
                cid, created = old["id"], False
            else:
                cid = c.execute("INSERT INTO configs(user_id, name, params, tf, created, updated) VALUES (?,?,?,?,?,?)",
                                (user_id, name, json.dumps(params, ensure_ascii=False), str(tf), now, now)).lastrowid
                created = True
            return self._cfg(c.execute("SELECT * FROM configs WHERE id=?", (cid,)).fetchone()), created

    def update_config(self, cfg_id, **fields):
        allowed = {"name", "paused", "baseline", "last_ts", "alerts", "error", "fill_ts", "ready", "auto"}
        sets = {k: (int(v) if isinstance(v, bool) else v) for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self.conn() as c:
            if "name" in sets:
                row = c.execute("SELECT user_id FROM configs WHERE id=?", (cfg_id,)).fetchone()
                clash = row and self._by_name(c, row["user_id"], sets["name"])
                if clash and clash["id"] != cfg_id:
                    raise DbError("Такое имя уже занято")
            try:
                c.execute(f"UPDATE configs SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), cfg_id))
            except sqlite3.IntegrityError:
                raise DbError("Такое имя уже занято") from None

    def update_params(self, cfg_id, params):
        """Заменяет параметры конфига (например, размер входа и плечо); состояние бота по конфигу (последний сигнал, исполнения) не сбрасывается."""
        with self.conn() as c:
            c.execute("UPDATE configs SET params=?, updated=? WHERE id=?", (json.dumps(params, ensure_ascii=False), time.time(), cfg_id))

    def remove_config(self, cfg_id):
        with self.conn() as c:
            c.execute("DELETE FROM configs WHERE id=?", (cfg_id,))

    def unique_config_name(self, user_id, base):
        names = {c["name"].lower() for c in self.list_configs(user_id)}
        if base.lower() not in names:
            return base
        i = 2
        while f"{base} {i}".lower() in names:
            i += 1
        return f"{base} {i}"
