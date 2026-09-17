# logpy

CLI for logging psychoactive substance ingestions to a CSV file, with optional Discord webhook notifications.

## Installation

```sh
nix shell
```

## Configuration

logpy looks for a config file at `~/.config/logpy/config.toml` (or `$LOGPY_CONFIG`). You can also pass everything as flags.

```toml
user = "yourname"
logfile = "log.csv"
webhook = "https://discord.com/api/webhooks/..."
```

## Usage

```sh
logpy SUBSTANCE DOSAGE ROA [TIME...]
```

`logpy` defaults to flag/argument input. You can also choose a mode explicitly:

```sh
logpy --flags --substance LSD --dosage 100μg --roa sublingual
logpy --flags --substance Ketamine --dosage 25mg --roa intramuscular --volume-ml 1.5
logpy --mixture --substance "Ketamine; Midazolam" --dosage "25mg; 1mg" --roa intramuscular --volume-ml 1.5
logpy --prompt
logpy --dmenu
logpy --bemenu
logpy --fuzzel
```

In `--dmenu`, `--bemenu`, and `--fuzzel` modes, selectors use rougher, case-insensitive matching where supported by the menu program. Substance, route, and salt selectors also accept common abbreviations and expand them before logging, e.g. `k` -> `Ketamine`, `im` -> `intramuscular`, and `hcl` -> `hydrochloride`.

`--dmenu` and `--bemenu` selectors use a `0.75` width factor.

The substance selector includes substances already present in the current CSV log file, common built-in substances, cached AnodyneWiki substances, plus a `Custom...` option for entering a new value. Substance titles and abbreviations are cached in `~/.config/logpy/substances.json`; local cache entries are loaded before input collection, while uncached prior CSV history entries and newly logged substances are refreshed after prompts.

Dosages are logged exactly as entered after trimming whitespace. For example, `25` logs as `25`, and `25mg` logs as `25mg`.

The salt selector starts with `freebase`, `hydrochloride`, and `sulfate`, followed by the remaining known salt forms.

For intravenous routes, prompt and selector modes ask for a vein and then a side, logging the combined site such as `left-median-cubital` or `right-median-cubital`.

For subcutaneous, intramuscular, intradermal, intrarectal, and intravenous routes, prompt and selector modes optionally ask for a solution volume and append it to the dosage, e.g. `25mg@1.5`. Selector modes offer `0.25ml`, `0.5ml`, `1ml`, `1.5ml`, `2ml`, `2.5ml`, `5ml`, and `10ml`, while still allowing custom values.

Selector mode extra-info time prompts offer `15 minutes ago`, `30 minutes ago`, `45 minutes ago`, and `1 hour ago`, while still allowing custom values. Stdin prompt mode asks for free text.

Use `--mixture` or `--kind mixture` to log several compounds combined in one syringe volume. In flags mode, separate substances, dosages, and optional salts with `;` or `+`; the row is logged as one combined substance and one combined dosage with the shared volume appended, e.g. `Ketamine + Midazolam`, `25mg + 1mg@1.5`.

Basic log:
```sh
logpy LSD 100μg sublingual
logpy LSD 100μg sublingual -sa tartrate
logpy MDMA 120mg oral -sa hcl -n "taken with food"
```

With a backdated time:
```sh
logpy LSD 100μg sublingual 2 hours ago
logpy LSD 100μg sublingual -sa tartrate yesterday at 10pm
logpy MDMA 120mg oral -sa hcl 2025-12-04 10:00
```

If the ingestion time is more than 10 minutes in the past, it gets appended to the Discord message automatically.

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--flags` | | read ingestion fields from arguments/options |
| `--prompt` | | prompt for ingestion fields on stdin |
| `--dmenu` | | prompt for ingestion fields with dmenu selectors |
| `--bemenu` | | prompt for ingestion fields with bemenu selectors |
| `--fuzzel` | | prompt for ingestion fields with fuzzel selectors |
| `--substance` | | substance to log |
| `--dosage` | | dosage and unit |
| `--roa` | | route of administration |
| `--kind` | | entry kind: `single`, `composite`, `solution`, or `mixture` |
| `--mixture` | | shortcut for `--kind mixture` |
| `--time` | | ingestion time |
| `--volume-ml` | | solution volume appended to dosage for injection/rectal routes |
| `--salt` | `-sa` | salt form |
| `--site` | `-si` | site of administration |
| `--note` | `-n` | note, added to the Discord message and CSV |
| `--csv` | | CSV file to write to |
| `--user` | `-u` | username |
| `--webhook` | `-w` | Discord webhook URL |
| `--config` | `-c` | config file path |

## CSV format

Columns: `timestamp, user, substance, dosage, roa, site, salt, note`
