@echo off
:: Launches Chrome with remote debugging on port 9222 and opens TradingView
:: Runs on Windows startup so signal.js always works

set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"
set PROFILE="C:\ChromeDebug"
set URL=https://www.tradingview.com/chart/

start "" %CHROME% --remote-debugging-port=9222 --user-data-dir=%PROFILE% %URL%
