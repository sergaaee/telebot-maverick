"""Диапазон DS для входа и «экстремум» для выхода в боте: карточка, старые конфиги, закрытие позиции в автомоде."""
import json

from conftest import *  # noqa: F401,F403
from test_signalbot import CHAT, H, NEUTRAL, T0, UI_CONFIG, append_reports, env, msg, validate  # noqa: F401

NEW = ("long_scalp_max", "short_scalp_min", "exit_long_ds_max", "exit_short_ds_min")


def ds_rows(t0, values):
    """Отчёты (ts, global, local, scalp) с заданными DS: Local 2.0 — условие входа по Local выполняется всегда."""
    return [(t0 + i * H // 2, 1.2, 2.0, v) for i, v in enumerate(values)]


def test_summary_and_params_card_show_the_range_and_the_extreme_exit(env):
    from sigbot import formatting as fm
    base = {"source": "a", "symbol": "b", "side": "both", "confirm": 2}
    s = fm.config_summary({"params": {**base, "long_scalp_max": 2.5, "short_scalp_min": 0.5, "exit_long_ds_max": 2.5, "exit_short_ds_min": 0.4}})
    assert "лонг только при DS ≤ 2.5" in s and "шорт только при DS ≥ 0.5" in s and "закрыть лонг при DS ≥ 2.5 (экстремум)" in s and "закрыть шорт при DS ≤ 0.4 (экстремум)" in s
    assert "экстремум" not in fm.config_summary({"params": base}) and "только при DS" not in fm.config_summary({"params": base})


def test_old_stored_config_ignores_env_defaults_of_the_new_bounds(env):
    old = {k: v for k, v in validate(UI_CONFIG["params"], env.s.defaults)[0].items() if k not in NEW}
    env.s.defaults.update(long_scalp_max=2.5, short_scalp_min=0.5, exit_long_ds_max=2.6, exit_short_ds_min=0.4)
    res = env.engine.evaluate(old)["params"]
    assert all(res[k] is None for k in NEW)                                                          # старый конфиг без полей — границы выключены
    assert validate(UI_CONFIG["params"], env.s.defaults)[0]["long_scalp_max"] == 2.5                 # новый фиксирует умолчания .env явно


def test_extreme_ds_closes_the_long_in_manage_mode_and_blocks_a_new_entry(env):
    cfg_json = json.loads(json.dumps(UI_CONFIG))
    cfg_json["params"].update(tps=[{"pct": 100, "share": 100}], sl_pct=99, be_after_tp1=False, local_min=1.5, scalp_min=2.0, long_scalp_max=2.5, exit_long_ds_max=2.5)     # вход при DS 2.0…2.5, при 2.5+ — закрытие
    env.handle_update(msg(json.dumps(cfg_json)))
    env.cycle()
    cfg = env.db.list_configs(env.alice["id"])[0]
    assert cfg["params"]["long_scalp_max"] == 2.5 and cfg["params"]["exit_long_ds_max"] == 2.5 and cfg["params"]["exit_short_ds_min"] is None
    env.db.set_auto(env.alice["id"], cfg["id"], True, True)
    start = T0 + 10 * H // 2
    append_reports(env.hist, ds_rows(start, [2.2, 2.3, 2.2]), 11)                                    # DS внутри диапазона: вход
    env.now["t"] = (start + 3 * H // 2) / 1000
    env.cycle()
    log = lambda kind: [x for x in env.db.list_auto_log(env.alice["id"]) if x["kind"] == kind]
    assert len(log("open")) == 1 and not log("close_ds")
    nxt = start + 3 * H // 2
    env.db.update_config(cfg["id"], fill_ts=nxt - 1)
    append_reports(env.hist, ds_rows(nxt, [2.8, 2.9, 2.8]), 14)                                      # экстремум: DS выше 2.5
    env.now["t"] = (nxt + 3 * H // 2) / 1000
    env.cycle()
    closes = log("close_ds")
    assert len(closes) == 1 and closes[0]["side"] == "long" and closes[0]["status"] == "dry_run" and closes[0]["details"]["share"] == 1.0
    assert len(log("open")) == 1                                                                     # серия отчётов с DS 2.8+ новым входом не стала
    env.cycle()
    assert len(log("close_ds")) == 1 and len(log("open")) == 1
    env.db.set_auto(env.alice["id"], cfg["id"], True, False)
