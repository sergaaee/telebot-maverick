"""Конфиги, сохранённые до появления некоторых возможностей, не содержат их полей: такие поля считаются ВЫКЛЮЧЕННЫМИ (а не берутся из умолчаний .env),
чтобы результат старого конфига не менялся. Новые конфиги при загрузке фиксируют все поля явно. Общий список для бота и мониторинга."""
LEGACY_OFF = {"pyramid_enabled": False, "pyramid_avg": False, "signal_gap_h": 0.0, "sl_hold_strong": False, "pyramid_move_pct": 0.0, "pyramid_move_dir": "any",
              "pyramid_leverage": None, "liquidation_sim": False, "weekdays_only": False, "friday_close_before_min": 0.0,
              "global_min": None, "short_global_max": None, "funding_apr": 0.0, "funding_period_h": 8, "funding_mode": "signed"}
