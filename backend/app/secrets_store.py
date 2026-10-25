"""Шифрование секретов в базе (ключи API-кошельков бирж). Fernet (AES-128-CBC + HMAC): без мастер-ключа содержимое базы бесполезно.

Мастер-ключ — переменная SECRET_KEY (urlsafe base64 от 32 байт: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`);
если её нет, ключ создаётся один раз и хранится файлом secret.key рядом с базой (права 600). Потеряли мастер-ключ — сохранённые ключи бирж расшифровать нельзя: подключите счёт заново."""
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class SecretError(RuntimeError):
    pass


def _master(db_path=None):
    env = os.getenv("SECRET_KEY", "").strip()
    if env:
        return env.encode()
    base = Path(db_path or os.getenv("DB_FILE") or "data/users.db").resolve().parent
    f = base / "secret.key"
    if f.exists():
        return f.read_bytes().strip()
    base.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:                                   # backend и бот стартуют одновременно: ключ создаёт другой процесс — ждём, пока он допишет
        for _ in range(50):
            data = f.read_bytes().strip()
            if data:
                return data
            time.sleep(0.05)
        raise SecretError("secret.key пуст — удалите файл и перезапустите")
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)
    return key


def encrypt(text, db_path=None):
    return Fernet(_master(db_path)).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt(token, db_path=None):
    try:
        return Fernet(_master(db_path)).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as e:
        raise SecretError("Не удалось расшифровать сохранённый ключ (сменился SECRET_KEY / потерян secret.key) — подключите счёт заново.") from e
