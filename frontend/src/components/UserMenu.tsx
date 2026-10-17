import { useState } from 'react';
import { logout, telegramLinkCode, telegramUnlink, type Me } from '../api';
import { HELP } from '../helpTexts';
import Help from './Help';

interface Props {
  me: Me;
  botUsername: string | null;
  authEnabled: boolean;
  onChanged: () => void;
  onLogout: () => void;
}

/** Пользователь, привязка Telegram (код для команды /link в боте) и выход. */
export default function UserMenu({ me, botUsername, authEnabled, onChanged, onLogout }: Props) {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState<{ code: string; ttl: number } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const getCode = async () => {
    setErr(null);
    try {
      const r = await telegramLinkCode();
      setCode({ code: r.code, ttl: r.ttl_min });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };
  const unlink = async () => {
    await telegramUnlink().catch(() => undefined);
    setCode(null);
    onChanged();
  };
  const out = async () => {
    await logout().catch(() => undefined);
    onLogout();
  };

  return (
    <div className="user-menu">
      <button type="button" className="ghost" onClick={() => setOpen(!open)} aria-expanded={open} data-testid="user-button">
        👤 {me.login}{me.telegram ? ' · ✈' : ''}
      </button>
      {open && (
        <div className="user-pop" data-testid="user-pop">
          <div>
            <b>{me.login}</b>{me.is_admin ? ' · админ' : ''}
          </div>
          <div className="note">
            Telegram-бот сигналов: {me.telegram ? `привязан${me.telegram_name ? ` (@${me.telegram_name})` : ''}` : 'не привязан'} <Help text={HELP.telegram_link} />
          </div>
          {me.telegram ? (
            <button type="button" className="link" onClick={() => void unlink()}>отвязать Telegram</button>
          ) : code ? (
            <div className="link-code">
              Отправьте боту{botUsername ? ` @${botUsername}` : ''} команду:
              <code>/link {code.code}</code>
              <span className="note">код действует {code.ttl} мин; после привязки обновите это окно</span>
              <button type="button" className="link" onClick={onChanged}>я привязал(а)</button>
            </div>
          ) : (
            <button type="button" className="ghost" onClick={() => void getCode()}>Привязать Telegram</button>
          )}
          {err && <div className="src-info bad-text">{err}</div>}
          {authEnabled && <button type="button" className="ghost" onClick={() => void out()} data-testid="logout">Выйти</button>}
        </div>
      )}
    </div>
  );
}
