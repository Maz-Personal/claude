import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import ContractType
from datetime import date, timedelta

from dotenv import dotenv_values
creds = dotenv_values(".env")
API_KEY    = creds["ALPACA_API_KEY"]
API_SECRET = creds["ALPACA_API_SECRET"]

trading = TradingClient(API_KEY, API_SECRET, paper=True)
account = trading.get_account()

print(f"Account status       : {account.status}")
print(f"Options approved     : {getattr(account, 'options_approved_level', 'N/A')}")
print(f"Options level        : {getattr(account, 'options_level', 'N/A')}")
print(f"Buying power         : ${float(account.buying_power):,.2f}")
print(f"Cash                 : ${float(account.cash):,.2f}")
print()

two_weeks  = date.today() + timedelta(weeks=2)
four_weeks = date.today() + timedelta(weeks=4)

try:
    contracts = trading.get_option_contracts(GetOptionContractsRequest(
        underlying_symbols=["TSLA"],
        expiration_date_gte=two_weeks,
        expiration_date_lte=four_weeks,
        type=ContractType.PUT,
        limit=5,
    ))
    print(f"Options available    : YES -- {len(contracts.option_contracts)} put contracts found (2-4 week window)")
    for c in contracts.option_contracts[:3]:
        print(f"  {c.symbol}  strike=${c.strike_price}  exp={c.expiration_date}")
except Exception as e:
    print(f"Options error        : {e}")
