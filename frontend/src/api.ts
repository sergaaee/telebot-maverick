import type { AppConfig, BacktestResult, CandlesResponse, DataInfo, Params } from './types';

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

export const getCandles = (startSec?: number, endSec?: number, tfSec?: number, maxBars?: number) => {
  const q = new URLSearchParams();
  if (tfSec != null) q.set('tf', String(tfSec));
  if (maxBars != null) q.set('max_bars', String(maxBars));
  if (startSec != null) q.set('start', String(Math.floor(startSec * 1000)));
  if (endSec != null) q.set('end', String(Math.ceil(endSec * 1000)));
  return request<CandlesResponse>('/api/candles?' + q.toString());
};

export const refreshCandles = () => request<DataInfo>('/api/candles/refresh', { method: 'POST' });
