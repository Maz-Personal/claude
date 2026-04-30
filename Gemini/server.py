"""
Gemini Options Research MCP Server

Exposes a tool that asks Google's Gemini model to produce an educational
analysis of asymmetric, defined-risk options setups grounded in live market
data via Gemini's built-in Google Search tool.

Tools:
    options_research(focus: str = "", min_roi: str = "", dte: str = "",
                     extra_constraints: str = "")

Environment:
    GEMINI_API_KEY   (required)  -- your Google AI Studio API key
    GEMINI_MODEL     (optional)  -- model id, default "gemini-2.5-pro"

Run:
    pip install mcp google-genai
    export GEMINI_API_KEY=...
    python server.py
"""

from __future__ import annotations

import os
import sys
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:
    print(
        "Missing dependency: install with `pip install google-genai`",
        file=sys.stderr,
    )
    raise


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

BASE_PROMPT = """\
You are an options trading research analyst. Produce an educational analysis
of asymmetric, defined-risk options setups based on TODAY's actual market
conditions. Use Google Search to ground every market level, VIX reading,
sector catalyst, and earnings headline in current data — do not rely on
training data or memory for any number. Today's date is {today}.

Structure your response exactly as follows:

OPENING FRAME
- One paragraph explaining that "low risk, high reward" in options is best
  achieved through strategies that cap max loss to a small upfront premium
  while offering a multiple of that risk in payout.
- One-line disclaimer that this is educational research, not personalized
  financial advice.

SECTION 1 — CURRENT MARKET CONDITIONS (as of {today})
Cover, with cited live figures:
- Broad market levels (S&P 500, Nasdaq, and any other relevant index)
- Volatility regime (VIX level + what it implies for option premiums)
- Sector strength/weakness (tech, AI, semis, energy, financials, etc.)
- Macro / commodity drivers (oil, rates, FX, geopolitics)
- Notable earnings or news flow shaping near-term sentiment this week

SECTION 2 — STRATEGY SELECTION
Based on the current VIX regime and price action, recommend the most
appropriate defined-risk vehicle (e.g., debit spreads when IV is moderate,
credit spreads when IV is elevated, calendars when term structure is steep,
etc.). Explain WHY this structure fits today specifically — address how it
handles theta decay and IV behavior given current conditions.

SECTION 3 — TWO SPECIFIC TRADE IDEAS
The two ideas must reflect OPPOSING sides of current market action (e.g.,
one bullish on a sector showing relative strength, one bearish on a sector
showing structural weakness). For each idea provide:
- Thesis: the directional/sector view and the catalysts driving it today
- Setup: structure name, underlying (ETF or stock), strike selection
  guidance (e.g., slightly OTM long leg, further OTM short leg), and DTE
- Risk/Reward: max loss (net debit), max profit (strike width minus debit),
  typical ROI range on risk
- Why it works today: the specific live condition making this attractive
  right now, not in general

CLOSING
One question asking which side of current market dynamics aligns better
with my research thesis.
"""


def build_prompt(
    focus: str = "",
    min_roi: str = "",
    dte: str = "",
    extra_constraints: str = "",
) -> str:
    """Build the full prompt with optional user-supplied knobs appended."""
    today = date.today().isoformat()
    prompt = BASE_PROMPT.format(today=today)

    extras: list[str] = []
    if focus.strip():
        extras.append(f"- Bias the two ideas toward: {focus.strip()}.")
    if min_roi.strip():
        extras.append(f"- Target {min_roi.strip()} ROI minimum on risk.")
    if dte.strip():
        extras.append(f"- Use {dte.strip()} DTE instead of 30-45.")
    if extra_constraints.strip():
        extras.append(f"- Additional constraint: {extra_constraints.strip()}.")

    if extras:
        prompt += "\n\nADDITIONAL CONSTRAINTS\n" + "\n".join(extras) + "\n"

    return prompt


# ---------------------------------------------------------------------------
# Gemini client
# ---------------------------------------------------------------------------

def call_gemini(prompt: str) -> str:
    """Send prompt to Gemini with Google Search grounding enabled."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is not set. "
            "Get one at https://aistudio.google.com/apikey"
        )

    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")
    client = genai.Client(api_key=api_key)

    # Enable Google Search grounding so Gemini fetches live market data.
    grounding_tool = genai_types.Tool(
        google_search=genai_types.GoogleSearch()
    )
    config = genai_types.GenerateContentConfig(tools=[grounding_tool])

    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )

    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty response.")

    # Append grounding citations if present.
    citations = _extract_citations(response)
    if citations:
        text += "\n\n---\nSources:\n" + "\n".join(
            f"  [{i}] {url}" for i, url in enumerate(citations, 1)
        )

    return text


def _extract_citations(response: Any) -> list[str]:
    """Pull URIs from grounding metadata if Gemini provided any."""
    urls: list[str] = []
    try:
        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            meta = getattr(cand, "grounding_metadata", None)
            if not meta:
                continue
            chunks = getattr(meta, "grounding_chunks", None) or []
            for chunk in chunks:
                web = getattr(chunk, "web", None)
                if web and getattr(web, "uri", None):
                    uri = web.uri
                    if uri not in urls:
                        urls.append(uri)
    except Exception:
        pass
    return urls


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP("gemini-options-research")


@mcp.tool()
def options_research(
    focus: str = "",
    min_roi: str = "",
    dte: str = "",
    extra_constraints: str = "",
) -> str:
    """
    Generate an educational analysis of asymmetric, defined-risk options
    setups using today's live market data via Gemini + Google Search.

    Args:
        focus: Optional bias for the trade ideas, e.g.
            "earnings plays", "macro themes", "single-name vs ETF".
        min_roi: Optional minimum ROI on risk, e.g. "200%".
        dte: Optional days-to-expiration override, e.g. "7-14" or "60".
        extra_constraints: Any other constraint, e.g.
            "liquid names with penny-wide bid-ask spreads".

    Returns:
        A structured markdown analysis covering current market conditions,
        strategy selection, and two opposing trade ideas, with sources.
    """
    prompt = build_prompt(focus, min_roi, dte, extra_constraints)
    return call_gemini(prompt)


if __name__ == "__main__":
    # Default transport is stdio, which is what MCP clients (Claude Desktop,
    # Claude Code, etc.) expect.
    mcp.run()
