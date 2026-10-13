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


class Confirmer:
    """Считает отчёты подряд, удовлетворяющие условию. update() -> True, когда подряд набралось >= confirm."""

    def __init__(self, local_min: float, scalp_min: float, confirm: int = 2):
        self.local_min, self.scalp_min, self.confirm = local_min, scalp_min, confirm
        self.streak = 0

    def update(self, sig: dict) -> bool:
        self.streak = self.streak + 1 if should_long(sig, self.local_min, self.scalp_min) else 0
        return self.streak >= self.confirm
