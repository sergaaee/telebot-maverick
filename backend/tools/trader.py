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

    async def _position(self):
        """(side, contracts) открытой позиции по символу или (None, 0)."""
        for p in await self.ex.fetch_positions([self.symbol]):
            amount = abs(float(p.get("contracts") or 0))
            if p.get("symbol") == self.symbol and amount > 0:
                return p.get("side"), amount
        return None, 0

    async def _cancel_all(self):
        # обычные и условные (SL/TP) ордера на Binance отменяются разными вызовами
        for params in ({}, {"trigger": True}):
            try:
                await self.ex.cancel_all_orders(self.symbol, params)
            except Exception as e:
                log.warning("cancel_all_orders%s: %s", params or "", e)

    async def open_position(self, side: str) -> bool:
        """Открывает лонг/шорт. Та же сторона уже открыта — пропуск; противоположная — разворот
        (закрываем по рынку, отменяем её SL/TP, открываем новую). True — если новая позиция открыта."""
        existing, amount = await self._position()
        if existing == side:
            log.info("По %s уже открыт %s — сигнал пропускаю", self.symbol, side)
            return False
        if existing:
            if self.dry_run:
                log.info("[DRY_RUN] закрыл бы %s %s по рынку (разворот в %s)", existing, amount, side)
            else:
                log.info("Разворот: закрываю %s %s по рынку", existing, amount)
                await self._cancel_all()
                await self.ex.create_order(self.symbol, "market", "sell" if existing == "long" else "buy",
                                           amount, None, {"reduceOnly": True})
        d = 1 if side == "long" else -1
        close_side = "sell" if side == "long" else "buy"
        if self.dry_run:
            log.info("[DRY_RUN] открыл бы %s %s %s, TP %s%%, SL %s%% от входа", side, self.size, self.symbol, self.tp_pct, self.sl_pct)
            return True
        await self.ex.load_markets()
        order = await self.ex.create_order(self.symbol, "market", "buy" if side == "long" else "sell", self.size)
        log.info("Ордер %s отправлен: %s", side, order.get("id"))
        entry = order.get("average") or order.get("price")
        if not entry:
            entry = (await self.ex.fetch_ticker(self.symbol))["last"]
        tp = float(self.ex.price_to_precision(self.symbol, entry * (1 + d * self.tp_pct / 100)))
        sl = float(self.ex.price_to_precision(self.symbol, entry * (1 - d * self.sl_pct / 100)))
        try:
            # SL первым — он важнее
            await self.ex.create_order(self.symbol, "market", close_side, self.size, None,
                                       {"stopLossPrice": sl, "reduceOnly": True})
            await self.ex.create_order(self.symbol, "market", close_side, self.size, None,
                                       {"takeProfitPrice": tp, "reduceOnly": True})
            log.info("Вход %s ~%s, SL %s, TP %s", side, entry, sl, tp)
        except Exception:
            # позиция без стопа недопустима — закрываем и отменяем что успели выставить
            log.exception("Не удалось выставить SL/TP — закрываю позицию")
            try:
                await self._cancel_all()
            finally:
                await self.ex.create_order(self.symbol, "market", close_side, self.size, None, {"reduceOnly": True})
            raise
        return True

    async def close(self):
        await self.ex.close()
