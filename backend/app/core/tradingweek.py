"""Торговая неделя «только будни»: вход разрешён с открытия Токио в понедельник до закрытия Нью-Йорка в пятницу.

Открытие Токио — понедельник 09:00 JST = 00:00 UTC (Япония без перехода на летнее время). Закрытие Нью-Йорка — пятница 17:00 по времени Нью-Йорка
(закрытие недели на форексе; с учётом летнего времени США это 21:00 UTC летом и 22:00 UTC зимой). Всё остальное — выходные: пятница после закрытия, суббота, воскресенье.
Правила перехода США считаются здесь же (2-е воскресенье марта — 1-е воскресенье ноября), без внешней базы часовых поясов."""
from datetime import date, datetime, timedelta, timezone

HOUR_MS = 3_600_000
MINUTE_MS = 60_000


def _nth_sunday(year, month, n):
    d = date(year, month, 1)
    first = d + timedelta(days=(6 - d.weekday()) % 7)
    return first + timedelta(weeks=n - 1)


def _ny_dst(d):
    """Действует ли летнее время США в дату d (для пятницы: переходы случаются в воскресенье, так что пятница однозначна)."""
    return _nth_sunday(d.year, 3, 2) <= d < _nth_sunday(d.year, 11, 1)


def week_bounds(t_ms, before_min=0):
    """(открытие, закрытие) торговой недели в мс UTC для недели, в которую попадает момент t_ms. Закрытие сдвигается на before_min минут раньше."""
    d = datetime.fromtimestamp(t_ms / 1000, timezone.utc)
    monday = (d - timedelta(days=d.weekday())).date()
    friday = monday + timedelta(days=4)
    open_dt = datetime(monday.year, monday.month, monday.day, tzinfo=timezone.utc)
    close_dt = datetime(friday.year, friday.month, friday.day, 17 + (4 if _ny_dst(friday) else 5), tzinfo=timezone.utc)      # 17:00 ET = 21:00/22:00 UTC
    return int(open_dt.timestamp() * 1000), int(close_dt.timestamp() * 1000) - int(before_min * MINUTE_MS)


def in_window(t_ms, before_min=0):
    o, c = week_bounds(t_ms, before_min)
    return o <= t_ms < c


def window_close(t_ms, before_min=0):
    return week_bounds(t_ms, before_min)[1]


def next_close(t_ms, before_min=0):
    """Ближайшее закрытие недели (с учётом сдвига before_min) строго после момента t_ms. Внутри торгового окна это закрытие текущей недели, а для момента в выходные
    (позиция, открытая в субботу или воскресенье, когда вход в выходные разрешён) — закрытие следующей пятницы."""
    c = week_bounds(t_ms, before_min)[1]
    return c if t_ms < c else week_bounds(t_ms + 7 * 24 * HOUR_MS, before_min)[1]


def options(p):
    """Настройки выходных из параметров -> (не открывать по выходным, закрывать перед выходными, сдвиг закрытия в минутах).
    Две независимые опции: weekend_no_entry — сигналы вне торгового окна (с пятницы после закрытия Нью-Йорка до открытия Токио в понедельник) пропускаются;
    weekend_close — позиция закрывается по open свечи закрытия пятницы (метка WKND), на friday_close_before_min минут раньше.
    Если опции не заданы (параметры старого вида), обе берутся из прежней галочки weekdays_only: она включала обе сразу."""
    legacy = bool(p.get("weekdays_only"))
    no_entry = p.get("weekend_no_entry")
    close = p.get("weekend_close")
    return (legacy if no_entry is None else bool(no_entry)), (legacy if close is None else bool(close)), float(p.get("friday_close_before_min") or 0.0)
