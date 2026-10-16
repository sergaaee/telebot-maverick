import type { Toast } from '../jobs';

/** Уведомления в правом нижнем углу. Уведомления о завершении загрузки остаются, пока их не закроют. */
export default function Toasts({ toasts, onClose }: { toasts: Toast[]; onClose: (id: string) => void }) {
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div className={`toast toast-${t.kind}`} key={t.id}>
          <span className="toast-icon">{t.kind === 'success' ? '✓' : t.kind === 'error' ? '!' : 'ℹ'}</span>
          <div className="toast-body">
            <div>{t.text}</div>
            {t.action && (
              <button type="button" className="toast-action" onClick={() => { t.action!.onClick(); onClose(t.id); }}>
                {t.action.label}
              </button>
            )}
          </div>
          <button type="button" className="toast-close" aria-label="Закрыть" onClick={() => onClose(t.id)}>×</button>
        </div>
      ))}
    </div>
  );
}
