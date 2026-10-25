import type { Me } from './api';
import MonitorPage from './components/MonitorPage';
import UserMenu from './components/UserMenu';

interface Props {
  me: Me;
  authEnabled: boolean;
  botUsername: string | null;
  onProfileChanged: () => void;
  onLogout: () => void;
}

/** Отдельное приложение мониторинга позиций: своя шапка, навигация и раскладка. Бэктест здесь не загружается. */
export default function MonitorApp({ me, authEnabled, botUsername, onProfileChanged, onLogout }: Props) {
  return (
    <div className="mon-app">
      <header className="mon-header">
        <h1>Maverick <span>· мониторинг</span></h1>
        <nav className="mon-nav">
          <a href="/" data-testid="nav-backtest">Бэктест</a>
          <a href="/monitor.html" className="on" aria-current="page">Мониторинг позиций</a>
        </nav>
        <span className="mon-spacer" />
        <UserMenu me={me} authEnabled={authEnabled} botUsername={botUsername} onChanged={onProfileChanged} onLogout={onLogout} />
      </header>
      <MonitorPage />
    </div>
  );
}
