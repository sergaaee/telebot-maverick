import logging

import ccxt.async_support as ccxt

log = logging.getLogger("trader")


class Trader:
    def __init__(self, exchange_id, api_key, secret, password, symbol, size, dry_run, tp_pct=3.0, sl_pct=1.0):
        cls = getattr(ccxt, exchange_id)
        params = {"apiKey": api_key, "secret": secret, "enableRateLimit": True}
        if password:
            params["password"] = password
        self.ex = cls(params)
        self.symbol, self.size, self.dry_run = symbol, size, dry_run
        self.tp_pct, self.sl_pct = tp_pct, sl_pct

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
            log.info("[DRY_RUN] открыл бы лонг %s %s, TP +%s%%, SL -%s%%", self.size, self.symbol, self.tp_pct, self.sl_pct)
            return True
        await self.ex.load_markets()
        order = await self.ex.create_market_buy_order(self.symbol, self.size)
        log.info("Ордер отправлен: %s", order.get("id"))
        entry = order.get("average") or order.get("price")
        if not entry:
            entry = (await self.ex.fetch_ticker(self.symbol))["last"]
        tp = float(self.ex.price_to_precision(self.symbol, entry * (1 + self.tp_pct / 100)))
        sl = float(self.ex.price_to_precision(self.symbol, entry * (1 - self.sl_pct / 100)))
        try:
            # SL первым — он важнее
            await self.ex.create_order(self.symbol, "market", "sell", self.size, None,
                                       {"stopLossPrice": sl, "reduceOnly": True})
            await self.ex.create_order(self.symbol, "market", "sell", self.size, None,
                                       {"takeProfitPrice": tp, "reduceOnly": True})
            log.info("Вход ~%s, SL %s, TP %s", entry, sl, tp)
        except Exception:
            # позиция без стопа недопустима — закрываем и отменяем что успели выставить
            log.exception("Не удалось выставить SL/TP — закрываю позицию")
            try:
                await self.ex.cancel_all_orders(self.symbol)
            finally:
                await self.ex.create_market_sell_order(self.symbol, self.size, {"reduceOnly": True})
            raise
        return True

    async def close(self):
        await self.ex.close()
