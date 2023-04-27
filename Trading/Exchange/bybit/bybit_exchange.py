#  Drakkar-Software OctoBot-Tentacles
#  Copyright (c) Drakkar-Software, All rights reserved.
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library.
import decimal
import typing

import ccxt

import octobot_commons.constants as commons_constants
import octobot_trading.enums as trading_enums
import octobot_trading.exchanges as exchanges
import octobot_trading.exchanges.connectors.ccxt.enums as ccxt_enums
import octobot_trading.exchanges.connectors.ccxt.constants as ccxt_constants
import octobot_trading.constants as constants
import octobot_trading.personal_data as trading_personal_data
import octobot_trading.errors


class Bybit(exchanges.RestExchange):
    DESCRIPTION = ""

    # Bybit default take profits are market orders
    # note: use BUY_MARKET and SELL_MARKET since in reality those are conditional market orders, which behave the same
    # way as limit order but with higher fees
    _BYBIT_BUNDLED_ORDERS = [trading_enums.TraderOrderType.STOP_LOSS, trading_enums.TraderOrderType.TAKE_PROFIT,
                             trading_enums.TraderOrderType.BUY_MARKET, trading_enums.TraderOrderType.SELL_MARKET]
    SUPPORTED_BUNDLED_ORDERS = {}
    FUTURES_SUPPORTED_BUNDLED_ORDERS = {
        trading_enums.TraderOrderType.BUY_MARKET: _BYBIT_BUNDLED_ORDERS,
        trading_enums.TraderOrderType.SELL_MARKET: _BYBIT_BUNDLED_ORDERS,
        trading_enums.TraderOrderType.BUY_LIMIT: _BYBIT_BUNDLED_ORDERS,
        trading_enums.TraderOrderType.SELL_LIMIT: _BYBIT_BUNDLED_ORDERS,
    }
    SPOT_SUPPORTED_BUNDLED_ORDER = {}

    MARK_PRICE_IN_TICKER = True
    FUNDING_IN_TICKER = True

    # set True when get_positions() is not returning empty positions and should use get_position() instead
    REQUIRES_SYMBOL_FOR_EMPTY_POSITION = True
    REQUIRE_ORDER_FEES_FROM_TRADES = True  # set True when get_order is not giving fees on closed orders and fees
    EXPECT_POSSIBLE_ORDER_NOT_FOUND_DURING_ORDER_CREATION = True  # set True when get_order() can return None
    # (order not found) when orders are instantly filled on exchange and are not fully processed on the exchange side.

    BUY_STR = "Buy"
    SELL_STR = "Sell"

    LONG_STR = BUY_STR
    SHORT_STR = SELL_STR

    # Order category. 0：normal order by default; 1：TP/SL order, Required for TP/SL order.
    ORDER_CATEGORY = "orderCategory"

    def __init__(self, config, exchange_manager, connector_class=None):
        super().__init__(config, exchange_manager, connector_class=connector_class)
        self.order_quantity_by_amount = {}
        self.order_quantity_by_id = {}

    def get_additional_connector_config(self):
        connector_config = {
            ccxt_constants.CCXT_OPTIONS: {
                "recvWindow": 60000,    # default is 5000, avoid time related issues
            }
        }
        if not self.exchange_manager.is_future:
            # tell ccxt to use amount as provided and not to compute it by multiplying it by price which is done here
            # (price should not be sent to market orders). Only used for buy market orders
            connector_config[ccxt_constants.CCXT_OPTIONS][
                "createMarketBuyOrderRequiresPrice"
            ] = False  # disable quote conversion
        return connector_config

    @classmethod
    def update_supported_elements(cls, exchange_manager):
        if exchange_manager.is_future:
            cls.SUPPORTED_BUNDLED_ORDERS = cls.FUTURES_SUPPORTED_BUNDLED_ORDERS
        else:
            cls.SUPPORTED_BUNDLED_ORDERS = cls.SPOT_SUPPORTED_BUNDLED_ORDER

    def get_adapter_class(self):
        return BybitCCXTAdapter

    @classmethod
    def get_name(cls) -> str:
        return 'bybit'

    @classmethod
    def get_supported_exchange_types(cls) -> list:
        """
        :return: The list of supported exchange types
        """
        return [
            trading_enums.ExchangeTypes.SPOT,
            trading_enums.ExchangeTypes.FUTURE,
        ]

    def get_market_status(self, symbol, price_example=None, with_fixer=True):
        return self.get_fixed_market_status(symbol, price_example=price_example, with_fixer=with_fixer)

    async def get_open_orders(self, symbol: str = None, since: int = None,
                              limit: int = None, **kwargs: dict) -> list:
        if symbol and not self.exchange_manager.is_future:
            # not done by ccxt spot request
            symbol = self.connector.client.markets[symbol]["id"]
        orders = await super().get_open_orders(symbol=symbol, since=since, limit=limit, **kwargs)
        if not self.exchange_manager.is_future:
            kwargs = kwargs or {}
            # include stop orders
            kwargs[self.ORDER_CATEGORY] = 1
            orders += await super().get_open_orders(symbol=symbol, since=since, limit=limit, **kwargs)
        return orders

    async def get_order(self, order_id: str, symbol: str = None, **kwargs: dict) -> dict:
        try:
            return await super().get_order(order_id, symbol=symbol, **kwargs)
        except octobot_trading.errors.FailedRequest:
            if self.ORDER_CATEGORY not in kwargs:
                kwargs[self.ORDER_CATEGORY] = 1
                return await super().get_order(order_id, symbol=symbol, **kwargs)
            raise

    async def cancel_order(
            self, order_id: str, symbol: str, order_type: trading_enums.TraderOrderType, **kwargs: dict
    ) -> trading_enums.OrderStatus:
        kwargs = kwargs or {}
        if trading_personal_data.is_stop_order(order_type):
            kwargs[self.ORDER_CATEGORY] = 1
        return await super().cancel_order(
            order_id, symbol, order_type, **kwargs
        )

    async def create_order(self, order_type: trading_enums.TraderOrderType, symbol: str, quantity: decimal.Decimal,
                           price: decimal.Decimal = None, stop_price: decimal.Decimal = None,
                           side: trading_enums.TradeOrderSide = None, current_price: decimal.Decimal = None,
                           reduce_only: bool = False, params: dict = None) -> typing.Optional[dict]:
        if not self.exchange_manager.is_future:
            if order_type is trading_enums.TraderOrderType.BUY_MARKET:
                # on Bybit, market orders are in quote currency (YYY in XYZ/YYY)
                used_price = price or current_price
                if not used_price:
                    raise octobot_trading.errors.NotSupported(f"{self.get_name()} requires a price parameter to create "
                                                              f"market orders as quantity is in quote currency")
                origin_quantity = quantity
                quantity = quantity * used_price
                self.order_quantity_by_amount[float(quantity)] = float(origin_quantity)
        return await super().create_order(order_type, symbol, quantity,
                                          price=price, stop_price=stop_price,
                                          side=side, current_price=current_price,
                                          reduce_only=reduce_only, params=params)

    async def _create_market_stop_loss_order(self, symbol, quantity, price, side, current_price, params=None) -> dict:
        params = params or {}
        params["triggerPrice"] = price
        if self.exchange_manager.is_future:
            # Trigger the order when market price rises to triggerPrice or falls to triggerPrice. 1: rise; 2: fall
            params["triggerDirection"] = 1 if price > current_price else 2
        else:
            params[self.ORDER_CATEGORY] = 1
        order = await self.connector.client.create_order(
            symbol, trading_enums.TradeOrderType.MARKET.value, side, quantity, params=params
        )
        return order

    async def _edit_order(self, order_id: str, order_type: trading_enums.TraderOrderType, symbol: str,
                          quantity: float, price: float, stop_price: float = None, side: str = None,
                          current_price: float = None, params: dict = None):
        params = params or {}
        if trading_personal_data.is_stop_order(order_type):
            params["stop_order_id"] = order_id
        if stop_price is not None:
            # params["stop_px"] = stop_price
            # params["stop_loss"] = stop_price
            params["triggerPrice"] = str(stop_price)
        return await super()._edit_order(order_id, order_type, symbol, quantity=quantity,
                                         price=price, stop_price=stop_price, side=side,
                                         current_price=current_price, params=params)

    async def _verify_order(self, created_order, order_type, symbol, price, side, get_order_params=None):
        if trading_personal_data.is_stop_order(order_type):
            get_order_params = get_order_params or {}
            if self.exchange_manager.is_future:
                get_order_params["stop"] = True
            else:
                get_order_params[self.ORDER_CATEGORY] = 1
        return await super()._verify_order(created_order, order_type, symbol, price, side,
                                           get_order_params=get_order_params)

    async def set_symbol_partial_take_profit_stop_loss(self, symbol: str, inverse: bool,
                                                       tp_sl_mode: trading_enums.TakeProfitStopLossMode):
        # /contract/v3/private/position/switch-tpsl-mode
        # from https://bybit-exchange.github.io/docs/derivativesV3/contract/#t-dv_switchpositionmode
        params = {
            "symbol": self.connector.client.market(symbol)['id'],
            "tpSlMode": tp_sl_mode.value
        }
        try:
            await self.connector.client.privatePostContractV3PrivatePositionSwitchTpslMode(params)
        except ccxt.ExchangeError as e:
            if "same tp sl mode1" in str(e):
                # can't fetch the tp sl mode1 value
                return
            raise

    def get_order_additional_params(self, order) -> dict:
        params = {}
        if self.exchange_manager.is_future:
            contract = self.exchange_manager.exchange.get_pair_future_contract(order.symbol)
            params["positionIdx"] = self._get_position_idx(contract)
            params["reduceOnly"] = order.reduce_only
        return params

    def _get_margin_type_query_params(self, symbol, **kwargs):
        if not self.exchange_manager.exchange.has_pair_future_contract(symbol):
            raise KeyError(f"{symbol} contract unavailable")
        else:
            contract = self.exchange_manager.exchange.get_pair_future_contract(symbol)
            kwargs = kwargs or {}
            kwargs[ccxt_enums.ExchangePositionCCXTColumns.LEVERAGE.value] = float(contract.current_leverage)
        return kwargs

    async def set_symbol_margin_type(self, symbol: str, isolated: bool, **kwargs: dict):
        kwargs = self._get_margin_type_query_params(symbol, **kwargs)
        await super().set_symbol_margin_type(symbol, isolated, **kwargs)

    def get_bundled_order_parameters(self, order, stop_loss_price=None, take_profit_price=None) -> dict:
        """
        Returns the updated params when this exchange supports orders created upon other orders fill
        (ex: a stop loss created at the same time as a buy order)
        :param order: the initial order
        :param stop_loss_price: the bundled order stopLoss price
        :param take_profit_price: the bundled order takeProfit price
        :return: A dict with the necessary parameters to create the bundled order on exchange alongside the
        base order in one request
        """
        params = {}
        if stop_loss_price is not None:
            params["stopLoss"] = str(stop_loss_price)
        if take_profit_price is not None:
            params["takeProfit"] = str(take_profit_price)
        return params

    def _get_position_idx(self, contract):
        # "position_idx" has to be set when trading futures
        # from https://bybit-exchange.github.io/docs/inverse/#t-myposition
        # Position idx, used to identify positions in different position modes:
        # 0-One-Way Mode
        # 1-Buy side of both side mode
        # 2-Sell side of both side mode
        if contract.is_one_way_position_mode():
            return 0
        else:
            raise NotImplementedError(
                f"Hedge mode is not implemented yet. Please switch to One-Way position mode from the Bybit "
                f"trading interface preferences of {contract.pair}"
            )
            # TODO
            # if Buy side of both side mode:
            #     return 1
            # else Buy side of both side mode:
            #     return 2


class BybitCCXTAdapter(exchanges.CCXTAdapter):
    # Position
    BYBIT_BANKRUPTCY_PRICE = "bustPrice"
    BYBIT_CLOSING_FEE = "occClosingFee"
    BYBIT_MODE = "positionIdx"
    BYBIT_REALIZED_PNL = "RealisedPnl"
    BYBIT_ONE_WAY = "MergedSingle"
    BYBIT_ONE_WAY_DIGIT = "0"
    BYBIT_HEDGE = "BothSide"
    BYBIT_HEDGE_DIGITS = ["1", "2"]

    # Funding
    BYBIT_DEFAULT_FUNDING_TIME = 8 * commons_constants.HOURS_TO_SECONDS

    # Orders
    BYBIT_REDUCE_ONLY = "reduceOnly"
    BYBIT_TRIGGER_ABOVE_KEY = "triggerDirection"
    BYBIT_TRIGGER_ABOVE_VALUE = "1"

    # Trades
    EXEC_TYPE = "execType"
    TRADE_TYPE = "Trade"

    def fix_order(self, raw, **kwargs):
        fixed = super().fix_order(raw, **kwargs)
        order_info = raw[trading_enums.ExchangeConstantsOrderColumns.INFO.value]
        # parse reduce_only if present
        fixed[trading_enums.ExchangeConstantsOrderColumns.REDUCE_ONLY.value] = \
            order_info.get(self.BYBIT_REDUCE_ONLY, False)
        if tigger_above := order_info.get(trading_enums.ExchangeConstantsOrderColumns.TRIGGER_ABOVE.value):
            fixed[trading_enums.ExchangeConstantsOrderColumns.TRIGGER_ABOVE.value] = \
                tigger_above == self.BYBIT_TRIGGER_ABOVE_VALUE
        status = fixed.get(trading_enums.ExchangeConstantsOrderColumns.STATUS.value)
        if status == 'ORDER_NEW':
            fixed[trading_enums.ExchangeConstantsOrderColumns.STATUS.value] = trading_enums.OrderStatus.OPEN.value
        if status == 'ORDER_CANCELED':
            fixed[trading_enums.ExchangeConstantsOrderColumns.STATUS.value] = trading_enums.OrderStatus.CANCELED.value
        if status == 'PARTIALLY_FILLED_CANCELED':
            fixed[trading_enums.ExchangeConstantsOrderColumns.STATUS.value] = trading_enums.OrderStatus.FILLED.value
        self._adapt_order_type(fixed)
        if not self.connector.exchange_manager.is_future:
            try:
                if fixed[trading_enums.ExchangeConstantsOrderColumns.TYPE.value] \
                        == trading_enums.TradeOrderType.MARKET.value and \
                        fixed[trading_enums.ExchangeConstantsOrderColumns.SIDE.value] \
                        == trading_enums.TradeOrderSide.BUY.value:
                    try:
                        quantity = self.connector.exchange_manager.exchange.order_quantity_by_amount[
                            kwargs.get("quantity")
                        ]
                        self.connector.exchange_manager.exchange.order_quantity_by_id[
                            fixed[ccxt_enums.ExchangeOrderCCXTColumns.ID.value]
                        ] = quantity
                    except KeyError:
                        quantity = self.connector.exchange_manager.exchange.order_quantity_by_id[
                            fixed[ccxt_enums.ExchangeOrderCCXTColumns.ID.value]
                        ]
                    if fixed[trading_enums.ExchangeConstantsOrderColumns.AMOUNT.value] is None or \
                            fixed[trading_enums.ExchangeConstantsOrderColumns.AMOUNT.value] < quantity * 0.999:
                        # when order status is PARTIALLY_FILLED_CANCELED but is actually filled
                        fixed[trading_enums.ExchangeConstantsOrderColumns.STATUS.value] = \
                            trading_enums.OrderStatus.OPEN.value
                    # convert amount to have the same units as every other exchange
                    fixed[trading_enums.ExchangeConstantsOrderColumns.AMOUNT.value] = quantity
            except KeyError:
                pass
        return fixed

    def _adapt_order_type(self, fixed):
        order_info = fixed[trading_enums.ExchangeConstantsOrderColumns.INFO.value]
        if stop_order_type := order_info.get("stopOrderType", None):
            if "StopLoss" in stop_order_type or "Stop" in stop_order_type:
                # stop loss are not tagged as such by ccxt, force it
                fixed[trading_enums.ExchangeConstantsOrderColumns.TYPE.value] = \
                    trading_enums.TradeOrderType.STOP_LOSS.value
            elif "TakeProfit" in stop_order_type:
                # take profit are not tagged as such by ccxt, force it
                fixed[trading_enums.ExchangeConstantsOrderColumns.TYPE.value] = \
                    trading_enums.TradeOrderType.TAKE_PROFIT.value
        elif not self.connector.exchange_manager.is_future:
            order_category = order_info.get(self.connector.exchange_manager.exchange.ORDER_CATEGORY, None)
            if order_category == "1":
                # can't identify take profits, use stop loss
                fixed[trading_enums.ExchangeConstantsOrderColumns.TYPE.value] = \
                    trading_enums.TradeOrderType.STOP_LOSS.value
        return fixed

    def fix_ticker(self, raw, **kwargs):
        fixed = super().fix_ticker(raw, **kwargs)
        fixed[trading_enums.ExchangeConstantsTickersColumns.TIMESTAMP.value] = self.connector.client.milliseconds()
        return fixed
    
    def parse_position(self, fixed, **kwargs):
        try:
            # todo handle contract value
            raw_position_info = fixed.get(ccxt_enums.ExchangePositionCCXTColumns.INFO.value)
            size = decimal.Decimal(
                str(fixed.get(ccxt_enums.ExchangePositionCCXTColumns.CONTRACTS.value, 0)))
            # if size == constants.ZERO:
            #     return {}  # Don't parse empty position

            symbol = self.connector.get_pair_from_exchange(
                fixed[ccxt_enums.ExchangePositionCCXTColumns.SYMBOL.value])
            raw_mode = raw_position_info.get(self.BYBIT_MODE)
            mode = trading_enums.PositionMode.ONE_WAY
            if raw_mode == self.BYBIT_HEDGE or raw_mode in self.BYBIT_HEDGE_DIGITS:
                mode = trading_enums.PositionMode.HEDGE
            original_side = fixed.get(ccxt_enums.ExchangePositionCCXTColumns.SIDE.value)

            side = trading_enums.PositionSide.BOTH
            # todo when handling cross positions
            # side = fixed.get(ccxt_enums.ExchangePositionCCXTColumns.SIDE.value, enums.PositionSide.UNKNOWN.value)
            # position_side = enums.PositionSide.LONG \
            #     if side == enums.PositionSide.LONG.value else enums.PositionSide.SHORT

            unrealized_pnl = self.safe_decimal(fixed,
                                               ccxt_enums.ExchangePositionCCXTColumns.UNREALISED_PNL.value,
                                               constants.ZERO)
            liquidation_price = self.safe_decimal(fixed,
                                                  ccxt_enums.ExchangePositionCCXTColumns.LIQUIDATION_PRICE.value,
                                                  constants.ZERO)
            entry_price = self.safe_decimal(fixed,
                                            ccxt_enums.ExchangePositionCCXTColumns.ENTRY_PRICE.value,
                                            constants.ZERO)
            return {
                trading_enums.ExchangeConstantsPositionColumns.SYMBOL.value: symbol,
                trading_enums.ExchangeConstantsPositionColumns.TIMESTAMP.value:
                    self.connector.client.safe_value(fixed,
                                                     ccxt_enums.ExchangePositionCCXTColumns.TIMESTAMP.value, 0),
                trading_enums.ExchangeConstantsPositionColumns.SIDE.value: side,
                trading_enums.ExchangeConstantsPositionColumns.MARGIN_TYPE.value:
                    trading_enums.MarginType(
                        fixed.get(ccxt_enums.ExchangePositionCCXTColumns.MARGIN_MODE.value)
                    ),
                trading_enums.ExchangeConstantsPositionColumns.SIZE.value:
                    size if original_side == trading_enums.PositionSide.LONG.value else -size,
                trading_enums.ExchangeConstantsPositionColumns.INITIAL_MARGIN.value:
                    self.safe_decimal(
                        fixed, ccxt_enums.ExchangePositionCCXTColumns.INITIAL_MARGIN.value,
                        constants.ZERO
                    ),
                trading_enums.ExchangeConstantsPositionColumns.NOTIONAL.value:
                    self.safe_decimal(
                        fixed, ccxt_enums.ExchangePositionCCXTColumns.NOTIONAL.value, constants.ZERO
                    ),
                trading_enums.ExchangeConstantsPositionColumns.LEVERAGE.value:
                    self.safe_decimal(
                        fixed, ccxt_enums.ExchangePositionCCXTColumns.LEVERAGE.value, constants.ONE
                    ),
                trading_enums.ExchangeConstantsPositionColumns.UNREALIZED_PNL.value: unrealized_pnl,
                trading_enums.ExchangeConstantsPositionColumns.REALISED_PNL.value:
                    self.safe_decimal(
                        fixed, self.BYBIT_REALIZED_PNL, constants.ZERO
                    ),
                trading_enums.ExchangeConstantsPositionColumns.LIQUIDATION_PRICE.value: liquidation_price,
                trading_enums.ExchangeConstantsPositionColumns.CLOSING_FEE.value:
                    self.safe_decimal(
                        fixed, self.BYBIT_CLOSING_FEE, constants.ZERO
                    ),
                trading_enums.ExchangeConstantsPositionColumns.BANKRUPTCY_PRICE.value:
                    self.safe_decimal(
                        fixed, self.BYBIT_BANKRUPTCY_PRICE, constants.ZERO
                    ),
                trading_enums.ExchangeConstantsPositionColumns.ENTRY_PRICE.value: entry_price,
                trading_enums.ExchangeConstantsPositionColumns.CONTRACT_TYPE.value:
                    self.connector.exchange_manager.exchange.get_contract_type(symbol),
                trading_enums.ExchangeConstantsPositionColumns.POSITION_MODE.value: mode,
            }
        except KeyError as e:
            self.logger.error(f"Fail to parse position dict ({e})")
        return fixed

    def parse_funding_rate(self, fixed, from_ticker=False, **kwargs):
        """
        Bybit last funding time is not provided
        To obtain the last_funding_time :
        => timestamp(next_funding_time) - timestamp(BYBIT_DEFAULT_FUNDING_TIME)
        """
        funding_dict = super().parse_funding_rate(fixed, from_ticker=from_ticker, **kwargs)
        if from_ticker:
            if ccxt_constants.CCXT_INFO not in funding_dict:
                return {}
            # no data in fixed when coming from ticker
            funding_dict = fixed[ccxt_constants.CCXT_INFO]
            funding_next_timestamp = float(
                funding_dict.get(ccxt_enums.ExchangeFundingCCXTColumns.NEXT_FUNDING_TIME.value, 0)
            )
            funding_dict.update({
                trading_enums.ExchangeConstantsFundingColumns.LAST_FUNDING_TIME.value:
                    funding_next_timestamp - self.BYBIT_DEFAULT_FUNDING_TIME,
                trading_enums.ExchangeConstantsFundingColumns.FUNDING_RATE.value: decimal.Decimal(
                    funding_dict.get(ccxt_enums.ExchangeFundingCCXTColumns.FUNDING_RATE.value, constants.NaN)),
                trading_enums.ExchangeConstantsFundingColumns.NEXT_FUNDING_TIME.value: funding_next_timestamp,
                trading_enums.ExchangeConstantsFundingColumns.PREDICTED_FUNDING_RATE.value: constants.NaN
            })
        else:
            funding_next_timestamp = float(
                funding_dict.get(trading_enums.ExchangeConstantsFundingColumns.NEXT_FUNDING_TIME.value, 0)
            )
            funding_dict.update({
                trading_enums.ExchangeConstantsFundingColumns.LAST_FUNDING_TIME.value:
                    funding_next_timestamp - self.BYBIT_DEFAULT_FUNDING_TIME,
                trading_enums.ExchangeConstantsFundingColumns.FUNDING_RATE.value: decimal.Decimal(
                    funding_dict.get(ccxt_constants.CCXT_INFO, {})
                    .get(ccxt_enums.ExchangeFundingCCXTColumns.FUNDING_RATE.value, constants.NaN)
                ),
            })
        return funding_dict

    def parse_mark_price(self, fixed, from_ticker=False, **kwargs) -> dict:
        if from_ticker and ccxt_constants.CCXT_INFO in fixed:
            try:
                return {
                    trading_enums.ExchangeConstantsMarkPriceColumns.MARK_PRICE.value:
                        fixed[ccxt_constants.CCXT_INFO][trading_enums.ExchangeConstantsMarkPriceColumns.MARK_PRICE.value]
                }
            except KeyError:
                pass
        return {
            trading_enums.ExchangeConstantsMarkPriceColumns.MARK_PRICE.value:
                decimal.Decimal(fixed[
                    trading_enums.ExchangeConstantsTickersColumns.CLOSE.value])
        }

    def fix_trades(self, raw, **kwargs):
        if self.connector.exchange_manager.is_future:
            raw = [
                trade
                for trade in raw
                if trade[trading_enums.ExchangeConstantsOrderColumns.INFO.value].get(
                    self.EXEC_TYPE, None) == self.TRADE_TYPE    # ignore non-trade elements (such as funding)
            ]
        return super().fix_trades(raw, **kwargs)
