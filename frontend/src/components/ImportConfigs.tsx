import { useState } from 'react';
import { importBotConfigs } from '../api';
import type { ImportConfigsResult } from '../api';
import { HELP } from '../helpTexts';
import Help from './Help';

interface Picked { label: string; text: string; hint: string }

/** Имя конфига по имени файла (как делает бот): общие «maverick-config…» / «config…» имени не дают. */
const hintFromFile = (name: string) => (/^(maverick-config|config)/i.test(name) ? '' : name.replace(/\.[^.]+$/, ''));

/** Добавление конфигов бота прямо на странице мониторинга: вставленный JSON и/или файлы (можно несколько). */
export default function ImportConfigs({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [name, setName] = useState('');
  const [files, setFiles] = useState<Picked[]>([]);
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false);
  const [lines, setLines] = useState<{ ok: boolean; text: string }[]>([]);

  const pick = async (list: FileList | null) => {
    if (!list || !list.length) return;
    const got: Picked[] = [];
    for (const f of Array.from(list)) {
      try {
        got.push({ label: f.name, text: (await f.text()).replace(/^﻿/, ''), hint: hintFromFile(f.name) });
      } catch {
        setLines((l) => [...l, { ok: false, text: `${f.name}: не удалось прочитать файл` }]);
      }
    }
    setFiles((cur) => [...cur, ...got]);
  };

  const submit = async () => {
    const jobs: { label: string; text: string; name: string }[] = [
      ...files.map((f) => ({ label: f.label, text: f.text, name: files.length === 1 && !text.trim() ? name.trim() || f.hint : f.hint })),
      ...(text.trim() ? [{ label: 'Вставленный текст', text, name: name.trim() }] : []),
    ];
    if (!jobs.length) return;
    if (replace && !window.confirm('Конфиги, чьи имена совпадут с уже существующими, будут ЗАМЕНЕНЫ: их автомод и учёт сигналов начнутся заново, а уже открытую на бирже позицию бот перестанет вести (стоп и тейки на бирже останутся). Продолжить?')) return;
    setBusy(true);
    setLines([]);
    const out: { ok: boolean; text: string }[] = [];
    let any = false;
    for (const j of jobs) {
      try {
        const r: ImportConfigsResult = await importBotConfigs(j.text, { name: j.name, replace });
        for (const a of r.added) {
          any = true;
          out.push({ ok: true, text: `${a.created ? '➕ добавлен' : '♻️ заменён'}: #${a.id} ${a.name}${a.ignored.length ? ` (пропущены поля: ${a.ignored.slice(0, 6).join(', ')})` : ''}` });
        }
        for (const e of r.errors) out.push({ ok: false, text: `${e.name}: ${e.error}` });
      } catch (e) {
        out.push({ ok: false, text: `${j.label}: ${e instanceof Error ? e.message : String(e)}` });
      }
    }
    setLines(out);
    setBusy(false);
    if (any) {
      setText('');
      setName('');
      setFiles([]);
      onDone();
    }
  };

  return (
    <div className="mon-import" data-testid="cfg-add">
      <div className="mon-test">
        <button type="button" className="ghost" onClick={() => setOpen(!open)} aria-expanded={open} data-testid="cfg-add-open">{open ? '▾' : '➕'} Добавить конфиг</button>
        <Help text={HELP.config_add} />
        {!open && lines.length > 0 && <span className="muted small" data-testid="cfg-add-summary">{lines.filter((l) => l.ok).length ? `${lines.filter((l) => l.ok).length} добавлено` : ''}{lines.some((l) => !l.ok) ? ` · ошибок: ${lines.filter((l) => !l.ok).length}` : ''}</span>}
      </div>
      {open && (
        <div className="cfg-import mon-import-form">
          <textarea rows={7} placeholder="Вставьте сюда JSON конфига (один, список или пакет) или выберите файлы" value={text} onChange={(e) => { setText(e.target.value); setLines([]); }} aria-label="JSON конфига для добавления" data-testid="cfg-add-text" />
          <div className="add-asset-row">
            <input type="file" multiple accept=".json,.txt,application/json" onChange={(e) => { void pick(e.target.files); e.target.value = ''; }} aria-label="Файлы конфигов" data-testid="cfg-add-file" />
            {files.length > 0 && <span className="muted small" data-testid="cfg-add-files">Файлов: {files.length} ({files.map((f) => f.label).join(', ')}) <button type="button" className="link" onClick={() => setFiles([])}>убрать</button></span>}
          </div>
          <div className="add-asset-row">
            <label className="small">Имя <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="по желанию, если конфиг один" maxLength={60} aria-label="Имя нового конфига" data-testid="cfg-add-name" /></label>
            <label className="small"><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} data-testid="cfg-add-replace" /> заменить конфиг с таким же именем</label>
          </div>
          <div className="add-asset-row">
            <button type="button" className="ghost" disabled={busy || (!text.trim() && !files.length)} onClick={() => void submit()} data-testid="cfg-add-submit">{busy ? 'Добавляю…' : 'Добавить'}</button>
            <button type="button" className="link" onClick={() => { setOpen(false); }}>свернуть</button>
          </div>
          {lines.length > 0 && (
            <div className="mon-import-result" data-testid="cfg-add-result">
              {lines.map((l, i) => <div key={i} className={`small ${l.ok ? '' : 'bad-text'}`}>{l.ok ? '' : '❌ '}{l.text}</div>)}
              {lines.some((l) => l.ok) && <div className="muted small">Автомод у нового конфига выключен — включите в его карточке. Сигналы бот начнёт присылать после первого расчёта (ближайший цикл).</div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
