"""E2E-проверка интерфейса в настоящем браузере на синтетических данных (боевые файлы не нужны и не трогаются).

    pip install playwright            # браузер: либо системный Chromium (CHROME_PATH), либо `playwright install chromium`
    (cd frontend && npm ci && npm run build)
    python e2e/run.py

Скрипт сам создаёт демо-данные во временной папке, поднимает бэкенд и `vite preview` на свободных портах,
проверяет выбор сделки, смену таймфрейма, листание, навигацию по сделкам, выбор источника и актива, сохранение
выбора между перезагрузками и подсказки «?», затем всё выключает.
Код возврата 0 — все проверки прошли.
"""
import csv
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
H = 3600
fails = []
STEP = ["start"]       # последняя выполненная проверка — для привязки ошибок браузера к шагу сценария


def check(name, cond, extra=""):
    STEP[0] = name
    print(("OK   " if cond else "FAIL ") + name, extra)
    if not cond:
        fails.append(name)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def T(s):
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp())


def fmt(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")


def make_demo(d: Path):
    """150 дней минутных свечей и два источника отчётов: «coinbase» (без ликвидаций) и «binance» (с ликвидациями)."""
    random.seed(42)
    M, HMS = 60_000, 3_600_000
    t0 = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    days = 150
    n = days * 1440
    price, drift, candles = 90000.0, 0.0, []
    for i in range(n):
        if i % 2000 == 0:
            drift = random.gauss(0, 0.00004)
        price *= 1 + drift + random.gauss(0, 0.00045)
        o = price
        h = o * (1 + abs(random.gauss(0, 0.0003)))
        l = o * (1 - abs(random.gauss(0, 0.0003)))
        c = o * (1 + random.gauss(0, 0.0002))
        candles.append((t0 + i * M, o, max(o, h, c), min(o, l, c), c))
    (d / "cache").mkdir()
    with open(d / "cache" / "binanceusdm_BTCUSDT_USDT_1m.csv", "w", newline="") as f:
        csv.writer(f).writerows(candles)
    k = 3000 / 90000        # второй актив с ценой ~3000: те же движения в другом масштабе
    with open(d / "cache" / "binanceusdm_ETHUSDT_USDT_1m.csv", "w", newline="") as f:
        csv.writer(f).writerows([(t, o * k, h * k, l * k, c * k) for t, o, h, l, c in candles])
    rows, loc, sc = [], 1.2, 1.2
    for k in range(days * 48):
        ts = t0 + k * HMS // 2
        idx = min(k * 30, n - 1)
        ret = (candles[idx][4] / candles[max(idx - 240, 0)][4] - 1) * 100
        loc = 0.9 * loc + 0.1 * (1.2 + ret * 0.8) + random.gauss(0, 0.12)
        sc = 0.8 * sc + 0.2 * (1.2 + ret * 1.1) + random.gauss(0, 0.2)
        rows.append((ts, loc, sc, max(0, random.gauss(300, 400)) * (1 + max(-ret, 0) * 3), max(0, random.gauss(200, 300)) * (1 + max(ret, 0) * 3)))
    hdr = ["id", "date_utc", "deep_global", "deep_local", "deep_scalp", "liq_long_k", "liq_short_k", "text"]
    for name, with_liq, noise in (("history_cb.csv", False, 0.0), ("history_bn.csv", True, 1.0)):
        with open(d / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(hdr)
            for i, (ts, a, b, ll, ls) in enumerate(rows, 1):
                w.writerow([i, datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), 1.0,
                            round(a + random.gauss(0, 0.15 * noise), 2), round(b + random.gauss(0, 0.25 * noise), 2),
                            round(ll, 1) if with_liq else "", round(ls, 1) if with_liq else "", ""])


def wait_http(url, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception:
            time.sleep(0.4)
    raise SystemExit(f"не дождался {url}")


def field(pg, label):
    return pg.locator("aside.params label.field", has_text=label)


def num(pg, label):
    return field(pg, label).locator("input").first


def scenario(pg, base, state_url):
    reqs = []
    pg.on("request", lambda r: reqs.append(r.url.split("candles")[1]) if "/api/candles?" in r.url else None)
    pg.goto(base + "/?debug")
    pg.wait_for_selector("table.trades tbody tr", timeout=30000)
    pg.wait_for_timeout(1500)
    vr = lambda: pg.evaluate("(() => { const r = window.__chart.timeScale().getVisibleRange(); return r ? [r.from, r.to] : null })()")
    rows = pg.locator("table.trades tbody tr")

    # --- источники
    sel = field(pg, "Источник метрик").locator("select")
    check("источники в списке", sel.locator("option").all_inner_texts() == ["Coinbase Maverick", "Binance Maverick"])
    check("источник по умолчанию из env", sel.input_value() == "binance")
    n_bn, tot_bn = rows.count(), pg.locator(".card-value").first.inner_text()
    sel.select_option("coinbase")
    pg.wait_for_timeout(3000)
    check("смена источника пересчитывает бэктест", (rows.count(), pg.locator(".card-value").first.inner_text()) != (n_bn, tot_bn))
    sel.select_option("binance")
    pg.wait_for_timeout(3000)

    # --- выбор сделки и ТФ
    tr = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({})}).then(r => r.json())""")["trades"][4]
    ea, eb = tr["entry_ts"] // 1000, tr["exit_ts"] // 1000
    pad = max((eb - ea) * 0.6, 6 * H)
    exp = (ea - pad, eb + pad)
    tol = (exp[1] - exp[0]) * 0.08
    near = lambda v: bool(v) and abs(v[0] - exp[0]) < tol and abs(v[1] - exp[1]) < tol
    check("старт: виден весь период", vr()[1] - vr()[0] > 100 * 86400)
    rows.nth(4).click()
    pg.wait_for_timeout(2500)
    v = vr()
    check("выбор сделки приближает график к ней", near(v), f"{fmt(v[0])} .. {fmt(v[1])}")
    for label, sec in (("1h", 3600), ("15m", 900), ("4h", 14400)):
        n0 = len(reqs)
        pg.locator(".seg button", has_text=label).first.click()
        pg.wait_for_timeout(2500)
        v = vr()
        check(f"ТФ {label}: вид остаётся на сделке и запрошен tf={sec}", near(v) and any(f"tf={sec}" in r for r in reqs[n0:]), f"{fmt(v[0])} .. {fmt(v[1])}")

    # --- навигация по сделкам
    pg.get_by_title("Следующая сделка").click()
    pg.wait_for_timeout(2500)
    check("▶ — следующая сделка", pg.locator(".sel-info").inner_text().startswith("#6"))
    pg.keyboard.press("ArrowLeft")
    pg.wait_for_timeout(2000)
    check("← — предыдущая сделка", pg.locator(".sel-info").inner_text().startswith("#5"))
    pg.locator(".seg button", has_text="Авто").first.click()
    pg.wait_for_timeout(2500)
    v = vr()
    check("возврат в «Авто» оставляет вид на сделке", abs((v[0] + v[1]) / 2 - (ea + eb) / 2) < 12 * H, f"{fmt(v[0])} .. {fmt(v[1])}")

    # --- листание из середины истории
    rows.nth(60).scroll_into_view_if_needed()
    rows.nth(60).click()
    pg.wait_for_timeout(2000)
    pg.locator(".seg button", has_text="15m").first.click()
    pg.wait_for_timeout(2500)
    v0, n0 = vr(), len(reqs)
    box = pg.locator(".price").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.mouse.move(x + 900, y, steps=20)
    pg.mouse.up()
    pg.wait_for_timeout(2500)
    v1 = vr()
    check("листание сдвигает вид в прошлое", v0[0] - v1[0] > (v0[1] - v0[0]) * 0.3)
    span = v1[1] - v1[0]
    for _ in range(3):
        pg.mouse.move(x, y)
        pg.mouse.down()
        pg.mouse.move(x + 1400, y, steps=30)
        pg.mouse.up()
        pg.wait_for_timeout(2200)
    v2 = vr()
    check("дальнее листание догружает свечи, масштаб сохраняется", v2[0] < v1[0] - span and len(reqs) > n0 and abs((v2[1] - v2[0]) - span) < span * 0.15,
          f"{fmt(v2[0])} .. {fmt(v2[1])}, запросов +{len(reqs) - n0}")
    rows.nth(60).click()
    pg.wait_for_timeout(2500)
    v3 = vr()
    check("повторный клик по сделке возвращает к ней", v3[0] > v2[0], f"{fmt(v3[0])} .. {fmt(v3[1])}")

    # --- актив: другие свечи и пересчёт
    asset = field(pg, "Актив")
    check("активы в списке", asset.locator("option").all_inner_texts() == ["BTC/USDT:USDT", "ETH/USDT:USDT"])
    btc_price = float(rows.nth(0).locator("td").nth(3).inner_text().replace("\xa0", "").replace(" ", "").replace(",", "."))
    asset.locator("select").select_option("ETH/USDT:USDT")
    pg.wait_for_timeout(3500)
    eth_price = float(rows.nth(0).locator("td").nth(3).inner_text().replace("\xa0", "").replace(" ", "").replace(",", "."))
    check("смена актива пересчитала сделки на ценах актива", btc_price > 20000 > eth_price > 100, f"BTC {btc_price} -> ETH {eth_price}")
    asset.locator("select").select_option("BTC/USDT:USDT")
    pg.wait_for_timeout(3500)

    # --- сохранение выбора по связкам «источник + актив»
    src = field(pg, "Источник метрик").locator("select")
    num(pg, "Тейк-профит").fill("7")
    pg.locator(".seg button", has_text="1h").first.click()
    pg.wait_for_timeout(1200)                     # дебаунс сохранения
    src.select_option("coinbase")
    pg.wait_for_timeout(3000)
    check("новая связка начинает с умолчаний из env", num(pg, "Тейк-профит").input_value() == "6")
    num(pg, "Тейк-профит").fill("8")
    pg.locator(".seg button", has_text="4h").first.click()
    pg.wait_for_timeout(1200)
    src.select_option("binance")
    pg.wait_for_timeout(3000)
    check("возврат к связке восстанавливает её параметры и ТФ", num(pg, "Тейк-профит").input_value() == "7"
          and "on" in (pg.locator(".seg button", has_text="1h").first.get_attribute("class") or ""))
    asset.locator("select").select_option("ETH/USDT:USDT")
    pg.wait_for_timeout(3000)
    check("у другого актива свои настройки", num(pg, "Тейк-профит").input_value() == "6")
    pg.wait_for_timeout(1000)
    saved = pg.evaluate(f"fetch('{state_url}').then(r => r.json())")
    check("состояние записано на сервере", saved.get("last") == {"source": "binance", "symbol": "ETH/USDT:USDT"} and len(saved.get("profiles", {})) >= 3,
          str(saved.get("last")))
    pg.reload()
    pg.wait_for_selector("table.trades tbody tr", timeout=30000)
    pg.wait_for_timeout(1500)
    check("после перезагрузки восстановлены последние источник и актив",
          field(pg, "Источник метрик").locator("select").input_value() == "binance" and field(pg, "Актив").locator("select").input_value() == "ETH/USDT:USDT")
    field(pg, "Актив").locator("select").select_option("BTC/USDT:USDT")
    pg.wait_for_timeout(3000)
    check("после перезагрузки сохранены параметры связки", num(pg, "Тейк-профит").input_value() == "7")

    # --- подсказки «?»
    # раскрываем свёрнутые секции и включаем фильтры, чтобы увидеть все поля
    for title in ("Фильтр: остывание метрик", "Фильтр: ликвидации закончились", "Период"):
        btn = pg.get_by_role("button", name=title)
        if "▸" in btn.inner_text():
            btn.click()
    field(pg, "Режим").first.locator("select").select_option("drop")     # режим фильтра остывания: открывает поля метрики и порога
    pg.locator("aside.params label.field.check input").check()
    fields = pg.locator("aside.params label.field")
    n_fields = fields.count()
    no_help = [fields.nth(i).inner_text().strip().split("\n")[0] for i in range(n_fields) if fields.nth(i).locator(".help").count() == 0]
    check("у КАЖДОГО параметра в панели есть «?»", n_fields >= 20 and not no_help, f"полей {n_fields}, без подсказки: {no_help}")
    groups = pg.locator("aside.params .group-head")
    no_group_help = [groups.nth(i).inner_text().strip() for i in range(groups.count())
                     if groups.nth(i).locator(".help").count() == 0 and groups.nth(i).inner_text().strip() not in ("▾ Данные", "▾ Режим")]
    check("у групп параметров и фильтров есть «?»", not no_group_help, str(no_group_help))
    for label in ("Подтверждение", "Тейк-профит", "Источник метрик", "Актив"):
        h = field(pg, label).locator(".help").first
        h.hover()
        pg.wait_for_timeout(200)
        tip = pg.locator(".tooltip")
        box = tip.bounding_box() if tip.count() else None
        vp = pg.viewport_size
        ok = bool(box) and len(tip.inner_text()) > 40 and box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vp["width"] and box["y"] + box["height"] <= vp["height"]
        check(f"подсказка «{label}» показывается целиком", ok, (tip.inner_text()[:50] if tip.count() else "нет"))
        pg.mouse.move(5, 5)
        pg.wait_for_timeout(100)
    check("подсказка скрывается", pg.locator(".tooltip").count() == 0)
    pg.locator(".seg").locator("xpath=following-sibling::*[contains(@class,'help')][1]").hover()
    pg.wait_for_timeout(200)
    check("подсказка у переключателя ТФ", pg.locator(".tooltip").count() == 1 and "Авто" in pg.locator(".tooltip").inner_text())


def main():
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="maverick-e2e-"))
    procs = []
    try:
        make_demo(tmp)
        if not (ROOT / "frontend" / "dist" / "index.html").exists():
            sys.exit("Сначала соберите фронтенд: (cd frontend && npm ci && npm run build)")
        bp, fp = free_port(), free_port()
        env = dict(os.environ, SOURCES="coinbase,binance", SOURCE_DEFAULT="binance", SOURCE_COINBASE_LABEL="Coinbase Maverick",
                   SOURCE_COINBASE_FILE=str(tmp / "history_cb.csv"), SOURCE_BINANCE_LABEL="Binance Maverick",
                   SOURCE_BINANCE_FILE=str(tmp / "history_bn.csv"), CACHE_DIR=str(tmp / "cache"), LIQ_SOURCE="", HISTORY_FILE="", SYMBOLS="BTC/USDT:USDT,ETH/USDT:USDT", SYMBOL_DEFAULT="BTC/USDT:USDT",
                   STATE_FILE=str(tmp / "ui_state.json"),
                   LOCAL_MIN="1.85", SCALP_MIN="1.25", CONFIRM="4", TP_PCT="6", SL_PCT="2", SIDE="both", ONLY="", COOL_DROP="",
                   COOL_DECLINES="", LIQ_HOURS="", COOLDOWN_H="0", FEE="0.0005", SLIPPAGE="0", ORDER_SIZE="0.1",
                   VITE_PROXY_TARGET=f"http://127.0.0.1:{bp}", VITE_PORT=str(fp))
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(bp), "--log-level", "warning"],
                                      cwd=ROOT / "backend", env=env))
        procs.append(subprocess.Popen(["npx", "vite", "preview", "--host", "127.0.0.1", "--port", str(fp), "--strictPort"],
                                      cwd=ROOT / "frontend", env=env, stdout=subprocess.DEVNULL))
        wait_http(f"http://127.0.0.1:{bp}/api/health")
        wait_http(f"http://127.0.0.1:{fp}/")
        errors = []
        with sync_playwright() as p:
            chrome = os.getenv("CHROME_PATH")
            b = p.chromium.launch(executable_path=chrome, args=["--no-sandbox"]) if chrome else p.chromium.launch(args=["--no-sandbox"])
            pg = b.new_page(viewport={"width": 1700, "height": 1000})
            pg.on("console", lambda m: errors.append(f"после «{STEP[0]}»: {m.type}: {m.text}") if m.type in ("error", "warning") else None)
            pg.on("pageerror", lambda e: errors.append(f"после «{STEP[0]}»: pageerror: {e}\n{(getattr(e, 'stack', '') or '')[:700]}"))
            scenario(pg, f"http://127.0.0.1:{fp}", f"http://127.0.0.1:{fp}/api/ui-state")
            check("нет ошибок в консоли браузера", not errors, "\n" + "\n".join(errors)[:3000])
            b.close()
    finally:
        for pr in procs:
            pr.terminate()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nИТОГ:", "все проверки прошли" if not fails else f"упало: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
