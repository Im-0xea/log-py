import csv
import dateparser
import io
import json
import re
import shutil
import subprocess
import sys

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
import requests
from pydantic import BaseModel, HttpUrl, field_validator
from typer_config import toml_loader, conf_callback_factory
from rich.console import Console

from log_py.salts import SALT_ALIASES, SALT_CHOICES

con = Console()
err_con = Console(stderr=True)
app = typer.Typer(add_completion=True, no_args_is_help=True)
app_dir = Path(typer.get_app_dir("logpy"))
SUBSTANCE_CACHE_FILE = app_dir / "substances.json"
PROMPT_OPTIONS_CACHE_FILE = app_dir / "prompt-options.json"
PROMPT_OPTIONS_CACHE_VERSION = 2
DONE_SUBSTANCE_CHOICE = "None"
UNKNOWN_VOLUME_CHOICE = "unknown"
BACK_CHOICE = "back"
MENU_CUSTOM_PREFIX = "~"
# Populated only from AnodyneWiki API responses cached in SUBSTANCE_CACHE_FILE.
SUBSTANCE_ALIASES: dict[str, list[str]] = {}
ROA_ALIASES = {
    "oral": ["po", "by mouth", "ingested"],
    "sublingual": ["sl", "under tongue"],
    "buccal": ["bucc"],
    "intranasal": ["in", "nasal", "snorted", "insufflated"],
    "inhaled": ["smoked", "vaped", "vaporized"],
    "intravenous": ["iv"],
    "intramuscular": ["im"],
    "subcutaneous": ["sc", "sq", "subq"],
    "intradermal": ["id"],
    "intrarectal": ["ir", "rectal", "plugged", "boofed"],
    "transdermal": ["td", "topical"],
}
UNIT_CHOICES = ["mg", "ug", "mcg", "g", "ml"]
EXTRA_INFO_ALIASES = {
    "No": ["n"],
    "Yes": ["y"],
}


def _time_offset_label(minutes: int) -> str:
    if minutes < 60 or minutes % 60:
        return f"{minutes} minutes ago"
    hours = minutes // 60
    return f"{hours} hour{'s' if hours != 1 else ''} ago"


TIME_CHOICES = [
    _time_offset_label(minutes)
    for minutes in range(15, 24 * 60 + 1, 15)
]
IV_SITE_BASES = [
    "median-cubital",
    "cephalic",
    "basilic",
    "dorsal-metacarpal",
    "femoral",
    "jugular",
    "small-saphenous",
    "dorsal-venous-arch",
    "great-saphenous",
]
IV_SITE_CHOICES = [
    choice
    for site in IV_SITE_BASES
    for choice in (f"left-{site}", f"right-{site}")
]
IV_SITE_SIDES = ["left", "right"]
IV_SITE_WIKIPEDIA_ARTICLES = {
    "median-cubital": "Median_cubital_vein",
    "cephalic": "Cephalic_vein",
    "basilic": "Basilic_vein",
    "dorsal-metacarpal": "Dorsal_metacarpal_veins",
    "femoral": "Femoral_vein",
    "jugular": "Jugular_vein",
    "small-saphenous": "Small_saphenous_vein",
    "dorsal-venous-arch": "Dorsal_venous_arch_of_the_foot",
    "great-saphenous": "Great_saphenous_vein",
}
MENU_WIDTH_FACTOR = "0.75"
DAILY_USAGE_TODAY = "today"


class BackRequested(Exception):
    pass


def _normalize_optional_daily_usage_arg(argv: list[str]) -> None:
    for index, arg in enumerate(argv[1:], 1):
        if arg != "--daily-usage":
            continue
        if index == len(argv) - 1 or argv[index + 1].startswith("-"):
            argv[index] = f"--daily-usage={DAILY_USAGE_TODAY}"


_normalize_optional_daily_usage_arg(sys.argv)


class InputMode(str, Enum):
    flags = "flags"
    prompt = "prompt"
    dmenu = "dmenu"
    bemenu = "bemenu"
    fuzzel = "fuzzel"


class LogKind(str, Enum):
    single = "single"
    solution = "solution"
    mixture = "mixture"


class LogConfig(BaseModel):
    user: str | None = None
    userpage: str | None = None
    logfile: Path | None = None
    webhook: HttpUrl | None = None

    @field_validator("logfile", mode="after")
    def valiadte_path(cls, logfile: Path):
        logfile = logfile.expanduser()
        if not logfile.is_absolute():
            return app_dir / logfile
        return logfile


def _unique_aliases(title: str, abbreviations: list[str]) -> list[str]:
    seen = {_alias_key(title)}
    aliases = []
    for abbreviation in abbreviations:
        abbreviation = abbreviation.strip()
        key = _alias_key(abbreviation)
        if not abbreviation or key in seen:
            continue
        seen.add(key)
        aliases.append(abbreviation)
    return aliases


def _cached_abbreviations(value) -> list[str]:
    abbreviations = value.get("Abbreviation") if isinstance(value, dict) else None
    if isinstance(abbreviations, str):
        return [abbreviations]
    if isinstance(abbreviations, list):
        return [item for item in abbreviations if isinstance(item, str)]
    return []


def _load_substance_cache() -> dict[str, dict[str, object]]:
    if not SUBSTANCE_CACHE_FILE.exists():
        return {}
    try:
        with open(SUBSTANCE_CACHE_FILE) as infile:
            raw_cache = json.load(infile)
    except (OSError, json.JSONDecodeError):
        return {}

    cache = raw_cache.get("substances", raw_cache) if isinstance(raw_cache, dict) else {}
    if not isinstance(cache, dict):
        return {}

    valid_cache = {}
    for title, entry in cache.items():
        if not isinstance(title, str) or not isinstance(entry, dict):
            continue
        entry_title = entry.get("Title")
        if not isinstance(entry_title, str):
            entry_title = title
        valid_cache[entry_title] = {
            "Title": entry_title,
            "Abbreviation": _unique_aliases(entry_title, _cached_abbreviations(entry)),
            "synced_at": entry.get("synced_at") if isinstance(entry.get("synced_at"), str) else None,
        }
    return valid_cache


def _write_substance_cache(cache: dict[str, dict[str, object]]) -> None:
    SUBSTANCE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"substances": dict(sorted(cache.items()))}
    tmpfile = SUBSTANCE_CACHE_FILE.with_suffix(f"{SUBSTANCE_CACHE_FILE.suffix}.tmp")
    with open(tmpfile, "w") as outfile:
        json.dump(payload, outfile, indent=2, sort_keys=True)
        outfile.write("\n")
    tmpfile.replace(SUBSTANCE_CACHE_FILE)


def _merge_cached_substance_aliases(cache: dict[str, dict[str, object]] | None = None) -> None:
    for title, entry in (cache or _load_substance_cache()).items():
        existing_aliases = SUBSTANCE_ALIASES.get(title, [])
        SUBSTANCE_ALIASES[title] = _unique_aliases(
            title,
            [*existing_aliases, *_cached_abbreviations(entry)],
        )


def _normalize_api_abbreviations(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _fetch_substance_entry_result(substance: str) -> tuple[dict[str, object] | None, bool]:
    try:
        response = requests.get(f"https://anodyne.wiki/api/substance/{substance}", timeout=5)
        response.raise_for_status()
        data = response.json()
    except Exception:
        return None, False

    if not isinstance(data, dict):
        return None, False
    if data.get("NotFound"):
        return None, True
    title = data.get("Title")
    if not isinstance(title, str) or not title:
        title = substance
    return {
        "Title": title,
        "Abbreviation": _unique_aliases(title, _normalize_api_abbreviations(data.get("Abbreviation"))),
        "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, False


def _fetch_substance_entry(substance: str) -> dict[str, object] | None:
    entry, _ = _fetch_substance_entry_result(substance)
    return entry


def _cache_substance_entry(entry: dict[str, object] | None) -> None:
    if not entry:
        return
    title = entry.get("Title")
    if not isinstance(title, str) or not title:
        return

    cache = _load_substance_cache()
    cache[title] = {
        "Title": title,
        "Abbreviation": _unique_aliases(title, _cached_abbreviations(entry)),
        "synced_at": entry.get("synced_at") if isinstance(entry.get("synced_at"), str) else None,
    }
    _write_substance_cache(cache)
    _merge_cached_substance_aliases({title: cache[title]})

def _logfile_cache_key(logfile: Path) -> str:
    return str(logfile.expanduser().resolve(strict=False))

def _logfile_fingerprint(logfile: Path) -> dict[str, int] | None:
    try:
        stat = logfile.stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _load_prompt_options_cache() -> dict[str, object]:
    if not PROMPT_OPTIONS_CACHE_FILE.exists():
        return {}
    try:
        with open(PROMPT_OPTIONS_CACHE_FILE) as infile:
            raw_cache = json.load(infile)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw_cache, dict):
        return {}
    logs = raw_cache.get("logs")
    return logs if isinstance(logs, dict) else {}


def _write_prompt_options_cache(cache: dict[str, object]) -> None:
    PROMPT_OPTIONS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": PROMPT_OPTIONS_CACHE_VERSION,
        "logs": dict(sorted(cache.items())),
    }
    tmpfile = PROMPT_OPTIONS_CACHE_FILE.with_suffix(f"{PROMPT_OPTIONS_CACHE_FILE.suffix}.tmp")
    with open(tmpfile, "w") as outfile:
        json.dump(payload, outfile, indent=2, sort_keys=True)
        outfile.write("\n")
    tmpfile.replace(PROMPT_OPTIONS_CACHE_FILE)


def _valid_prompt_option_substances(value) -> list[str]:
    if not isinstance(value, list):
        return []
    substances = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        substance = item.strip()
        if not substance or _is_mixture_value(substance) or substance in seen:
            continue
        seen.add(substance)
        substances.append(substance)
    return substances


def _valid_prompt_option_strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    items = []
    seen = set()
    for item in value:
        if not isinstance(item, str):
            continue
        item = item.strip()
        key = _alias_key(item)
        if not item or key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def _valid_prompt_option_dosages(value) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    dosages = {}
    for substance, substance_dosages in value.items():
        if not isinstance(substance, str):
            continue
        valid_dosages = _valid_prompt_option_strings(substance_dosages)
        if valid_dosages:
            dosages[substance] = valid_dosages
    return dosages


def _load_cached_log_choices(logfile: Path) -> dict[str, object] | None:
    entry = _load_prompt_options_cache().get(_logfile_cache_key(logfile))
    if not isinstance(entry, dict):
        return {"substances": [], "dosages": {}, "salts": []} if not logfile.exists() else None
    if entry.get("fingerprint") != _logfile_fingerprint(logfile):
        return {"substances": [], "dosages": {}, "salts": []} if not logfile.exists() else None
    return {
        "substances": _valid_prompt_option_substances(entry.get("substances")),
        "dosages": _valid_prompt_option_dosages(entry.get("dosages")),
        "salts": _valid_prompt_option_strings(entry.get("salts")),
    }


def _store_log_choices(
    logfile: Path,
    substances: list[str],
    dosages: dict[str, list[str]],
    salts: list[str],
) -> None:
    cache = _load_prompt_options_cache()
    cache[_logfile_cache_key(logfile)] = {
        "fingerprint": _logfile_fingerprint(logfile),
        "substances": substances,
        "dosages": dosages,
        "salts": salts,
    }
    _write_prompt_options_cache(cache)


def _add_logged_choice_options(
    logfile: Path,
    substance: str,
    dosage: str,
    salt: str | None,
) -> None:
    raw_entry = _load_prompt_options_cache().get(_logfile_cache_key(logfile))
    entry = raw_entry if isinstance(raw_entry, dict) else {"substances": [], "dosages": {}, "salts": []}
    substances = _valid_prompt_option_substances(entry.get("substances"))
    dosages = _valid_prompt_option_dosages(entry.get("dosages"))
    salts = _valid_prompt_option_strings(entry.get("salts"))

    updated_substances = substances
    for row_substance, row_dosage in zip(
        _split_mixture_values(substance),
        _split_mixture_values(_dosage_without_volume(dosage)),
    ):
        canonical_substance = _resolve_alias(row_substance, SUBSTANCE_ALIASES) or row_substance
        updated_substances = [
            canonical_substance,
            *[item for item in updated_substances if _alias_key(item) != _alias_key(canonical_substance)],
        ]
        substance_dosages = dosages.get(canonical_substance, [])
        dosage_key = _alias_key(row_dosage)
        dosages[canonical_substance] = [
            row_dosage,
            *[item for item in substance_dosages if _alias_key(item) != dosage_key],
        ]

    for row_salt in _split_mixture_values(salt):
        if _is_known_salt_choice(row_salt):
            continue
        salt_key = _alias_key(row_salt)
        salts = [row_salt, *[item for item in salts if _alias_key(item) != salt_key]]

    _store_log_choices(logfile, updated_substances, dosages, salts)


def _sync_choices_from_history(logfile: Path) -> tuple[list[str], dict[str, list[str]], list[str]]:
    _merge_cached_substance_aliases()
    substances, _, _ = _sync_choice_cache_from_history(logfile)

    cache = _load_substance_cache()
    changed = False
    for substance in substances:
        if substance in cache or _is_list_value(substance):
            continue
        entry = _fetch_substance_entry(substance)
        if not entry:
            continue
        title = entry.get("Title")
        if isinstance(title, str):
            cache[title] = entry
            changed = True

    if changed:
        _write_substance_cache(cache)
    _merge_cached_substance_aliases(cache)
    return _sync_choice_cache_from_history(logfile)

TIME_OFFSET_RE = re.compile(
    r"^\s*(?P<amount>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*"
    r"(?P<unit>s|seconds?|m|minutes?|h|hours?|d|days?|w|weeks?|M|months?|y|years?)\s+"
    r"(?P<direction>ago|before)\s*$",
)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    this_month = datetime(year, month, 1, tzinfo=timezone.utc)
    return (next_month - this_month).days


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, _days_in_month(year, month))
    return value.replace(year=year, month=month, day=day)


def _parse_decimal_amount(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise typer.BadParameter(f"Invalid numeric amount: {value}") from exc
    if amount < 0:
        raise typer.BadParameter("Offset amount must be non-negative")
    return amount


def _apply_time_offset(base: datetime, amount: Decimal, unit: str) -> datetime:
    unit_key = unit.casefold()
    if unit == "M" or unit_key.startswith("month"):
        if amount != amount.to_integral_value():
            raise typer.BadParameter("Month offsets must be whole numbers")
        return _add_months(base, -int(amount))
    if unit_key.startswith("y"):
        if amount != amount.to_integral_value():
            raise typer.BadParameter("Year offsets must be whole numbers")
        return _add_months(base, -int(amount) * 12)

    seconds_by_unit = {
        "s": Decimal("1"),
        "second": Decimal("1"),
        "seconds": Decimal("1"),
        "m": Decimal("60"),
        "minute": Decimal("60"),
        "minutes": Decimal("60"),
        "h": Decimal("3600"),
        "hour": Decimal("3600"),
        "hours": Decimal("3600"),
        "d": Decimal("86400"),
        "day": Decimal("86400"),
        "days": Decimal("86400"),
        "w": Decimal("604800"),
        "week": Decimal("604800"),
        "weeks": Decimal("604800"),
    }
    seconds = amount * seconds_by_unit[unit_key]
    return base - timedelta(seconds=float(seconds))


def _parse_offset_time(offset_time: str) -> datetime:
    match = TIME_OFFSET_RE.match(offset_time)
    if not match:
        raise typer.BadParameter(
            "Offset time must look like '<number> <s|seconds|m|minutes|h|hours|d|days|w|weeks|M|months|y|years> <ago|before>'"
        )
    return _apply_time_offset(
        datetime.now(timezone.utc),
        _parse_decimal_amount(match.group("amount")),
        match.group("unit"),
    )


def _parse_set_time(set_time: str | None) -> datetime:
    if set_time is None:
        return datetime.now(timezone.utc)
    parsed: datetime | None = dateparser.parse(
        set_time,
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TO_TIMEZONE": "UTC",
            "PREFER_DATES_FROM": "past",
        },
    )
    if parsed is None:
        raise typer.BadParameter(f"Could not parse '{set_time}' as a date/time")
    return parsed.astimezone(timezone.utc)


def _parse_ingestion_time(set_time: str | None, offset_time: str | None) -> datetime:
    if set_time and offset_time:
        raise typer.BadParameter("--set-time and --offset-time cannot be combined")
    if offset_time:
        return _parse_offset_time(offset_time)
    return _parse_set_time(set_time)


def _menu_default_ingestion_time(mode: InputMode, time: str | None, offset_time: str | None, started_at: datetime) -> datetime | None:
    if mode in (InputMode.dmenu, InputMode.bemenu, InputMode.fuzzel) and not time and not offset_time:
        return started_at
    return None


def _coalesce(*values: str | None) -> str | None:
    for value in values:
        if value:
            return value
    return None


def _alias_key(value: str) -> str:
    return "".join(char for char in value.casefold() if char.isalnum())


def _alias_lookup(aliases: dict[str, list[str]]) -> dict[str, str]:
    lookup = {}
    for canonical, variants in aliases.items():
        lookup[_alias_key(canonical)] = canonical
        for variant in variants:
            lookup[_alias_key(variant)] = canonical
    return lookup


def _resolve_alias(value: str | None, aliases: dict[str, list[str]]) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return stripped
    return _alias_lookup(aliases).get(_alias_key(stripped), stripped)


def _is_list_value(value: str) -> bool:
    return len(_split_bracketed_list_value(value)) > 1


def _is_mixture_value(value: str) -> bool:
    return len(_split_mixture_values(value)) > 1


def _choice_label(canonical: str, aliases: dict[str, list[str]]) -> str:
    variants = aliases.get(canonical)
    if not variants:
        return canonical
    return f"{canonical} ({', '.join(variants)})"


def _menu_candidates(
    candidates: list[str],
    aliases: dict[str, list[str]] | None = None,
) -> tuple[list[str], dict[str, str]]:
    aliases = aliases or {}
    labels = []
    label_values = {}
    seen = set()

    for candidate in candidates:
        if not candidate:
            continue
        canonical = _resolve_alias(candidate, aliases) or candidate
        key = _alias_key(canonical)
        if key in seen:
            continue
        seen.add(key)
        label = _choice_label(canonical, aliases)
        labels.append(label)
        label_values[label] = canonical

    return labels, label_values


def _resolve_input_mode(
    flags: bool,
    prompt: bool,
    dmenu: bool,
    bemenu: bool,
    fuzzel: bool,
) -> InputMode:
    selected = [
        mode
        for enabled, mode in (
            (flags, InputMode.flags),
            (prompt, InputMode.prompt),
            (dmenu, InputMode.dmenu),
            (bemenu, InputMode.bemenu),
            (fuzzel, InputMode.fuzzel),
        )
        if enabled
    ]
    if len(selected) > 1:
        raise typer.BadParameter("Choose only one input mode")
    return selected[0] if selected else InputMode.flags


def _prompt_for_value(label: str, current: str | None = None, optional: bool = False) -> str | None:
    value = typer.prompt(label, default=current or "", show_default=bool(current))
    value = value.strip()
    if value:
        return value
    if optional:
        return None
    raise typer.BadParameter(f"{label} is required")


def _menu_for_value(
    executable: str,
    label: str,
    current: str | None = None,
    candidates: list[str] | None = None,
    optional: bool = False,
    aliases: dict[str, list[str]] | None = None,
    allow_back: bool = False,
) -> str | None:
    if not shutil.which(executable):
        raise typer.BadParameter(f"{executable} was not found in PATH")

    menu_candidates = candidates or ([current] if current else [])
    if allow_back:
        menu_candidates = [*menu_candidates, BACK_CHOICE]
    labels, label_values = _menu_candidates(menu_candidates, aliases)
    menu_input = "\n".join(labels)
    if menu_input:
        menu_input += "\n"
    command = [executable, "-p", label]
    if executable in (InputMode.dmenu.value, InputMode.bemenu.value):
        command.extend(["-i", "-W", MENU_WIDTH_FACTOR])
    if executable == InputMode.fuzzel.value:
        command = [
            executable,
            "--dmenu",
            f"--prompt={label}",
            "--match-mode=fuzzy",
            "--fuzzy-min-length=1",
            "--fuzzy-max-length-discrepancy=4",
            "--fuzzy-max-distance=3",
            "--no-cursor",
        ]
    try:
        completed = subprocess.run(
            command,
            input=menu_input,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise typer.BadParameter(f"Could not run {executable}: {exc}") from exc

    if completed.returncode not in (0, 1):
        raise typer.BadParameter(completed.stderr.strip() or f"{executable} failed")

    value = completed.stdout.strip()
    if allow_back and _alias_key(value) == _alias_key(BACK_CHOICE):
        raise BackRequested
    if value:
        selected = label_values.get(value)
        if selected is None and value.startswith(MENU_CUSTOM_PREFIX):
            selected = value.removeprefix(MENU_CUSTOM_PREFIX).strip()
        return selected or _resolve_alias(value, aliases or {})
    if optional:
        return None
    raise typer.BadParameter(f"{label} is required")


VOLUME_RE = re.compile(r"^\s*(?P<amount>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?P<unit>[a-zA-Zµμ]*)\s*$")
VOLUME_UNIT_TO_ML = {
    "": Decimal("1"),
    "ml": Decimal("1"),
    "milliliter": Decimal("1"),
    "milliliters": Decimal("1"),
    "millilitre": Decimal("1"),
    "millilitres": Decimal("1"),
    "l": Decimal("1000"),
    "liter": Decimal("1000"),
    "liters": Decimal("1000"),
    "litre": Decimal("1000"),
    "litres": Decimal("1000"),
    "kl": Decimal("1000000"),
    "kiloliter": Decimal("1000000"),
    "kiloliters": Decimal("1000000"),
    "kilolitre": Decimal("1000000"),
    "kilolitres": Decimal("1000000"),
    "cl": Decimal("10"),
    "centiliter": Decimal("10"),
    "centiliters": Decimal("10"),
    "centilitre": Decimal("10"),
    "centilitres": Decimal("10"),
    "dl": Decimal("100"),
    "deciliter": Decimal("100"),
    "deciliters": Decimal("100"),
    "decilitre": Decimal("100"),
    "decilitres": Decimal("100"),
    "ul": Decimal("0.001"),
    "µl": Decimal("0.001"),
    "μl": Decimal("0.001"),
    "microliter": Decimal("0.001"),
    "microliters": Decimal("0.001"),
    "microlitre": Decimal("0.001"),
    "microlitres": Decimal("0.001"),
    "nl": Decimal("0.000001"),
    "nanoliter": Decimal("0.000001"),
    "nanoliters": Decimal("0.000001"),
    "nanolitre": Decimal("0.000001"),
    "nanolitres": Decimal("0.000001"),
}


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _route_supports_solution_volume(roa: str) -> bool:
    normalized = roa.strip().casefold().replace("-", " ").replace("_", " ")
    compact = normalized.replace(" ", "")
    return compact in {
        "subcutaneous",
        "subcutaneously",
        "sc",
        "sq",
        "intramuscular",
        "intramuscularly",
        "im",
        "intradermal",
        "intradermally",
        "id",
        "intrarectal",
        "intrarectally",
        "rectal",
        "ir",
        "intravenous",
        "intravenously",
        "iv",
    }


def _format_solution_volume(volume: str | None) -> str | None:
    if not volume:
        return None
    volume = volume.strip()
    if not volume or _alias_key(volume) == _alias_key(UNKNOWN_VOLUME_CHOICE):
        return None
    match = VOLUME_RE.match(volume)
    if not match:
        raise typer.BadParameter(f"Could not parse '{volume}' as a volume")
    amount = _parse_decimal_amount(match.group("amount"))
    unit = match.group("unit").casefold()
    multiplier = VOLUME_UNIT_TO_ML.get(unit)
    if multiplier is None:
        raise typer.BadParameter(f"Unsupported volume unit: {match.group('unit')}")
    return f"{_format_decimal(amount * multiplier)}ml"


def _append_solution_volume(dosage: str, volume: str | None) -> str:
    volume = _format_solution_volume(volume)
    if not volume or "@" in dosage:
        return dosage
    return f"{dosage}@{volume}"


def _split_dosage_unit(current: str | None) -> tuple[str | None, str | None]:
    if not current:
        return None, None
    stripped = current.strip()
    for unit in sorted(UNIT_CHOICES, key=len, reverse=True):
        if stripped.casefold().endswith(unit.casefold()):
            amount = stripped[:-len(unit)].strip()
            return amount or stripped, unit
    return stripped, None


def _format_dosage(amount: str, unit: str | None) -> str:
    amount = amount.strip()
    if _is_custom_dosage(amount):
        return amount
    return f"{amount}{unit or 'mg'}"


def _is_custom_dosage(amount: str) -> bool:
    return any(char.isalpha() for char in amount)


def _prompt_for_dosage(label: str, current: str | None = None, optional: bool = False) -> str:
    current_amount, current_unit = _split_dosage_unit(current)
    amount = _prompt_for_value(label, current_amount, optional=optional)
    if amount is None:
        return ""
    if _is_custom_dosage(amount):
        return amount.strip()
    unit = _prompt_for_value("Unit", current_unit or "mg", optional=False)
    return _format_dosage(amount, unit)


def _menu_for_dosage(
    executable: str,
    label: str,
    current: str | None = None,
    optional: bool = False,
    candidates: list[str] | None = None,
    allow_back: bool = False,
) -> str:
    current_amount, current_unit = _split_dosage_unit(current)
    dosage_candidates = []
    if current:
        dosage_candidates.append(current)
    dosage_candidates.extend(candidates or [])
    while True:
        amount = _menu_for_value(
            executable,
            label,
            current_amount,
            candidates=dosage_candidates,
            optional=optional,
            allow_back=allow_back,
        )
        if amount is None:
            return ""
        selected_amount, selected_unit = _split_dosage_unit(amount)
        if selected_unit:
            return _format_dosage(selected_amount or amount, selected_unit)
        if _is_custom_dosage(amount):
            return amount.strip()
        unit_candidates = [current_unit] if current_unit else []
        unit_candidates.extend(UNIT_CHOICES)
        try:
            unit = _menu_for_value(
                executable,
                "Unit",
                current_unit or "mg",
                candidates=unit_candidates,
                allow_back=allow_back,
            )
        except BackRequested:
            continue
        return _format_dosage(amount, unit)


def _prompt_for_extra_info(note: str | None, time: str | None) -> tuple[str | None, str | None]:
    if note or time:
        return note, time
    if not typer.confirm("Extra options?", default=False):
        return None, None
    return (
        _prompt_for_value("Note", note, optional=True),
        _prompt_for_value("Time offset", time, optional=True),
    )


def _menu_for_extra_info(
    executable: str,
    note: str | None,
    time: str | None,
    allow_back: bool = False,
) -> tuple[str | None, str | None]:
    if note or time:
        return note, time
    while True:
        add_extra_info = _menu_for_value(
            executable,
            "Extra options?",
            "No",
            candidates=["No", "Yes"],
            aliases=EXTRA_INFO_ALIASES,
            optional=True,
            allow_back=allow_back,
        )
        if add_extra_info is None:
            return None, None
        if add_extra_info == "No":
            return None, None
        if add_extra_info == "Yes":
            while True:
                selected_note = _menu_for_value(executable, "Note", note, optional=True, allow_back=allow_back)
                try:
                    selected_time = _menu_for_value(
                        executable,
                        "Time offset",
                        time,
                        candidates=([time] if time else []) + TIME_CHOICES,
                        optional=True,
                        allow_back=allow_back,
                    )
                except BackRequested:
                    continue
                return selected_note, selected_time


def _is_intravenous_route(roa: str) -> bool:
    return (_resolve_alias(roa, ROA_ALIASES) or roa) == "intravenous"


def _split_mixture_values(value: str | None) -> list[str]:
    if not value:
        return []
    value = value.strip()
    list_values = _split_bracketed_list_value(value)
    if list_values:
        return list_values
    value = _strip_wrapping_angle_list(value)
    if ";" in value:
        delimiter = ";"
    elif "/" in value:
        delimiter = "/"
    else:
        delimiter = "+"
    return [part.strip() for part in value.split(delimiter) if part.strip()]


def _strip_wrapping_angle_list(value: str) -> str:
    if value.startswith("<") and value.endswith(">"):
        return value[1:-1].strip()
    if value.startswith("\\<") and value.endswith("\\>"):
        return value[2:-2].strip()
    return value


def _split_bracketed_list_value(value: str | None) -> list[str]:
    if not value:
        return []
    value = value.strip()
    end_index = _bracketed_list_end_index(value)
    if end_index is None:
        return []
    list_value = value[1:end_index]
    try:
        parts = next(csv.reader([list_value], skipinitialspace=True))
    except csv.Error:
        parts = list_value.split(",")
    return [part.strip() for part in parts if part.strip()]


def _split_mixture_values_for_count(value: str | None, count: int) -> list[str]:
    values = _split_mixture_values(value)
    if count <= 0 or len(values) <= count:
        return values

    stripped = (value or "").strip()
    if _bracketed_list_end_index(stripped) is None:
        return values

    overflow = len(values) - count + 1
    return f"[{",".join(values[:overflow]), *values[overflow:]}]"


def _bracketed_list_end_index(value: str) -> int | None:
    if not value.startswith("["):
        return None
    in_quotes = False
    index = 1
    while index < len(value):
        char = value[index]
        if char == '"':
            if in_quotes and index + 1 < len(value) and value[index + 1] == '"':
                index += 2
                continue
            in_quotes = not in_quotes
        elif char == "]" and not in_quotes:
            return index
        index += 1
    return None


def _dosage_without_volume(dosage: str) -> str:
    dosage = dosage.strip()
    list_end_index = _bracketed_list_end_index(dosage)
    if list_end_index is not None:
        return dosage[:list_end_index + 1]
    if "@" not in dosage:
        return dosage
    before, after = [part.strip() for part in dosage.split("@", 1)]
    return after if before.endswith("ml") and after else before


def _volume_from_dosage(dosage: str) -> str | None:
    if "@" not in dosage:
        return None
    before, after = [_clean_volume_choice(part) for part in dosage.rsplit("@", 1)]
    if _is_volume_value(after):
        return after
    if _is_volume_value(before):
        return before
    return None


def _clean_volume_choice(value: str) -> str:
    return value.strip().strip('"').strip("'").strip()


def _is_volume_value(value: str) -> bool:
    match = VOLUME_RE.match(value)
    if not match:
        return False
    return match.group("unit").casefold() in VOLUME_UNIT_TO_ML


DOSAGE_AMOUNT_RE = re.compile(
    r"^\s*~?\s*(?P<amount>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?P<unit>[a-zA-Z\u00b5\u03bc]+)\b"
)


def _parse_daily_usage_date(value: str) -> date:
    if value == DAILY_USAGE_TODAY:
        return datetime.now(timezone.utc).date()
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise typer.BadParameter("Daily usage date must use d.m.y format, e.g. 23.9.2026")


def _parse_log_timestamp_date(value: str) -> date | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date()


def _normalize_usage_unit(amount: Decimal, unit: str) -> tuple[Decimal, str] | None:
    unit_key = unit.casefold().replace("\u00b5", "u").replace("\u03bc", "u")
    if unit_key == "g":
        return amount * Decimal("1000"), "mg"
    if unit_key == "mg":
        return amount, "mg"
    if unit_key in {"ug", "mcg"}:
        return amount, "ug"
    if unit_key == "ml":
        return amount, "ml"
    return None


def _parse_usage_dosage(dosage: str) -> tuple[Decimal, str] | None:
    match = DOSAGE_AMOUNT_RE.match(dosage)
    if not match:
        return None
    try:
        amount = Decimal(match.group("amount"))
    except InvalidOperation:
        return None
    return _normalize_usage_unit(amount, match.group("unit"))


def _collect_daily_usage(logfile: Path, usage_date: date) -> tuple[dict[tuple[str, str], Decimal], int, int]:
    totals: dict[tuple[str, str], Decimal] = {}
    counted_rows = 0
    skipped_dosages = 0
    if not logfile.exists():
        return totals, counted_rows, skipped_dosages

    with open(logfile, newline="") as infile:
        for row in csv.reader(infile):
            if len(row) <= 3 or row[0].strip().lower() == "timestamp":
                continue
            if _parse_log_timestamp_date(row[0]) != usage_date:
                continue

            counted_rows += 1
            dosages = _split_mixture_values(_dosage_without_volume(row[3]))
            substances = _split_mixture_values_for_count(row[2], len(dosages))
            for substance, dosage in zip(substances, dosages):
                parsed_dosage = _parse_usage_dosage(dosage)
                if parsed_dosage is None:
                    skipped_dosages += 1
                    continue
                amount, unit = parsed_dosage
                key = (substance, unit)
                totals[key] = totals.get(key, Decimal("0")) + amount

    return totals, counted_rows, skipped_dosages


def _format_daily_usage_date(value: date) -> str:
    return f"{value.day}.{value.month}.{value.year}"


def _print_daily_usage(logfile: Path, value: str) -> None:
    usage_date = _parse_daily_usage_date(value)
    totals, counted_rows, skipped_dosages = _collect_daily_usage(logfile, usage_date)
    con.print(f"Daily usage for {_format_daily_usage_date(usage_date)}:")
    if not totals:
        con.print("No usage found.")
    else:
        for (substance, unit), amount in sorted(
            totals.items(),
            key=lambda item: (item[0][0].casefold(), item[0][1]),
        ):
            con.print(f"{substance}: {_format_decimal(amount)}{unit}")
    con.print(f"Rows counted: {counted_rows}")
    if skipped_dosages:
        con.print(f"Skipped dosages: {skipped_dosages}", style="yellow")


def _format_list_value(values: list[str | None]) -> str | None:
    present = [value for value in values if value]
    if not present:
        return None
    return '/'.join(_format_list_item(value) for value in present)


def _format_wrapped_list_value(values: list[str | None]) -> str | None:
    value = _format_list_value(values)
    return f"<{value}>" if value else None


def _format_substance_mixture_value(values: list[str | None]) -> str | None:
    present = [value.strip() for value in values if value and value.strip()]
    if not present:
        return None
    return f"<{'/'.join(present)}>"


def _format_list_item(value: str) -> str:
    if not any(char in value for char in ',[]"'):
        return value
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow([value])
    return buffer.getvalue()


def _is_known_salt_choice(salt: str) -> bool:
    resolved = _resolve_alias(salt, SALT_ALIASES)
    if not resolved:
        return False
    known_keys = {_alias_key(choice) for choice in SALT_CHOICES}
    known_keys.update(_alias_key(choice) for choice in SALT_ALIASES)
    return _alias_key(resolved) in known_keys


def _format_salt_for_log(salt: str | None) -> str | None:
    if not salt:
        return None
    return salt.strip()


def _format_mixture(
    substances: list[str],
    dosages: list[str],
    salts: list[str | None],
    volume_ml: str | None,
) -> tuple[str, str, str | None]:
    sub_len = 0
    for sub in substances:
        if not sub is None:
            sub_len += 1
    dos_len = 0
    for dos in dosages:
        if not dos is None:
            dos_len += 1
    sal_len = 0
    for sal in salts:
        if not sal is None:
            sal_len += 1
    if sub_len != dos_len:
        raise typer.BadParameter("Mixture substance and dosage counts must match")
    if sal_len > sub_len:
        raise typer.BadParameter("Mixture salt count cannot exceed substance count")
    if sal_len < sub_len:
        salts = [*salts, *([None] * (len(substances) - len(salts)))]

    resolved_substances = [
        _resolve_alias(substance, SUBSTANCE_ALIASES) or substance
        for substance in substances
    ]
    resolved_salts = [
        _format_salt_for_log(_resolve_alias(salt, SALT_ALIASES)) if salt else None
        for salt in salts
    ]
    resolved_dosages = [dosage.strip() for dosage in dosages]
    dosage_value = _format_wrapped_list_value(resolved_dosages)
    volume_ml = _format_solution_volume(volume_ml)
    if volume_ml:
        dosage_value = f"{dosage_value}@{volume_ml}"
    return (
        _format_substance_mixture_value(resolved_substances),
        dosage_value,
        _format_wrapped_list_value(resolved_salts),
    )


def _split_iv_site(site: str | None) -> tuple[str | None, str | None]:
    if not site:
        return None, None
    for side in IV_SITE_SIDES:
        prefix = f"{side}-"
        if site.startswith(prefix):
            return site.removeprefix(prefix), side
    return site, None


def _format_iv_site(vein: str | None, side: str | None) -> str | None:
    if not vein:
        return None
    if side:
        return f"{side}-{vein}"
    return vein


def _plain_markdown_link_label(value: str) -> str:
    match = re.fullmatch(r"\[(?P<label>[^\]]+)\]\([^)]+\)", value.strip())
    return match.group("label") if match else value


def _iv_site_key(vein: str) -> str:
    return _plain_markdown_link_label(vein).strip().casefold().replace("_", "-").replace(" ", "-")


def _format_webhook_site(roa: str, site: str | None) -> str | None:
    if not site or not _is_intravenous_route(roa):
        return site

    vein, side = _split_iv_site(site)
    if not vein:
        return site

    article = IV_SITE_WIKIPEDIA_ARTICLES.get(_iv_site_key(vein))
    if not article:
        return site

    label = _format_markdown_link_label(_plain_markdown_link_label(vein))
    linked_vein = f"[{label}](https://en.wikipedia.org/wiki/{article})"
    return f"{side}-{linked_vein}" if side else linked_vein


def _prompt_for_site(roa: str, current: str | None = None) -> str | None:
    if not _is_intravenous_route(roa):
        return current
    current_vein, current_side = _split_iv_site(current)
    vein = _prompt_for_value("Vein", current_vein, optional=True)
    if not vein:
        return None
    side = _prompt_for_value("Side", current_side, optional=False)
    return _format_iv_site(vein, side)


def _menu_for_site(
    executable: str,
    roa: str,
    current: str | None = None,
    allow_back: bool = False,
) -> str | None:
    if not _is_intravenous_route(roa):
        return current
    current_vein, current_side = _split_iv_site(current)
    vein_candidates = [current_vein] if current_vein else []
    vein_candidates.extend(IV_SITE_BASES)
    while True:
        vein = _menu_for_value(
            executable,
            "Vein",
            current_vein,
            candidates=vein_candidates,
            optional=True,
            allow_back=allow_back,
        )
        if not vein:
            return None
        side_candidates = [current_side] if current_side else []
        side_candidates.extend(IV_SITE_SIDES)
        try:
            side = _menu_for_value(
                executable,
                "Side",
                current_side,
                candidates=side_candidates,
                optional=False,
                allow_back=allow_back,
            )
        except BackRequested:
            continue
        return _format_iv_site(vein, side)


def _menu_for_route(executable: str, current: str | None = None) -> str:
    roa_candidates = [current] if current else []
    roa_candidates.extend(ROA_ALIASES)
    roa = _menu_for_value(
        executable,
        "Route",
        current,
        candidates=roa_candidates,
        aliases=ROA_ALIASES,
    )
    if not roa:
        raise typer.BadParameter("Route of administration is required")
    return roa


def _append_recent_unique(items: list[str], item: str, aliases: dict[str, list[str]] | None = None) -> None:
    item = item.strip()
    if not item:
        return
    comparable = _resolve_alias(item, aliases or {}) or item
    key = _alias_key(comparable)
    items[:] = [existing for existing in items if _alias_key(_resolve_alias(existing, aliases or {}) or existing) != key]
    #err_con.print(f"adding: {comparable}")
    items.insert(0, comparable)

def _scan_logged_choices(logfile: Path) -> tuple[list[str], dict[str, list[str]], list[str]]:
    if not logfile.exists():
        return [], {}, []

    substances = []
    dosages: dict[str, list[str]] = {}
    salts = []
    with open(logfile, newline="") as infile:
        for row in csv.reader(infile):
            if len(row) <= 3 or row[2].strip().lower() in ("substance", "med"):
                continue

            row_substances = [
                _resolve_alias(item, SUBSTANCE_ALIASES) or item
                for item in _split_mixture_values(row[2])
            ]
            row_dosages = _split_mixture_values(_dosage_without_volume(row[3]))
            row_salts = _split_mixture_values(row[6] if len(row) > 6 else None)

            for row_substance in row_substances:
                _append_recent_unique(substances, row_substance, SUBSTANCE_ALIASES)

            for row_substance, row_dosage in zip(row_substances, row_dosages):
                substance_dosages = dosages.setdefault(row_substance, [])
                _append_recent_unique(substance_dosages, row_dosage)

            for row_salt in row_salts:
                if not _is_known_salt_choice(row_salt):
                    _append_recent_unique(salts, row_salt)

    return substances, dosages, salts

def _sync_choice_cache_from_history(logfile: Path) -> tuple[list[str], dict[str, list[str]], list[str]]:
    substances, dosages, salts = _scan_logged_choices(logfile)
    _store_log_choices(logfile, substances, dosages, salts)
    return substances, dosages, salts

def _read_logged_dosages_for_substance(logfile: Path, substance: str) -> list[str]:
    cached_choices = _load_cached_log_choices(logfile)
    if cached_choices is not None:
        target_key = _alias_key(_resolve_alias(substance, SUBSTANCE_ALIASES) or substance)
        for cached_substance, cached_dosages in _valid_prompt_option_dosages(
            cached_choices.get("dosages")
        ).items():
            cached_key = _alias_key(_resolve_alias(cached_substance, SUBSTANCE_ALIASES) or cached_substance)
            if cached_key == target_key:
                return cached_dosages
        return []

    if not logfile.exists():
        return []

    target_key = _alias_key(_resolve_alias(substance, SUBSTANCE_ALIASES) or substance)
    dosages = []
    with open(logfile, newline="") as infile:
        for row in csv.reader(infile):
            if len(row) <= 3 or row[2].strip().lower() in ("substance", "med"):
                continue
            row_substances = [
                _resolve_alias(item, SUBSTANCE_ALIASES) or item
                for item in _split_mixture_values(row[2])
            ]
            row_dosages = _split_mixture_values(_dosage_without_volume(row[3]))
            for row_substance, row_dosage in zip(row_substances, row_dosages):
                if _alias_key(row_substance) == target_key:
                    dosages.append(row_dosage)

    seen = set()
    unique = []
    for dosage in reversed(dosages):
        key = _alias_key(dosage)
        if key in seen:
            continue
        seen.add(key)
        unique.append(dosage)
    return unique


def _read_logged_substances(logfile: Path) -> list[str]:
    cached_choices = _load_cached_log_choices(logfile)
    if cached_choices is not None:
        return _valid_prompt_option_substances(cached_choices.get("substances"))

    substances, _, _ = _sync_choice_cache_from_history(logfile)
    return substances


def _read_logged_salts(logfile: Path) -> list[str]:
    cached_choices = _load_cached_log_choices(logfile)
    if cached_choices is not None:
        return _valid_prompt_option_strings(cached_choices.get("salts"))
    _, _, salts = _sync_choice_cache_from_history(logfile)
    return salts


def _read_logged_volumes(logfile: Path) -> list[str]:
    if not logfile.exists():
        return []

    volumes = []
    with open(logfile, newline="") as infile:
        for row in csv.reader(infile):
            if len(row) <= 3 or row[2].strip().lower() in ("substance", "med"):
                continue
            volume = _volume_from_dosage(row[3])
            if volume:
                _append_recent_unique(volumes, volume)
    return volumes


def _menu_substance_candidates(logfile: Path, current: str | None = None) -> list[str]:
    candidates = []
    seen = set()
    for candidate in [current, *_read_logged_substances(logfile)]:
        if not candidate:
            continue
        key = _alias_key(_resolve_alias(candidate, SUBSTANCE_ALIASES) or candidate)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def _collect_substances_prompt(
    substance: str | None,
    dosage: str | None,
    salt: str | None,
) -> tuple[list[str], list[str], list[str | None]]:
    substances = []
    dosages = []
    salts = []
    seeded_substances = _split_mixture_values(substance)
    seeded_dosages = _split_mixture_values(dosage)
    seeded_salts = _split_mixture_values(salt)

    index = 0
    while True:
        index += 1
        current_substance = seeded_substances[index - 1] if index <= len(seeded_substances) else None
        current_dosage = seeded_dosages[index - 1] if index <= len(seeded_dosages) else None
        current_salt = seeded_salts[index - 1] if index <= len(seeded_salts) else None

        substance_label = f"Substance[{index}]"
        default_substance = current_substance
        if index > 1 and not default_substance:
            default_substance = DONE_SUBSTANCE_CHOICE
        component_substance = _prompt_for_value(
            substance_label,
            default_substance,
            optional=False,
        )

        if _alias_key(component_substance) == _alias_key(DONE_SUBSTANCE_CHOICE):
            break

        substances.append(_resolve_alias(component_substance, SUBSTANCE_ALIASES) or component_substance)
        dosages.append(_prompt_for_dosage("Dosage", current_dosage))
        salts.append(
            _resolve_alias(
                _prompt_for_value("Salt", current_salt, optional=True),
                SALT_ALIASES,
            )
        )

    return substances, dosages, salts


def _collect_substances_menu(
    executable: str,
    logfile: Path,
    substance: str | None,
    dosage: str | None,
    salt: str | None,
    allow_back: bool = False,
) -> tuple[list[str], list[str], list[str | None]]:
    substances = []
    dosages = []
    salts = []
    seeded_substances = _split_mixture_values(substance)
    seeded_dosages = _split_mixture_values(dosage)
    seeded_salts = _split_mixture_values(salt)

    index = 0
    while True:
        index += 1
        while True:
            current_substance = seeded_substances[index - 1] if index <= len(seeded_substances) else None
            current_dosage = seeded_dosages[index - 1] if index <= len(seeded_dosages) else None
            current_salt = seeded_salts[index - 1] if index <= len(seeded_salts) else None
            candidates = _menu_substance_candidates(logfile, current_substance)
            selected_substance_keys = {
                _alias_key(_resolve_alias(selected_substance, SUBSTANCE_ALIASES) or selected_substance)
                for selected_substance in substances
            }
            candidates = [
                candidate
                for candidate in candidates
                if (
                    candidate == current_substance
                    or _alias_key(_resolve_alias(candidate, SUBSTANCE_ALIASES) or candidate)
                    not in selected_substance_keys
                )
            ]
            if index > 1:
                candidates = [DONE_SUBSTANCE_CHOICE, *candidates]

            try:
                component_substance = _menu_for_value(
                    executable,
                    f"Substance[{index}]",
                    DONE_SUBSTANCE_CHOICE if index > 1 else current_substance,
                    candidates=candidates,
                    aliases=SUBSTANCE_ALIASES,
                    optional=index > 1,
                    allow_back=allow_back,
                )
            except BackRequested:
                if index == 1:
                    raise
                if substances:
                    substances.pop()
                if dosages:
                    dosages.pop()
                if salts:
                    salts.pop()
                index = max(index - 2, 0)
                break
            if component_substance is None or _alias_key(component_substance) == _alias_key(DONE_SUBSTANCE_CHOICE):
                return substances, dosages, salts

            while True:
                try:
                    component_dosage = _menu_for_dosage(
                        executable,
                        "Dosage",
                        current_dosage,
                        candidates=_read_logged_dosages_for_substance(logfile, component_substance),
                        allow_back=allow_back,
                    )
                except BackRequested:
                    break

                salt_candidates = [current_salt] if current_salt else []
                salt_candidates.extend(SALT_CHOICES)
                salt_candidates.extend(_read_logged_salts(logfile))
                try:
                    component_salt = _menu_for_value(
                        executable,
                        "Salt",
                        current_salt,
                        candidates=salt_candidates,
                        optional=True,
                        aliases=SALT_ALIASES,
                        allow_back=allow_back,
                    )
                except BackRequested:
                    continue

                substances.append(component_substance)
                dosages.append(component_dosage)
                salts.append(component_salt)
                break
            else:
                continue
            if len(substances) == index:
                break
        if len(substances) != index:
            index = max(index - 1, 0)
            continue

    return substances, dosages, salts


def _format_single_solution_component(
    substance: str,
    dosage: str,
    salt: str | None,
    volume: str | None,
) -> tuple[str, str, str | None]:
    return (
        _resolve_alias(substance, SUBSTANCE_ALIASES) or substance,
        _append_solution_volume(dosage.strip(), volume),
        _format_salt_for_log(_resolve_alias(salt, SALT_ALIASES)) if salt else None,
    )


def _format_collected_substances(
    substances: list[str],
    dosages: list[str],
    salts: list[str | None],
    volume: str | None,
) -> tuple[LogKind, str, str, str | None]:
    if len(substances) > 1:
        substance, dosage, salt = _format_mixture(substances, dosages, salts, volume)
        return LogKind.mixture, substance, dosage, salt

    substance, dosage, salt = _format_single_solution_component(
        substances[0],
        dosages[0],
        salts[0] if salts else None,
        volume,
    )
    kind = LogKind.solution if "@" in dosage else LogKind.single
    return kind, substance, dosage, salt


def _collect_input(
    mode: InputMode,
    logfile: Path,
    substance: str | None,
    dosage: str | None,
    roa: str | None,
    site: str | None,
    salt: str | None,
    note: str | None,
    time: str | None,
    volume: str | None,
) -> tuple[LogKind, str, str, str, str | None, str | None, str | None, str | None]:
    if mode == InputMode.flags:
        substances = _split_mixture_values(substance)
        dosages = _split_mixture_values(dosage)
        salts = _split_mixture_values(salt)
        is_mixture = len(substances) > 1 or len(dosages) > 1 or len(salts) > 1
        roa = _resolve_alias(roa, ROA_ALIASES)

        missing = [
            label
            for label, value in (
                ("substance", substance),
                ("dosage", dosage),
                ("roa", roa),
            )
            if not value
        ]
        if missing:
            raise typer.BadParameter(f"Missing required input field(s): {', '.join(missing)}")

        if is_mixture:
            substance, dosage, salt = _format_mixture(substances, dosages, salts, volume)
            return LogKind.mixture, substance, dosage, roa, site, salt, note, time

        substance = _resolve_alias(substance, SUBSTANCE_ALIASES)
        salt = _format_salt_for_log(_resolve_alias(salt, SALT_ALIASES))
        dosage = dosage.strip()
        if _route_supports_solution_volume(roa):
            dosage = _append_solution_volume(dosage, volume)
        kind = LogKind.solution if "@" in dosage else LogKind.single
        return kind, substance, dosage, roa, site, salt, note, time

    if mode == InputMode.prompt:
        roa = _resolve_alias(_prompt_for_value("Route", roa), ROA_ALIASES)
        volume = _prompt_for_value("Volume", volume or UNKNOWN_VOLUME_CHOICE, optional=False)
        site = _prompt_for_site(roa, site)
        substances, dosages, salts = _collect_substances_prompt(
            substance,
            dosage,
            salt,
        )
        kind, substance, dosage, salt = _format_collected_substances(
            substances,
            dosages,
            salts,
            volume,
        )
        note, time = _prompt_for_extra_info(note, time)
        return (
            kind,
            substance,
            dosage,
            roa,
            site,
            salt,
            note,
            time,
        )

    while True:
        menu = mode.value
        roa = _menu_for_route(menu, roa)
        while True:
            try:
                volume_ml = _menu_for_value(
                    menu,
                    "Volume",
                    volume,
                    candidates=[
                        *([volume] if volume else []),
                        *_read_logged_volumes(logfile),
                        UNKNOWN_VOLUME_CHOICE,
                    ],
                    allow_back=True,
                )
            except BackRequested:
                break
            while True:
                try:
                    site = _menu_for_site(menu, roa, site, allow_back=True)
                except BackRequested:
                    volume = volume_ml
                    break
                while True:
                    try:
                        substances, dosages, salts = _collect_substances_menu(
                            menu,
                            logfile,
                            substance,
                            dosage,
                            salt,
                            allow_back=True,
                        )
                    except BackRequested:
                        break
                    while True:
                        kind, substance, dosage, salt = _format_collected_substances(
                            substances,
                            dosages,
                            salts,
                            volume_ml,
                        )
                        try:
                            note, time = _menu_for_extra_info(menu, note, time, allow_back=True)
                        except BackRequested:
                            break
                        return (
                            kind,
                            substance,
                            dosage,
                            roa,
                            site,
                            salt,
                            note,
                            time,
                        )


def _require_option(label: str, value):
    if value is None:
        raise typer.BadParameter(f"Missing required option: {label}")
    return value


def _format_markdown_link_label(label: str) -> str:
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _format_substance_link(title: str, label: str | None = None) -> str:
    title_slug = title.replace(" ", "_")
    link_label = _format_markdown_link_label(label or title)
    return f"[{link_label}](https://anodyne.wiki/substance/{title_slug})"


def _resolve_cached_substance_title(substance: str) -> str | None:
    lookup = _alias_lookup(SUBSTANCE_ALIASES)
    return lookup.get(_alias_key(substance))


def _lookup_single_substance_title(substance: str, accept = None) -> tuple[str, str]:
    entry, not_found = _fetch_substance_entry_result(substance)
    if entry:
        _cache_substance_entry(entry)
        title = entry["Title"] if isinstance(entry["Title"], str) else substance
        return title, _format_substance_link(title)

    cached_title = _resolve_cached_substance_title(substance)
    if cached_title:
        return cached_title, _format_substance_link(cached_title)

    if not_found:
        err_con.print(f"Substance '{substance}' not found on AnodyneWiki", style="bold yellow")
        if accept == "confirm":
            _ =  typer.confirm("Log anyway?", abort=True)
        elif accept == "always":
            err_con.print(f"By default accepting unindexed substance")
            _ = True
        elif accept == "strict":
            err_con.print(f"Rejecting unindexed substance due to stict-mode")
            _ = False
        else:
            _ = False
    return substance, substance


def _format_linked_substance_mixture(titles: list[str], linked_titles: list[str]) -> str:
    if len(titles) <= 1:
        return linked_titles[0] if linked_titles else ""

    formatted_links = []
    for title, linked_title in zip(titles, linked_titles):
        if linked_title == title:
            formatted_links.append(title)
        else:
            formatted_links.append(_format_substance_link(title))
    return f"\\<{'/'.join(formatted_links)}\\>"


def _lookup_substance_title(_kind: LogKind, substance: str, accept = None) -> tuple[str, str]:
    substances = _split_mixture_values(substance)
    if len(substances) <= 1:
        return _lookup_single_substance_title(substance, accept=accept)

    titles = []
    linked_titles = []
    for component in substances:
        title, linked_title = _lookup_single_substance_title(component, accept=accept)
        titles.append(title)
        linked_titles.append(linked_title)

    title = _format_substance_mixture_value(titles) or substance
    return title, _format_linked_substance_mixture(titles, linked_titles)


def _validate_config(param_value: Path | None):
    default_conf = app_dir / "config.toml"
    if not default_conf.exists():
        default_conf.parent.mkdir(parents=True, exist_ok=True)
        default_conf.touch()

    if not param_value:
        conf = toml_loader(default_conf)
    else:
        conf = toml_loader(param_value)

    return LogConfig.model_validate(conf).model_dump()

@app.command()
def log_ingestion(
    substance_arg: Annotated[str | None, typer.Argument(help="substance to log")] = None,
    dosage_arg: Annotated[str | None, typer.Argument(help="dosage and unit")] = None,
    roa_arg: Annotated[str | None, typer.Argument(help="route of administration")] = None,
    time_arg: Annotated[list[str] | None, typer.Argument(
        help="ingestion time, e.g. 'two hours ago' or '2025-12-04 10:00'"
    )] = None,
    substance_opt: Annotated[str | None, typer.Option(
        "--substance",
        help="substance to log",
        rich_help_panel="Input Options",
    )] = None,
    dosage_opt: Annotated[str | None, typer.Option(
        "--dosage",
        help="dosage and unit",
        rich_help_panel="Input Options",
    )] = None,
    roa_opt: Annotated[str | None, typer.Option(
        "--roa",
        help="route of administration",
        rich_help_panel="Input Options",
    )] = None,
    offset_time: Annotated[str | None, typer.Option(
        "--offset-time",
        help="relative ingestion time, e.g. '2 hours ago' or '30 m before'",
        rich_help_panel="Input Options",
    )] = None,
    set_time: Annotated[str | None, typer.Option(
        "--set-time",
        help="ingestion timestamp, e.g. '2025-12-04 10:00' or 'yesterday at 10pm'",
        rich_help_panel="Input Options",
    )] = None,
    volume_opt: Annotated[str | None, typer.Option(
        "--volume",
        help="solution volume to append to dosage for injection/rectal routes",
        rich_help_panel="Input Options",
    )] = None,
    flags_mode: Annotated[bool, typer.Option(
        "--flags",
        help="read all ingestion fields from positional arguments and flags",
        rich_help_panel="Input Mode",
    )] = False,
    prompt_mode: Annotated[bool, typer.Option(
        "--prompt",
        help="prompt for ingestion fields on stdin",
        rich_help_panel="Input Mode",
    )] = False,
    #verbose_mode: Annotated[bool, typer.Option(
    #    "--verbose",
    #    help="enable verbose logging.",
    #    rich_help_panel="Verbose Mode",
    #)] = False,
    strict_mode: Annotated[bool, typer.Option(
        "--strict",
        help="only accept substances found in AnodyneWiki's API",
        rich_help_panel="Strict Mode",
    )] = False,
    dmenu_mode: Annotated[bool, typer.Option(
        "--dmenu", "-md",
        help="prompt for ingestion fields with dmenu selectors",
        rich_help_panel="Input Mode",
    )] = False,
    bemenu_mode: Annotated[bool, typer.Option(
        "--bemenu", "-mb",
        help="prompt for ingestion fields with bemenu selectors",
        rich_help_panel="Input Mode",
    )] = False,
    fuzzel_mode: Annotated[bool, typer.Option(
        "--fuzzel",
        help="prompt for ingestion fields with fuzzel selectors",
        rich_help_panel="Input Mode",
    )] = False,
    sync_choices: Annotated[bool, typer.Option(
        "--sync-choices",
        help="scan the CSV log and refresh menu choices, dosages, salts, and substance abbreviations",
        rich_help_panel="Input Mode",
    )] = False,
    daily_usage: Annotated[str | None, typer.Option(
        "--daily-usage",
        help="sum usage for a day from the CSV log, using d.m.y format; defaults to today when passed without a value",
        rich_help_panel="Input Mode",
    )] = None,
    logfile: Annotated[Path | None, typer.Option(
        "--csv",
        help="File to write logs to",
        rich_help_panel="Configuration Options"
    )] = None,
    user: Annotated[str | None, typer.Option(
        "--user", "-u",
        help="username",
        rich_help_panel="Configuration Options"
    )] = None,
    userpage: Annotated[str | None, typer.Option(
        "--userpage", "-up",
        help="url linked to the username in the discord message",
        rich_help_panel="Configuration Options"
    )] = None,
    salt: Annotated[str | None, typer.Option("--salt", "-sa", help="salt form")] = None,
    site: Annotated[str | None, typer.Option("--site", "-si", help="site of administration")] = None,
    note: Annotated[str | None, typer.Option("--note", "-n", help="note added to discord message")] = None,
    webhook: Annotated[HttpUrl | None, typer.Option(
        "--webhook", "-w",
        parser=HttpUrl,
        help="discord webhook url",
        rich_help_panel="Configuration Options"
    )] = None,
    config: Annotated[Path | None, typer.Option(
        "--config", "-c",
        callback=conf_callback_factory(_validate_config),
        show_default=str(Path(typer.get_app_dir("logpy")) / "config.toml"),
        envvar="LOGPY_CONFIG",
        help="configuration file",
        rich_help_panel="Configuration Options",
        is_eager=True,
    )] = None
):
    started_at = datetime.now(timezone.utc)
    logfile = _require_option("--csv", logfile)
    if daily_usage:
        _print_daily_usage(logfile, daily_usage)
        return

    mode = _resolve_input_mode(flags_mode, prompt_mode, dmenu_mode, bemenu_mode, fuzzel_mode)
    _merge_cached_substance_aliases()
    if sync_choices:
        synced_substances, synced_dosages, synced_salts = _sync_choices_from_history(logfile)
        con.print(
            "Synced choices: "
            f"{len(synced_substances)} substances, "
            f"{sum(len(items) for items in synced_dosages.values())} dosages, "
            f"{len(synced_salts)} non-indexed salt forms."
        )
        has_ingestion_input = any(
            (
                substance_arg,
                dosage_arg,
                roa_arg,
                time_arg,
                substance_opt,
                dosage_opt,
                roa_opt,
                offset_time,
                set_time,
                volume_opt,
                prompt_mode,
                dmenu_mode,
                bemenu_mode,
                fuzzel_mode,
                flags_mode,
            )
        )
        if not has_ingestion_input:
            return

    user = _require_option("--user", user)
    kind, substance, dosage, roa, site, salt, note, time = _collect_input(
        mode,
        logfile,
        _coalesce(substance_arg, substance_opt),
        _coalesce(dosage_arg, dosage_opt),
        _coalesce(roa_arg, roa_opt),
        site,
        salt,
        note,
        _coalesce(" ".join(time_arg) if time_arg else None, set_time),
        volume_opt,
    )
    ingestion_time = (
        _menu_default_ingestion_time(mode, time, offset_time, started_at)
        or _parse_ingestion_time(time, offset_time)
    )
    time_now = ingestion_time.isoformat(timespec='milliseconds')
    is_backdated = datetime.now(timezone.utc) - ingestion_time > timedelta(minutes=10)
    title = substance_md = substance

    if not logfile.exists():
        logfile.parent.mkdir(parents=True, exist_ok=True)
        logfile.touch()
        con.print(f"File '{logfile}' has been created.")

    if mode == InputMode.prompt:
        accept_mode = "confirm"
    elif strict_mode == False:
        accept_mode = "always"
    else:
        accept_mode = "strict"
    title, substance_md = _lookup_substance_title(kind, substance, accept=accept_mode )

    with open(logfile, "a", newline="") as of:
        log = csv.writer(of)
        log.writerow([time_now, user, title, dosage, roa, site, salt, note])
    _add_logged_choice_options(logfile, title, dosage, salt)

    if not webhook:
        return

    user_md = f"[{user}]({userpage})" if userpage else user
    if kind == LogKind.mixture:
        logline = f"{user_md}: {dosage} {substance_md}" + (
            f" {salt.replace('<','[').replace('>',']')}" if salt else ""
        )
    else:
        logline = f"{user_md}: {dosage} {substance_md}" + (
            f" [{salt}]" if salt else ""
        )
    webhook_site = _format_webhook_site(roa, site)
    logline += f" via {roa}" + (
        f" at {webhook_site} site" if webhook_site else ""
    ) + (
        f"\n> {note}" if note else ""
    ) + (
        f"\n> {time_now}" if is_backdated else ""
    )

    try:
        p = requests.post(webhook.encoded_string(), json={ "content": logline, "flags": 4})
        p.raise_for_status()
        con.print(f"Status: {p.status_code}")
    except Exception as e:
        err_con.print(f"Webhook failed: '{e}'", style="bold yellow")
