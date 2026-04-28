import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

client = TradingClient(
    os.getenv("ALPACA_API_KEY"),
    os.getenv("ALPACA_API_SECRET"),
    paper=True,
)

account = client.get_account()
print(f"Account status : {account.status}")
print(f"Buying power   : ${float(account.buying_power):,.2f}")

order_request = MarketOrderRequest(
    symbol="AAPL",
    qty=1,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
)

order = client.submit_order(order_request)
print(f"\nOrder submitted:")
print(f"  ID     : {order.id}")
print(f"  Symbol : {order.symbol}")
print(f"  Qty    : {order.qty}")
print(f"  Side   : {order.side}")
print(f"  Type   : {order.order_type}")
print(f"  Status : {order.status}")
