export type Side = 'both' | 'long' | 'short';

/** Один тейк: расстояние до него от цены входа (%) и доля позиции (%), которая на нём закрывается. */
export interface TpLevel {
  pct: number;
  share: number;
}
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
  /** тейки частями: от 1 до 3, доли в сумме 100% */
  tps: TpLevel[];
  /** после первого тейка переносить стоп в цену входа (безубыток) */
  be_after_tp1: boolean;
  sl_pct: number;
  /** устарело (размер в BTC); сейчас размер задают депозит, доля и плечо */
  size: number;
  /** начальный депозит, $ */
  deposit: number;
  /** доля текущего депозита в марже сделки, % */
  trade_pct: number;
  leverage: number;
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
  /** торговля только по тренду: лонг выше всех выбранных EMA, шорт ниже всех (EMA по свечам таймфрейма trend_tf) */
  trend_enabled: boolean;
  trend_emas: number[];
  trend_tf: string;
  /** топ-коины: торговля монетой из блока Top Coin отчёта по сигналу основной стратегии */
  top_enabled: boolean;
  top_side: Side;
  top_tps: TpLevel[];
  top_be_after_tp1: boolean;
  top_sl_pct: number;
  /** размер позиции в долларах */
  top_size: number;
  top_fee: number;
  top_slip: number;
  /** сколько суток после сигнала держим сделку и качаем свечи */
  top_days: number;
  top_min_value: number;
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
  /** добавлен из интерфейса (можно убрать) */
  user: boolean;
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
  user_symbols: string[];
  symbol: string;
  tf: string;
  max_bars: number;
  timeframes: Record<string, number[]>;
  data: DataInfo;
}

export type Reason = 'TP' | 'SL' | 'BE' | 'REV' | 'OPEN' | 'TIME';

/** Частичное или финальное закрытие: метка TP1..TP3 / SL / BE / REV / OPEN, доля от исходной позиции (0..1). */
export interface Fill {
  ts: number;
  price: number;
  share: number;
  label: string;
}

export interface Trade {
  n: number;
  side: 'long' | 'short';
  signal_ts: number;
  entry_ts: number;
  exit_ts: number;
  entry: number;
  exit: number;
  exit_avg: number;
  tp: number;
  tps: number[];
  tp_shares: number[];
  sl: number;
  be: boolean;
  be_ts: number | null;
  tp_hits: number;
  fills: Fill[];
  reason: Reason;
  pnl_pct: number;
  pnl_usd: number;
  /** объём позиции (маржа × плечо) и маржа, $ — только у сделок основной стратегии */
  notional?: number | null;
  margin?: number | null;
  hold_h: number;
  local: number | null;
  scalp: number | null;
  liq_long_k: number | null;
  liq_short_k: number | null;
  /** только для сделок по топ-коинам */
  symbol?: string;
  coin?: string;
  top_value?: number | null;
  /** окно ещё не закончилось: сделка может быть не завершена */
  partial?: boolean;
  window_end?: number;
}

export interface Signal {
  ts: number;
  side: 'long' | 'short';
  taken: boolean;
  trend_blocked?: boolean;
}

export interface Stats {
  trades: number;
  tp: number;
  sl: number;
  be: number;
  open: number;
  /** закрыто по времени (топ-коины: вышли N суток) */
  time: number;
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
  /** годовой Шарп по дневным доходностям; null — мало данных */
  sharpe: number | null;
  return_pct: number | null;
  max_dd_pct: number | null;
  /** сколько сделок дошли до 1-го / 2-го / 3-го тейка */
  tp_reach: number[];
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
  top: TopSection;
  trend: { enabled: boolean; blocked: number; confirmed: number };
}

export interface TopSkipped {
  ts: number;
  side: 'long' | 'short';
  coin: string | null;
  symbol: string | null;
  reason: string;
  pending?: boolean;
}

export interface TopPendingItem {
  symbol: string;
  from: number;
  to: number;
}

export type TopSection =
  | { enabled: false }
  | {
      enabled: true;
      stats: Stats;
      trades: Trade[];
      by_coin: { symbol: string; coin: string; trades: number; wins: number; pnl_usd: number }[];
      skipped: TopSkipped[];
      skipped_total: number;
      equity: [number, number][];
      pending: { symbols: number; windows: number; requests: number; items: TopPendingItem[] };
    };

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

/** Фоновая загрузка свечей актива. */
export interface Job {
  id: number;
  symbol: string;
  /** candles — свечи актива, top — окна свечей топ-коинов */
  kind: 'candles' | 'top';
  label: string;
  /** для top: сколько монет всего / готово */
  total: number;
  done: number;
  /** задача завершена, но часть работы не удалась */
  warning: string | null;
  status: 'queued' | 'running' | 'done' | 'error';
  count: number;
  last_ts: number | null;
  error: string | null;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  /** пользователь уже закрыл уведомление о завершении */
  acked: boolean;
}

export interface AddAssetResult {
  symbol: string;
  added: boolean;
  job: Job | null;
}
