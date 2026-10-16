import re
from typing import Optional

_NUM = r"(-?\d+(?:[.,]\d+)?)"


def _find(name: str, text: str) -> Optional[float]:
    m = re.search(rf"{name}\s*=\s*{_NUM}", text, re.IGNORECASE)
    return float(m.group(1).replace(",", ".")) if m else None


def parse_signal(text: str) -> Optional[dict]:
    """Возвращает {'local': float, 'scalp': float, 'global': float|None} или None, если это не сигнал."""
    local = _find("Deep Local", text)
    scalp = _find("Deep Scalp", text)
    if local is None or scalp is None:
        return None
    return {"local": local, "scalp": scalp, "global": _find("Deep Global", text)}


def should_long(sig: dict, local_min: float, scalp_min: float) -> bool:
    return sig["local"] >= local_min and sig["scalp"] >= scalp_min


def should_short(sig: dict, local_max: float, scalp_max: float) -> bool:
    return sig["local"] <= local_max and sig["scalp"] <= scalp_max


class Confirmer:
    """Считает отчёты подряд, для которых predicate(sig) истинен. update() -> True, когда подряд набралось >= confirm."""

    def __init__(self, predicate, confirm: int = 2):
        self.predicate, self.confirm = predicate, confirm
        self.streak = 0

    def update(self, sig: dict) -> bool:
        self.streak = self.streak + 1 if self.predicate(sig) else 0
        return self.streak >= self.confirm


def parse_liquidation(text: str) -> Optional[dict]:
    """'Long 3494.9k $ | Short 58288.3k $' -> {'long': 3494.9, 'short': 58288.3} (в тыс. $)."""
    m = re.search(rf"Long\s*{_NUM}\s*k\s*\$\s*\|\s*Short\s*{_NUM}\s*k", text, re.IGNORECASE)
    return {"long": float(m.group(1).replace(",", ".")), "short": float(m.group(2).replace(",", "."))} if m else None


def parse_top_coin(text: str) -> Optional[dict]:
    """Топ-коин отчёта — первая строка блока Top Coin: 'TIA-USD      | 10.12' -> {'coin': 'TIA-USD', 'value': 10.12}.
    Встречаются разные написания тикера: TIA-USD (Coinbase), BANKUSDT (Binance)."""
    m = re.search(r"top\s*coin[^\n]*\n(?:[^\n|A-Za-z0-9]*\n)?\s*([A-Za-z0-9._-]+)\s*\|\s*" + _NUM, text, re.IGNORECASE)
    if not m:
        return None
    return {"coin": m.group(1).upper(), "value": float(m.group(2).replace(",", "."))}
