import { useCallback, useEffect, useRef, useState } from 'react';
import { ackJob, getJobs } from './api';
import type { Job } from './types';

export interface Toast {
  id: string;
  kind: 'info' | 'success' | 'error';
  text: string;
  /** задача, о завершении которой уведомление: при закрытии тоста она помечается просмотренной на сервере */
  jobId?: number;
  action?: { label: string; onClick: () => void };
  /** информационные тосты исчезают сами, уведомления о завершении ждут закрытия */
  autoCloseMs?: number;
}

export const isActive = (j: Job) => j.status === 'queued' || j.status === 'running';

/** Системное уведомление браузера (если разрешено и вкладка в фоне): загрузка может идти минутами, пользователь уходит на другие дела. */
export function notifyBrowser(title: string, body: string) {
  try {
    if ('Notification' in window && Notification.permission === 'granted' && document.hidden) new Notification(title, { body });
  } catch {
    /* уведомления недоступны — остаётся уведомление внутри страницы */
  }
}

/** Просим разрешение на системные уведомления в ответ на действие пользователя (браузеры требуют жест). */
export function askNotificationPermission() {
  try {
    if ('Notification' in window && Notification.permission === 'default') void Notification.requestPermission();
  } catch {
    /* ignore */
  }
}

/**
 * Фоновые загрузки свечей: опрашивает сервер, пока есть активные задачи (иначе редко — на случай задач, запущенных в другом окне),
 * и сообщает о завершении. При открытии страницы показывает завершённые, но ещё не подтверждённые задачи — так уведомление
 * не теряется, если страницу перезагрузили или закрыли во время загрузки.
 */
export function useJobs(onFinished: (job: Job, restored: boolean) => void) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const known = useRef(new Map<number, Job['status']>());
  const firstPoll = useRef(true);
  const cb = useRef(onFinished);
  cb.current = onFinished;

  const poll = useCallback(async () => {
    let list: Job[];
    try {
      list = await getJobs();
    } catch {
      return; // бэкенд временно недоступен — повторим на следующем тике
    }
    setJobs(list);
    const first = firstPoll.current;
    firstPoll.current = false;
    for (const j of list) {
      const prev = known.current.get(j.id);
      known.current.set(j.id, j.status);
      const finished = j.status === 'done' || j.status === 'error';
      if (!finished) continue;
      if (first) {
        if (!j.acked) cb.current(j, true); // завершилось, пока страница была закрыта
      } else if (prev !== j.status && !j.acked) {
        cb.current(j, false);
      }
    }
  }, []);

  const hasActive = jobs.some(isActive);
  useEffect(() => {
    void poll();
  }, [poll]);
  useEffect(() => {
    const t = setInterval(() => void poll(), hasActive ? 2000 : 20000);
    return () => clearInterval(t);
  }, [hasActive, poll]);

  const push = useCallback((t: Omit<Toast, 'id'>) => {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setToasts((all) => [...all.filter((x) => !(t.jobId != null && x.jobId === t.jobId)), { ...t, id }]);
    if (t.autoCloseMs) setTimeout(() => setToasts((all) => all.filter((x) => x.id !== id)), t.autoCloseMs);
  }, []);

  const dismiss = useCallback((id: string) => {
    setToasts((all) => {
      const t = all.find((x) => x.id === id);
      if (t?.jobId != null) ackJob(t.jobId).catch(() => undefined);
      return all.filter((x) => x.id !== id);
    });
  }, []);

  /** Новая задача, только что запущенная пользователем: сразу показываем её и ускоряем опрос. */
  const track = useCallback(
    (job: Job) => {
      if (!known.current.has(job.id)) known.current.set(job.id, job.status);
      setJobs((all) => [...all.filter((x) => x.id !== job.id), job]);
      void poll();
    },
    [poll],
  );

  return { jobs, toasts, push, dismiss, track };
}
