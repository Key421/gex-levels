# GEX Levels

A local web page that calculates daily **gamma exposure (GEX) levels** for SPX, NDX, SPY and QQQ, plus a TradingView indicator that draws them on your chart.

- **Call wall**: strike with the most call gamma (often resistance)
- **Put wall**: strike with the most put gamma (often support)
- **Zero gamma**: price where dealer gamma flips from positive (chop, mean reversion) to negative (trend, bigger moves)
- **Extra ±GEX strikes**: the next-largest net-gamma strikes

> Context, not a trade signal. The data feeds are free and unofficial, and levels are estimates. Always confirm with your own price action.

## Quick start

1. Install **Python 3.10+** from [python.org](https://www.python.org/downloads/) (tick "Add Python to PATH").
2. Double-click **`start_gex.bat`**. It installs the packages in `requirements.txt`, starts the page and opens `http://localhost:8000`.
3. Pick an underlying and click **Get levels**, then **Copy** the string.
4. In TradingView, paste it into the **"Paste levels"** box of the *Daily GEX Levels* indicator.

Manual start instead of the .bat:

```powershell
py -m pip install -r requirements.txt
py gex_page.py
```

## The web page

| Section | What it does |
|---|---|
| **Controls** | Underlying, strike range (± %), max days to expiry (blank = all, or *0DTE only*), positioning (open interest, OI + volume, or volume), extra strikes, data source |
| **Pine string** | Copy-ready string for the indicator, plus a levels table and a data-health check |
| **Live read** | Gamma regime, where price sits between the walls, distance to each level, a plain-language playbook and cautions (OPEX, news days, last hour, stale levels). Alerts on regime flips and wall approaches. |
| **Chart** | Index or futures candles with the levels drawn on, and net GEX by strike |
| **Track record** | Scores each logged day: did the walls hold, did price stay inside them, and were negative-gamma days more volatile? |
| **Auto-refresh** | Pulls fresh levels on weekdays at 8:30, 9:46 and 12:30 ET (editable). It copies the string, beeps and shows a notification. Keep the tab open. |

### Data sources

- **Cboe** (default): Cboe's free delayed options chain (about 15 minutes behind), including greeks.
- **Yahoo Finance** (automatic backup): used when Cboe is blocked or sends blank greeks. Yahoo has no greeks, so gamma is computed from its implied volatility. Its "all expirations" covers the next 60 days.
- The **Data source** picker can force either one. Each result shows which feed it came from.

Chains with blank or half-refreshed greeks are rejected rather than saved, and a scheduled refresh retries every 2 minutes, up to 5 times.

### Run log

Every *Get levels* and auto-refresh is saved to `gex_log.db` (SQLite) next to the script. The track record reads from it. This file is personal and is excluded from Git by `.gitignore`.

## TradingView indicator (`gex_levels.pine`)

1. TradingView → **Pine Editor** → paste the contents of `gex_levels.pine` → **Add to chart**.
2. Paste the string from the page into **Paste levels**.

String format:

```
@YYYY-MM-DDTHHMM,price:Label:netGEX,price:Label:netGEX,...
```

- `@date/time` lets the indicator warn when levels are out of date.
- `netGEX` ($mn per 1% move) scales line thickness.
- Labels containing `Call`, `Put` or `Zero`/`Flip` are the walls and zero gamma. Anything else is a ±GEX strike, and a `-` in the label means negative.

Features:
- **Futures:** automatic index-to-futures basis (NDX → NQ/MNQ, SPX → ES/MES), or set a manual basis.
- **On the chart:** zones around levels, regime background tint, a summary table and a live-read panel.
- **Reminders:** run-time reminders and stale-level warnings.
- **Alerts:** price crosses a level, nears a level, regime flips or wall approaches. Create an alert with **"Any alert() function call"**.

TradingView scripts can't fetch outside data, so the levels have to be pasted in.

## Start with Windows (optional)

- **`install_startup.bat`**: launches the page minimized every time you sign in.
- **`remove_startup.bat`**: removes it again.

## Files

| File | Purpose |
|---|---|
| `gex_page.py` | FastAPI server, GEX math and the whole web page |
| `gex_levels.pine` | TradingView indicator (Pine Script v6) |
| `requirements.txt` | Python packages |
| `start_gex.bat` | Install packages and start the page |
| `install_startup.bat` / `remove_startup.bat` | Add or remove the Windows startup shortcut |

## Troubleshooting

- **"An older GEX page is already running on port 8000"**: close the other GEX window (or end `python.exe` in Task Manager) and start again.
- **"Greeks are blank right now"**: Cboe is mid-refresh, which is common around the open. In Auto mode Yahoo is used automatically. Otherwise wait a couple of minutes.
- **Chart doesn't load**: the chart library loads from unpkg.com and needs internet. Levels still work without it.
