import type { Me } from './api';
import { useState } from 'react';
import MonitorPage from './components/MonitorPage';
import PortfolioPanel from './components/PortfolioPanel';
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
  const [tab, setTab] = useState<'strategies' | 'portfolio'>('strategies');
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
      <div className="seg mon-seg" role="tablist">
        <button type="button" role="tab" aria-selected={tab === 'strategies'} className={tab === 'strategies' ? 'on' : ''} data-testid="tab-strategies" onClick={() => setTab('strategies')}>Стратегии и позиции</button>
        <button type="button" role="tab" aria-selected={tab === 'portfolio'} className={tab === 'portfolio' ? 'on' : ''} data-testid="tab-portfolio" onClick={() => setTab('portfolio')}>Портфель и риск</button>
      </div>
      {tab === 'strategies' ? <MonitorPage /> : <PortfolioPanel />}
    </div>
  );
}
