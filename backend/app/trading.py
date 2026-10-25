"""Торговля: слой исполнения действий (открыть позицию, переставить стоп, закрыть) за единым интерфейсом.

ВАЖНО: реального коннектора к бирже здесь пока нет. Данные (свечи, отчёты) идут с Binance как раньше, а для торговли предполагается Arcus, но документация его API
из среды разработки была недоступна (docs.arcus.xyz закрыт сетевой политикой), поэтому коннектор НЕ написан, чтобы не выдумывать чужой API и не рисковать деньгами.
Пока работает только DryRunAdapter: действие записывается в журнал со статусом «симуляция», никакие ордера не отправляются."""
import os

TEST_MAX_USD = 5.0                      # потолок тестовой позиции (кнопка «тестовая позиция $1»): даже если подменить запрос, больше не откроется


class NotConfigured(RuntimeError):
    pass


class ExchangeAdapter:
    name = "base"
    real = False

    def open_market(self, symbol, side, usd, leverage=1.0, stop=None, takes=None):
        raise NotImplementedError

    def set_stop(self, symbol, side, price):
        raise NotImplementedError

    def close_market(self, symbol, side, share=1.0):
        raise NotImplementedError


class DryRunAdapter(ExchangeAdapter):
    """Ничего не отправляет: возвращает то, что было бы сделано."""
    name = "dry-run"

    def open_market(self, symbol, side, usd, leverage=1.0, stop=None, takes=None):
        return {"status": "dry_run", "note": "симуляция: ордер не отправлялся", "symbol": symbol, "side": side, "usd": usd, "leverage": leverage, "stop": stop, "takes": takes}

    def set_stop(self, symbol, side, price):
        return {"status": "dry_run", "note": "симуляция: стоп не переставлялся", "symbol": symbol, "side": side, "stop": price}

    def close_market(self, symbol, side, share=1.0):
        return {"status": "dry_run", "note": "симуляция: позиция не закрывалась", "symbol": symbol, "side": side, "share": share}


class ArcusAdapter(ExchangeAdapter):
    """Заглушка: коннектор Arcus не реализован (нет доступа к документации API). Любое действие — понятная ошибка, а не молчаливая «успешная» сделка."""
    name = "arcus"
    real = True

    def _no(self, *a, **k):
        raise NotConfigured("Коннектор Arcus ещё не реализован: нужна документация API (docs.arcus.xyz), тип ключей и тестовая сеть. Пока доступна только симуляция.")

    open_market = set_stop = close_market = _no


def get_adapter(name=None):
    name = (name if name is not None else os.getenv("TRADE_EXCHANGE", "")).strip().lower()
    if name == "arcus":
        return ArcusAdapter()
    return DryRunAdapter()
