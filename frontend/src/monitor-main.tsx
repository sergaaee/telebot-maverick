import React from 'react';
import ReactDOM from 'react-dom/client';
import Root from './Root';
import './styles.css';

// отдельная страница мониторинга позиций (/monitor.html): своя шапка и раскладка, бэктест не загружается
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <Root page="monitor" />
  </React.StrictMode>,
);
