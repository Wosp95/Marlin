"""Z-offset calibration G-code generator.

Produces G-code for 5 single-layer squares printed left-to-right across the bed,
each at a slightly different Z-offset. The user picks the best-looking square
and the plugin applies that offset.
"""


def generate_z_calibration_gcode(
    center_z_offset,
    step_mm=0.02,
    current_z_offset=None,
    bed_temp=60,
    nozzle_temp=220,
    square_size=30.0,
    line_width=0.45,
    layer_height=0.2,
    first_layer_height=0.24,
    extrusion_multiplier=1.0,
    filament_diameter=1.75,
    print_speed=25,
    travel_speed=120,
    bed_x=235.0,
    bed_y=195.0,
):
    """Generate G-code for 5 Z-offset test squares.

    Each square is printed at a different effective first-layer height that
    simulates the requested absolute M851 Z offsets relative to the printer's
    current Z offset.
    Square 1 (leftmost) = most squished (more negative Z)
    Square 5 (rightmost) = least squished (more positive Z)

    Returns:
        dict with:
            - gcode: list of G-code lines
            - offsets: list of 5 (square_number, z_offset) tuples
            - center_offset: the middle value (square 3)
    """
    # Warm the nozzle partway while the bed heats so the calibration starts
    # sooner, but keep it below strong ooze temps for PLA+.
    standby_nozzle_temp = min(int(nozzle_temp), 150)

    # Calculate the 5 offsets: center ± 2 steps
    offsets = []
    for i in range(5):
        offset = center_z_offset + (i - 2) * step_mm
        offsets.append((i + 1, round(offset, 3)))

    baseline_z_offset = center_z_offset if current_z_offset is None else float(current_z_offset)

    # Layout: 5 squares evenly spaced across the X axis with margins
    margin_x = 20.0
    usable_x = bed_x - 2 * margin_x
    spacing = (usable_x - 5 * square_size) / 4
    start_y = (bed_y - square_size) / 2.0

    # Use a left-edge back-to-front purge line so the nozzle is primed before
    # the first square without crossing the main square lane.
    purge_x = 5.0
    purge_y_start = max(20.0, bed_y - 20.0)
    purge_y_end = min(bed_y - 20.0, max(20.0, bed_y * 0.2))

    # Extrusion math: E per mm of travel
    filament_area = 3.14159 * (filament_diameter / 2.0) ** 2
    extrusion_width = line_width
    e_per_mm = (extrusion_width * first_layer_height) / filament_area * extrusion_multiplier
    travel_retract_e = 1.5

    lines = []

    # Header
    lines.append("; === Z-Offset Calibration: 5 Test Squares ===")
    lines.append("; Square 1 (left) = most squished, Square 5 (right) = least squished")
    lines.append(f"; Current baseline offset: M851 Z{baseline_z_offset:.3f}")
    for sq_num, z_off in offsets:
        square_layer_z = max(0.02, first_layer_height + (z_off - baseline_z_offset))
        lines.append(f"; Square {sq_num}: target M851 Z{z_off:.3f}, print Z{square_layer_z:.3f}")
    lines.append("")

    # Preamble
    lines.append("G90")
    lines.append("M83")
    lines.append(f"M140 S{bed_temp}")
    lines.append(f"M104 S{standby_nozzle_temp}")
    lines.append(f"M190 S{bed_temp}")
    lines.append("G28")
    lines.append("M420 S1 Z10")
    lines.append("G1 Z5 F600")
    lines.append(f"G1 X{purge_x:.2f} Y{purge_y_start:.2f} F{travel_speed * 60:.0f}")
    lines.append(f"M109 S{nozzle_temp}")
    lines.append("G92 E0")
    lines.append(f"G1 Z{first_layer_height:.3f} F600")
    purge_length = abs(purge_y_start - purge_y_end)
    purge_e = purge_length * e_per_mm
    lines.append(f"G1 Y{purge_y_end:.2f} E{purge_e:.4f} F{print_speed * 60:.0f}")
    lines.append(f"G1 E-{travel_retract_e:.1f} F2100")
    lines.append("G1 Z2 F600")
    lines.append("")

    # Print each square
    for idx, (sq_num, z_off) in enumerate(offsets):
        sq_start_x = margin_x + idx * (square_size + spacing)
        sq_end_x = sq_start_x + square_size
        sq_start_y = start_y
        sq_end_y = start_y + square_size
        square_layer_z = max(0.02, first_layer_height + (z_off - baseline_z_offset))

        lines.append(f"; --- Square {sq_num}: Z offset = {z_off:.3f} ---")
        lines.append(f"M117 Square {sq_num}/5 Z={z_off:.3f}")

        # Move to start position
        lines.append(f"G1 Z5 F600")
        lines.append(f"G1 X{sq_start_x:.2f} Y{sq_start_y:.2f} F{travel_speed * 60:.0f}")
        lines.append(f"G1 Z{square_layer_z:.3f} F600")
        lines.append("G92 E0")
        lines.append(f"G1 E{travel_retract_e:.1f} F2100")
        lines.append("G92 E0")

        # Fill the square with rectilinear lines
        y = sq_start_y
        direction = 1  # 1 = left-to-right, -1 = right-to-left
        while y <= sq_end_y:
            if direction == 1:
                x_from = sq_start_x
                x_to = sq_end_x
            else:
                x_from = sq_end_x
                x_to = sq_start_x

            travel_dist = abs(x_to - x_from)
            e_amount = travel_dist * e_per_mm

            lines.append(f"G1 X{x_to:.2f} Y{y:.2f} E{e_amount:.4f} F{print_speed * 60:.0f}")

            y += line_width * 0.9  # slight overlap between lines
            if y <= sq_end_y:
                # Move to next line
                step_e = line_width * 0.9 * e_per_mm
                lines.append(f"G1 Y{y:.2f} E{step_e:.4f} F{print_speed * 60:.0f}")

            direction *= -1

        # Small retract between squares
        lines.append(f"G1 E-{travel_retract_e:.1f} F2100")
        lines.append(f"G1 Z2 F600")
        lines.append("")

    lines.append("; Calibration print finished. The chosen offset is only applied when the user confirms it.")
    lines.append("")

    # End
    lines.append("; === Calibration complete ===")
    lines.append("; Pick the square with the smoothest, most uniform surface")
    lines.append("; No gaps between lines = good. Transparent/rough = too high.")
    lines.append("; Dragging/ridges = too low.")
    lines.append("M117 Pick best square 1-5")
    lines.append("M118 TESTLEVEL_Z_CAL_DONE")
    lines.append("G1 Z10 F600")
    lines.append(f"G1 X0 Y{bed_y:.0f} F{travel_speed * 60:.0f}")
    lines.append("M104 S0")
    lines.append("M140 S0")
    lines.append("M107")
    lines.append("M84")

    return {
        "gcode": lines,
        "offsets": offsets,
        "center_offset": center_z_offset,
        "step_mm": step_mm,
    }
