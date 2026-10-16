"""Фиксирует результаты симуляции на детерминированных данных: если логика входов/выходов случайно изменится,
тест упадёт. Ожидаемые числа сняты с коммита 7846cd5 (разворот позиций) и совпадали во всех коммитах до ...7177838."""
import os, random, sys
from app.core import backtest as b


def make():
    random.seed(11)
    H = 3_600_000
    sig, t = [], 0
    for _ in range(400):
        kind = random.choice("LLSN")
        for _ in range(random.randint(1, 5)):
            if kind == "L":
                sig.append((t, random.uniform(1.65, 2.3), random.uniform(2.0, 3.0)))
            elif kind == "S":
                sig.append((t, random.uniform(0.6, 1.05), random.uniform(0.2, 0.65)))
            else:
                sig.append((t, random.uniform(1.1, 1.6), random.uniform(0.8, 1.9)))
            t += H // 2
    price, candles = 100.0, []
    for i in range(t // 60000 + 20000):
        price *= 1 + random.gauss(0, 0.0007)
        candles.append((i * 60000, price, price * 1.0007, price * 0.9993, price))
    return sig, candles


def run(short, confirm, sig, candles, with_liq=False):
    if with_liq:   # 5-кортежи (с ликвидациями) без фильтров не должны менять результат
        sig = [(*s, 1.0, 2.0) for s in sig]
    kw = dict(tp_pct=3, sl_pct=1, fee=0.0005, size=0.1, slip=0.0, confirm=confirm, short=short)
    tr = b.simulate(sig, candles, 1.65, 2.0, **kw)
    return len(tr), round(sum(x["pnl_usd"] for x in tr), 4)


def test_golden_results():
    sig, candles = make()
    assert run(None, 1, sig, candles) == (61, -1.1562)
    assert run(None, 2, sig, candles) == (48, -0.8964)
    assert run((1.05, 0.65), 2, sig, candles) == (143, -1.4728)


def test_liquidation_columns_do_not_change_results_without_filters():
    sig, candles = make()
    assert run((1.05, 0.65), 2, sig, candles, with_liq=True) == run((1.05, 0.65), 2, sig, candles)
