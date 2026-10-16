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
    only: Optional[Literal["local", "scalp"]] = None
    local_min: Optional[float] = Field(None, ge=-10, le=10)
    scalp_min: Optional[float] = Field(None, ge=-10, le=10)
    short_local_max: Optional[float] = Field(None, ge=-10, le=10)
    short_scalp_max: Optional[float] = Field(None, ge=-10, le=10)
    confirm: Optional[int] = Field(None, ge=1, le=100)
    tps: Optional[list[TpLevel]] = Field(None, min_length=1, max_length=3, description="тейки частями (1–3), доли в сумме 100%")
    be_after_tp1: Optional[bool] = None
    tp_pct: Optional[float] = Field(None, gt=0, le=100, description="устарело: один тейк на 100% (если tps не передан)")
    sl_pct: Optional[float] = Field(None, gt=0, le=100)
    size: Optional[float] = Field(None, gt=0)
    fee: Optional[float] = Field(None, ge=0, le=0.05)
    slip: Optional[float] = Field(None, ge=0, le=0.05)
    cooldown_h: Optional[float] = Field(None, ge=0, le=24 * 365)
    cool_metric: Optional[Literal["scalp", "local"]] = None
    cool_drop: Optional[float] = Field(None, gt=0, le=10)
    cool_declines: Optional[int] = Field(None, ge=1, le=50)
    liq_hours: Optional[float] = Field(None, gt=0, le=24 * 365)
    liq_frac: Optional[float] = Field(None, gt=0, le=1)
    liq_pct: Optional[float] = Field(None, ge=0, le=100)
    date_from: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
