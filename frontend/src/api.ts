import type { ExchangeAccount, ExchangeInfo, AddAssetResult, AppConfig, BacktestResult, CandlesResponse, Job, LatestSource, MonitorEntry, MonitorResponse, MonteCarlo, Params, Portfolio, RunSummary, SavedRun, SyncStatus, TfMode, UiState, WalkForward } from './types';

const BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? '';

/** Событие «сессия закончилась»: корневой компонент возвращает пользователя на экран входа. */
export const UNAUTH_EVENT = 'mav-unauth';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  // служебный заголовок — защита от CSRF (межсайтовая форма не может его отправить); cookie сессии уходит автоматически
  const res = await fetch(BASE + path, { ...init, credentials: 'include', headers: { 'X-Maverick': '1', ...(init?.headers ?? {}) } });
  if (res.status === 401 && !path.startsWith('/api/auth/')) window.dispatchEvent(new Event(UNAUTH_EVENT));
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* тело не JSON */
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export interface Me {
  login: string;
  is_admin: boolean;
  telegram: boolean;
  telegram_name: string | null;
}
export interface MeResponse {
  auth_enabled: boolean;
  user: Me;
  bot_username: string | null;
}

export const getAuthStatus = () => request<{ auth_enabled: boolean; users_exist: boolean }>('/api/auth/status');
export const getMe = () => request<MeResponse>('/api/auth/me');
export const login = (login: string, password: string) =>
  request<{ user: Me; bot_username: string | null }>('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ login, password }),
  });
export const logout = () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' });
export const telegramLinkCode = () => request<{ code: string; ttl_min: number; bot_username: string | null }>('/api/me/telegram/link-code', { method: 'POST' });
export const telegramUnlink = () => request<{ ok: boolean }>('/api/me/telegram', { method: 'DELETE' });

export const getConfig = () => request<AppConfig>('/api/config');

export const runBacktest = (p: Params) =>
  request<BacktestResult>('/api/backtest', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(p),
  });

export const getCandles = (symbol: string, startSec?: number, endSec?: number, tfSec?: number, maxBars?: number) => {
  const q = new URLSearchParams({ symbol });
  if (tfSec != null) q.set('tf', String(tfSec));
  if (maxBars != null) q.set('max_bars', String(maxBars));
  if (startSec != null) q.set('start', String(Math.floor(startSec * 1000)));
  if (endSec != null) q.set('end', String(Math.ceil(endSec * 1000)));
  return request<CandlesResponse>('/api/candles?' + q.toString());
};

/** Ставит загрузку свечей в фон и сразу возвращается; ход и завершение — через getJobs(). */
export const refreshCandles = (symbol: string) =>
  request<{ job: Job; created: boolean }>('/api/candles/refresh?' + new URLSearchParams({ symbol }).toString(), { method: 'POST' });

/** Ставит в фон загрузку недостающих окон свечей топ-коинов (момент сигнала + N суток) для текущих параметров. */
export const downloadTop = (p: Params) =>
  request<{ job: Job | null; created: boolean; windows: number }>('/api/top/download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(p),
  });

export const addAsset = (symbol: string) =>
  request<AddAssetResult>('/api/assets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol }),
  });

export const removeAsset = (symbol: string) =>
  request<{ ok: boolean }>('/api/assets?' + new URLSearchParams({ symbol }).toString(), { method: 'DELETE' });

export const getJobs = () => request<{ jobs: Job[] }>('/api/jobs').then((r) => r.jobs);

export const ackJob = (id: number) => request<Job>(`/api/jobs/${id}/ack`, { method: 'POST' });

export const getLatest = () => request<{ sources: LatestSource[] }>('/api/latest').then((r) => r.sources);

export const getSync = () => request<SyncStatus>('/api/sync');

/** Подгрузить новые сообщения Telegram сейчас, не дожидаясь срока. */
export const runSync = () => request<SyncStatus & { started: boolean }>('/api/sync/run', { method: 'POST' });

export const getUiState = () => request<UiState>('/api/ui-state');

export const putUiState = (state: UiState, keepalive = false) =>
  request<{ ok: boolean }>('/api/ui-state', {
    method: 'PUT',
    keepalive,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(state),
  });

export const runMonteCarlo = (p: Params, runs: number, mode: MonteCarlo['mode']) =>
  request<MonteCarlo>('/api/montecarlo', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...p, runs, mode }),
  });

export const listRuns = () => request<{ runs: SavedRun[] }>('/api/runs').then((r) => r.runs);

export const saveRun = (name: string, params: Params, tf: TfMode, summary: RunSummary) =>
  request<SavedRun>('/api/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, params, tf, summary }),
  });

export const deleteRun = (id: number) => request<{ ok: boolean }>(`/api/runs/${id}`, { method: 'DELETE' });

export const getMonitor = () =>
  request<MonitorResponse>('/api/monitor', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) });

export const getMonitorDetail = (id: number) => request<MonitorEntry>(`/api/monitor/configs/${id}`);

export const pauseConfig = (id: number, paused: boolean) =>
  request<{ paused: boolean }>(`/api/monitor/configs/${id}/pause`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ paused }) });

export const renameConfig = (id: number, name: string) =>
  request<{ name: string }>(`/api/monitor/configs/${id}/name`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }) });

export const deleteConfig = (id: number) => request<{ ok: boolean }>(`/api/monitor/configs/${id}`, { method: 'DELETE' });

export const exportBotConfig = (id: number) => request<Record<string, unknown>>(`/api/monitor/configs/${id}/export`);

export const setAuto = (configId: number, patch: { open?: boolean; manage?: boolean; base?: 'config' | 'account' }) =>
  request<{ auto: { open: boolean; manage: boolean; base: 'config' | 'account' } }>(`/api/monitor/configs/${configId}/auto`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });

export const autoOffAll = () => request<{ disabled: number }>('/api/monitor/auto/off', { method: 'POST' });

export const openTestPosition = (side: 'long' | 'short') =>
  request<{ status: string; result: Record<string, unknown>; margin: number; leverage: number; notional: number; exchange: ExchangeInfo }>('/api/trading/test-position', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true, side, usd: 1 }),
  });

export const closeTestPosition = () =>
  request<{ status: string; result: Record<string, unknown>; exchange: ExchangeInfo }>('/api/trading/test-position/close', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });

export const getExchangeAccount = () => request<ExchangeAccount>('/api/exchange/account');

export const disconnectExchange = () => request<{ removed: boolean }>('/api/exchange/account', { method: 'DELETE' });

export const panicStop = () =>
  request<{ auto_off: number; closed: unknown[]; cancelled: number; errors: string[] }>('/api/panic', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirm: true }) });

export interface WfRequest { grid: { param: string; values: number[] }[]; train_days: number; test_days: number; step_days?: number | null; metric: string; min_trades: number }
export const runWalkForward = (p: Params, wf: WfRequest) =>
  request<WalkForward>('/api/walkforward', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...p, ...wf }) });

export const getPortfolio = () => request<Portfolio>('/api/portfolio', { method: 'POST' });

export const getDigest = () => request<{ mode: 'off' | 'daily' | 'weekly'; telegram: boolean; hour_utc: number }>('/api/digest');
export const setDigest = (mode: string) => request<{ mode: string }>('/api/digest', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ mode }) });

export const savePortfolioPrefs = (patch: { exclude?: number[]; total_deposit?: number | null }) =>
  request<{ exclude: number[]; total_deposit: number | null }>('/api/portfolio/prefs', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
