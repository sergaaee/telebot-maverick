import csv, os, sys
from datetime import datetime, timezone
from types import SimpleNamespace
from tools import export_history as e

MSG = "Deep Global = 0.99\nDeep Local = 1.25\nDeep Scalp = 1.44\n\nLiquidation\nLong 5.0k $ | Short 7.0k $\n"


def msg(i, text=MSG):
    return SimpleNamespace(id=i, raw_text=text, date=datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc))


def rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def test_append_only_new_and_no_bom_in_middle(tmp_path):
    p = str(tmp_path / "h.csv")
    assert e.read_state(p) == (None, 0)
    w = e.RowWriter(p, None, append=False)
    for i in (1, 2, 3):
        w.write(msg(i))
    w.close()
    header, last = e.read_state(p)
    assert last == 3 and header == e.COLUMNS
    w = e.RowWriter(p, header, append=True)
    for i in (4, 5):
        w.write(msg(i))
    w.close()
    r = rows(p)
    assert [x["id"] for x in r] == ["1", "2", "3", "4", "5"]
    assert "﻿" not in open(p, encoding="utf-8").read()[1:]       # BOM только в начале файла
    assert r[4]["deep_local"] == "1.25" and r[4]["liq_long_k"] == "5.0"
    assert r[0]["text"] == MSG                                        # многострочный текст не ломает определение последнего id


def test_old_format_file_keeps_its_columns(tmp_path):
    p = str(tmp_path / "old.csv")
    old = ["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "text"]   # формат первой выгрузки
    with open(p, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(old)
        w.writerow([7, "2026-01-01T00:00:00+00:00", 0.9, 1.0, 1.0, "x"])
    header, last = e.read_state(p)
    assert header == old and last == 7
    w = e.RowWriter(p, header, append=True)
    w.write(msg(8))
    w.close()
    r = rows(p)
    assert list(r[1].keys()) == old and r[1]["id"] == "8" and r[1]["deep_scalp"] == "1.44"
