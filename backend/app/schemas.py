from typing import Literal, Optional

from pydantic import BaseModel, Field


class TpLevel(BaseModel):
    pct: float = Field(gt=0, le=100, description="расстояние до тейка от цены входа, %")
    share: float = Field(gt=0, le=100, description="какая доля позиции закрывается на этом тейке, %")


class BacktestParams(BaseModel):
    """Любое поле можно не передавать — тогда берётся значение из env (backend/.env).
    Явный null отключает необязательный параметр (фильтры, --only)."""
    symbol: Optional[str] = Field(None, max_length=40)
    source: Optional[str] = Field(None, max_length=40)
    liq_source: Optional[str] = Field(None, max_length=40)
    side: Optional[Literal["both", "long", "short"]] = None
    only: Optional[Literal["local", "scalp", "global"]] = None
    local_min: Optional[float] = Field(None, ge=-10, le=10)
    scalp_min: Optional[float] = Field(None, ge=-10, le=10)
    short_local_max: Optional[float] = Field(None, ge=-10, le=10)
    short_scalp_max: Optional[float] = Field(None, ge=-10, le=10)
    global_min: Optional[float] = Field(None, ge=-10, le=10, description="порог Deep Global для лонга (null — не учитывается)")
    short_global_max: Optional[float] = Field(None, ge=-10, le=10, description="порог Deep Global для шорта (null — не учитывается)")
    confirm: Optional[int] = Field(None, ge=1, le=100)
    tps: Optional[list[TpLevel]] = Field(None, min_length=1, max_length=3, description="тейки частями (1–3), доли в сумме 100%")
    be_after_tp1: Optional[bool] = None
    tp_pct: Optional[float] = Field(None, gt=0, le=100, description="устарело: один тейк на 100% (если tps не передан)")
    sl_pct: Optional[float] = Field(None, gt=0, le=100)
    size: Optional[float] = Field(None, gt=0, description="устарело: размер в единицах актива (в бэктесте интерфейса не используется)")
    deposit: Optional[float] = Field(None, gt=0, le=1e12, description="начальный депозит, $")
    trade_pct: Optional[float] = Field(None, gt=0, le=100, description="доля текущего депозита, которая идёт в маржу сделки, %")
    compound: Optional[bool] = Field(None, description="true — сложный процент (доля от текущего баланса), false — фиксированная сумма от начального депозита")
    leverage: Optional[float] = Field(None, ge=1, le=125, description="плечо")
    fee: Optional[float] = Field(None, ge=0, le=0.05)
    slip: Optional[float] = Field(None, ge=0, le=0.05)
    cooldown_h: Optional[float] = Field(None, ge=0, le=24 * 365)
    cool_metric: Optional[Literal["scalp", "local", "global"]] = None
    cool_drop: Optional[float] = Field(None, gt=0, le=10)
    cool_declines: Optional[int] = Field(None, ge=1, le=50)
    liq_hours: Optional[float] = Field(None, gt=0, le=24 * 365)
    liq_frac: Optional[float] = Field(None, gt=0, le=1)
    liq_pct: Optional[float] = Field(None, ge=0, le=100)
    date_from: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    # пирамидинг, паузы между сигналами, «стоп-лосс не ставить, пока метрики выше порогов входа»
    pyramid_enabled: Optional[bool] = None
    pyramid_max: Optional[int] = Field(None, ge=1, le=20, description="максимум доборов за позицию")
    pyramid_pct: Optional[float] = Field(None, gt=0, le=100, description="размер каждого добора, % депозита")
    pyramid_avg: Optional[bool] = Field(None, description="доборы усредняют одну позицию: тейки и стоп — от новой средней цены входа")
    pyramid_gap_h: Optional[float] = Field(None, ge=0, le=24 * 365, description="минимум часов между доборами")
    pyramid_move_pct: Optional[float] = Field(None, ge=0, le=100, description="добор только если цена ушла от последнего входа не меньше чем на столько %; 0 — не проверять")
    pyramid_move_dir: Optional[Literal["any", "favor", "against"]] = Field(None, description="куда должна уйти цена: в любую сторону / в сторону позиции / против неё")
    pyramid_leverage: Optional[float] = Field(None, ge=1, le=125, description="плечо доборов; не задано (null) — как у первой ноги")
    liquidation_sim: Optional[bool] = Field(None, description="моделировать ликвидацию: касание цены ликвидации закрывает ногу с потерей всей маржи")
    maint_margin_pct: Optional[float] = Field(None, ge=0, le=20, description="поддерживающая маржа, % объёма (для цены ликвидации)")
    weekdays_only: Optional[bool] = Field(None, description="вход только с открытия Токио в понедельник; остаток закрывается к закрытию Нью-Йорка в пятницу")
    friday_close_before_min: Optional[float] = Field(None, ge=0, le=720, description="закрыть позицию в пятницу за столько минут до закрытия Нью-Йорка")
    signal_gap_h: Optional[float] = Field(None, ge=0, le=24 * 365, description="минимум часов между открытиями позиций")
    sl_hold_strong: Optional[bool] = None
    # торговля только по тренду (EMA на таймфрейме trend_tf)
    trend_enabled: Optional[bool] = None
    trend_emas: Optional[list[Literal[20, 50, 100, 200]]] = Field(None, min_length=1, max_length=4)
    trend_tf: Optional[Literal["5m", "15m", "30m", "1h", "4h", "1d"]] = None
    # топ-коины
    top_enabled: Optional[bool] = None
    top_side: Optional[Literal["both", "long", "short"]] = None
    top_tps: Optional[list[TpLevel]] = Field(None, min_length=1, max_length=3)
    top_be_after_tp1: Optional[bool] = None
    top_sl_pct: Optional[float] = Field(None, gt=0, le=100)
    top_size: Optional[float] = Field(None, gt=0, le=1e9)
    top_fee: Optional[float] = Field(None, ge=0, le=0.05)
    top_slip: Optional[float] = Field(None, ge=0, le=0.05)
    top_days: Optional[float] = Field(None, gt=0, le=30)
    top_min_value: Optional[float] = Field(None, ge=0, le=1000)


class MonteCarloRequest(BacktestParams):
    """Параметры бэктеста плюс настройки Монте-Карло."""
    runs: int = Field(1000, ge=100, le=5000, description="число прогонов")
    mode: Literal["shuffle", "bootstrap"] = Field("shuffle", description="shuffle — тот же набор сделок в случайном порядке; bootstrap — случайная выборка с возвращением")
    seed: Optional[int] = Field(None, description="зерно генератора (для воспроизводимости)")


WF_PARAMS = ("local_min", "scalp_min", "global_min", "short_local_max", "short_scalp_max", "short_global_max", "sl_pct", "confirm")


class GridAxis(BaseModel):
    param: Literal["local_min", "scalp_min", "global_min", "short_local_max", "short_scalp_max", "short_global_max", "sl_pct", "confirm"]
    values: list[float] = Field(min_length=2, max_length=30)


class WalkForwardRequest(BacktestParams):
    """Параметры бэктеста плюс настройки walk-forward: на каждом окне подбираются лучшие значения из сетки, затем проверяются на следующем отрезке."""
    grid: list[GridAxis] = Field(min_length=1, max_length=2, description="что подбирать (1–2 параметра) и какие значения перебирать")
    train_days: int = Field(90, ge=14, le=1500, description="длина окна подбора, дней")
    test_days: int = Field(30, ge=7, le=730, description="длина окна проверки, дней")
    step_days: Optional[int] = Field(None, ge=7, le=730, description="сдвиг окон; по умолчанию равен окну проверки")
    metric: Literal["pnl", "sharpe", "profit_factor", "calmar"] = Field("pnl", description="по чему выбирать лучшие значения на окне подбора")
    min_trades: int = Field(5, ge=1, le=1000, description="минимум сделок на окне подбора, чтобы значения учитывались")
