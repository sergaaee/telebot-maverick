export type Side = 'both' | 'long' | 'short';
export type Only = 'local' | 'scalp' | null;

/** Параметры, как их отдаёт и принимает бэкенд (fee/slip — доли, не проценты). */
export interface Params {
  /** актив (символ ccxt), по которому берутся свечи */
  symbol: string;
  /** источник метрик (id из SOURCES) и источник ликвидаций (null = из самого источника) */
  source: string;
  liq_source: string | null;
  side: Side;
  only: Only;
  local_min: number;
  scalp_min: number;
  short_local_max: number;
  short_scalp_max: number;
  confirm: number;
  tp_pct: number;
  sl_pct: number;
  size: number;
  fee: number;
  slip: number;
  cooldown_h: number;
  cool_metric: 'scalp' | 'local';
  cool_drop: number | null;
  cool_declines: number | null;
  liq_hours: number | null;
  liq_frac: number;
  liq_pct: number;
  date_from: string | null;
  date_to: string | null;
}

export interface SourceInfo {
  id: string;
  label: string;
  file: string;
  exists: boolean;
  count: number;
  first_ts: number | null;
  last_ts: number | null;
  has_liq: boolean;
}

export interface AssetInfo {
  symbol: string;
  count: number;
  first_ts: number | null;
  last_ts: number | null;
  tf: string;
  file: string;
  error: string | null;
}

export interface DataInfo {
  sources: SourceInfo[];
  assets: AssetInfo[];
}

export interface AppConfig {
  defaults: Params;
  exchange: string;
  symbols: string[];
  symbol: string;
  tf: string;
  max_bars: number;
  timeframes: Record<string, number[]>;
  data: DataInfo;
}

export type Reason = 'TP' | 'SL' | 'REV' | 'OPEN';

export interface Trade {
  n: number;
  side: 'long' | 'short';
  signal_ts: number;
  entry_ts: number;
  exit_ts: number;
  entry: number;
  exit: number;
  tp: number;
  sl: number;
  reason: Reason;
  pnl_pct: number;
  pnl_usd: number;
  hold_h: number;
  local: number | null;
  scalp: number | null;
  liq_long_k: number | null;
  liq_short_k: number | null;
}

export interface Signal {
  ts: number;
  side: 'long' | 'short';
  taken: boolean;
}

export interface Stats {
  trades: number;
  tp: number;
  sl: number;
  open: number;
  rev: number;
  winrate: number;
  total_usd: number;
  avg_pct: number;
  profit_factor: number | null;
  max_dd_usd: number;
  avg_win_usd: number;
  avg_loss_usd: number;
  best_usd: number;
  worst_usd: number;
  max_loss_streak: number;
  exposure_pct: number;
  expectancy_usd: number;
}

export interface BacktestResult {
  params: Params;
  symbol: string;
  source: { id: string; label: string; liq_source: string | null };
  warnings: string[];
  range: { first_ts: number; last_ts: number; split_ts: number; candles_last_ts: number };
  stats: { total: Stats; long: Stats; short: Stats; first_half: Stats; second_half: Stats };
  trades: Trade[];
  signals: Signal[];
  signals_truncated: boolean;
  equity: [number, number][];
  monthly: { month: string; trades: number; pnl_usd: number; wins: number }[];
}

export interface CandlesResponse {
  tf: number;
  truncated: boolean;
  bars: [number, number, number, number, number][];
}

/** 'auto' — бэкенд подбирает ТФ под масштаб; число — фиксированный ТФ в секундах */
export type TfMode = 'auto' | number;

/** Что хранится на сервере (backend/data/ui_state.json): последний выбор и настройки по каждой связке «источник + актив». */
export interface Profile {
  params: Partial<Params>;
  tf: TfMode;
}

export interface UiState {
  version?: number;
  last?: { source?: string; symbol?: string };
  profiles?: Record<string, Profile>;
}
