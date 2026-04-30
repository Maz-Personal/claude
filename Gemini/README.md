# Gemini Options Research MCP Server

An MCP server that exposes a single tool, `options_research`, which asks
Google's Gemini model to produce a structured educational analysis of
asymmetric, defined-risk options setups grounded in **today's** live market
data (via Gemini's built-in Google Search tool).

> Educational research only. Not personalized financial advice.

## What it does

The tool sends a carefully structured prompt to Gemini that forces it to:

1. Pull live S&P 500, Nasdaq, VIX, sector, and macro readings via Google Search.
2. Recommend the appropriate defined-risk vehicle for the current IV regime.
3. Generate two opposing trade ideas (one bullish, one bearish) with full
   thesis, setup, risk/reward, and "why now" rationale.
4. Cite the sources Gemini used.

## Setup

### 1. Install

```bash
git clone <this-repo> gemini-options-mcp
cd gemini-options-mcp
pip install -r requirements.txt
```

### 2. Get a Gemini API key

Free tier keys are available at https://aistudio.google.com/apikey.

```bash
export GEMINI_API_KEY="your-key-here"
# Optional - defaults to gemini-2.5-pro
export GEMINI_MODEL="gemini-2.5-pro"
```

### 3. Run

```bash
python server.py
```

The server speaks MCP over stdio.

## Connecting to Claude Desktop

Add this to your `claude_desktop_config.json`
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "gemini-options-research": {
      "command": "python",
      "args": ["/absolute/path/to/gemini-options-mcp/server.py"],
      "env": {
        "GEMINI_API_KEY": "your-key-here"
      }
    }
  }
}
```

Restart Claude Desktop. The `options_research` tool will appear in the tool
picker.

## Connecting to Claude Code

```bash
claude mcp add gemini-options \
  -e GEMINI_API_KEY=your-key-here \
  -- python /absolute/path/to/gemini-options-mcp/server.py
```

## Tool reference

### `options_research`

All arguments are optional strings; pass empty strings or omit for defaults.

| Argument | Description | Example |
|---|---|---|
| `focus` | Bias the trade ideas | `"earnings plays"` |
| `min_roi` | Minimum ROI on risk | `"200%"` |
| `dte` | Days-to-expiration override | `"7-14"` |
| `extra_constraints` | Anything else | `"penny-wide spreads only"` |

### Example invocations

From a connected client:

> "Run options_research"

> "Run options_research with focus on macro themes and 60 DTE"

> "Run options_research with min_roi 300% and constraint that I want only
>  liquid ETFs"

## How the prompt works

The base prompt forces a fixed structure (Opening Frame, Section 1 Market
Conditions, Section 2 Strategy Selection, Section 3 Two Trade Ideas, Closing
Question), and the optional arguments are appended as additional constraints
under their own header. The full prompt template lives in `BASE_PROMPT`
inside `server.py` if you want to customize it.

## Notes

- Gemini's Google Search grounding is what makes "live" data possible; if
  you swap to a model that doesn't support it, remove the `tools=[...]`
  config in `call_gemini()`.
- Output quality depends heavily on Gemini's search results. Treat any
  trade idea as a research starting point, never as a trade ticket.
