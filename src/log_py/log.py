import csv
import dateparser
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
import requests
from pydantic import BaseModel, HttpUrl, field_validator
from typer_config import toml_loader, conf_callback_factory
from rich.console import Console

con = Console()
err_con = Console(stderr=True)
app = typer.Typer(add_completion=True, no_args_is_help=True)
app_dir = Path(typer.get_app_dir("logpy"))
SUBSTANCE_CACHE_FILE = app_dir / "substances.json"
PROMPT_OPTIONS_CACHE_FILE = app_dir / "prompt-options.json"
PROMPT_OPTIONS_CACHE_VERSION = 1
DONE_SUBSTANCE_CHOICE = "Done"
SUBSTANCE_ALIASES = {
    "2c-b": ["2cb", "2-cb"],
    "Amphetamine": ["amph", "speed"],
    "Caffeine": ["caf", "coffee"],
    "Cannabis": ["weed", "thc", "marijuana", "hash"],
    "Cocaine": ["coke", "cola"],
    "DMT": ["n,n-dmt", "nndmt"],
    "DXM": ["dextromethorphan"],
    "Diazepam": ["valium", "val"],
    "Dextroamphetamine": ["dex", "d-amph"],
    "GHB": ["gamma-hydroxybutyrate"],
    "Ketamine": ["k", "ket", "2-Cl-2'-Oxo-PCM"],
    "LSD": ["acid", "lucy", "LSD-25"],
    "MDMA": ["m", "molly", "ecstasy", "e"],
    "Methamphetamine": ["meth", "crystal", "MA"],
    "Methylphenidate": ["mph", "ritalin"],
    "Nicotine": ["nic", "tobacco"],
    "Nitrous oxide": ["n2o", "nos", "Nitrous"],
    "Psilocybin mushrooms": ["mushrooms", "shrooms", "psilocybin"],
}
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
SALT_ALIASES = {
    "acetate": ["ace"],
    "citrate": ["cit"],
    "fumarate": ["fum"],
    "freebase": ["fb", "base"],
    "hydrobromide": ["hbr"],
    "hydrochloride": ["hcl"],
    "malate": ["mal"],
    "mesylate": ["mes"],
    "phosphate": ["phos"],
    "succinate": ["succ"],
    "sulfate": ["sulphate", "sulf"],
    "tartrate": ["tart", "tar"],
}
SALT_CHOICES = [
    "undefined",
    "freebase",
    "hydrochloride",
    "sulfate",
    *[
        salt
        for salt in SALT_ALIASES
        if salt not in {"freebase", "hydrochloride", "sulfate"}
    ],
]
VOLUME_CHOICES = [
    f"{value / 10:g}ml"
    for value in range(1, 51)
]
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
MENU_WIDTH_FACTOR = "0.75"


class InputMode(str, Enum):
    flags = "flags"
    prompt = "prompt"
    dmenu = "dmenu"
    bemenu = "bemenu"
    fuzzel = "fuzzel"


class LogKind(str, Enum):
    single = "single"
    composite = "composite"
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
        if not substance or substance in seen:
            continue
        seen.add(substance)
        substances.append(substance)
    return substances


def _load_cached_logged_substances(logfile: Path) -> list[str] | None:
    entry = _load_prompt_options_cache().get(_logfile_cache_key(logfile))
    if not isinstance(entry, dict):
        return [] if not logfile.exists() else None
    if entry.get("fingerprint") != _logfile_fingerprint(logfile):
        return [] if not logfile.exists() else None
    return _valid_prompt_option_substances(entry.get("substances"))


def _store_logged_substances(logfile: Path, substances: list[str]) -> None:
    cache = _load_prompt_options_cache()
    cache[_logfile_cache_key(logfile)] = {
        "fingerprint": _logfile_fingerprint(logfile),
        "substances": substances,
    }
    _write_prompt_options_cache(cache)


def _add_logged_substance_option(logfile: Path, substance: str) -> None:
    entry = _load_prompt_options_cache().get(_logfile_cache_key(logfile))
    substances = (
        _valid_prompt_option_substances(entry.get("substances"))
        if isinstance(entry, dict)
        else []
    )
    updated = [substance, *[item for item in substances if item != substance]]
    _store_logged_substances(logfile, updated)


def _sync_substance_cache_from_history(logfile: Path) -> None:
    cache = _load_substance_cache()
    changed = False
    for substance in _read_logged_substances(logfile):
        if substance in cache or _is_list_value(substance):
            continue
        entry = _fetch_substance_entry(substance)
        if not entry:
            continue
        title = entry["Title"]
        if isinstance(title, str):
            cache[title] = entry
            changed = True

    if changed:
        _write_substance_cache(cache)
    _merge_cached_substance_aliases(cache)

def _parse_ingestion_time(time_str: str | None) -> datetime:
    if time_str is None:
        return datetime.now(timezone.utc)
    parsed: datetime | None = dateparser.parse(
        time_str,
        settings={
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TO_TIMEZONE": "UTC",
            "PREFER_DATES_FROM": "past",
        },
    )
    if parsed is None:
        raise typer.BadParameter(f"Could not parse '{time_str}' as a date/time")
    return parsed.astimezone(timezone.utc)


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
    stripped = value.strip()
    return (
        " + " in stripped
        or (stripped.startswith("<") and stripped.endswith(">") and "," in stripped)
    )


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
) -> str | None:
    if not shutil.which(executable):
        raise typer.BadParameter(f"{executable} was not found in PATH")

    labels, label_values = _menu_candidates(candidates or ([current] if current else []), aliases)
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
    if value:
        return label_values.get(value) or _resolve_alias(value, aliases or {})
    if optional:
        return None
    raise typer.BadParameter(f"{label} is required")


def _parse_log_kind(kind: str | LogKind | None) -> LogKind:
    if isinstance(kind, LogKind):
        return kind
    if not kind:
        return LogKind.single
    try:
        return LogKind(kind.lower())
    except ValueError as exc:
        choices = ", ".join(item.value for item in LogKind)
        raise typer.BadParameter(f"Kind must be one of: {choices}") from exc


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


def _format_solution_volume(volume_ml: str | None) -> str | None:
    if not volume_ml:
        return None
    volume_ml = volume_ml.strip()
    if not volume_ml:
        return None
    return volume_ml


def _append_solution_volume(dosage: str, volume_ml: str | None) -> str:
    volume_ml = _format_solution_volume(volume_ml)
    if not volume_ml or "@" in dosage:
        return dosage
    return f"{dosage}@{volume_ml}"


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
    if any(char.isalpha() for char in amount):
        return amount
    return f"{amount}{unit or 'mg'}"


def _prompt_for_dosage(label: str, current: str | None = None, optional: bool = False) -> str:
    current_amount, current_unit = _split_dosage_unit(current)
    amount = _prompt_for_value(label, current_amount, optional=optional)
    if amount is None:
        return ""
    unit = _prompt_for_value("Unit", current_unit or "mg", optional=False)
    return _format_dosage(amount, unit)


def _menu_for_dosage(
    executable: str,
    label: str,
    current: str | None = None,
    optional: bool = False,
    candidates: list[str] | None = None,
) -> str:
    current_amount, current_unit = _split_dosage_unit(current)
    dosage_candidates = []
    if current:
        dosage_candidates.append(current)
    dosage_candidates.extend(candidates or [])
    amount = _menu_for_value(
        executable,
        label,
        current_amount,
        candidates=dosage_candidates,
        optional=optional,
    )
    if amount is None:
        return ""
    selected_amount, selected_unit = _split_dosage_unit(amount)
    if selected_unit:
        return _format_dosage(selected_amount or amount, selected_unit)
    unit_candidates = [current_unit] if current_unit else []
    unit_candidates.extend(UNIT_CHOICES)
    unit = _menu_for_value(
        executable,
        "Unit",
        current_unit or "mg",
        candidates=unit_candidates,
    )
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


def _menu_for_extra_info(executable: str, note: str | None, time: str | None) -> tuple[str | None, str | None]:
    if note or time:
        return note, time
    add_extra_info = _menu_for_value(
        executable,
        "Extra options?",
        "No",
        candidates=["No", "Yes"],
        aliases=EXTRA_INFO_ALIASES,
    )
    if add_extra_info != "Yes":
        return None, None
    return (
        _menu_for_value(executable, "Note", note, optional=True),
        _menu_for_value(
            executable,
            "Time offset",
            time,
            candidates=([time] if time else []) + TIME_CHOICES,
            optional=True,
        ),
    )


def _is_intravenous_route(roa: str) -> bool:
    return (_resolve_alias(roa, ROA_ALIASES) or roa) == "intravenous"


def _split_mixture_values(value: str | None) -> list[str]:
    if not value:
        return []
    value = value.strip()
    if value.startswith("<") and ">" in value:
        list_value = value[1:value.index(">")]
        return [part.strip() for part in list_value.split(",") if part.strip()]
    delimiter = ";" if ";" in value else "+"
    return [part.strip() for part in value.split(delimiter) if part.strip()]


def _dosage_without_volume(dosage: str) -> str:
    dosage = dosage.strip()
    if dosage.startswith("<") and ">" in dosage:
        return dosage[:dosage.index(">") + 1]
    if "@" not in dosage:
        return dosage
    before, after = [part.strip() for part in dosage.split("@", 1)]
    return after if before.endswith("ml") and after else before


def _format_list_value(values: list[str | None]) -> str | None:
    present = [value for value in values if value]
    if not present:
        return None
    return f"<{','.join(present)}>"


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

    volume_ml = _format_solution_volume(volume_ml)
    if not volume_ml:
        raise typer.BadParameter("Mixtures require --volume-ml")

    resolved_substances = [
        _resolve_alias(substance, SUBSTANCE_ALIASES) or substance
        for substance in substances
    ]
    resolved_salts = [
        _resolve_alias(salt, SALT_ALIASES) if salt else None
        for salt in salts
    ]
    resolved_dosages = [dosage.strip() for dosage in dosages]
    return (
        _format_list_value(resolved_substances),
        f"{_format_list_value(resolved_dosages)}@{volume_ml}",
        _format_list_value(resolved_salts),
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


def _prompt_for_site(roa: str, current: str | None = None) -> str | None:
    if not _is_intravenous_route(roa):
        return current
    current_vein, current_side = _split_iv_site(current)
    vein = _prompt_for_value("Vein", current_vein, optional=True)
    if not vein:
        return None
    side = _prompt_for_value("Side", current_side, optional=False)
    return _format_iv_site(vein, side)


def _menu_for_site(executable: str, roa: str, current: str | None = None) -> str | None:
    if not _is_intravenous_route(roa):
        return current
    current_vein, current_side = _split_iv_site(current)
    vein_candidates = [current_vein] if current_vein else []
    vein_candidates.extend(IV_SITE_BASES)
    vein = _menu_for_value(
        executable,
        "Vein",
        current_vein,
        candidates=vein_candidates,
        optional=True,
    )
    if not vein:
        return None
    side_candidates = [current_side] if current_side else []
    side_candidates.extend(IV_SITE_SIDES)
    side = _menu_for_value(
        executable,
        "Side",
        current_side,
        candidates=side_candidates,
        optional=False,
    )
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


def _scan_logged_substances(logfile: Path) -> list[str]:
    if not logfile.exists():
        return []

    substances = []
    with open(logfile, newline="") as infile:
        for row in csv.reader(infile):
            if len(row) > 2 and row[2].strip().lower() not in ("substance", "med"):
                substance = row[2].strip()
                if not _is_list_value(substance):
                    substances.append(substance)

    seen = set()
    unique = []
    for substance in reversed(substances):
        if substance in seen:
            continue
        seen.add(substance)
        unique.append(substance)
    return unique


def _read_logged_dosages_for_substance(logfile: Path, substance: str) -> list[str]:
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
    cached_substances = _load_cached_logged_substances(logfile)
    if cached_substances is not None:
        return cached_substances

    substances = _scan_logged_substances(logfile)
    _store_logged_substances(logfile, substances)
    return substances


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


def _collect_mixture_flags(
    substance: str | None,
    dosage: str | None,
    salt: str | None,
    volume_ml: str | None,
) -> tuple[str, str, str | None]:
    substances = _split_mixture_values(substance)
    dosages = _split_mixture_values(dosage)
    salts = _split_mixture_values(salt)
    return _format_mixture(substances, dosages, salts, volume_ml)


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
        candidates = _menu_substance_candidates(logfile, current_substance)
        if index > 1:
            candidates = [DONE_SUBSTANCE_CHOICE, *candidates]

        component_substance = _menu_for_value(
            executable,
            f"Substance[{index}]",
            DONE_SUBSTANCE_CHOICE if index > 1 else current_substance,
            candidates=candidates,
            aliases=SUBSTANCE_ALIASES,
            optional=index > 1,
        )
        if component_substance is None or _alias_key(component_substance) == _alias_key(DONE_SUBSTANCE_CHOICE):
            break

        substances.append(component_substance)
        dosages.append(
            _menu_for_dosage(
                executable,
                "Dosage",
                current_dosage,
                candidates=_read_logged_dosages_for_substance(logfile, component_substance),
            )
        )

        salt_candidates = [current_salt] if current_salt else []
        salt_candidates.extend(SALT_CHOICES)
        salts.append(
            _menu_for_value(
                executable,
                "Salt",
                current_salt,
                candidates=salt_candidates,
                optional=True,
                aliases=SALT_ALIASES,
            )
        )

    return substances, dosages, salts


def _format_single_solution_component(
    substance: str,
    dosage: str,
    salt: str | None,
    volume_ml: str | None,
) -> tuple[str, str, str | None]:
    return (
        _resolve_alias(substance, SUBSTANCE_ALIASES) or substance,
        _append_solution_volume(dosage.strip(), volume_ml),
        _resolve_alias(salt, SALT_ALIASES) if salt else None,
    )


def _format_collected_substances(
    kind: LogKind,
    substances: list[str],
    dosages: list[str],
    salts: list[str | None],
    volume_ml: str | None,
) -> tuple[LogKind, str, str, str | None]:
    if len(substances) > 1:
        substance, dosage, salt = _format_mixture(substances, dosages, salts, volume_ml)
        return LogKind.mixture, substance, dosage, salt

    substance, dosage, salt = _format_single_solution_component(
        substances[0],
        dosages[0],
        salts[0] if salts else None,
        volume_ml,
    )
    return kind, substance, dosage, salt


def _collect_input(
    mode: InputMode,
    logfile: Path,
    kind: str | LogKind | None,
    substance: str | None,
    dosage: str | None,
    roa: str | None,
    site: str | None,
    salt: str | None,
    note: str | None,
    time: str | None,
    volume_ml: str | None,
) -> tuple[LogKind, str, str, str, str | None, str | None, str | None, str | None]:
    kind = _parse_log_kind(kind)
    if mode == InputMode.flags:
        if kind == LogKind.mixture:
            substance, dosage, salt = _collect_mixture_flags(substance, dosage, salt, volume_ml)
            roa = _resolve_alias(roa, ROA_ALIASES)
            if not roa:
                raise typer.BadParameter("Missing required input field(s): roa")
            return kind, substance, dosage, roa, site, salt, note, time

        substance = _resolve_alias(substance, SUBSTANCE_ALIASES)
        roa = _resolve_alias(roa, ROA_ALIASES)
        salt = _resolve_alias(salt, SALT_ALIASES)
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
        dosage = dosage.strip()
        if _route_supports_solution_volume(roa):
            dosage = _append_solution_volume(dosage, volume_ml)
        return kind, substance, dosage, roa, site, salt, note, time

    if mode == InputMode.prompt:
        roa = _resolve_alias(_prompt_for_value("Route", roa), ROA_ALIASES)
        volume_ml = _prompt_for_value("Volume", volume_ml, optional=False)
        site = _prompt_for_site(roa, site)
        substances, dosages, salts = _collect_substances_prompt(
            substance,
            dosage,
            salt,
        )
        kind, substance, dosage, salt = _format_collected_substances(
            kind,
            substances,
            dosages,
            salts,
            volume_ml,
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

    menu = mode.value
    roa = _menu_for_route(menu, roa)
    volume_ml = _menu_for_value(
        menu,
        "Volume",
        volume_ml,
        candidates=([volume_ml] if volume_ml else []) + VOLUME_CHOICES,
    )
    site = _menu_for_site(menu, roa, site)
    substances, dosages, salts = _collect_substances_menu(
        menu,
        logfile,
        substance,
        dosage,
        salt,
    )
    kind, substance, dosage, salt = _format_collected_substances(
        kind,
        substances,
        dosages,
        salts,
        volume_ml,
    )
    note, time = _menu_for_extra_info(menu, note, time)
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


def _lookup_substance_title(kind: LogKind, substance: str) -> tuple[str, str]:
    if kind in (LogKind.solution, LogKind.mixture):
        return substance, substance

    if kind == LogKind.single:
        entry, not_found = _fetch_substance_entry_result(substance)
        if entry:
            _cache_substance_entry(entry)
            title = entry["Title"] if isinstance(entry["Title"], str) else substance
            title_slug = title.replace(" ", "_")
            return title, f"[{title}](https://anodyne.wiki/substance/{title_slug})"

        cached_title = _resolve_alias(substance, SUBSTANCE_ALIASES)
        if cached_title and cached_title != substance:
            title_slug = cached_title.replace(" ", "_")
            return cached_title, f"[{cached_title}](https://anodyne.wiki/substance/{title_slug})"

        if not_found:
            err_con.print(f"Substance '{substance}' not found on AnodyneWiki", style="bold yellow")
            _ = typer.confirm("Log anyway?", abort=True)
        return substance, substance

    api_path = "composite"
    page_path = "substance" if kind == LogKind.single else "composite"
    try:
        r = requests.get(f"https://anodyne.wiki/api/{api_path}/{substance}")
        r.raise_for_status()
        if "NotFound" in r.json() and r.json()["NotFound"]:
            raise ValueError

        title: str = r.json()["Title"] if isinstance(r.json()["Title"], str) else substance
        title_slug = title.replace(" ", "_")
        return title, f"[{title}](https://anodyne.wiki/{page_path}/{title_slug})"
    except ValueError:
        err_con.print(f"{kind.value.title()} '{substance}' not found on AnodyneWiki", style="bold yellow")
        _ = typer.confirm("Log anyway?", abort=True)
    except Exception:
        err_con.print_exception()

    return substance, substance


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
    kind_opt: Annotated[str | None, typer.Option(
        "--kind",
        help="entry kind: single, composite, solution, or mixture",
        rich_help_panel="Input Options",
    )] = None,
    mixture_kind: Annotated[bool, typer.Option(
        "--mixture",
        help="log multiple compounds in one syringe volume",
        rich_help_panel="Input Options",
    )] = False,
    time_opt: Annotated[str | None, typer.Option(
        "--time",
        help="ingestion time, e.g. 'two hours ago' or '2025-12-04 10:00'",
        rich_help_panel="Input Options",
    )] = None,
    volume_ml_opt: Annotated[str | None, typer.Option(
        "--volume-ml",
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
    logfile = _require_option("--csv", logfile)
    user = _require_option("--user", user)
    mode = _resolve_input_mode(flags_mode, prompt_mode, dmenu_mode, bemenu_mode, fuzzel_mode)
    _merge_cached_substance_aliases()
    if mixture_kind and kind_opt and _parse_log_kind(kind_opt) != LogKind.mixture:
        raise typer.BadParameter("--mixture cannot be combined with a different --kind")
    kind_value = LogKind.mixture if mixture_kind else kind_opt
    kind, substance, dosage, roa, site, salt, note, time = _collect_input(
        mode,
        logfile,
        kind_value,
        _coalesce(substance_arg, substance_opt),
        _coalesce(dosage_arg, dosage_opt),
        _coalesce(roa_arg, roa_opt),
        site,
        salt,
        note,
        _coalesce(" ".join(time_arg) if time_arg else None, time_opt),
        volume_ml_opt,
    )
    ingestion_time = _parse_ingestion_time(time)
    time_now = ingestion_time.isoformat(timespec='milliseconds')
    is_backdated = datetime.now(timezone.utc) - ingestion_time > timedelta(minutes=10)
    title = substance_md = substance

    if not logfile.exists():
        logfile.parent.mkdir(parents=True, exist_ok=True)
        logfile.touch()
        con.print(f"File '{logfile}' has been created.")

    _sync_substance_cache_from_history(logfile)
    title, substance_md = _lookup_substance_title(kind, substance)

    with open(logfile, "a", newline="") as of:
        log = csv.writer(of)
        log.writerow([time_now, user, title, dosage, roa, site, salt, note])
    _add_logged_substance_option(logfile, title)

    if not webhook:
        return

    user_md = f"[{user}]({userpage})" if userpage else user
    logline = f"{user_md}: {dosage} {substance_md}" + (
        f" [{salt}]" if salt else ""
    ) + f" via {roa}" + (
        f" at {site} site" if site else ""
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
