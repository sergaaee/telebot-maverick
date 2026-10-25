import { useCallback, useEffect, useState } from 'react';
import { UNAUTH_EVENT, getAuthStatus, getMe, type Me, type MeResponse } from './api';
import App from './App';
import MonitorApp from './MonitorApp';
import Login from './components/Login';

/** Корень: проверяет сессию. Без входа показывает форму логина, после входа — приложение с данными именно этого пользователя. */
export default function Root({ page = 'backtest' }: { page?: 'backtest' | 'monitor' }) {
  const [state, setState] = useState<'loading' | 'login' | 'app'>('loading');
  const [me, setMe] = useState<MeResponse | null>(null);
  const [usersExist, setUsersExist] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await getMe();
      setMe(r);
      setState('app');
    } catch {
      try {
        setUsersExist((await getAuthStatus()).users_exist);
        setState('login');
      } catch (e) {
        setError(`Не удалось связаться с бэкендом: ${e instanceof Error ? e.message : e}`);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    const onUnauth = () => setState((s) => (s === 'app' ? 'login' : s));      // сессия закончилась посреди работы
    window.addEventListener(UNAUTH_EVENT, onUnauth);
    return () => window.removeEventListener(UNAUTH_EVENT, onUnauth);
  }, [refresh]);

  if (error) return <div className="login-page"><div className="banner error">{error}</div></div>;
  if (state === 'loading') return <div className="login-page"><div className="empty">Загрузка…</div></div>;
  if (state === 'login') return <Login usersExist={usersExist} onLoggedIn={(_u: Me) => void refresh()} />;
  const common = {
    me: me!.user,
    onLogout: () => { setMe(null); setState('login'); },
    onProfileChanged: () => void refresh(),
    authEnabled: me!.auth_enabled,
    botUsername: me!.bot_username,
  };
  if (page === 'monitor') return <MonitorApp key={me!.user.login} {...common} />;
  return (
    <App
      key={me!.user.login}
      me={me!.user}
      authEnabled={me!.auth_enabled}
      botUsername={me!.bot_username}
      onProfileChanged={() => void refresh()}
      onLogout={() => { setMe(null); setState('login'); }}
    />
  );
}
