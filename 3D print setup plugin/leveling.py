# coding=utf-8
"""Pure helpers for bed-leveling guidance.

This module deliberately avoids any OctoPrint imports so it can be unit-tested
in isolation. All functions are stateless and work on plain dicts/floats.
"""

from __future__ import annotations

import statistics

# Direction language is computed from four inputs:
#   - sign of the Z delta (positive = bed sits low at this corner, needs to go up)
#   - viewpoint the user is looking at the wheel from
#   - screw thread handedness (right-hand thread on stock Ender 3 Pro M4 wheels)
#   - the physical effect: loosening the wheel lets the spring push the bed up,
#     tightening pulls the bed down toward the carriage.
#
# Marlin's G35 tramming assistant uses the same parameter set
# (S30/S31/S40/S41/S50/S51 = thread + CW/CCW). This mirrors that contract.

# Map (raise_or_lower, viewpoint) -> human sentence fragment.
# "raise" means we want the bed to move UP at this corner.
# "lower" means we want the bed to move DOWN at this corner.
# "from_below" means the user is squatting next to the printer looking up
# at the underside of the bed (the standard Ender 3 Pro view of the wheels).
# "from_above" means the user is looking down past the bed at the wheel.
_DIRECTION_PHRASES = {
    ("raise", "from_below"): {
        "rotation": "clockwise",
        "viewpoint_text": "looking up at the wheel from underneath",
        "physical_action": "loosen the wheel so the spring pushes the bed up",
    },
    ("lower", "from_below"): {
        "rotation": "counter-clockwise",
        "viewpoint_text": "looking up at the wheel from underneath",
        "physical_action": "tighten the wheel to pull the bed down",
    },
    ("raise", "from_above"): {
        "rotation": "counter-clockwise",
        "viewpoint_text": "looking down at the wheel from above the bed",
        "physical_action": "loosen the wheel so the spring pushes the bed up",
    },
    ("lower", "from_above"): {
        "rotation": "clockwise",
        "viewpoint_text": "looking down at the wheel from above the bed",
        "physical_action": "tighten the wheel to pull the bed down",
    },
}


def format_turns(turns):
    """Render a turn count as '1 + 1/4 turn (1.25 turns)'.

    Mirrors the existing in-plugin formatting so the new helpers can replace
    the old ones without changing on-screen text for the numeric part.
    """
    if turns < 0.01:
        return "0 turns"

    rounded_eighths = max(1, int(round(turns * 8)))
    whole_turns = rounded_eighths // 8
    remainder = rounded_eighths % 8
    fraction_labels = {
        0: "",
        1: "1/8",
        2: "1/4",
        3: "3/8",
        4: "1/2",
        5: "5/8",
        6: "3/4",
        7: "7/8",
    }

    parts = []
    if whole_turns:
        turn_label = "turn" if whole_turns == 1 else "turns"
        parts.append(f"{whole_turns} {turn_label}")
    if remainder:
        parts.append(fraction_labels[remainder] + " turn")

    rounded = " + ".join(parts)
    return f"{rounded} ({turns:.2f} turns)" if rounded else f"{turns:.2f} turns"


def direction_for_delta(delta_mm, viewpoint="from_below"):
    """Return human-direction info for a given Z delta.

    delta_mm > 0 means we want to raise the bed at this corner
                (measured Z is below the reference plane).
    delta_mm < 0 means we want to lower the bed at this corner.
    """
    if abs(delta_mm) < 1e-6:
        return {
            "action": "hold",
            "rotation": "no change",
            "viewpoint_text": "",
            "physical_action": "leave this wheel alone",
        }

    action = "raise" if delta_mm > 0 else "lower"
    key = (action, viewpoint)
    if key not in _DIRECTION_PHRASES:
        # Unknown viewpoint - fall back to from_below to stay safe.
        key = (action, "from_below")
    phrase = _DIRECTION_PHRASES[key]
    return {
        "action": action,
        "rotation": phrase["rotation"],
        "viewpoint_text": phrase["viewpoint_text"],
        "physical_action": phrase["physical_action"],
    }


def build_human_instruction(corner_label, delta_mm, turns, viewpoint="from_below"):
    """Compose the one-sentence instruction shown on the assist card.

    The instruction always names the wheel, the rotation direction,
    the viewpoint, the turn amount, and the resulting physical effect
    in millimetres. This is the format the review settled on as
    "easy to follow human instructions".
    """
    info = direction_for_delta(delta_mm, viewpoint=viewpoint)
    if info["action"] == "hold":
        return f"{corner_label}: leave this wheel alone, the corner is already on plane."

    direction = info["physical_action"]
    if delta_mm > 0:
        effect = f"this raises the bed by about {abs(delta_mm):.2f} mm at that corner"
    else:
        effect = f"this lowers the bed by about {abs(delta_mm):.2f} mm at that corner"

    return (
        f"Turn the {corner_label} wheel {info['rotation']} "
        f"({info['viewpoint_text']}) by about {format_turns(turns)} "
        f"to {direction} ({effect})."
    )


# Stock Ender 3 Pro 12864 LCD shows ~20 chars on the M117 status line.
# Anything longer is silently truncated by the firmware, so we budget hard.
LCD_STATUS_MAX_CHARS = 20


def _short_turns(turns):
    """Compact LCD-friendly turn label, e.g. '1/4', '1+1/2', '2'."""
    if turns < 0.01:
        return "0"
    rounded_eighths = max(1, int(round(turns * 8)))
    whole = rounded_eighths // 8
    remainder = rounded_eighths % 8
    fractions = {1: "1/8", 2: "1/4", 3: "3/8", 4: "1/2", 5: "5/8", 6: "3/4", 7: "7/8"}
    if whole and remainder:
        return f"{whole}+{fractions[remainder]}"
    if whole:
        return str(whole)
    return fractions[remainder]


def format_lcd_instruction(corner_short, delta_mm, turns, viewpoint="from_below"):
    """Render an instruction that fits on a stock 20-char LCD status line.

    Format: '<CORNER> <DIR> <TURNS> <SIGN><MM>mm'
    Examples:
        FL CW 3/4 +0.21mm
        RR CCW 1+1/2 -0.45mm
        FL OK 0.01mm
    Truncated to LCD_STATUS_MAX_CHARS as a last resort.
    """
    if abs(delta_mm) < 1e-6:
        text = f"{corner_short} OK {abs(delta_mm):.2f}mm"
        return text[:LCD_STATUS_MAX_CHARS]

    info = direction_for_delta(delta_mm, viewpoint=viewpoint)
    direction_short = "CW" if info["rotation"] == "clockwise" else "CCW"
    sign = "+" if delta_mm > 0 else "-"
    text = f"{corner_short} {direction_short} {_short_turns(turns)} {sign}{abs(delta_mm):.2f}mm"
    if len(text) <= LCD_STATUS_MAX_CHARS:
        return text
    # Drop the mm reading first - turns + direction are the actionable bits.
    short_text = f"{corner_short} {direction_short} {_short_turns(turns)}"
    return short_text[:LCD_STATUS_MAX_CHARS]


def probe_noise_sigma_mm(samples_mm):
    """Return the repeatability sigma for repeated probe samples.

    Uses population standard deviation because the plugin is reporting the
    spread of the collected probe sample set, not estimating an unseen
    population from a statistical study. A single sample is treated as zero
    spread so the caller can still render a partial-progress state.
    """
    cleaned = [float(sample) for sample in samples_mm]
    if len(cleaned) < 2:
        return 0.0
    return float(statistics.pstdev(cleaned))


def recommended_tolerance_mm(sigma_mm):
    """Choose a practical leveling tolerance from measured probe noise.

    The rule is max(4 * sigma, 0.03 mm):
    - 4σ covers 99.99% of probe readings so the workflow doesn't oscillate
      with corners flipping in/out of tolerance across passes
    - 0.03 mm floor is still extremely tight for manual bed screws (a quarter
      turn of an M4 screw moves the bed ~175 µm)
    """
    return max(4.0 * float(sigma_mm), 0.03)


def build_probe_noise_summary(samples_mm, hard_fail_sigma_mm=0.05):
    """Summarize repeated center-probe samples for the preflight gate."""
    cleaned = [float(sample) for sample in samples_mm]
    sigma_mm = probe_noise_sigma_mm(cleaned)
    recommended_mm = recommended_tolerance_mm(sigma_mm)
    return {
        "samples": cleaned,
        "sample_count": len(cleaned),
        "sigma_mm": sigma_mm,
        "recommended_tolerance_mm": recommended_mm,
        "passed": sigma_mm <= float(hard_fail_sigma_mm),
        "hard_fail_sigma_mm": float(hard_fail_sigma_mm),
    }


def build_tightening_target_profile(corner_measurements, minimum_tightening_mm):
    """Build a parallel target plane below the measured plane.

    The workflow assumes the user loosens all four wheels before probing.
    From that loose baseline, every guided move should be a tightening move,
    and every wheel should tighten by at least ``minimum_tightening_mm`` from
    its probed starting point.

    The caller supplies corner measurements annotated with ``plane_z`` from the
    best-fit plane through the measured corner points. This helper computes the
    single downward offset needed so that every corner's target height sits at
    least ``minimum_tightening_mm`` below its measured Z.
    """
    minimum_tightening_mm = float(minimum_tightening_mm)
    cleaned = [
        {
            "id": corner["id"],
            "z": float(corner["z"]),
            "plane_z": float(corner["plane_z"]),
        }
        for corner in corner_measurements
    ]

    if not cleaned:
        return {
            "minimum_tightening_mm": minimum_tightening_mm,
            "offset_mm": minimum_tightening_mm,
            "target_z_by_id": {},
        }

    offset_mm = max(corner["plane_z"] - corner["z"] for corner in cleaned) + minimum_tightening_mm
    return {
        "minimum_tightening_mm": minimum_tightening_mm,
        "offset_mm": offset_mm,
        "target_z_by_id": {
            corner["id"]: corner["plane_z"] - offset_mm for corner in cleaned
        },
    }


def pick_reference_corner(corner_measurements):
    """Pick the corner whose Z is closest to the mean of all corners.

    This minimises the maximum work on the other three corners and avoids
    both the "spring fully extended" and "spring fully compressed"
    failure modes that picking the highest or lowest corner can trigger.

    Returns the chosen corner dict (must contain at least 'id' and 'z')
    or None if the input is empty.
    """
    if not corner_measurements:
        return None
    mean_z = sum(corner["z"] for corner in corner_measurements) / float(
        len(corner_measurements)
    )
    return min(corner_measurements, key=lambda corner: abs(corner["z"] - mean_z))


# Corner / center layout: probe tips should land `corner_inset_mm` in from each
# of the four bed wheels. The four wheels sit at the bed corners on stock
# Ender 3 / Pro hardware, so the inset is taken from (0, 0)..(bed_size, bed_size).
# Nozzle XY is shifted by the negative of the CR Touch X/Y offset stored in
# Marlin via M851 so that the *probe tip* lands at the requested inset point.
_PROBE_POINT_DEFS = (
    ("front_left", "Front Left", "FL"),
    ("front_right", "Front Right", "FR"),
    ("rear_right", "Rear Right", "RR"),
    ("rear_left", "Rear Left", "RL"),
    ("center", "Center", "C"),
)


def compute_probe_points(
    bed_size_x_mm,
    bed_size_y_mm,
    corner_inset_mm,
    probe_offset_x_mm=0.0,
    probe_offset_y_mm=0.0,
    nozzle_min_x_mm=0.0,
    nozzle_max_x_mm=None,
    nozzle_min_y_mm=0.0,
    nozzle_max_y_mm=None,
    explicit_probe_targets=None,
):
    """Compute nozzle XY targets so the probe tip lands `inset` in from each wheel.

    Marlin's ``G30 X<x> Y<y>`` interprets X/Y as the *nozzle* target. The probe
    tip lands at ``(nozzle_x + probe_offset_x, nozzle_y + probe_offset_y)``,
    so the nozzle has to move by the negative of the probe offset to put the
    probe tip at the requested inset point.

    Returns a list of dicts ``{"id", "label", "short_label", "x", "y",
    "probe_x", "probe_y", "clamped"}`` in the order Front Left, Front Right,
    Rear Right, Rear Left, Center. ``probe_x`` / ``probe_y`` are where the
    probe tip will actually land after clamping to the reachable probe area;
    ``x`` / ``y`` are the nozzle target needed to hit that point. The
    ``clamped`` flag is True if the requested probe target had to be moved
    inward because the current ``M851`` offset would otherwise send the probe
    past the bed or beyond the nozzle travel limits.
    """
    bed_x = float(bed_size_x_mm)
    bed_y = float(bed_size_y_mm)
    inset = float(corner_inset_mm)
    offset_x = float(probe_offset_x_mm)
    offset_y = float(probe_offset_y_mm)

    max_x = float(nozzle_max_x_mm) if nozzle_max_x_mm is not None else bed_x
    max_y = float(nozzle_max_y_mm) if nozzle_max_y_mm is not None else bed_y
    min_x = float(nozzle_min_x_mm)
    min_y = float(nozzle_min_y_mm)

    if explicit_probe_targets:
        probe_targets = {
            point_id: (float(coords[0]), float(coords[1]))
            for point_id, coords in explicit_probe_targets.items()
        }
    else:
        probe_targets = {
            "front_left": (inset, inset),
            "front_right": (bed_x - inset, inset),
            "rear_right": (bed_x - inset, bed_y - inset),
            "rear_left": (inset, bed_y - inset),
            "center": (bed_x / 2.0, bed_y / 2.0),
        }

    safe_probe_min_x = min_x + offset_x
    safe_probe_max_x = max_x + offset_x
    safe_probe_min_y = min_y + offset_y
    safe_probe_max_y = max_y + offset_y

    points = []
    for point_id, label, short_label in _PROBE_POINT_DEFS:
        probe_x, probe_y = probe_targets[point_id]
        if explicit_probe_targets:
            nozzle_x = probe_x
            nozzle_y = probe_y
            points.append(
                {
                    "id": point_id,
                    "label": label,
                    "short_label": short_label,
                    "x": round(nozzle_x, 3),
                    "y": round(nozzle_y, 3),
                    "probe_x": round(nozzle_x + offset_x, 3),
                    "probe_y": round(nozzle_y + offset_y, 3),
                    "clamped": False,
                }
            )
            continue

        clamped_probe_x = max(safe_probe_min_x, min(safe_probe_max_x, probe_x))
        clamped_probe_y = max(safe_probe_min_y, min(safe_probe_max_y, probe_y))
        nozzle_x = clamped_probe_x - offset_x
        nozzle_y = clamped_probe_y - offset_y
        clamped_x = max(min_x, min(max_x, nozzle_x))
        clamped_y = max(min_y, min(max_y, nozzle_y))
        clamped = (
            (clamped_probe_x != probe_x)
            or (clamped_probe_y != probe_y)
            or (clamped_x != nozzle_x)
            or (clamped_y != nozzle_y)
        )
        points.append(
            {
                "id": point_id,
                "label": label,
                "short_label": short_label,
                "x": round(clamped_x, 3),
                "y": round(clamped_y, 3),
                "probe_x": round(clamped_probe_x, 3),
                "probe_y": round(clamped_probe_y, 3),
                "clamped": clamped,
            }
        )
    return points


# Regexes for parsing M851 probe-offset reports from Marlin.
# Marlin emits the offsets in several formats depending on the build:
#   "M851 X-43.00 Y-9.00 Z-2.500"
#   "echo:M851 X-43.00 Y-9.00 Z-2.500"
#   "echo: M851 X-43 Y-9 Z-2.5"
#   "Probe Offset: X-43.00 Y-9.00 Z-2.500"
# We parse X and Y; Z offset is not needed by this plugin.
import re as _re  # noqa: E402

_PROBE_OFFSET_PATTERNS = (
    _re.compile(
        r"M851\s+X\s*(-?\d+(?:\.\d+)?)\s+Y\s*(-?\d+(?:\.\d+)?)(?:\s+Z\s*(-?\d+(?:\.\d+)?))?" ,
        _re.IGNORECASE,
    ),
    _re.compile(
        r"Probe\s*Offset[:\s]+X\s*(-?\d+(?:\.\d+)?)\s+Y\s*(-?\d+(?:\.\d+)?)(?:\s+Z\s*(-?\d+(?:\.\d+)?))?" ,
        _re.IGNORECASE,
    ),
)


def parse_probe_offset_line(line):
    """Return ``(x, y, z)`` floats parsed from an M851/probe-offset echo line, or None.

    The Z component is the probe trigger offset below the nozzle (typically
    negative). If the firmware echo does not include a Z field the returned
    z value is 0.0.
    """
    if not line:
        return None
    for pattern in _PROBE_OFFSET_PATTERNS:
        match = pattern.search(line)
        if match is not None:
            x = float(match.group(1))
            y = float(match.group(2))
            z = float(match.group(3)) if match.group(3) is not None else 0.0
            return x, y, z
    return None
