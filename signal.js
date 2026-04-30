/**
 * signal.js — Buy/Sell signal generator for any stock
 * Usage: node signal.js TICKER [avg_cost] [shares]
 *        node signal.js TICKER clear          ← removes all drawings
 * Example: node signal.js NVDA 3.25 8000
 *          node signal.js NVDA clear
 *          node signal.js TSLA
 */

import { setSymbol, setTimeframe } from '../tradingview-mcp/src/core/chart.js';
import { getOhlcv, getQuote } from '../tradingview-mcp/src/core/data.js';
import { drawShape, clearAll } from '../tradingview-mcp/src/core/drawing.js';
import { execSync, spawn } from 'child_process';

// Auto-launch Chrome with TradingView if not already running on port 9222
async function ensureChrome() {
  try {
    const res = await fetch('http://localhost:9222/json');
    const tabs = await res.json();
    if (tabs.some(t => t.url?.includes('tradingview.com'))) return; // already good
  } catch { /* not running */ }

  console.log('Launching Chrome with TradingView...');
  spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', [
    '--remote-debugging-port=9222',
    '--user-data-dir=C:\\ChromeDebug',
    'https://www.tradingview.com/chart/'
  ], { detached: true, stdio: 'ignore' }).unref();

  // Wait for TradingView to load
  for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 3000));
    try {
      const res = await fetch('http://localhost:9222/json');
      const tabs = await res.json();
      if (tabs.some(t => t.url?.includes('tradingview.com'))) {
        await new Promise(r => setTimeout(r, 5000)); // extra wait for chart to render
        return;
      }
    } catch { /* keep waiting */ }
  }
  throw new Error('TradingView failed to load after 60 seconds.');
}

await ensureChrome();

const [,, rawTicker, rawAvg, rawShares] = process.argv;

if (!rawTicker) {
  console.error('Usage: node signal.js TICKER [avg_cost] [shares]');
  process.exit(1);
}

const ticker   = rawTicker.toUpperCase();
const clearMode = rawAvg === 'clear';
const avgCost  = (!clearMode && rawAvg)    ? parseFloat(rawAvg)    : null;
const shares   = (!clearMode && rawShares) ? parseFloat(rawShares) : null;

// ── Helpers ──────────────────────────────────────────────────────────────────

function ema(data, period) {
  const k = 2 / (period + 1);
  let e = data[0];
  for (let i = 1; i < data.length; i++) e = data[i] * k + e * (1 - k);
  return +e.toFixed(2);
}

function rsi(closes, period = 14) {
  let gains = 0, losses = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d > 0) gains += d; else losses -= d;
  }
  let avgGain = gains / period, avgLoss = losses / period;
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    avgGain = (avgGain * (period - 1) + (d > 0 ? d : 0)) / period;
    avgLoss = (avgLoss * (period - 1) + (d < 0 ? -d : 0)) / period;
  }
  return avgLoss === 0 ? 100 : +(100 - 100 / (1 + avgGain / avgLoss)).toFixed(1);
}

function atr(bars, period = 14) {
  const trs = bars.slice(1).map((b, i) => Math.max(
    b.high - b.low,
    Math.abs(b.high - bars[i].close),
    Math.abs(b.low  - bars[i].close)
  ));
  return +(trs.slice(-period).reduce((a, b) => a + b, 0) / period).toFixed(2);
}

function swings(bars, lookback = 3) {
  const highs = [], lows = [];
  for (let i = lookback; i < bars.length - lookback; i++) {
    if ([...Array(lookback)].every((_, o) =>
      bars[i].high > bars[i - o - 1].high && bars[i].high > bars[i + o + 1].high))
      highs.push(+bars[i].high.toFixed(2));
    if ([...Array(lookback)].every((_, o) =>
      bars[i].low < bars[i - o - 1].low && bars[i].low < bars[i + o + 1].low))
      lows.push(+bars[i].low.toFixed(2));
  }
  return { highs: highs.slice(-3), lows: lows.slice(-3) };
}

// ── Resolve exchange prefix ───────────────────────────────────────────────────

async function resolveSymbol(ticker) {
  const candidates = [
    `NASDAQ:${ticker}`, `NYSE:${ticker}`, `CBOE:${ticker}`, `AMEX:${ticker}`
  ];
  for (const sym of candidates) {
    try {
      await setSymbol({ symbol: sym });
      await new Promise(r => setTimeout(r, 5000));
      const q = await getQuote({});
      if (q?.last) return { sym, price: q.last };
    } catch { /* try next */ }
  }
  throw new Error(`Could not find ${ticker} on any exchange.`);
}

// ── Main ──────────────────────────────────────────────────────────────────────

// ── Clear mode ────────────────────────────────────────────────────────────────
if (clearMode) {
  console.log(`\n🗑  Clearing drawings for ${ticker}...`);
  await setTimeframe({ timeframe: 'D' });
  await new Promise(r => setTimeout(r, 2000));
  const { sym } = await resolveSymbol(ticker);
  await clearAll();
  console.log(`✅ All drawings cleared from ${sym}\n`);
  process.exit(0);
}

console.log(`\n📊 Generating signal for ${ticker}...\n`);

await setTimeframe({ timeframe: 'D' });
await new Promise(r => setTimeout(r, 2000));

const { sym, price } = await resolveSymbol(ticker);
const ohlcv = await getOhlcv({ count: 60, summary: false });
const bars   = ohlcv?.bars || [];

if (bars.length < 20) throw new Error('Not enough bar data.');

const closes  = bars.map(b => b.close);
const highs   = bars.map(b => b.high);
const lows    = bars.map(b => b.low);
const vols    = bars.map(b => b.volume);
const avgVol  = vols.reduce((a, b) => a + b, 0) / vols.length;
const lastVol = vols.at(-1);

const e20     = ema(closes, 20);
const e50     = ema(closes, 50);
const rsi14   = rsi(closes, 14);
const atr14   = atr(bars, 14);
const hi60    = +Math.max(...highs).toFixed(2);
const lo60    = +Math.min(...lows).toFixed(2);
const relVol  = +(lastVol / avgVol).toFixed(2);
const pctHi   = +((price - hi60) / hi60 * 100).toFixed(1);
const { highs: swHi, lows: swLo } = swings(bars);

// ── Signal Logic ──────────────────────────────────────────────────────────────

const aboveE20  = price > e20;
const aboveE50  = price > e50;
const emaSlope  = e20 > e50; // EMA20 above EMA50 = uptrend

let signal, signalEmoji;
if (aboveE20 && aboveE50 && emaSlope && rsi14 < 70)    { signal = 'BUY';       signalEmoji = '🟢'; }
else if (aboveE20 && aboveE50 && rsi14 >= 70)          { signal = 'OVERBOUGHT'; signalEmoji = '🟡'; }
else if (!aboveE20 && !aboveE50 && !emaSlope)          { signal = 'SELL';       signalEmoji = '🔴'; }
else if (!aboveE20 && aboveE50)                        { signal = 'CAUTION';    signalEmoji = '🟠'; }
else                                                    { signal = 'NEUTRAL';    signalEmoji = '⚪'; }

// ── Level Calculations ────────────────────────────────────────────────────────

// Buy zone: EMA20 ± 1 ATR
const buyZoneLow  = +(e20 - atr14 * 0.5).toFixed(2);
const buyZoneHigh = +(e20 + atr14 * 0.5).toFixed(2);

// Stop: EMA50 - 0.5 ATR (or recent swing low if tighter)
const stopLevel   = +(Math.max(e50 - atr14 * 0.5, lo60 + atr14)).toFixed(2);

// Resistance: nearest swing high above price, or 60d high
const nearResist  = swHi.filter(h => h > price).sort((a, b) => a - b)[0] || hi60;

// Target: 2× ATR above resistance (R:R ~2:1 from buy zone)
const target      = +(nearResist + atr14 * 2).toFixed(2);

// P&L if applicable
let plLine = '';
if (avgCost && shares) {
  const unrealized = ((price - avgCost) * shares);
  const pct = ((price - avgCost) / avgCost * 100).toFixed(1);
  plLine = `  Unrealized P&L : ${unrealized >= 0 ? '+' : ''}$${Math.round(unrealized).toLocaleString()} (${pct}%)`;
}

// ── Print Report ──────────────────────────────────────────────────────────────

const separator = '─'.repeat(52);
console.log(separator);
console.log(`  ${signalEmoji}  ${ticker} (${sym})   SIGNAL: ${signal}`);
console.log(separator);
console.log(`  Price           : $${price.toFixed(2)}`);
if (avgCost) console.log(`  Avg Cost        : $${avgCost}`);
if (plLine)  console.log(plLine);
console.log(`  60d High        : $${hi60}  (${pctHi}% from high)`);
console.log(`  60d Low         : $${lo60}`);
console.log('');
console.log(`  EMA20           : $${e20}  ${aboveE20 ? '✅ Price above' : '❌ Price below'}`);
console.log(`  EMA50           : $${e50}  ${aboveE50 ? '✅ Price above' : '❌ Price below'}`);
console.log(`  RSI (14)        : ${rsi14}  ${rsi14 > 70 ? '⚠️  Overbought' : rsi14 < 30 ? '⚠️  Oversold' : '✅ Neutral'}`);
console.log(`  ATR (14)        : $${atr14}`);
console.log(`  Rel. Volume     : ${relVol}x  ${relVol > 1.5 ? '⚠️  High volume' : '✅ Normal'}`);
console.log('');
console.log(`  🟢 Buy Zone     : $${buyZoneLow} – $${buyZoneHigh}  (EMA20 ± ½ ATR)`);
console.log(`  🔴 Stop Loss    : $${stopLevel}`);
console.log(`  🟡 Resistance   : $${nearResist}`);
console.log(`  🔵 Target       : $${target}`);
if (avgCost) {
  const rr = ((target - price) / (price - stopLevel)).toFixed(1);
  console.log(`  R:R from here   : ${rr}:1`);
}
console.log(separator);

// ── Draw on Chart ─────────────────────────────────────────────────────────────

const T = bars.at(-1)?.time;
const drawLevels = [
  { price: stopLevel,   color: '#ef5350', style: 2, label: `${ticker} Stop $${stopLevel}` },
  { price: buyZoneLow,  color: '#26a69a', style: 0, label: `${ticker} Buy Zone Low $${buyZoneLow}` },
  { price: buyZoneHigh, color: '#26a69a', style: 0, label: `${ticker} Buy Zone High $${buyZoneHigh}` },
  { price: nearResist,  color: '#ffb74d', style: 1, label: `${ticker} Resistance $${nearResist}` },
  { price: target,      color: '#42a5f5', style: 1, label: `${ticker} Target $${target}` },
];
if (avgCost) drawLevels.push(
  { price: avgCost, color: '#9e9e9e', style: 2, label: `${ticker} Avg Cost $${avgCost}` }
);

console.log('\nClearing existing drawings...');
await clearAll();
await new Promise(r => setTimeout(r, 1000));

console.log('Drawing levels on chart...');
for (const l of drawLevels) {
  try {
    await drawShape({ shape: 'horizontal_line', point: { time: T, price: l.price },
      overrides: { linecolor: l.color, linewidth: 2, linestyle: l.style }, text: l.label });
    console.log(`  ✓ ${l.label}`);
  } catch(e) {
    console.log(`  ✗ Failed: ${l.label}`);
  }
  await new Promise(r => setTimeout(r, 500));
}

console.log(`\n✅ Done — ${ticker} chart updated.\n`);
