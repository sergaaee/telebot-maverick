import type { AddAssetResult, AppConfig, BacktestResult, CandlesResponse, Job, Params, UiState } from './types';

const BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? '';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(BASE + path, init);
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

export const getUiState = () => request<UiState>('/api/ui-state');

export const putUiState = (state: UiState, keepalive = false) =>
  request<{ ok: boolean }>('/api/ui-state', {
    method: 'PUT',
    keepalive,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(state),
  });
