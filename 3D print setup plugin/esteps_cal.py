# coding=utf-8
"""E-Steps calibration logic for the Printer Setup plugin."""

import re

# Pattern to match M92 E-steps from M503 output
# Example line: echo:  M92 X80.00 Y80.00 Z400.00 E93.00
M92_ESTEPS_PATTERN = re.compile(r"M92\b.*\bE([\d.]+)", re.IGNORECASE)


def parse_esteps_from_m503(line):
    """Extract E-steps/mm value from an M503 response line containing M92.

    Returns the float value or None if the line doesn't contain E-steps info.
    """
    if "M92" not in line.upper():
        return None
    match = M92_ESTEPS_PATTERN.search(line)
    if match:
        return float(match.group(1))
    return None


def calculate_new_esteps(current_esteps, requested_length, actual_extruded):
    """Calculate new E-steps value based on measured extrusion.

    Args:
        current_esteps: Current steps/mm value from firmware.
        requested_length: How much was commanded to extrude (mm).
        actual_extruded: How much actually came out (mm), measured as
                         mark_distance - remaining_distance.

    Returns:
        dict with new_esteps, deviation, pct_error, or raises ValueError.
    """
    if actual_extruded <= 0:
        raise ValueError("Invalid measurement - nothing extruded")
    if current_esteps <= 0:
        raise ValueError("Current E-steps value is invalid")

    new_esteps = round(current_esteps * (requested_length / actual_extruded), 2)
    deviation = round(actual_extruded - requested_length, 2)
    pct_error = round((deviation / requested_length) * 100, 1)

    return {
        "current_esteps": current_esteps,
        "new_esteps": new_esteps,
        "requested": requested_length,
        "actual_extruded": round(actual_extruded, 2),
        "deviation": deviation,
        "pct_error": pct_error,
    }
