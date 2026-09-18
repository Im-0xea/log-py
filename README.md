# logpy

CLI for logging psychoactive substance ingestions to a CSV file and optionaly to a public logger (via discord webhook).

## Installation

### nix

```sh
nix shell
```

### uv
```sh
uv pip install .
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
# log through "argument" mode
# usage: logpy <SUBSTANCE> <DOSAGE> <ROA> [TIME...] [OPTIONS] )
logpy Methamphetamine ~50mg Intravenous --note "reused-syringe reused-needle omg-my-favorite"
logpy Methamphetamine 50mg Intravenous --site left-cephalic --salt Hydrochloride
logpy LSD 100μg sublingual 2 hours ago
logpy LSD 100μg sublingual -sa tartrate yesterday at 10pm
logpy MDMA 120mg oral -sa hcl 2025-12-04 10:00

# log via "flags" mode:
logpy --flags --substance LSD --dosage 100μg --roa sublingual
logpy --flags --substance Ketamine --dosage 25mg --roa intramuscular --volume-ml 1.5
```

Use `--mixture` or `--kind mixture` to log several compounds combined in one syringe volume. In flags mode, separate substances, dosages, and optional salts with `;` or `+`; the row is logged with list-valued fields formatted as angle-bracketed comma lists, e.g. `<Ketamine,Midazolam>`, `<25mg,1mg>@1.5`. In prompt and selector modes, entering more than one compound logs the row as a mixture automatically.

```sh
logpy --mixture --substance "Ketamine; Midazolam" --dosage "25mg; 1mg" --roa intramuscular --volume-ml 1.5
```

# log through menu modes for fast navigation and a more forgiving and user-friendly interface
# - "prompt" mode
logpy --prompt
# - "dmenu" mode
logpy --dmenu
# - "bemenu" mode
logpy --bemenu
# - "fuzzel" mode
logpy --fuzzel
```


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
