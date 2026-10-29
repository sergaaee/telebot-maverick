export type Side = 'both' | 'long' | 'short';

/** Один тейк: расстояние до него от цены входа (%) и доля позиции (%), которая на нём закрывается. */
export interface TpLevel {
  pct: number;
  share: number;
}
export type Only = 'local' | 'scalp' | 'global' | null;

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
  /** Deep Global (необязательно): лонг при DG ≥ global_min, шорт при DG ≤ short_global_max; null — не учитывается */
  global_min: number | null;
  short_global_max: number | null;
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
  /** true — сложный процент (доля от текущего баланса), false — фиксированная сумма от начального депозита */
  compound: boolean;
  fee: number;
  slip: number;
  /** фандинг: годовые % (+ лонг платит, шорт получает), период списания в часах и режим (signed — по знаку, cost — платят обе стороны); 0 — не учитывать */
  /** вход лимитным ордером на столько % движения цены от цены сигнала (0 — по рынку), сторона и срок жизни ордера, часов */
  entry_limit_pct: number;
  entry_limit_dir: 'pullback' | 'breakout';
  entry_limit_ttl_h: number;
  funding_apr: number;
  funding_period_h: 1 | 2 | 4 | 8 | 12 | 24;
  funding_mode: 'signed' | 'cost';
  cooldown_h: number;
  cool_metric: 'scalp' | 'local' | 'global';
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
  /** пирамидинг: добор в позицию по новым подтверждённым сигналам той же стороны (каждый добор — своя нога со своими тейками и стопом) */
  pyramid_enabled: boolean;
  pyramid_max: number;
  /** размер добора, % депозита */
  pyramid_pct: number;
  pyramid_gap_h: number;
  /** вход только с открытия Токио в понедельник; позиция, оставшаяся открытой, закрывается к закрытию Нью-Йорка в пятницу (на столько минут раньше) */
  weekdays_only: boolean;
  friday_close_before_min: number;
  /** плечо доборов; null — как у первой ноги */
  pyramid_leverage: number | null;
  /** моделировать ликвидацию: касание цены ликвидации закрывает ногу с потерей всей маржи; поддерживающая маржа — % объёма */
  liquidation_sim: boolean;
  maint_margin_pct: number;
  /** добор только если цена ушла от последнего входа не меньше чем на столько % (0 — не проверять) и куда именно */
  pyramid_move_pct: number;
  pyramid_move_dir: 'any' | 'favor' | 'against';
  /** доборы усредняют одну позицию: тейки и стоп считаются от новой средней цены входа */
  pyramid_avg: boolean;
  /** минимум часов между открытиями позиций */
  signal_gap_h: number;
  /** стоп-лосс не действует, пока метрики удовлетворяют условию входа */
  sl_hold_strong: boolean;
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

export type Reason = 'TP' | 'SL' | 'BE' | 'REV' | 'OPEN' | 'TIME' | 'LIQ' | 'WKND';

/** Частичное или финальное закрытие: метка TP1..TP3 / SL / BE / REV / OPEN, доля от исходной позиции (0..1). */
export interface Fill {
  ts: number;
  price: number;
  share: number;
  label: string;
  /** метрики последнего отчёта на момент исполнения */
  metrics?: { dg: number | null; dl: number; ds: number; strong?: boolean } | null;
}

export interface Trade {
  n: number;
  /** вход лимитным ордером: уровень, цена сигнала, сторона и сколько часов ждали исполнения */
  limit?: { level: number; ref: number; dir: 'pullback' | 'breakout'; wait_h: number };
  /** фандинг сделки: % от объёма (+ расход) и в долларах */
  funding_pct?: number;
  funding_usd?: number;
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
  global?: number | null;
  liq_long_k: number | null;
  liq_short_k: number | null;
  /** номер ноги позиции (1 — вход, 2+ — доборы), номер позиции и признак добора */
  leg?: number;
  group?: number;
  pyramid?: boolean;
  /** усреднённая позиция: число доборов, первый вход (entry — средняя цена) и сами доборы */
  avg?: boolean;
  adds_n?: number;
  entry_first?: number;
  adds?: { ts: number; price: number; weight: number; signal_ts: number | null }[];
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
  /** почему сигнал не открыл сделку */
  reason?: string | null;
}

/** Серия подтверждена, но вход держат фильтры: waiting — cool (откат метрики) / liq (ликвидации) / trend; lines — что именно не хватает, по-русски. */
export interface PendingSignal {
  ts: number;
  side: 'long' | 'short';
  waiting: ('cool' | 'liq' | 'trend')[];
  lines: string[];
  /** первый отчёт непрерывного ожидания (на графике отмечается только он и самый свежий) */
  run_start?: boolean;
  /** только в ready_now: позиция этой стороны уже открыта, ждём добор */
  add?: boolean;
}

/** Исходы сделок отдельно для будней и выходных (по моменту выхода); entered — число входов по моменту входа. */
export interface WeekdayBucket {
  trades: number;
  reasons: Record<'TP' | 'BE' | 'SL' | 'REV' | 'LIQ' | 'WKND' | 'OPEN', number>;
  pnl_usd: number;
  wins: number;
}
export interface WeekdaySplit {
  weekday: WeekdayBucket;
  weekend: WeekdayBucket;
  entered: { weekday: number; weekend: number };
}

/** Группа сделок по дню недели или сессии: причины выхода, число плюсовых, итог и суммы для profit factor. */
export interface TimeBucket extends WeekdayBucket {
  gross_win: number;
  gross_loss: number;
}
export interface TimeStats {
  entry: { day: TimeBucket[]; session: (TimeBucket & { key: string; name: string; hours: [number, number] })[] };
  exit: { day: TimeBucket[]; session: (TimeBucket & { key: string; name: string; hours: [number, number] })[] };
}

export interface HoldQuantiles {
  n: number;
  median: number | null;
  mean: number | null;
  p90: number | null;
}
/** Как долго держатся позиции: группы по сроку (часы) и сводка (медиана / среднее / p90), корреляция log(срок) с результатом сделки в %. */
export interface HoldStats {
  buckets: (TimeBucket & { label: string; from_h: number; to_h: number | null })[];
  open: number;
  summary: { all: HoldQuantiles; wins: HoldQuantiles; losses: HoldQuantiles; by_reason: Record<string, HoldQuantiles>; corr: number | null };
}

export interface Stats {
  /** фандинг за всё время в $: + уплачено, − получено */
  funding_usd?: number;
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
  /** позиций (группа ног) и доборов */
  positions: number;
  adds: number;
  /** годовой Шарп по дневным доходностям; null — мало данных */
  sharpe: number | null;
  return_pct: number | null;
  max_dd_pct: number | null;
  /** просадка с учётом открытых позиций по рыночной цене (плечо, доборы), только в «Всего» */
  max_dd_real_usd?: number;
  max_dd_real_pct?: number;
  min_equity_usd?: number;
  /** закрыто ликвидацией */
  liq?: number;
  /** закрыто в пятницу к концу торговой недели */
  wknd?: number;
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
  skipped_reasons: Record<string, number>;
  weekday_split: WeekdaySplit;
  time_stats: TimeStats;
  hold_stats: HoldStats;
  pending: PendingSignal[];
  /** что ждёт подтверждения по последнему отчёту прямо сейчас */
  ready_now: PendingSignal[];
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

/** Последний отчёт источника. */
export interface LatestReport {
  ts: number;
  dg: number | null;
  dl: number;
  ds: number;
  top_coin: string | null;
  top_value: number | null;
  liq_long_k: number | null;
  liq_short_k: number | null;
}

export interface LatestSource {
  id: string;
  label: string;
  report: LatestReport | null;
}

/** Подгрузка новых сообщений Telegram на сервере. Время — секунды (unix). */
export interface SyncSource {
  id: string;
  label: string;
  chat: string;
  status: 'idle' | 'running' | 'ok' | 'error';
  last_run: number | null;
  last_ok: number | null;
  added: number;
  added_total: number;
  error: string | null;
}

export interface SyncStatus {
  enabled: boolean;
  reason: string | null;
  running: boolean;
  interval_min: number;
  next_run: number | null;
  sources: SyncSource[];
  /** подгрузка свечей по расписанию */
  candles?: { enabled: boolean; reason: string | null; interval_min: number; next_run: number | null; symbols: { symbol: string; status: string; last_ok: number | null; error: string | null }[] };
}

export interface AddAssetResult {
  symbol: string;
  added: boolean;
  job: Job | null;
}

/** Монте-Карло по сделкам стратегии. Доли — в процентах; fan — перцентили капитала (в долях депозита) по ходу серии сделок. */
export interface MonteCarlo {
  runs: number;
  mode: 'shuffle' | 'bootstrap';
  trades: number;
  compound: boolean;
  final_pct: Record<'5' | '25' | '50' | '75' | '95' | 'mean', number>;
  max_dd_pct: Record<'5' | '25' | '50' | '75' | '95' | 'mean', number>;
  prob_loss_pct: number;
  prob_ruin_pct: number;
  prob_dd_pct: Record<string, number>;
  actual: { final_pct: number; max_dd_pct: number };
  fan: { n: number; '5': number; '25': number; '50': number; '75': number; '95': number }[];
}

/** Сохранённый прогон: параметры + ключевые цифры на момент сохранения (результат при загрузке пересчитывается заново). */
export interface RunSummary {
  trades: number;
  total_usd: number;
  return_pct: number | null;
  sharpe: number | null;
  max_dd_pct: number | null;
  winrate: number;
  profit_factor: number | null;
  symbol: string;
  source: string;
  first_ts: number;
  last_ts: number;
}

export interface SavedRun {
  id: number;
  name: string;
  params: Partial<Params>;
  tf: TfMode;
  summary: RunSummary;
  created: number;
}

/** Мониторинг: расчётная открытая позиция стратегии (по свечам Binance), не позиция на бирже. */
export interface MonitorLeg {
  leg: number;
  kind: string;
  adds_n: number;
  entry_ts: number;
  entry: number;
  notional: number;
  margin: number;
  leverage: number;
  tps: { n: number; price: number; hit: boolean; share: number }[];
  stop: number;
  stop_kind: 'SL' | 'БУ';
  tp_hits: number;
  liq_price: number | null;
  to_stop_pct: number | null;
  to_next_tp_pct: number | null;
  move_pct: number | null;
  pnl_usd: number;
  pnl_pct: number;
  signal_ts: number;
  fills: Fill[];
  metrics_at_entry: { dg: number | null; dl: number | null; ds: number | null };
}

export interface MonitorPositionSummary {
  side: 'long' | 'short';
  opened_ts: number;
  hold_h: number;
  notional: number;
  margin: number;
  avg_entry: number;
  pnl_usd: number;
  pnl_pct_deposit: number | null;
  pnl_pct_margin: number | null;
  deadline_ts: number | null;
  legs_n?: number;
  /** цена уже за оценочной ценой ликвидации */
  danger?: boolean;
  /** только в подробностях */
  legs?: MonitorLeg[];
}

/** Стратегия (конфиг бота) в мониторинге. В списке — краткий вид, в подробностях (detail) — ещё статистика, история, параметры и журнал. */
export interface MonitorEntry {
  name: string;
  title?: string;
  config_id: number | null;
  symbol: string | null;
  source?: string;
  tf?: string;
  baseline?: boolean;
  alerts?: number;
  created?: number;
  auto: { open: boolean; manage: boolean; base?: 'config' | 'account' } | null;
  catchup?: { on: boolean; age_min: number; move_pct: number; armed: boolean };
  pending_entry?: { side: 'long' | 'short'; mode: 'pullback' | 'breakout'; level: number; ref: number; order_id: string | null; expires: number } | null;
  paused: boolean;
  error?: string | null;
  price?: number | null;
  candle_ts?: number | null;
  report?: { ts: number; dg: number | null; dl: number; ds: number } | null;
  ready_now?: PendingSignal[];
  recent?: { side: 'long' | 'short'; entry_ts: number; exit_ts: number; reason: Reason; pnl_usd: number; pnl_pct: number; hold_h: number }[];
  settings?: { sl_pct: number; tps: TpLevel[]; be_after_tp1: boolean; leverage: number; pyramid_leverage: number | null; trade_pct: number; pyramid_enabled: boolean; pyramid_pct?: number | null; deposit: number; liquidation_sim: boolean; weekdays_only: boolean; side: Side };
  position: MonitorPositionSummary | null;
  stats?: Record<string, number | null>;
  params?: Record<string, unknown>;
  history?: { n: number; side: 'long' | 'short'; leg: number; entry_ts: number; entry: number; exit_ts: number; exit: number; reason: Reason; tp_hits: number; pnl_pct: number; pnl_usd: number; hold_h: number; fills: Fill[] }[];
  log?: AutoLogItem[];
}

export interface AutoLogItem {
  id: number;
  config_id: number | null;
  ts: number;
  kind: string;
  symbol: string | null;
  side: string | null;
  status: string;
  details: Record<string, unknown>;
}

export interface ExchangeInfo {
  name: string;
  real: boolean;
  testnet: boolean;
  min_notional: number;
}

/** Подключённый счёт на бирже: кто подключён (адрес маскируется, ключ наружу не отдаётся), баланс, позиции, открытые ордера, последние исполнения. */
export interface ExchangeAccount {
  connected: boolean;
  exchange: ExchangeInfo;
  wallet: string | null;
  agent: string | null;
  created: number | null;
  verified: number | null;
  error: string | null;
  account: { equity?: number | null; free?: number | null; used?: number | null };
  positions: { symbol: string; side: 'long' | 'short'; size: number; entry: number | null; mark: number | null; liq: number | null; pnl_usd: number | null; leverage: number | null; margin: number | null; notional: number | null }[];
  orders: { symbol: string; side: string; type: string; amount: number | null; price: number | null; trigger: number | null; reduce_only: boolean; ts: number | null }[];
  fills: { symbol: string; side: string; amount: number | null; price: number | null; fee: number | null; pnl: string | null; ts: number | null }[];
}

export interface MonitorResponse {
  entries: MonitorEntry[];
  log: AutoLogItem[];
  exchange: ExchangeInfo;
  account?: { equity: number | null; free: number | null } | null;
  bot?: { state: 'ok' | 'dead' | 'stalled' | 'unknown'; alive_age_s: number | null; cycle_age_s: number | null; dead_min: number; stall_min: number };
  data: { source: string; now: number };
}

export interface WfStats { trades: number; total_usd: number; winrate: number; profit_factor: number | null; sharpe: number | null; max_dd_usd: number; return_pct: number | null }
export interface WfFold { train: [string, string]; test: [string, string]; chosen: Record<string, number> | null; train_stats: WfStats | null; test_stats: WfStats | null; base_test_stats: WfStats | null }
export interface WalkForward {
  folds: WfFold[];
  summary: { folds: number; folds_with_choice: number; oos_total_usd: number; oos_trades: number; is_total_usd: number; base_oos_total_usd: number; profitable_folds: number; efficiency: number | null; metric: string; grid_size: number; runs: number };
  oos_curve: [number, number][];
  stability: { values: Record<string, number>; times: number }[];
  verdict: string;
}

export interface PfItem { id: number; name: string; symbol: string | null; paused: boolean; included: boolean; error: string | null; trade_pct?: number; leverage?: number; cfg_trade_pct?: number; cfg_leverage?: number; overridden?: boolean; liq_sim?: boolean; compound?: boolean; cfg_compound?: boolean; deposit?: number; trades?: number; winrate?: number | null; profit_factor?: number | null; total_usd?: number | null; return_pct?: number | null; sharpe?: number | null; max_dd_usd?: number; max_dd_pct?: number; max_dd_real_pct?: number | null; max_dd_real_usd?: number | null; curve?: [number, number][] }
export interface Portfolio {
  items: PfItem[];
  portfolio: { strategies: number; deposit: number; joint: boolean; total_usd: number; return_pct: number | null; max_dd_usd: number; max_dd_pct: number; max_dd_real_usd: number | null; max_dd_real_pct: number | null; min_equity_usd: number | null; max_dd_real_ts: number | null; curve: [number, number][]; trades: number; best: string | null; worst: string | null; peak_margin: number; peak_margin_pct: number | null; peak_margin_ts: number | null };
  correlation: { ids: number[]; names: string[]; matrix: (number | null)[][]; days: number | null; note: string | null };
  exposure: { symbols: { asset: string; long: number; short: number; net: number; gross: number; margin: number; configs: { name: string; side: string }[] }[]; gross: number; net: number; margin: number; leverage: number | null; margin_pct: number | null; warnings: string[]; real?: { equity: number; gross: number; leverage: number | null } };
  settings: { exclude: number[]; total_deposit: number | null; compound: boolean | null; sizing: Record<string, { trade_pct?: number; leverage?: number }> };
}
