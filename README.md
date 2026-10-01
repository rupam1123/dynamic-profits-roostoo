# Dynamic Profits — Team156 (IITK)

Initial research and public market-data setup for the APAC Quant Trading Hackathon.

## Current status

This starter collects public Roostoo snapshots into SQLite. It does not authenticate,
place orders, implement a strategy, or deploy to AWS. No API credentials are included.
Strategy selection and performance claims are pending empirical research.

## Run locally

Requires Python 3.10 or newer; no third-party packages are needed.

Windows PowerShell, from this folder:

```powershell
py -3 market_data.py
py -3 market_data.py --watch --interval 300
```

macOS/Linux:

```bash
python3 market_data.py
python3 market_data.py --watch --interval 300
```

The first command stores one snapshot. The second collects every five minutes until
stopped with Ctrl+C. Keep the terminal and machine running during collection.
Snapshots include exchange metadata, all returned tickers, server timestamp, and
local UTC receipt time. These snapshots are not historical OHLCV candles.
The chosen interval is a conservative project default, not an official API limit.

## Development order

See `docs/PLAN.md` for deliverables, rules to clarify, and proposed work allocation.
See `docs/SUBMISSION_OUTLINE.md` for the eventual judging README.

## Sources

- Problem statement: https://luma.com/coghwiyt
- API documentation: https://github.com/roostoo/Roostoo-API-Documents
- Info session: https://pitch.com/v/apac-quant-hackathon-info-session-zunvgj
- Data pack: https://roostoo.notion.site/Data-Sources-Pack-318ba22fed7980118a69c7a614995930
- AWS guide: https://roostoo.notion.site/Hackathon-Guide-How-to-Sign-In-AWS-and-Launch-Your-Bot-309ba22fed798071b4dde6d1e8666816

The problem statement, API documentation, and supplied email screenshots were reviewed.
The slide contents and Notion guide contents could not be retrieved on October 1, 2026.
Their URLs are recorded for follow-up, not treated as reviewed evidence.
