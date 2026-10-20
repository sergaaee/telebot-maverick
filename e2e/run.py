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
USER_PASS = "demo-pass-123"
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
                # блок Top Coin как в настоящих отчётах: TIA и ARB торгуются, WBETH — нет (нет фьючерса)
                coin = ("TIA-USD      | 10.12", "ARBUSDT | 7.30", "WBETHUSDT | 12.40")[i % 3] if with_liq else None
                text = f"Deep\nTop Coin\n————————————————\n{coin}\nBTC-USD | 1.02\n" if coin else ""
                w.writerow([i, datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat(), 1.0,
                            round(a + random.gauss(0, 0.15 * noise), 2), round(b + random.gauss(0, 0.25 * noise), 2),
                            round(ll, 1) if with_liq else "", round(ls, 1) if with_liq else "", text])


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
    pg.wait_for_selector("form.login-card", timeout=30000)
    check("без входа видна только форма логина, данных нет", pg.locator("table.trades").count() == 0 and pg.get_by_role("button", name="Войти").is_disabled())
    check("анонимный запрос к API отклонён", pg.evaluate("fetch('/api/config').then(r => r.status)") == 401)
    pg.get_by_label("Логин").fill("tester"); pg.get_by_label("Пароль").fill("не-тот-пароль")
    pg.get_by_role("button", name="Войти").click()
    pg.wait_for_selector(".login-card .banner.error", timeout=10000)
    check("неверный пароль: понятная ошибка, остаёмся на форме", "Неверный логин или пароль" in pg.locator(".login-card .banner.error").inner_text() and pg.locator("table.trades").count() == 0)
    pg.get_by_label("Пароль").fill(USER_PASS)
    pg.get_by_role("button", name="Войти").click()
    pg.wait_for_selector("table.trades tbody tr", timeout=30000)
    pg.wait_for_timeout(1500)
    check("после входа видны данные, в шапке имя пользователя", "tester" in pg.locator("[data-testid=user-button]").inner_text())
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
    tr = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({})}).then(r => r.json())""")["trades"][4]
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
    check("активы в списке", asset.locator("option").all_inner_texts() == ["BTC/USDT:USDT", "ETH/USDT:USDT", "＋ Добавить актив…"])
    btc_price = float(rows.nth(0).locator("td").nth(3).inner_text().replace("\xa0", "").replace(" ", "").replace(",", "."))
    asset.locator("select").select_option("ETH/USDT:USDT")
    pg.wait_for_timeout(3500)
    eth_price = float(rows.nth(0).locator("td").nth(3).inner_text().replace("\xa0", "").replace(" ", "").replace(",", "."))
    check("смена актива пересчитала сделки на ценах актива", btc_price > 20000 > eth_price > 100, f"BTC {btc_price} -> ETH {eth_price}")
    check("после смены актива шкала цены подобрана под ETH", 0 < float(pg.locator(".price .chart-host").get_attribute("data-price-top")) < 20000, pg.locator(".price .chart-host").get_attribute("data-price-top"))
    asset.locator("select").select_option("BTC/USDT:USDT")
    pg.wait_for_timeout(3500)
    # ручное масштабирование по цене (перетаскивание шкалы справа) отключает автошкалу — после смены актива она должна вернуться
    box = pg.locator(".price .chart-host").bounding_box()
    pg.mouse.move(box["x"] + box["width"] - 25, box["y"] + box["height"] / 2); pg.mouse.down()
    pg.mouse.move(box["x"] + box["width"] - 25, box["y"] + box["height"] / 2 - 120, steps=8); pg.mouse.up()
    asset.locator("select").select_option("ETH/USDT:USDT")
    pg.wait_for_timeout(3500)
    check("шкала цены, сдвинутая вручную, после смены актива снова подобрана автоматически", 0 < float(pg.locator(".price .chart-host").get_attribute("data-price-top")) < 20000, pg.locator(".price .chart-host").get_attribute("data-price-top"))
    asset.locator("select").select_option("BTC/USDT:USDT")
    pg.wait_for_timeout(3500)

    # --- сохранение выбора по связкам «источник + актив»
    src = field(pg, "Источник метрик").locator("select")
    num(pg, "Стоп-лосс").fill("7")
    pg.locator(".seg button", has_text="1h").first.click()
    pg.wait_for_timeout(1200)                     # дебаунс сохранения
    src.select_option("coinbase")
    pg.wait_for_timeout(3000)
    check("новая связка начинает с умолчаний из env", num(pg, "Стоп-лосс").input_value() == "2")
    num(pg, "Стоп-лосс").fill("8")
    pg.locator(".seg button", has_text="4h").first.click()
    pg.wait_for_timeout(1200)
    src.select_option("binance")
    pg.wait_for_timeout(3000)
    check("возврат к связке восстанавливает её параметры и ТФ", num(pg, "Стоп-лосс").input_value() == "7"
          and "on" in (pg.locator(".seg button", has_text="1h").first.get_attribute("class") or ""))
    asset.locator("select").select_option("ETH/USDT:USDT")
    pg.wait_for_timeout(3000)
    check("у другого актива свои настройки", num(pg, "Стоп-лосс").input_value() == "2")
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
    check("после перезагрузки сохранены параметры связки", num(pg, "Стоп-лосс").input_value() == "7")

    # --- добавление актива: проверка на бирже, фоновая загрузка, уведомления
    sel_asset = field(pg, "Актив").locator("select")
    toast = pg.locator(".toast")

    def add_asset(text):
        sel_asset.select_option("__add_asset__")
        pg.locator(".add-asset input[type=text]").fill(text)
        pg.get_by_role("button", name="Добавить и скачать").click()

    add_asset("xyz")
    pg.wait_for_timeout(800)
    check("несуществующий актив: понятная ошибка и подсказка, список не изменился",
          "нет на binanceusdm" in pg.locator(".add-asset").inner_text() and sel_asset.locator("option").count() == 3, pg.locator(".add-asset .bad-text").inner_text()[:80])
    pg.locator(".add-asset input[type=text]").fill("ethz")
    pg.get_by_role("button", name="Добавить и скачать").click(); pg.wait_for_timeout(600)
    check("в ошибке есть похожие активы", "ETH/USDT:USDT" in pg.locator(".add-asset .bad-text").inner_text())
    pg.locator(".add-asset input[type=text]").fill("sol")
    pg.get_by_role("button", name="Добавить и скачать").click(); pg.wait_for_selector(".toast", timeout=8000); pg.wait_for_timeout(300)
    t_all = toast.all_inner_texts()
    check("после добавления сообщение: качается в фоне, придёт уведомление",
          len(t_all) == 1 and "скачиваются в фоне" in t_all[0] and "придёт уведомление" in t_all[0], f"тостов {len(t_all)}: {t_all}")
    opts = sel_asset.locator("option").all_inner_texts()
    check("новый актив в списке с пометкой «загружается»", any(o.startswith("SOL/USDT:USDT") and "загружается" in o for o in opts), str(opts))
    check("в шапке виден ход загрузки", pg.locator(".chip-job", has_text="SOL").count() == 1)
    check("пока качается, актив выбрать нельзя", sel_asset.locator("option", has_text="SOL").first.get_attribute("disabled") is not None)
    pg.wait_for_selector(".toast-success", timeout=30000)
    check("по завершении приходит уведомление об успехе", "Свечи SOL/USDT:USDT загружены" in pg.locator(".toast-success").first.inner_text(), pg.locator(".toast-success").first.inner_text()[:80])
    check("ход загрузки из шапки исчез", pg.locator(".chip-job").count() == 0)
    pg.get_by_role("button", name="Открыть SOL/USDT:USDT").click()
    pg.wait_for_timeout(3500)
    sol_price = float(rows.nth(0).locator("td").nth(3).inner_text().replace("\xa0", "").replace(" ", "").replace(",", "."))
    check("кнопка «Открыть» переключила на новый актив, бэктест на его ценах", sel_asset.input_value() == "SOL/USDT:USDT" and sol_price < 1000, f"цена входа {sol_price}")
    check("уведомление закрылось после действия", pg.locator(".toast-success").count() == 0)

    # уведомление не теряется при перезагрузке страницы во время загрузки
    add_asset("doge")
    pg.wait_for_timeout(1500)
    pg.reload(); pg.wait_for_selector("table.trades tbody tr", timeout=30000); pg.wait_for_timeout(1200)
    check("после перезагрузки загрузка всё ещё отображается", pg.locator(".chip-job", has_text="DOGE").count() == 1)
    pg.wait_for_selector(".toast-success", timeout=30000)
    check("уведомление пришло и после перезагрузки", "DOGE/USDT:USDT загружены" in pg.locator(".toast-success").first.inner_text())
    pg.locator(".toast-close").first.click(); pg.wait_for_timeout(600)
    pg.reload(); pg.wait_for_selector("table.trades tbody tr", timeout=30000); pg.wait_for_timeout(2500)
    check("закрытое уведомление больше не показывается", pg.locator(".toast").count() == 0)

    # ошибка загрузки
    add_asset("fail")
    pg.wait_for_selector(".toast-error", timeout=30000)
    check("ошибка загрузки показана в уведомлении с причиной", "429" in pg.locator(".toast-error").first.inner_text(), pg.locator(".toast-error").first.inner_text()[:100])
    pg.locator(".toast-close").first.click()

    # убрать добавленный актив
    sel_asset.select_option("SOL/USDT:USDT"); pg.wait_for_timeout(3000)
    pg.get_by_role("button", name="убрать «SOL/USDT:USDT» из списка").click(); pg.wait_for_timeout(3000)
    check("убранный актив исчез из списка, выбран актив по умолчанию", "SOL/USDT:USDT" not in sel_asset.locator("option").all_inner_texts() and sel_asset.input_value() == "BTC/USDT:USDT")
    sel_asset.select_option("BTC/USDT:USDT"); pg.wait_for_timeout(2500)

    # --- тейки частями и безубыток
    pg.wait_for_timeout(500)
    check("по умолчанию один тейк на 100%", pg.locator(".tp-row").count() == 1 and "Σ долей: 100%" in pg.locator(".tp-sum").inner_text())
    pg.locator(".tp-editor .seg button", has_text="2").click()
    check("кнопка «2» даёт два тейка по 50%", pg.locator(".tp-row").count() == 2 and pg.locator(".tp-row input").nth(1).input_value() == "50")
    pg.locator(".tp-editor .seg button", has_text="3").first.click()
    pg.locator(".tp-editor .seg button", has_text="1").first.click()
    check("с 3 тейков можно вернуться на 1 (регрессия)", pg.locator(".tp-row").count() == 1 and pg.locator(".tp-row input").nth(1).input_value() == "100")
    pg.locator(".tp-editor .seg button", has_text="2").first.click()
    tp_in = pg.locator(".tp-row input")                      # [pct1, share1, pct2, share2]
    tp_in.nth(0).fill("3"); tp_in.nth(2).fill("6")
    tp_in.nth(1).fill("70")                                  # 70 + 50 = 120% — ошибка
    run_btn = pg.locator("button.run")
    check("доли не 100% блокируют запуск и объясняют почему", run_btn.is_disabled() and "Исправьте" in run_btn.inner_text() and "120" in pg.locator(".tp-sum").inner_text(),
          pg.locator(".tp-sum").inner_text()[:60])
    pg.get_by_role("button", name="дополнить до 100%").click()
    check("«дополнить до 100%» кладёт остаток на последний тейк", tp_in.nth(3).input_value() == "30" and not run_btn.is_disabled())
    be = pg.locator(".tp-editor label.field.check input")
    be.check()
    run_btn.click(); pg.wait_for_timeout(3500)
    n_rows = rows.count()
    cells = [rows.nth(i).locator("td").nth(7).inner_text() for i in range(n_rows)]
    check("в таблице сделок есть колонка тейков вида n/2", n_rows > 0 and all(c in ("0/2", "1/2", "2/2") or c == "–" for c in cells) and any(c in ("1/2", "2/2") for c in cells),
          f"{n_rows} сделок, {sorted(set(cells))}")
    check("в статистике появилась карточка «Дошли до тейков»", pg.locator(".card-label", has_text="Дошли до тейков").count() == 1)
    hit = next(i for i, c in enumerate(cells) if c in ("1/2", "2/2"))
    rows.nth(hit).scroll_into_view_if_needed(); rows.nth(hit).click(); pg.wait_for_timeout(2500)
    info = pg.locator(".sel-info").inner_text()
    check("сводка сделки показывает исполнения частями с метриками DG/DL/DS на момент каждого", "TP1 70%" in info and info.count("DG ") >= 2 and info.count("DS ") >= 2 and "TP1 70% @" in info, info[-160:])
    bes = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'},
        body: JSON.stringify({tps:[{pct:3,share:70},{pct:6,share:30}], be_after_tp1:true})}).then(r => r.json())""")
    reasons = {t["reason"] for t in bes["trades"]}
    check("API: сделки с безубытком и частичными тейками", {"TP", "SL"} & reasons and all(abs(sum(f["share"] for f in t["fills"]) - 1) < 1e-9 for t in bes["trades"]), str(sorted(reasons)))
    pg.wait_for_timeout(1200)
    pg.reload(); pg.wait_for_selector("table.trades tbody tr", timeout=30000); pg.wait_for_timeout(1500)
    check("после перезагрузки сохранились тейки частями и БУ", pg.locator(".tp-row").count() == 2 and pg.locator(".tp-row input").nth(1).input_value() == "70"
          and pg.locator(".tp-editor label.field.check input").is_checked())
    check("у блока тейков есть подсказки «?»", pg.locator(".tp-editor .help").count() >= 4)

    # --- топ-коины
    check("пока торговля топ-коинами выключена, вкладок нет", pg.locator(".tabs-row").count() == 0)
    pg.get_by_role("button", name="Топ-коины", exact=False).first.click() if "▸" in pg.locator("aside.params .group-head", has_text="Топ-коины").inner_text() else None
    field(pg, "торговать топ-коином").locator("input").check()
    check("у параметров топ-коинов есть «?»", all(field(pg, l).locator(".help").count() >= 1 for l in ("Размер", "Держать / качать", "Значение в отчёте")))
    check("у топ-коинов свой редактор тейков (второй на панели)", pg.locator(".tp-editor").count() == 2)
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    tabs = pg.locator(".tabs-row .seg button")
    check("после включения появились вкладки, у «Топ-коины» отмечена докачка", tabs.count() == 2 and "Топ-коины · 0" in tabs.nth(1).inner_text() and "⏬" in tabs.nth(1).inner_text(), tabs.all_inner_texts().__str__())
    tabs.nth(1).click(); pg.wait_for_timeout(800)
    check("на вкладке видно, что свечей нет, и кнопка «Скачать свечи»", pg.locator(".top-pending").count() == 1 and pg.locator(".top-pending button", has_text="Скачать свечи").count() == 1, pg.locator(".top-pending").inner_text()[:120])
    reasons = pg.locator(".reasons").inner_text()
    check("пропущенные сигналы объяснены причинами", "нет бессрочного USDT-контракта" in reasons and "нужно скачать свечи" in reasons, reasons[:200])
    n_pending = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({top_enabled: true})}).then(r => r.json())""")["top"]["pending"]
    check("API: ждут только TIA и ARB, не WBETH", sorted({i["symbol"] for i in n_pending["items"]}) == ["ARB/USDT:USDT", "TIA/USDT:USDT"], str(n_pending["symbols"]))
    pg.locator(".top-pending button", has_text="Скачать свечи").click(); pg.wait_for_selector(".toast:has-text('скачиваются в фоне')", timeout=8000); pg.wait_for_timeout(300)
    check("после нажатия: сообщение о фоновой загрузке и ход в шапке", any("скачиваются в фоне" in x for x in pg.locator(".toast").all_inner_texts()) and pg.locator(".chip-job", has_text="Топ-коины").count() == 1,
          f"{pg.locator('.toast').all_inner_texts()} chips={pg.locator('.chip-job').all_inner_texts()}")
    pg.wait_for_selector(".toast-success", timeout=40000)
    check("по завершении уведомление, свечи монет в кеше", "Свечи топ-коинов загружены" in pg.locator(".toast-success").first.inner_text())
    pg.wait_for_timeout(3500)
    check("сделки пересчитались автоматически, докачка не нужна", pg.locator(".top-pending").count() == 0 and rows.count() > 0 and "Топ-коины · 0" not in tabs.nth(1).inner_text(), tabs.nth(1).inner_text())
    heads = pg.locator("table.trades thead th").all_inner_texts()
    check("таблица топ-коинов: колонки монета и значение в отчёте", "монета" in heads and "в отчёте" in heads, str(heads))
    coins = {c.strip(" ⏳") for c in pg.locator("table.trades tbody tr td:nth-child(12)").all_inner_texts()}
    check("в таблице только торгуемые монеты", coins <= {"TIA", "ARB"} and coins, str(coins))
    top_tr = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({top_enabled: true})}).then(r => r.json())""")["top"]["trades"]
    check("у сделок по монетам размер 100$ и свои цены", all(t["symbol"] in ("TIA/USDT:USDT", "ARB/USDT:USDT") and t["entry"] < 200 for t in top_tr) and len(top_tr) == rows.count(), f"{len(top_tr)} сделок")
    n_req = len(reqs)
    rows.nth(0).click(); pg.wait_for_timeout(2500)
    first = top_tr[0]
    sym_q = first["symbol"].replace("/", "%2F").replace(":", "%3A")
    check("клик по сделке открывает график монеты", any("symbol=" + sym_q in r for r in reqs[n_req:]) and first["symbol"].split("/")[0] in pg.locator(".sel-info").inner_text(), pg.locator(".sel-info").inner_text()[:80])
    ea2, eb2 = first["entry_ts"] // 1000, first["exit_ts"] // 1000
    v = vr()
    check("график монеты приближен к сделке", bool(v) and v[0] <= ea2 and v[1] >= eb2 and v[1] - v[0] < 10 * 86400, f"{fmt(v[0])} .. {fmt(v[1])}")
    other = next(c for c in ("TIA/USDT:USDT", "ARB/USDT:USDT") if c != first["symbol"])
    pg.locator("table.mini.clickable tbody tr", has_text=other.split("/")[0]).click(); pg.wait_for_timeout(2500)
    check("клик по монете в разрезе открывает её первую сделку", other.split("/")[0] in pg.locator(".sel-info").inner_text(), pg.locator(".sel-info").inner_text()[:60])
    check("в сводке видно монету из отчёта и значение", "в отчёте" in pg.locator(".sel-info").inner_text())
    tabs.nth(0).click(); pg.wait_for_timeout(2500)
    heads = pg.locator("table.trades thead th").all_inner_texts()
    check("возврат на основную вкладку: снова Local/Scalp и график основного актива", "Local" in heads and pg.locator(".top-panel, [data-testid=top-panel]").count() == 0 and any("symbol=BTC" in r for r in reqs[n_req:]))
    # параметры топ-коинов сохраняются и меняют результат
    top_size = pg.locator("aside.params label.field", has_text="Размер").first.locator("input")
    top_size.fill("250")
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    tabs.nth(1).click(); pg.wait_for_timeout(800)
    check("размер 250$ отражён в карточке итога", "по 250 $" in pg.locator("[data-testid=top-panel] .card-sub").first.inner_text())
    pg.wait_for_timeout(900)
    pg.reload(); pg.wait_for_selector("table.trades tbody tr", timeout=30000); pg.wait_for_timeout(1500)
    check("после перезагрузки настройки топ-коинов сохранены, вкладки на месте",
          pg.locator(".tabs-row .seg button").count() == 2 and pg.locator("aside.params label.field", has_text="Размер").first.locator("input").input_value() == "250")

    # --- фильтр тренда
    check("фильтр тренда выключен по умолчанию, чипа нет", pg.locator(".chip", has_text="тренд:").count() == 0)
    pg.get_by_role("button", name="Фильтр: тренд (EMA)").click() if "▸" in pg.get_by_role("button", name="Фильтр: тренд (EMA)").inner_text() else None
    field(pg, "торговать только по тренду").locator("input").check()
    emas = pg.locator(".ema-row input")
    check("предложены EMA 20/50/100/200, по умолчанию 50 и 200", pg.locator(".ema-row label").all_inner_texts() == ["20", "50", "100", "200"] and [emas.nth(i).is_checked() for i in range(4)] == [False, True, False, True])
    emas.nth(3).uncheck(); emas.nth(0).check()
    n_before = rows.count()
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    chip = pg.locator(".chip", has_text="тренд:")
    check("после запуска виден чип с числом отсечённых сигналов", chip.count() == 1 and "отсечено" in chip.inner_text(), chip.all_inner_texts().__str__())
    api_t = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({trend_enabled: true, trend_emas: [20, 50], trend_tf: '1h'})}).then(r => r.json())""")
    base_t = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({trend_enabled: false})}).then(r => r.json())""")
    check("API: с трендом сделок не больше, отсечённые сигналы помечены", len(api_t["trades"]) <= len(base_t["trades"]) and api_t["trend"]["blocked"] > 0 and not any(s["taken"] and s["trend_blocked"] for s in api_t["signals"]), str(api_t["trend"]))
    emas.nth(0).uncheck(); emas.nth(1).uncheck()
    check("без выбранных EMA запуск заблокирован", pg.get_by_role("button", name="Исправьте тейки").is_disabled() or pg.locator("button.run").is_disabled())
    emas.nth(1).check(); pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(2500)

    # --- депозит, доля на сделку, плечо, Шарп
    check("в статистике есть коэффициент Шарпа", pg.locator(".card-label", has_text="Коэф. Шарпа").count() == 1 and pg.locator(".card-label", has_text="Коэф. Шарпа").locator("xpath=following-sibling::div[1]").inner_text() != "")
    check("у депозита, доли и плеча есть «?»", all(field(pg, l).first.locator(".help").count() == 1 for l in ("Депозит", "Доля депозита на сделку", "Плечо")))
    base_t = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({deposit: 10000, trade_pct: 10, leverage: 1})}).then(r => r.json())""")
    lev_t = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({deposit: 10000, trade_pct: 10, leverage: 3})}).then(r => r.json())""")
    check("API: плечо ×3 утраивает объём и результат первой сделки", abs(lev_t["trades"][0]["notional"] / base_t["trades"][0]["notional"] - 3) < 1e-9 and abs(lev_t["trades"][0]["pnl_usd"] / base_t["trades"][0]["pnl_usd"] - 3) < 1e-9)
    num(pg, "Плечо").fill("4"); num(pg, "Депозит").fill("5000"); num(pg, "Доля депозита на сделку").fill("20")
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    pg.locator("table.trades tbody tr").first.click(); pg.wait_for_timeout(1000)
    check("объём сделки в сводке = депозит × доля × плечо", "объём 4 000 $" in pg.locator(".sel-info").inner_text().replace("\xa0", " "), pg.locator(".sel-info").inner_text()[-120:])
    check("в карточке итога указан депозит", "5 000 $" in pg.locator(".card-sub").first.inner_text().replace("\xa0", " "), pg.locator(".card-sub").first.inner_text())
    pg.wait_for_timeout(900); pg.reload(); pg.wait_for_selector("table.trades tbody tr", timeout=30000); pg.wait_for_timeout(1200)
    check("депозит, доля и плечо сохраняются", [num(pg, l).input_value() for l in ("Депозит", "Доля депозита на сделку", "Плечо")] == ["5000", "20", "4"])
    num(pg, "Плечо").fill("1"); num(pg, "Депозит").fill("10000"); num(pg, "Доля депозита на сделку").fill("10")
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(2500)

    # --- экспорт и импорт полного конфига
    import json as _json
    with pg.expect_download() as dl:
        pg.get_by_role("button", name="Скачать конфиг").click()
    path = dl.value.path()
    cfg = _json.loads(Path(path).read_text(encoding="utf-8"))
    check("скачан JSON с форматом и полными параметрами", cfg["format"] == "maverick-config" and cfg["params"]["symbol"] == "BTC/USDT:USDT"
          and {"tps", "top_tps", "deposit", "leverage", "compound", "trend_emas", "cool_metric", "liq_frac", "date_from"} <= set(cfg["params"]) and dl.value.suggested_filename.endswith(".json"), dl.value.suggested_filename)
    check("в файле текущие значения (депозит 10000, плечо 1)", cfg["params"]["deposit"] == 10000 and cfg["params"]["leverage"] == 1)
    cfg["params"].update(sl_pct=9, leverage=3, trend_enabled=True, trend_emas=[20, 100], top_days=2, tps=[{"pct": 2, "share": 30}, {"pct": 4, "share": 70}], be_after_tp1=True, bogus_field=1)
    cfg["params"]["symbol"] = "NOPE/USDT:USDT"
    pg.get_by_role("button", name="Загрузить…").click()
    pg.get_by_label("Конфиг для загрузки").fill("{не json")
    pg.get_by_role("button", name="Применить и запустить").click()
    check("кривой JSON: понятная ошибка, ничего не изменилось", "Не удалось разобрать JSON" in pg.locator(".cfg-import .bad-text").inner_text() and num(pg, "Плечо").input_value() == "1")
    pg.get_by_label("Конфиг для загрузки").fill(_json.dumps(cfg))
    pg.get_by_role("button", name="Применить и запустить").click(); pg.wait_for_timeout(3500)
    toast_txt = " ".join(pg.locator(".toast").all_inner_texts())
    check("конфиг применён: поля обновились", num(pg, "Плечо").input_value() == "3" and num(pg, "Стоп-лосс").first.input_value() == "9"
          and pg.locator(".tp-row").count() >= 2 and [pg.locator(".ema-row input").nth(i).is_checked() for i in range(4)] == [True, False, True, False])
    check("уведомление: актив не найден, неизвестное поле пропущено", "NOPE/USDT:USDT" in toast_txt and "bogus_field" in toast_txt, toast_txt[:200])
    check("актив остался текущим, бэктест пересчитан", field(pg, "Актив").locator("select").input_value() == "BTC/USDT:USDT" and pg.locator("table.trades tbody tr").count() > 0)
    # вернуть исходные значения этого теста
    cfg["params"].update(sl_pct=2, leverage=1, trend_enabled=False, top_days=3, be_after_tp1=False, tps=[{"pct": 6, "share": 100}], symbol="BTC/USDT:USDT")
    del cfg["params"]["bogus_field"]
    pg.get_by_role("button", name="Загрузить…").click()
    pg.get_by_label("Конфиг для загрузки").fill(_json.dumps(cfg))
    pg.get_by_role("button", name="Применить и запустить").click(); pg.wait_for_timeout(3000)
    check("конфиг без ошибок возвращает значения", num(pg, "Плечо").input_value() == "1" and num(pg, "Стоп-лосс").first.input_value() == "2")

    # --- текущие метрики и автоподгрузка истории
    bar = pg.locator("[data-testid=metrics-bar]")
    check("видны текущие DG/DL/DS обоих ботов", bar.locator(".mb-src").count() == 2 and all(t in bar.inner_text() for t in ("DG", "DL", "DS", "Coinbase Maverick", "Binance Maverick")), bar.inner_text()[:160])
    api_l = pg.evaluate("() => fetch('/api/latest').then(r => r.json())")["sources"]
    bn = next(x for x in api_l if x["id"] == "binance")["report"]
    check("значения в панели совпадают с последним отчётом", f"{bn['dl']:.2f}" in bar.locator("[data-source=binance]").inner_text() and f"{bn['ds']:.2f}" in bar.locator("[data-source=binance]").inner_text())
    check("статус автообновления: при запуске уже обновлено, указан период", "история обновлена" in bar.inner_text() and "каждые 32 мин" in bar.inner_text(), bar.inner_text()[-120:])
    check("у блока есть «?»", bar.locator(".help").count() == 2)
    pg.locator(".toast-close").all()
    bar.get_by_role("button", name="Обновить сейчас").click()
    pg.wait_for_function("() => document.querySelector('[data-testid=metrics-bar] [data-source=binance]').innerText.includes('1.90')", timeout=15000)
    check("после «Обновить сейчас» подтянут новый отчёт: DL 1.90, DS 1.30, топ-коин", all(t in bar.locator("[data-source=binance]").inner_text() for t in ("1.90", "1.30", "TIA-USD")), bar.locator("[data-source=binance]").inner_text())
    pg.wait_for_selector(".toast-info:has-text('Подгружены новые сообщения'), .toast:has-text('Подгружены новые сообщения')", timeout=15000)
    check("уведомление о новых сообщениях с кнопкой «Пересчитать»", pg.get_by_role("button", name="Пересчитать").count() >= 1)
    pg.get_by_role("button", name="Пересчитать").first.click(); pg.wait_for_timeout(3000)
    check("после «Пересчитать» бэктест на месте", pg.locator("table.trades tbody tr").count() > 0)

    # --- индикация расчёта: запрос удерживаем, чтобы увидеть состояние «идёт расчёт»
    held = []
    pg.route("**/api/backtest", lambda r: held.append(r))
    pg.get_by_role("button", name="Запустить").click()
    pg.wait_for_timeout(1700)
    btn_txt = pg.locator("button.run").inner_text()
    check("во время расчёта: полоса сверху, плашка и спиннер на кнопке", pg.locator(".progress-bar.on").count() == 1 and pg.locator(".calc-badge").is_visible()
          and "Считаю" in btn_txt and pg.locator("button.run").is_disabled() and pg.locator(".app").get_attribute("aria-busy") == "true", btn_txt)
    check("видно, сколько секунд идёт расчёт", " с" in pg.locator(".calc-badge").inner_text(), pg.locator(".calc-badge").inner_text())
    check("результат приглушён, но на месте", pg.locator("table.trades tbody tr").count() > 0 and pg.evaluate("getComputedStyle(document.querySelector('main .center')).opacity") != "1")
    held[0].continue_()
    pg.wait_for_function("() => !document.querySelector('.calc-badge')", timeout=15000)
    pg.wait_for_timeout(500)
    check("после расчёта анимация исчезла, кнопка снова активна", pg.locator(".progress-bar.on").count() == 0 and not pg.locator("button.run").is_disabled()
          and pg.locator(".app").get_attribute("aria-busy") == "false" and pg.locator("button.run").inner_text() == "Запустить")
    pg.unroute("**/api/backtest")

    # --- пирамидинг, паузы, «стоп не ставить, пока метрики сильны»
    sec = pg.get_by_role("button", name="Пирамидинг и паузы")
    if "▸" in sec.inner_text():
        sec.click()
    check("секция «Пирамидинг и паузы» есть, пауза между сигналами с «?»", field(pg, "Пауза между сигналами").locator(".help").count() == 1)
    check("по умолчанию пирамидинг выключен, доборов нет", not field(pg, "пирамидинг (доборы в позицию)").locator("input").is_checked() and pg.locator(".tag-add").count() == 0
          and pg.locator(".card-label", has_text="Позиций / доборов").count() == 0)
    field(pg, "пирамидинг (доборы в позицию)").locator("input").check()
    check("у новых параметров есть «?»", all(field(pg, l).locator(".help").count() == 1 for l in ("Максимум доборов", "Размер добора", "Пауза между доборами"))
          and field(pg, "не ставить SL").locator(".help").count() == 1)
    num(pg, "Максимум доборов").fill("2"); num(pg, "Размер добора").fill("5")
    field(pg, "не ставить SL").locator("input").check()
    check("при «SL после остывания» показано предупреждение о неограниченном убытке", "не ограничен" in pg.locator("aside.params").inner_text())
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3500)
    check("в статистике появились позиции/доборы и причины пропусков", pg.locator(".card-label", has_text="Позиций / доборов").count() == 1
          and pg.locator("[data-testid=skipped-reasons] li").count() >= 1, pg.locator("[data-testid=skipped-reasons]").inner_text()[:120] if pg.locator("[data-testid=skipped-reasons]").count() else "нет блока")
    check("в таблице сделок доборы помечены", pg.locator(".tag-add").count() > 0)
    n_legs = pg.locator("table.trades tbody tr").count()
    check("«пирамидинг усредняет позу» выключена по умолчанию и имеет «?»", not field(pg, "пирамидинг усредняет позу").locator("input").is_checked()
          and field(pg, "пирамидинг усредняет позу").locator(".help").count() == 1)
    field(pg, "пирамидинг усредняет позу").locator("input").check()
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3500)
    n_pos = pg.locator("table.trades tbody tr").count()
    tags = pg.locator(".tag-add").all_inner_texts()
    check("с усреднением доборы живут внутри одной сделки: строк меньше, в таблице метки ×N", n_pos < n_legs and tags and all("×" in t for t in tags), f"ног {n_legs} -> позиций {n_pos}, метки {tags[:3]}")
    pg.locator("table.trades tbody tr", has=pg.locator(".tag-add")).first.click(); pg.wait_for_timeout(1500)
    check("в сводке усреднённой сделки видна средняя цена входа", "средняя из" in pg.locator(".sel-info").inner_text(), pg.locator(".sel-info").inner_text()[:140])
    api_avg = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({pyramid_enabled: true, pyramid_avg: true, pyramid_max: 2, pyramid_pct: 5, trade_pct: 10, compound: false, deposit: 10000, leverage: 1})}).then(r => r.json())""")
    ta = next(t for t in api_avg["trades"] if t["adds_n"] > 0)
    check("API: у усреднённой сделки одна запись, средняя между входами, размер = вход + доборы", ta["avg"] and len(ta["adds"]) == ta["adds_n"] + 1
          and min(a["price"] for a in ta["adds"]) <= ta["entry"] <= max(a["price"] for a in ta["adds"]) and abs(ta["notional"] - 10000 * (10 + 5 * ta["adds_n"]) / 100) < 1e-6)
    field(pg, "пирамидинг усредняет позу").locator("input").uncheck()
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    check("выключили усреднение — снова отдельные ноги", pg.locator("table.trades tbody tr").count() == n_legs)
    check("поле «Движение от последнего входа» есть, с «?», по умолчанию 0; выбор направления появляется при значении > 0", field(pg, "Движение от последнего входа").locator(".help").count() == 1
          and num(pg, "Движение от последнего входа").input_value() == "0" and field(pg, "Куда должна уйти цена").count() == 0)
    num(pg, "Движение от последнего входа").fill("50")
    check("при ненулевом движении видно направление с «?»", field(pg, "Куда должна уйти цена").locator(".help").count() == 1)
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3500)
    check("недостижимое движение: доборов нет, причина пропуска названа", pg.locator(".tag-add").count() == 0 and "мало движения от последнего входа" in pg.locator("[data-testid=skipped-reasons]").inner_text())
    num(pg, "Движение от последнего входа").fill("0")
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3500)
    check("движение 0 — доборы снова есть", pg.locator("table.trades tbody tr").count() == n_legs)
    api_p = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({pyramid_enabled: true, pyramid_max: 2, pyramid_pct: 5, trade_pct: 10, compound: false, deposit: 10000, leverage: 1})}).then(r => r.json())""")
    legs = api_p["trades"]
    first_leg = next(t for t in legs if t["leg"] == 1)
    one_add = next(t for t in legs if t["leg"] == 2 and t["group"] == first_leg["group"]) if any(t["leg"] == 2 and t["group"] == first_leg["group"] for t in legs) else next(t for t in legs if t["leg"] == 2)
    check("API: добор берёт 5% депозита, вход — 10%; число доборов ограничено", abs(first_leg["notional"] - 1000) < 1e-6 and abs(one_add["notional"] - 500) < 1e-6
          and max(t["leg"] for t in legs) <= 3 and api_p["stats"]["total"]["adds"] > 0)
    gap = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({signal_gap_h: 24, pyramid_gap_h: 24, pyramid_enabled: true})}).then(r => r.json())""")
    check("API: паузы дают причины пропуска", "пауза между сигналами" in gap["skipped_reasons"] or "пауза между доборами" in gap["skipped_reasons"], str(gap["skipped_reasons"]))
    hold_api = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({sl_hold_strong: true})}).then(r => r.json())""")
    sl_fills = [f for t in hold_api["trades"] for f in t["fills"] if f["label"] == "SL"]
    check("API: при «не ставить SL…» исполнения по SL происходят только вне зоны входа (флаг strong = false)", sl_fills and all(f["metrics"]["strong"] is False for f in sl_fills), f"SL {len(sl_fills)}, strong: {sum(1 for f in sl_fills if f['metrics']['strong'])}")
    # конфиг: новые поля уходят в файл; старый файл без них ВЫКЛЮЧАЕТ возможности
    with pg.expect_download() as dl2:
        pg.get_by_role("button", name="Скачать конфиг").click()
    cfg2 = _json.loads(Path(dl2.value.path()).read_text(encoding="utf-8"))
    check("в скачанном конфиге есть новые поля", cfg2["params"]["pyramid_enabled"] is True and cfg2["params"]["pyramid_max"] == 2 and cfg2["params"]["sl_hold_strong"] is True)
    for k in ("pyramid_enabled", "pyramid_avg", "pyramid_max", "pyramid_pct", "pyramid_gap_h", "signal_gap_h", "sl_hold_strong"):
        cfg2["params"].pop(k)
    pg.get_by_role("button", name="Загрузить…").click()
    pg.get_by_label("Конфиг для загрузки").fill(_json.dumps(cfg2))
    pg.get_by_role("button", name="Применить и запустить").click(); pg.wait_for_timeout(3500)
    sec = pg.get_by_role("button", name="Пирамидинг и паузы")
    if "▸" in sec.inner_text():
        sec.click()
    check("старый конфиг без новых полей: пирамидинг и SL-режим выключены, пауза 0", not field(pg, "пирамидинг (доборы в позицию)").locator("input").is_checked()
          and not field(pg, "не ставить SL").locator("input").is_checked() and num(pg, "Пауза между сигналами").input_value() == "0" and pg.locator(".tag-add").count() == 0)

    # --- Deep Global: пороги входа (по умолчанию выключены), «?», влияние на результат, конфиг и обратная совместимость
    check("Deep Global: поля порогов лонга/шорта есть, пустые по умолчанию, с «?»", all(field(pg, l).locator(".help").count() == 1 and field(pg, l).locator("input").input_value() == "" for l in ("Global ≥", "Global ≤")))
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    n_dg0 = pg.locator("table.trades tbody tr").count()
    num(pg, "Global ≥").fill("5"); num(pg, "Global ≤").fill("-5")                      # в демо-отчётах DG = 1.0: ни лонг, ни шорт не пройдут
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    check("жёсткие пороги Global отсекают все входы", pg.locator("table.trades tbody tr").count() == 0 and n_dg0 > 0, f"было {n_dg0}")
    num(pg, "Global ≥").fill("0.5"); num(pg, "Global ≤").fill("1.5")                    # DG = 1.0 проходит оба условия — результат как без порогов
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    check("мягкие пороги Global не меняют результат", pg.locator("table.trades tbody tr").count() == n_dg0)
    with pg.expect_download() as dl3:
        pg.get_by_role("button", name="Скачать конфиг").click()
    cfg3 = _json.loads(Path(dl3.value.path()).read_text(encoding="utf-8"))
    check("в скачанном конфиге есть пороги Global", cfg3["params"]["global_min"] == 0.5 and cfg3["params"]["short_global_max"] == 1.5)
    cfg3["params"].pop("global_min"); cfg3["params"].pop("short_global_max")
    pg.get_by_role("button", name="Загрузить…").click()
    pg.get_by_label("Конфиг для загрузки").fill(_json.dumps(cfg3))
    pg.get_by_role("button", name="Применить и запустить").click(); pg.wait_for_timeout(3500)
    check("старый конфиг без Global: пороги выключены, результат прежний", num(pg, "Global ≥").input_value() == "" and num(pg, "Global ≤").input_value() == "" and pg.locator("table.trades tbody tr").count() == n_dg0)
    api_dg = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({only: 'global'})}).then(r => r.status)""")
    check("API: режим «только Global» без порогов — ошибка валидации", api_dg == 422, str(api_dg))

    # --- экспорт сделок в CSV
    n_rows = pg.locator("table.trades tbody tr").count()
    check("у экспорта CSV есть «?»", pg.locator(".table-bar .help").count() == 1)
    with pg.expect_download() as dl_csv:
        pg.get_by_test_id("export-csv").click()
    csv_dl = dl_csv.value
    raw = Path(csv_dl.path()).read_bytes().decode("utf-8")
    lines = [l for l in raw.split("\r\n") if l]
    check("CSV: UTF-8 с BOM, шапка и строка на каждую сделку таблицы", raw.startswith("\ufeff") and lines[0].lstrip("\ufeff").startswith("n,side,leg,group,signal_utc") and len(lines) - 1 == n_rows and csv_dl.suggested_filename.endswith(".csv"), f"{len(lines) - 1} строк, таблица {n_rows}")
    cols = next(csv.reader([lines[1]]))
    check("CSV: сторона, цена, причина и метрики заполнены", cols[1] in ("long", "short") and float(cols[7]) > 0 and cols[9] in ("TP", "SL", "BE", "REV", "OPEN") and cols[16] != "" and cols[17] != "" and "@" in cols[19], lines[1][:140])

    # --- Монте-Карло
    while pg.locator(".toast-close").count():           # уведомления перекрывают правую колонку — закрываем, как это сделал бы пользователь
        pg.locator(".toast-close").first.click(); pg.wait_for_timeout(150)
    mcp = pg.get_by_test_id("mc-panel")
    check("Монте-Карло: у раздела и параметров есть «?»", mcp.locator(".help").count() == 3 and mcp.get_by_test_id("mc-table").count() == 0)
    mcp.get_by_role("button", name="Рассчитать").click(); mcp.get_by_test_id("mc-table").wait_for(timeout=30000)
    check("Монте-Карло: веер, таблица итога/просадки и вероятности", mcp.get_by_test_id("mc-fan").count() == 1 and mcp.locator("tbody tr").count() == 3 and "Вероятность убытка" in mcp.inner_text() and "Это не прогноз" in mcp.inner_text(), mcp.inner_text()[:200])
    mcp.locator("select").nth(1).select_option("bootstrap")
    mcp.get_by_role("button", name="Рассчитать").click(); pg.wait_for_timeout(2500)
    check("Монте-Карло: режим «выборка с возвращением»", "выборка с возвращением" in mcp.get_by_test_id("mc-panel").inner_text() if False else "выборка с возвращением" in mcp.inner_text().split("Вероятность убытка")[-1])
    pg.get_by_role("button", name="Запустить").first.click(); pg.wait_for_timeout(3000)
    check("после пересчёта бэктеста результат Монте-Карло сброшен", pg.get_by_test_id("mc-panel").get_by_test_id("mc-table").count() == 0)

    # --- сохранённые прогоны
    rp = pg.get_by_test_id("runs-panel")
    check("прогоны: пусто, у раздела есть «?»", "Пока ничего не сохранено" in rp.inner_text() and rp.locator(".help").count() == 1 and rp.get_by_role("button", name="Сохранить").is_disabled())
    sl_saved = num(pg, "Стоп-лосс").first.input_value()
    rp.get_by_label("Название прогона").fill("Тест Ж"); rp.get_by_role("button", name="Сохранить").click()
    rp.locator("li[data-run='Тест Ж']").wait_for(timeout=10000)
    check("прогон сохранён: имя, сделки, Шарп, просадка", all(w in rp.locator("li").first.inner_text() for w in ("Тест Ж", "сделок", "Шарп", "просадка")), rp.locator("li").first.inner_text())
    num(pg, "Стоп-лосс").first.fill("9")
    pg.get_by_role("button", name="Запустить").first.click(); pg.wait_for_timeout(3000)
    rp.get_by_role("button", name="Загрузить").click(); pg.wait_for_timeout(3500)
    check("загрузка прогона вернула параметры и пересчитала результат", num(pg, "Стоп-лосс").first.input_value() == sl_saved and pg.locator("table.trades tbody tr").count() == n_rows, f"SL {sl_saved}")
    rp = pg.get_by_test_id("runs-panel")
    rp.get_by_label("Название прогона").fill("тест ж"); rp.get_by_role("button", name="Сохранить").click(); pg.wait_for_timeout(1500)
    check("то же имя без учёта регистра перезаписывает прогон", pg.get_by_test_id("runs-panel").locator("li").count() == 1 and pg.get_by_test_id("runs-panel").locator("li b").inner_text() == "тест ж")
    pg.once("dialog", lambda d: d.accept())
    pg.get_by_test_id("runs-panel").get_by_role("button", name="Удалить тест ж").click(); pg.wait_for_timeout(1500)
    check("прогон удалён", "Пока ничего не сохранено" in pg.get_by_test_id("runs-panel").inner_text())
    rp.get_by_label("Название прогона").fill("Для второго пользователя"); rp.get_by_role("button", name="Сохранить").click(); pg.wait_for_timeout(1500)

    # --- плечо доборов, моделирование ликвидации и реальная просадка
    check("карточка «Реальная просадка» есть, с «?», и не меньше обычной", pg.locator(".card-label", has_text="Реальная просадка").locator(".help").count() == 1)
    dd_closed = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({leverage: 5, trade_pct: 25})}).then(r => r.json()).then(j => [j.stats.total.max_dd_usd, j.stats.total.max_dd_real_usd, j.stats.total.max_dd_real_pct])""")
    check("API: реальная просадка (по рынку) не меньше просадки по закрытым и в пределах 0–100%", dd_closed[1] >= dd_closed[0] - 1e-6 and 0 < dd_closed[2] <= 100, str(dd_closed))
    field(pg, "пирамидинг (доборы в позицию)").locator("input").check()
    check("поля «Моделировать ликвидацию» и «Плечо доборов» есть, с «?», по умолчанию выключены/пусты", field(pg, "моделировать ликвидацию").locator(".help").count() == 1
          and not field(pg, "моделировать ликвидацию").locator("input").is_checked() and field(pg, "Плечо доборов").locator(".help").count() == 1 and num(pg, "Плечо доборов").input_value() == "")
    check("поддерживающая маржа появляется только при включённой ликвидации", field(pg, "Поддерживающая маржа").count() == 0)
    num(pg, "Плечо").first.fill("30"); num(pg, "Стоп-лосс").first.fill("20")
    field(pg, "моделировать ликвидацию").locator("input").check()
    check("у поддерживающей маржи есть «?»", field(pg, "Поддерживающая маржа").locator(".help").count() == 1)
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3500)
    liq_n = pg.locator("table.trades tbody tr .tag-LIQ").count()
    check("плечо 30× со стопом 20% и ликвидацией: в таблице сделки с причиной LIQ", liq_n > 0, f"LIQ: {liq_n}")
    check("в карточке сделок видно число ликвидаций", "ликвид." in pg.locator(".card", has_text="Сделок").first.inner_text())
    liq_api = pg.evaluate("""() => fetch('/api/backtest', {method:'POST', headers:{'content-type':'application/json','x-maverick':'1'}, body: JSON.stringify({leverage: 30, sl_pct: 20, liquidation_sim: true})}).then(r => r.json()).then(j => j.trades.filter(t => t.reason === 'LIQ').map(t => [t.pnl_pct, t.pnl_usd, t.margin]))""")
    check("API: ликвидация съедает всю маржу ноги (−100%/плечо от объёма) плюс комиссия 2 × 0.05% объёма", liq_api and all(abs(p + 100 / 30 + 0.1) < 1e-6 and abs(u + m * 1.03) < 1e-6 * m for p, u, m in liq_api), str(liq_api[:2]))
    field(pg, "моделировать ликвидацию").locator("input").uncheck()
    num(pg, "Плечо").first.fill("1"); num(pg, "Стоп-лосс").first.fill("2")
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    check("выключили ликвидацию — LIQ пропали", pg.locator("table.trades tbody tr .tag-LIQ").count() == 0)
    # старый конфиг без этих полей: ликвидация выключена, плечо доборов пусто
    with pg.expect_download() as dl4:
        pg.get_by_role("button", name="Скачать конфиг").click()
    cfg4 = _json.loads(Path(dl4.value.path()).read_text(encoding="utf-8"))
    check("в скачанном конфиге есть новые поля", cfg4["params"]["liquidation_sim"] is False and cfg4["params"]["pyramid_leverage"] is None and "maint_margin_pct" in cfg4["params"])
    cfg4["params"]["liquidation_sim"] = True
    for k in ("liquidation_sim", "pyramid_leverage", "maint_margin_pct"):
        cfg4["params"].pop(k)
    pg.get_by_role("button", name="Загрузить…").click()
    pg.get_by_label("Конфиг для загрузки").fill(_json.dumps(cfg4))
    pg.get_by_role("button", name="Применить и запустить").click(); pg.wait_for_timeout(3500)
    check("старый конфиг без ликвидации и плеча доборов: всё выключено", not field(pg, "моделировать ликвидацию").locator("input").is_checked() and pg.locator("table.trades tbody tr .tag-LIQ").count() == 0)
    with pg.expect_download() as dl5:
        pg.get_by_role("button", name="Скачать конфиг").click()
    cfg5 = _json.loads(Path(dl5.value.path()).read_text(encoding="utf-8"))
    check("после загрузки старого конфига в экспорте: ликвидация выключена, плечо доборов не задано", cfg5["params"]["liquidation_sim"] is False and cfg5["params"]["pyramid_leverage"] is None)

    # --- подсказки «?»
    # раскрываем свёрнутые секции и включаем фильтры, чтобы увидеть все поля
    for title in ("Фильтр: остывание метрик", "Фильтр: ликвидации закончились", "Период"):
        btn = pg.get_by_role("button", name=title)
        if "▸" in btn.inner_text():
            btn.click()
    field(pg, "Режим").first.locator("select").select_option("drop")     # режим фильтра остывания: открывает поля метрики и порога
    field(pg, "включить").locator("input").check()                       # фильтр ликвидаций: показывает его поля
    fields = pg.locator("aside.params label.field")
    n_fields = fields.count()
    no_help = [fields.nth(i).inner_text().strip().split("\n")[0] for i in range(n_fields) if fields.nth(i).locator(".help").count() == 0]
    check("у КАЖДОГО параметра в панели есть «?»", n_fields >= 20 and not no_help, f"полей {n_fields}, без подсказки: {no_help}")
    groups = pg.locator("aside.params .group-head")
    no_group_help = [groups.nth(i).inner_text().strip() for i in range(groups.count())
                     if groups.nth(i).locator(".help").count() == 0 and groups.nth(i).inner_text().strip() not in ("▾ Данные", "▾ Режим")]
    check("у групп параметров и фильтров есть «?»", not no_group_help, str(no_group_help))
    for label in ("Подтверждение", "Стоп-лосс", "Источник метрик", "Актив"):
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
    pg.locator(".chart-bar .seg").locator("xpath=following-sibling::*[contains(@class,'help')][1]").hover()
    pg.wait_for_timeout(200)
    check("подсказка у переключателя ТФ", pg.locator(".tooltip").count() == 1 and "Авто" in pg.locator(".tooltip").inner_text())

    # --- несколько пользователей: у каждого свои настройки, данные общие
    pg.mouse.move(5, 5)
    num(pg, "Стоп-лосс").first.fill("5")
    pg.get_by_role("button", name="Запустить").click(); pg.wait_for_timeout(3000)
    pg.wait_for_timeout(1500)                                        # состояние сохраняется с задержкой 600 мс
    rows_before = pg.locator("table.trades tbody tr").count()
    pg.locator("[data-testid=user-button]").click()
    check("в меню пользователя: Telegram не привязан, есть кнопка привязки", "не привязан" in pg.locator("[data-testid=user-pop]").inner_text() and pg.get_by_role("button", name="Привязать Telegram").count() == 1)
    pg.get_by_role("button", name="Привязать Telegram").click(); pg.wait_for_selector(".link-code code", timeout=5000)
    code_txt = pg.locator(".link-code code").inner_text()
    check("показан одноразовый код для команды /link", code_txt.startswith("/link ") and len(code_txt.split()[1]) == 8, code_txt)
    pg.get_by_test_id("logout").click()
    pg.wait_for_selector("form.login-card", timeout=10000)
    check("после выхода снова форма логина, API закрыт", pg.locator("table.trades").count() == 0 and pg.evaluate("fetch('/api/ui-state').then(r => r.status)") == 401)
    pg.get_by_label("Логин").fill("second"); pg.get_by_label("Пароль").fill(USER_PASS)
    pg.get_by_role("button", name="Войти").click()
    pg.wait_for_selector("table.trades tbody tr", timeout=30000); pg.wait_for_timeout(1200)
    check("второй пользователь: свои настройки (стоп по умолчанию 2), те же общие данные", num(pg, "Стоп-лосс").first.input_value() == "2" and pg.locator("table.trades tbody tr").count() > 0)
    check("второй пользователь не видит чужих сохранённых прогонов", "Пока ничего не сохранено" in pg.get_by_test_id("runs-panel").inner_text())
    pg.get_by_test_id("user-button").click(); pg.get_by_test_id("logout").click()
    pg.wait_for_selector("form.login-card", timeout=10000)
    pg.get_by_label("Логин").fill("tester"); pg.get_by_label("Пароль").fill(USER_PASS)
    pg.get_by_role("button", name="Войти").click()
    pg.wait_for_selector("table.trades tbody tr", timeout=30000); pg.wait_for_timeout(1200)
    check("первый пользователь после повторного входа: подгружены его последние настройки", num(pg, "Стоп-лосс").first.input_value() == "5" and pg.locator("table.trades tbody tr").count() == rows_before)
    pg.reload(); pg.wait_for_selector("table.trades tbody tr", timeout=30000)
    check("сессия переживает перезагрузку страницы (без повторного ввода пароля)", num(pg, "Стоп-лосс").first.input_value() == "5")


def main():
    from playwright.sync_api import sync_playwright

    tmp = Path(tempfile.mkdtemp(prefix="maverick-e2e-"))
    procs = []
    try:
        make_demo(tmp)
        sys.path.insert(0, str(ROOT / "backend"))
        from app.db import Database
        db = Database(tmp / "e2e.db")
        db.add_user("tester", USER_PASS)
        db.add_user("second", USER_PASS)
        if not (ROOT / "frontend" / "dist" / "index.html").exists():
            sys.exit("Сначала соберите фронтенд: (cd frontend && npm ci && npm run build)")
        bp, fp = free_port(), free_port()
        env = dict(os.environ, SOURCES="coinbase,binance", SOURCE_DEFAULT="binance", SOURCE_COINBASE_LABEL="Coinbase Maverick",
                   SOURCE_COINBASE_FILE=str(tmp / "history_cb.csv"), SOURCE_BINANCE_LABEL="Binance Maverick",
                   SOURCE_BINANCE_FILE=str(tmp / "history_bn.csv"), CACHE_DIR=str(tmp / "cache"), LIQ_SOURCE="", HISTORY_FILE="", SYMBOLS="BTC/USDT:USDT,ETH/USDT:USDT", SYMBOL_DEFAULT="BTC/USDT:USDT",
                   STATE_FILE=str(tmp / "ui_state.json"), ASSETS_FILE=str(tmp / "assets.json"), DB_FILE=str(tmp / "e2e.db"), AUTH_ENABLED="true", SIGNALBOT_USERNAME="demo_bot",
                   LOCAL_MIN="1.85", SCALP_MIN="1.25", CONFIRM="4", TP_PCT="6", SL_PCT="2", SIDE="both", ONLY="", COOL_DROP="",
                   COOL_DECLINES="", LIQ_HOURS="", COOLDOWN_H="0", FEE="0.0005", SLIPPAGE="0", ORDER_SIZE="0.1",
                   TOP_ENABLED="false", TOP_SIDES="long", TOP_TP_LEVELS="", TOP_TP_PCT="5", TOP_SL_PCT="3", TOP_SIZE_USD="100", TOP_FEE="0.0005",
                   HISTORY_SYNC="false", CANDLES_SYNC="false", TOP_SLIPPAGE="0", TOP_DAYS="3", TOP_MIN_VALUE="0", TOP_BE_AFTER_TP1="",
                   VITE_PROXY_TARGET=f"http://127.0.0.1:{bp}", VITE_PORT=str(fp))
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "server:app", "--app-dir", str(ROOT / "e2e"), "--port", str(bp),
                                       "--log-level", "warning"], cwd=ROOT / "backend", env=env, start_new_session=True))
        procs.append(subprocess.Popen(["npx", "vite", "preview", "--host", "127.0.0.1", "--port", str(fp), "--strictPort"],
                                      cwd=ROOT / "frontend", env=env, stdout=subprocess.DEVNULL, start_new_session=True))
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
            expected = [e for e in errors if "status of 422" in e]      # браузер логирует отказ API: две намеренно неверные попытки добавить актив и запрос «только Global» без порогов
            other = [e for e in errors if e not in expected and "status of 401" not in e]      # 401 — проверки доступа без входа и неверный пароль (намеренно)
            check("в консоли браузера нет неожиданных ошибок", not other and len(expected) == 3, f"ожидаемых 422: {len(expected)}\n" + "\n".join(other)[:3000])
            b.close()
    finally:
        for pr in procs:                      # npx запускает node дочерним процессом — гасим всю группу, иначе он остаётся висеть
            try:
                os.killpg(pr.pid, 15)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nИТОГ:", "все проверки прошли" if not fails else f"упало: {fails}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
