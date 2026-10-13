import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from signal_parser import parse_signal, should_long

MSG = """Deep Global = 0.99
Deep Local = 1.25
Deep Scalp = 1.44

Liquidation
Long 0.0k $ | Short 0.0k $
"""


def test_parse():
    assert parse_signal(MSG) == {"local": 1.25, "scalp": 1.44, "global": 0.99}


def test_not_signal():
    assert parse_signal("hello") is None


def test_thresholds():
    assert not should_long({"local": 1.25, "scalp": 1.44}, 1.65, 2.0)
    assert not should_long({"local": 1.7, "scalp": 1.9}, 1.65, 2.0)
    assert should_long({"local": 1.65, "scalp": 2.0}, 1.65, 2.0)


def test_confirmer():
    from signal_parser import Confirmer
    c = Confirmer(1.65, 2.0, 2)
    ok, bad = {"local": 2, "scalp": 2.5}, {"local": 1, "scalp": 1}
    assert [c.update(x) for x in (ok, bad, ok, ok, ok, bad, ok)] == [False, False, False, True, True, False, False]
