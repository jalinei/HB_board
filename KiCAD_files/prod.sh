#!/bin/sh
set -eu

OUT_DIR="prod"
PANEL="${OUT_DIR}/InverterLeg_panel_3x_vcut.kicad_pcb"
FAB_DIR="${OUT_DIR}/fab_jlcpcb_3x_vcut"
BOM="${OUT_DIR}/bom.csv"
PLACEMENT="${OUT_DIR}/positions.csv"
KICAD_PY="/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3"
JLC_ROTATION_OFFSET="0"
ROTATION_TEMPLATE="production/test_one_hb_positions.csv"

mkdir -p "${OUT_DIR}"
rm -rf "${FAB_DIR}"

kikit panelize \
  -p kikit-panel-3x-vcut.json \
  InverterLeg.kicad_pcb \
  "${PANEL}"

"${KICAD_PY}" - "${PANEL}" "${FAB_DIR}" <<'PY'
import os
import shutil
import sys

import pcbnew
from kikit.export import exportSettingsJlcpcb, gerberImpl


PANEL, FAB_DIR = sys.argv[1:3]
GERBER_DIR = os.path.join(FAB_DIR, "gerber")

settings = exportSettingsJlcpcb.copy()
settings["UseAuxOrigin"] = False

shutil.rmtree(GERBER_DIR, ignore_errors=True)
os.makedirs(FAB_DIR, exist_ok=True)
gerberImpl(PANEL, GERBER_DIR, settings=settings)
shutil.make_archive(os.path.join(FAB_DIR, "gerbers"), "zip", FAB_DIR, "gerber")
PY

"${KICAD_PY}" - "${PANEL}" "${BOM}" "${PLACEMENT}" "${JLC_ROTATION_OFFSET}" "${ROTATION_TEMPLATE}" <<'PY'
import csv
import math
import os
import re
import sys
from collections import defaultdict

import pcbnew


PANEL, BOM, PLACEMENT = sys.argv[1:4]
JLC_ROTATION_OFFSET = float(sys.argv[4])
ROTATION_TEMPLATE = sys.argv[5]


def mm(value):
    return value / 1_000_000


def from_mm(value):
    return int(round(value * 1_000_000))


def field(footprint, name):
    for item in footprint.GetFields():
        try:
            if item.GetName().lower() == name.lower():
                return item.GetText()
        except Exception:
            pass
    return ""


def field_float(footprint, name, default=0.0):
    value = field(footprint, name).strip()
    if not value:
        return default
    return float(value)


def field_xy(footprint, name):
    value = field(footprint, name).strip()
    if not value:
        return 0.0, 0.0
    parts = value.replace(";", ",").split(",")
    if len(parts) != 2:
        raise ValueError(f"{footprint.GetReference()}: invalid {name}: {value}")
    return float(parts[0]), float(parts[1])


def corrected_position(footprint):
    position = footprint.GetPosition()
    offset_x, offset_y = field_xy(footprint, "Position Offset")
    if offset_x == 0 and offset_y == 0:
        return position

    angle = -footprint.GetOrientation().AsRadians()
    rotated_x = offset_x * math.cos(angle) - offset_y * math.sin(angle)
    rotated_y = offset_x * math.sin(angle) + offset_y * math.cos(angle)
    return pcbnew.VECTOR2I(
        position.x + from_mm(rotated_x),
        position.y + from_mm(rotated_y),
    )


def jlc_rotation(footprint):
    original_reference = re.sub(r"^Board_\d+-", "", footprint.GetReference())
    if original_reference in rotation_template:
        return rotation_template[original_reference]

    kicad_rotation = footprint.GetOrientationDegrees()
    if footprint.IsFlipped():
        kicad_rotation = 180 - kicad_rotation
    rotation_offset = field_float(footprint, "Rotation Offset")
    return (kicad_rotation + rotation_offset + JLC_ROTATION_OFFSET) % 360

def footprint_name(footprint):
    fpid = footprint.GetFPID()
    try:
        return f"{fpid.GetLibNickname()}:{fpid.GetLibItemName()}"
    except Exception:
        return str(fpid)


def is_fitted(footprint):
    if "*" in footprint.GetReference():
        return False
    value = footprint.GetValue().strip().lower()
    if value in ("", "~", "dnp", "dnf", "do not populate", "do not fit"):
        return False
    return not field(footprint, "DNP").strip()


def load_rotation_template(path):
    if not os.path.exists(path):
        return {}

    rotations = {}
    with open(path, newline="") as template_file:
        reader = csv.DictReader(template_file)
        reader.fieldnames = [name.strip().lstrip("\ufeff") for name in reader.fieldnames]
        for row in reader:
            reference = row.get("Designator", "").strip()
            rotation = row.get("Rotation", "").strip()
            if reference and rotation:
                rotations[reference] = float(rotation) % 360
    return rotations


rotation_template = load_rotation_template(ROTATION_TEMPLATE)
board = pcbnew.LoadBoard(PANEL)

groups = defaultdict(list)
placements = []

for footprint in board.GetFootprints():
    if not is_fitted(footprint):
        continue

    reference = footprint.GetReference()
    value = footprint.GetValue()
    package = footprint_name(footprint)
    lcsc = field(footprint, "LCSC")
    description = field(footprint, "Description")
    key = (value, package, lcsc, description)
    groups[key].append(reference)

    position = corrected_position(footprint)
    side = "B" if footprint.IsFlipped() else "T"
    placements.append({
        "Designator": reference,
        "Mid X": f"{mm(position.x):.2f}",
        "Mid Y": f"{-mm(position.y):.2f}",
        "Layer": side,
        "Rotation": f"{jlc_rotation(footprint):.2f}",
    })

with open(BOM, "w", newline="") as bom_file:
    writer = csv.DictWriter(
        bom_file,
        fieldnames=["Comment", "Designator", "Footprint", "LCSC Part #", "Quantity", "Description"],
    )
    writer.writeheader()
    for (value, package, lcsc, description), references in sorted(groups.items()):
        writer.writerow({
            "Comment": value,
            "Designator": ",".join(sorted(references)),
            "Footprint": package,
            "LCSC Part #": lcsc,
            "Quantity": len(references),
            "Description": description,
        })

with open(PLACEMENT, "w", newline="") as placement_file:
    writer = csv.DictWriter(
        placement_file,
        fieldnames=["Designator", "Mid X", "Mid Y", "Layer", "Rotation"],
    )
    writer.writeheader()
    writer.writerows(sorted(placements, key=lambda row: row["Designator"]))
PY

rm -f "${OUT_DIR}"/~*.lck

printf 'Done: %s/gerbers.zip\n' "${FAB_DIR}"
printf 'Done: %s\n' "${BOM}"
printf 'Done: %s\n' "${PLACEMENT}"
