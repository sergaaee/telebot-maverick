"""Проверка символов на бирже и разбор ввода пользователя («ETH», «ETHUSDT», «ETH/USDT», «ETH/USDT:USDT»)."""
import threading
import time


class MarketsError(RuntimeError):
    """Не удалось получить список рынков биржи (нет сети, биржа недоступна)."""


def normalize_symbol(raw: str) -> str:
    """Приводит ввод к символу бессрочного USDT-фьючерса ccxt: ETH -> ETH/USDT:USDT."""
    s = (raw or "").strip().upper().replace(" ", "")
    if not s:
        raise ValueError("Введите символ актива, например ETH или ETH/USDT:USDT")
    if "/" in s:
        base, _, rest = s.partition("/")
        quote, _, settle = rest.partition(":")
        return f"{base}/{quote}:{settle or quote}"
    for quote in ("USDT", "USDC"):
        if s.endswith(quote) and len(s) > len(quote):
            return f"{s[:-len(quote)]}/{quote}:{quote}"
    return f"{s}/USDT:USDT"


def ccxt_swap_symbols(exchange_id: str) -> set:
    """Активные линейные бессрочные контракты биржи (запрос к бирже — несколько секунд)."""
    import ccxt

    try:
        ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        markets = ex.load_markets()
    except Exception as e:  # сеть, лимиты, недоступность биржи
        raise MarketsError(f"Не удалось получить список рынков {exchange_id}: {type(e).__name__}: {e}") from e
    return {s for s, m in markets.items() if m.get("swap") and m.get("linear", True) and m.get("active") is not False}


class MarketsCache:
    """Кеш списка рынков на ttl секунд; provider() -> set символов (подменяется в тестах)."""

    def __init__(self, provider, ttl=600):
        self._provider, self._ttl = provider, ttl
        self._lock = threading.Lock()
        self._at, self._data = 0.0, set()

    def symbols(self) -> set:
        with self._lock:
            if not self._data or time.time() - self._at > self._ttl:
                self._data = set(self._provider())
                self._at = time.time()
            return self._data

    def suggest(self, symbol: str, limit=6) -> list:
        """Похожие символы (та же база), чтобы подсказать при опечатке."""
        base = symbol.split("/")[0]
        first = [s for s in sorted(self.symbols()) if s.split("/")[0] == base]
        rest = [s for s in sorted(self.symbols()) if s.split("/")[0].startswith(base[:3]) and s not in first]
        return (first + rest)[:limit]
