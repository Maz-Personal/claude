import os, time
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

load_dotenv()
API_KEY    = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_API_SECRET")

trading = TradingClient(API_KEY, API_SECRET, paper=True)
data    = StockHistoricalDataClient(API_KEY, API_SECRET)

def get_price(symbol):
    resp = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
    return float(resp[symbol].price)

def place(symbol, qty, side):
    return trading.submit_order(MarketOrderRequest(
        symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
    ))

print("=" * 60)
print("  TSLA TRAILING STOP STRATEGY  --  paper trading")
print("=" * 60)

order = place("TSLA", 10, OrderSide.BUY)
print(f"\n  >>> INITIAL BUY ORDER PLACED")
print(f"      ID     : {order.id}")
print(f"      Symbol : {order.symbol}  |  Qty: {order.qty}  |  Side: BUY")
print(f"      Type   : MARKET  |  TIF: DAY")
print(f"      Status : {order.status.value}")

time.sleep(2)
entry = get_price("TSLA")

print(f"\n{'=' * 60}")
print(f"  STRATEGY RULES SUMMARY")
print(f"{'=' * 60}")
print(f"  Entry price         : ${entry:.2f}")
print(f"  Stop loss (floor)   : ${entry * 0.90:.2f}  (entry - 10%)  -> SELL ALL")
print()
print(f"  Trailing stop kicks : when price reaches ${entry * 1.10:.2f}  (+10%)")
print(f"                        stop tracks 5% below running high (floor never drops)")
print()
print(f"  Ladder -20%         : if price hits ${entry * 0.80:.2f}  -> BUY 20 shares")
print(f"  Ladder -30%         : if price hits ${entry * 0.70:.2f}  -> BUY 10 more shares")
print(f"{'=' * 60}")
print(f"\n  Monitoring script ready: tsla_trailing_stop.py")
