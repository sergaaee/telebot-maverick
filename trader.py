import logging

import ccxt.async_support as ccxt

log = logging.getLogger("trader")


class Trader:
    def __init__(self, exchange_id, api_key, secret, password, symbol, size, dry_run):
        cls = getattr(ccxt, exchange_id)
        params = {"apiKey": api_key, "secret": secret, "enableRateLimit": True}
        if password:
            params["password"] = password
        self.ex = cls(params)
        self.symbol, self.size, self.dry_run = symbol, size, dry_run

    async def has_open_long(self) -> bool:
        positions = await self.ex.fetch_positions([self.symbol])
        for p in positions:
            amount = float(p.get("contracts") or 0)
            if p.get("symbol") == self.symbol and p.get("side") == "long" and amount > 0:
                return True
        return False

    async def open_long(self) -> bool:
        """Открывает лонг, если такой позиции ещё нет. True — если ордер отправлен."""
        if await self.has_open_long():
            log.info("Лонг %s уже открыт — пропускаю", self.symbol)
            return False
        if self.dry_run:
            log.info("[DRY_RUN] открыл бы лонг %s %s", self.size, self.symbol)
            return True
        await self.ex.load_markets()
        order = await self.ex.create_market_buy_order(self.symbol, self.size)
        log.info("Ордер отправлен: %s", order.get("id"))
        return True

    async def close(self):
        await self.ex.close()
