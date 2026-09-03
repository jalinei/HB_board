#!/usr/bin/env python3
"""Update KiCad symbol Datasheet properties from PDFs already on disk.

The script is completely independent from any download script.

It scans a local datasheet directory for PDF filenames containing an LCSC number,
for example:

    datasheet/C783598_AMC1311BDWVR.pdf
    datasheet/C42420609_TCA9555RTWR.pdf

Then, for every placed symbol in a .kicad_sch containing:

    (property "LCSC" "C783598" ...)

it sets the symbol's Datasheet property to the matching local PDF, e.g.:

    ${KIPRJMOD}/datasheet/C783598_AMC1311BDWVR.pdf

Only PDFs that actually exist in the scanned folder are used. Components whose
LCSC number has no matching PDF are left untouched.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


LCSC_RE = re.compile(r"^C\d+$", re.IGNORECASE)
LCSC_IN_FILENAME_RE = re.compile(r"(?<![A-Za-z0-9])(C\d+)(?!\d)", re.IGNORECASE)
PROPERTY_HEADER_RE = re.compile(
    r'\(\s*property\s+"((?:\\.|[^"\\])*)"\s+"((?:\\.|[^"\\])*)"',
    re.DOTALL,
)


@dataclass
class ExprSpan:
    start: int
    end: int  # exclusive
    head: str


def _skip_string(text: str, i: int) -> int:
    """Return the index immediately after a KiCad quoted string."""
    assert text[i] == '"'
    i += 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
        elif text[i] == '"':
            return i + 1
        else:
            i += 1
    raise ValueError("Unterminated quoted string in schematic")


def _head_after_open(text: str, open_pos: int) -> str:
    i = open_pos + 1
    n = len(text)
    while i < n and text[i].isspace():
        i += 1
    start = i
    while i < n and not text[i].isspace() and text[i] not in '()"':
        i += 1
    return text[start:i]


def find_expressions(text: str, wanted_head: str) -> list[ExprSpan]:
    """Find balanced S-expressions whose first atom is ``wanted_head``."""
    stack: list[tuple[int, str]] = []
    result: list[ExprSpan] = []
    i = 0
    n = len(text)

    while i < n:
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c == '(':
            stack.append((i, _head_after_open(text, i)))
            i += 1
            continue
        if c == ')':
            if not stack:
                raise ValueError(f"Unbalanced ')' at byte {i}")
            start, head = stack.pop()
            if head == wanted_head:
                result.append(ExprSpan(start, i + 1, head))
            i += 1
            continue
        i += 1

    if stack:
        raise ValueError("Unbalanced '(' in schematic")
    return result


def direct_property_spans(text: str, symbol: ExprSpan) -> dict[str, tuple[str, int, int]]:
    """Return direct child properties of a placed ``(symbol ...)`` expression.

    Mapping: property name -> (value, absolute value_start, absolute value_end),
    where start/end select only the contents between the value quotes.
    """
    props: dict[str, tuple[str, int, int]] = {}
    i = symbol.start + 1
    depth = 1

    while i < symbol.end - 1:
        c = text[i]
        if c == '"':
            i = _skip_string(text, i)
            continue
        if c == '(':
            head = _head_after_open(text, i)
            if depth == 1 and head == "property":
                p_start = i
                p_depth = 1
                j = i + 1
                while j < symbol.end:
                    if text[j] == '"':
                        j = _skip_string(text, j)
                        continue
                    if text[j] == '(':
                        p_depth += 1
                    elif text[j] == ')':
                        p_depth -= 1
                        if p_depth == 0:
                            p_end = j + 1
                            break
                    j += 1
                else:
                    raise ValueError("Unbalanced property expression")

                chunk = text[p_start:p_end]
                m = PROPERTY_HEADER_RE.match(chunk)
                if m:
                    name = m.group(1)
                    value = m.group(2)
                    value_start = p_start + m.start(2)
                    value_end = p_start + m.end(2)
                    props[name] = (value, value_start, value_end)
                i = p_end
                continue
            depth += 1
            i += 1
            continue
        if c == ')':
            depth -= 1
        i += 1

    return props


def scan_datasheet_folder(folder: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    """Return unique LCSC->PDF mappings and ambiguous duplicate mappings.

    An LCSC number can appear anywhere in the filename, so both
    ``C12345.pdf`` and ``C12345_part_name.pdf`` are accepted.
    """
    found: dict[str, list[Path]] = {}

    for pdf in sorted(folder.glob("*.pdf"), key=lambda p: p.name.lower()):
        match = LCSC_IN_FILENAME_RE.search(pdf.stem)
        if not match:
            continue
        lcsc = match.group(1).upper()
        found.setdefault(lcsc, []).append(pdf)

    unique = {lcsc: paths[0] for lcsc, paths in found.items() if len(paths) == 1}
    ambiguous = {lcsc: paths for lcsc, paths in found.items() if len(paths) > 1}
    return unique, ambiguous


def kicad_path_for_pdf(pdf: Path, datasheet_dir: Path, path_mode: str) -> str:
    """Build the value to place in KiCad's Datasheet property."""
    if path_mode == "absolute":
        return pdf.resolve().as_posix()

    if path_mode == "relative":
        return (datasheet_dir / pdf.name).as_posix()

    # Default: project-relative KiCad path. This keeps the project portable.
    rel = (datasheet_dir / pdf.name).as_posix()
    if rel.startswith("./"):
        rel = rel[2:]
    return "${KIPRJMOD}/" + rel


def build_updates(
    text: str,
    pdf_map: dict[str, Path],
    datasheet_dir: Path,
    path_mode: str,
    only_empty: bool,
) -> tuple[list[tuple[int, int, str, str, str]], set[str]]:
    """Return property replacements and LCSC IDs missing a local PDF."""
    updates: list[tuple[int, int, str, str, str]] = []
    missing: set[str] = set()

    for symbol in find_expressions(text, "symbol"):
        props = direct_property_spans(text, symbol)
        if "LCSC" not in props or "Datasheet" not in props:
            continue

        lcsc = props["LCSC"][0].strip().upper()
        if not LCSC_RE.fullmatch(lcsc):
            continue

        pdf = pdf_map.get(lcsc)
        if pdf is None:
            missing.add(lcsc)
            continue

        old_ds, ds_start, ds_end = props["Datasheet"]
        if only_empty and old_ds.strip():
            continue

        new_ds = kicad_path_for_pdf(pdf, datasheet_dir, path_mode)
        if old_ds == new_ds:
            continue

        reference = props.get("Reference", ("?", 0, 0))[0]
        updates.append((ds_start, ds_end, new_ds, reference, lcsc))

    return updates, missing


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update KiCad Datasheet fields from PDFs already present in a local folder."
    )
    parser.add_argument("schematic", type=Path, help="Input .kicad_sch file")
    parser.add_argument(
        "--datasheet-dir",
        type=Path,
        default=Path("datasheet"),
        help="Folder containing downloaded PDFs (default: %(default)s)",
    )
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Write another .kicad_sch instead of updating the input in place",
    )
    parser.add_argument(
        "--path-mode",
        choices=("kicad", "relative", "absolute"),
        default="kicad",
        help=(
            "Datasheet path style: 'kicad' writes ${KIPRJMOD}/datasheet/file.pdf "
            "(default), 'relative' writes datasheet/file.pdf, 'absolute' writes a full path"
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show mappings and changes without writing the schematic",
    )
    parser.add_argument(
        "--only-empty", action="store_true",
        help="Only fill empty Datasheet fields; leave existing values untouched",
    )
    parser.add_argument(
        "--no-backup", action="store_true",
        help="Do not create <schematic>.bak when editing in place",
    )
    args = parser.parse_args()

    src = args.schematic
    if not src.is_file():
        print(f"ERROR: schematic not found: {src}", file=sys.stderr)
        return 2

    folder = args.datasheet_dir
    if not folder.is_absolute():
        # Interpret a relative datasheet directory relative to the schematic/project,
        # not relative to whichever shell directory invoked the script.
        folder_on_disk = src.parent / folder
    else:
        folder_on_disk = folder

    if not folder_on_disk.is_dir():
        print(f"ERROR: datasheet folder not found: {folder_on_disk}", file=sys.stderr)
        return 2

    pdf_map, ambiguous = scan_datasheet_folder(folder_on_disk)

    print(f"Datasheet folder: {folder_on_disk}")
    print(f"PDFs with unique LCSC number: {len(pdf_map)}")

    if ambiguous:
        print("\nWARNING: multiple PDFs found for these LCSC numbers; they will be skipped:")
        for lcsc, paths in sorted(ambiguous.items()):
            print(f"  {lcsc}")
            for path in paths:
                print(f"    - {path.name}")

    text = src.read_text(encoding="utf-8")

    # For the property itself, retain the path spelling requested on the command line.
    # Relative paths are based on the project directory, even though scanning used an
    # absolute resolved folder_on_disk path.
    if args.datasheet_dir.is_absolute():
        property_dir = args.datasheet_dir
    else:
        property_dir = args.datasheet_dir

    try:
        updates, missing = build_updates(
            text=text,
            pdf_map=pdf_map,
            datasheet_dir=property_dir,
            path_mode=args.path_mode,
            only_empty=args.only_empty,
        )
    except ValueError as exc:
        print(f"ERROR: could not parse schematic safely: {exc}", file=sys.stderr)
        return 3

    if missing:
        print("\nLCSC numbers used by schematic but without a unique local PDF:")
        for lcsc in sorted(missing, key=lambda x: int(x[1:])):
            reason = "multiple matching PDFs" if lcsc in ambiguous else "PDF not found"
            print(f"  {lcsc:<12} {reason}")

    if not updates:
        print("\nNo Datasheet properties need updating.")
        return 0

    print("\nChanges:")
    for _, _, new_ds, ref, lcsc in sorted(updates, key=lambda x: (x[3], x[4])):
        print(f"  {ref:>8}  {lcsc:<12} -> {new_ds}")

    print(f"\n{len(updates)} Datasheet propert{'y' if len(updates) == 1 else 'ies'} to update.")

    if args.dry_run:
        print("Dry run: no file written.")
        return 0

    new_text = text
    for start, end, new_value, _, _ in sorted(updates, key=lambda x: x[0], reverse=True):
        new_text = new_text[:start] + new_value + new_text[end:]

    dst = args.output if args.output else src
    dst.parent.mkdir(parents=True, exist_ok=True)

    if args.output is None and not args.no_backup:
        backup = src.with_name(src.name + ".bak")
        shutil.copy2(src, backup)
        print(f"Backup: {backup}")

    if dst.resolve() == src.resolve():
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(src.parent),
            prefix=src.name + ".",
            suffix=".tmp",
            delete=False,
        ) as f:
            f.write(new_text)
            temp_name = Path(f.name)
        temp_name.replace(src)
    else:
        dst.write_text(new_text, encoding="utf-8")

    print(f"Updated: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
