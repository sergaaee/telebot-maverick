import { useState, type FormEvent } from 'react';
import { login, type Me } from '../api';

/** Экран входа. Регистрации здесь нет: пользователей заводит администратор через `python cli.py user add`. */
export default function Login({ onLoggedIn, usersExist }: { onLoggedIn: (u: Me) => void; usersExist: boolean }) {
  const [name, setName] = useState('');
  const [pass, setPass] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await login(name.trim(), pass);
      setPass('');
      onLoggedIn(r.user);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={submit}>
        <h1>Maverick <span>· вход</span></h1>
        <label>
          Логин
          <input autoFocus autoComplete="username" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label>
          Пароль
          <input type="password" autoComplete="current-password" value={pass} onChange={(e) => setPass(e.target.value)} />
        </label>
        {error && <div className="banner error" role="alert">{error}</div>}
        {!usersExist && <div className="banner warn">Пользователей ещё нет. Создайте первого в консоли: <code>python cli.py user add ЛОГИН</code></div>}
        <button type="submit" className="run" disabled={busy || !name.trim() || !pass}>{busy ? 'Вхожу…' : 'Войти'}</button>
        <p className="note">Регистрация — только через администратора (консоль сервера).</p>
      </form>
    </div>
  );
}
