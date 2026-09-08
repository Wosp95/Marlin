# coding=utf-8

import copy
import json
import math
import os
import re
import threading
import time

import flask
import octoprint.plugin

from .leveling import (
    LCD_STATUS_MAX_CHARS,
    build_probe_noise_summary,
    build_tightening_target_profile,
    build_human_instruction,
    compute_probe_points,
    direction_for_delta,
    format_lcd_instruction,
    parse_probe_offset_line,
)
from .esteps_cal import calculate_new_esteps, parse_esteps_from_m503


PROBE_RESULT_PATTERN = re.compile(
    r"Bed\s+X:\s*(-?\d+(?:\.\d+)?)\s+Y:\s*(-?\d+(?:\.\d+)?)\s+Z:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
Z_PROBE_STATE_PATTERN = re.compile(r"z_probe:\s*(TRIGGERED|open)", re.IGNORECASE)


class TestLevelPlugin(
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.TemplatePlugin,
):
    def __init__(self):
        self._state_lock = threading.RLock()
        self._history = []
        self._current_run_id = None
        self._z_cal_offsets = []
        self._z_calibration_running = False
        self._pending_z_calibration_request = None
        self._z_search_state = self._default_z_search_state()
        # CR Touch X/Y/Z offset from the nozzle. None until M851 has been parsed.
        self._probe_offset_x_mm = None
        self._probe_offset_y_mm = None
        self._probe_offset_z_mm = None
        self._probe_points = self._compute_probe_points()
        self._probe_noise_remaining = 0
        self._probe_noise_samples = []
        self._pending_probe_ids = []
        self._active_assist_corner = None
        self._assist_trigger_monitor = None
        self._assist_trigger_corner = None
        self._guide_avoid_corner_id = None
        # When set, the plugin sent M851 at the start of the workflow and is
        # waiting for the offset echo before continuing with heat/home/probe.
        self._pending_start_bed_temp = None
        self._pending_start_noise_samples = None
        self._offset_query_timer = None
        self._last_logged_stage = None
        self._last_logged_status = None
        self._state = self._make_idle_state()
        # E-Steps calibration state
        self._esteps_current = None
        self._esteps_waiting = False
        self._esteps_target_temp = 200

        # PID tune state
        self._pid_running = False
        self._pid_results = None  # {"kp": float, "ki": float, "kd": float}


    def get_settings_defaults(self):
        return {
            "bed_temp": 60,
            "safe_z": 10.0,
            "pre_travel_lift_z": 2.0,
            "minimum_assist_target_z": 0.1,
            "travel_feedrate": 6000,
            "z_feedrate": 600,
            "screw_pitch_mm": 0.7,
            "minimum_initial_tightening_turns": 0.0,
            "tolerance_mm": 0.03,
            # Stock Ender 3 / Pro / V2 bed is 235 x 235.
            "bed_size_x_mm": 235.0,
            "bed_size_y_mm": 235.0,
            # Probe tip should land above the bed screws. On stock Ender 3 Pro
            # the screws are approximately 32 mm in from each edge, but bed
            # clips can extend further. Use 40 mm to stay clear of clips.
            "corner_inset_mm": 40.0,
            # Optional explicit nozzle coordinates where the CR Touch is
            # vertically above each bed screw. When all four are present, they
            # override the inferred inset layout and the center point is derived
            # from their midpoint.
            # Leave explicit screw coordinates unset by default. If these are
            # not carefully calibrated for the current printer, they can place
            # the nozzle over the wrong physical wheel location.
            "front_left_probe_x_mm": None,
            "front_left_probe_y_mm": None,
            "front_right_probe_x_mm": None,
            "front_right_probe_y_mm": None,
            "rear_right_probe_x_mm": None,
            "rear_right_probe_y_mm": None,
            "rear_left_probe_x_mm": None,
            "rear_left_probe_y_mm": None,
            "raise_label": "counter-clockwise",
            "lower_label": "clockwise",
            # Viewpoint the user looks at the wheels from. "from_below" is the
            # standard Ender 3 Pro view (squat next to the printer, look up).
            "viewpoint": "from_below",
            # Documented for clarity; not used yet for direction parity but
            # reserved for the future Marlin-G35-style thread parameter.
            "screw_thread": "M4",
            # When True, push a short instruction to the printer LCD via M117
            # whenever a corner is positioned for assist.
            "show_on_printer_lcd": True,
            # When True, after positioning at a corner the queue blocks on
            # M0 until the user clicks the printer encoder. Useful when
            # working at the printer without watching the OctoPrint UI.
            "wait_for_encoder_click": False,
            # Repeat the center probe a few times before the full pass so the
            # plugin can estimate CR Touch repeatability and stop if the probe
            # is too noisy to support trustworthy wheel guidance.
            "probe_noise_sample_count": 5,
            "probe_noise_hard_fail_sigma_mm": 0.05,
            # When True, run a full mesh probe (G29) after all corners are
            # within tolerance so firmware mesh compensation is up to date.
            "auto_mesh_on_done": True,
            # Inset from bed edges for the G29 mesh probe grid (probe-tip coordinates).
            # Keeps the mesh away from bed clips. Use a value >= corner_inset_mm.
            "mesh_inset_mm": 40.0,
        }

    def get_assets(self):
        return {
            "js": ["js/testlevel.js", "js/testlevel_esteps.js"],
            "css": ["css/testlevel.css"],
        }

    def get_template_configs(self):
        return [
            {
                "type": "tab",
                "name": "Printer Setup",
                "icon": "wrench",
                "template": "testlevel_tab.jinja2",
                "data_bind": "visible: loginState.isUser()",
            },
        ]

    def get_api_commands(self):
        return {
            "abort_workflow": [],
            "home": [],
            "reprobe": [],
            "start_workflow": [],
            "z_calibrate": [],
            "z_search_start": [],
            "z_search_refine": [],
            "apply_z_offset": [],
            "esteps_start": ["target_temp"],
            "esteps_calculate_and_apply": ["measured_remaining"],
            "esteps_save": [],
            "pid_start": ["target_temp"],
            "pid_apply": [],
        }

    def is_api_protected(self):
        return True

    def on_api_get(self, request):
        return flask.jsonify(self._public_state())

    def on_api_command(self, command, data):
        self._logger.info("API command received: command=%s payload=%s", command, data or {})
        if command == "abort_workflow":
            return self._handle_abort_workflow()

        if command == "home":
            return self._handle_home()

        if command == "start_workflow":
            return self._handle_start_workflow(data)

        if command == "reprobe":
            return self._handle_reprobe()

        if command == "z_calibrate":
            return self._handle_z_calibrate(data)

        if command == "z_search_start":
            return self._handle_z_search_start(data)

        if command == "z_search_refine":
            return self._handle_z_search_refine(data)

        if command == "apply_z_offset":
            return self._handle_apply_z_offset(data)

        if command == "pid_start":
            return self._handle_pid_start(data)
        if command == "pid_apply":
            return self._handle_pid_apply()

        if command == "esteps_start":
            return self._handle_esteps_start(data)
        if command == "esteps_calculate_and_apply":
            return self._handle_esteps_calculate_and_apply(data)
        if command == "esteps_save":
            return self._handle_esteps_save()

        return flask.make_response("Unsupported command", 400)

    def _handle_home(self):
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        self._printer.commands(["G28"])
        self._logger.info("Manual home requested; queued commands=%s", ["G28"])

        return flask.jsonify({"ok": True, "message": "Homing command queued"})

    def _handle_start_workflow(self, data):
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        with self._state_lock:
            if (
                self._probe_noise_remaining
                or self._pending_probe_ids
                or self._active_assist_corner is not None
                or self._pending_start_bed_temp is not None
            ):
                return (
                    flask.jsonify({"ok": False, "error": "A leveling workflow is already active"}),
                    409,
                )

            bed_temp = self._parse_bed_temp(data.get("bed_temp") if data else None)
            probe_noise_sample_count = max(2, int(self._settings.get_int(["probe_noise_sample_count"])))

            # Reset everything but keep the cached probe offset (if any) so we
            # don't lose what M851 already told us on a previous run.
            self._probe_noise_remaining = 0
            self._probe_noise_samples = []
            self._pending_probe_ids = []
            self._active_assist_corner = None
            self._cancel_assist_trigger_monitor()
            self._guide_avoid_corner_id = None
            self._pending_start_bed_temp = bed_temp
            self._pending_start_noise_samples = probe_noise_sample_count
            self._history = []
            self._current_run_id = "run-%d" % int(time.time() * 1000)
            self._state = self._make_idle_state()

            self._state.update(
                {
                    "bed_temp": bed_temp,
                    "stage": "querying_offsets",
                    "status": (
                        "Querying CR Touch offsets from Marlin (M851) so the probe lands "
                        f"{self._probe_point_status_text()}."
                    ),
                }
            )
            self._touch_state()
            self._append_history("workflow_started", bed_temp=bed_temp, probe_noise_samples=probe_noise_sample_count)

        # Send M851 first; the line handler will parse the echo and continue
        # the workflow. Set up a fallback so we still proceed if Marlin does
        # not echo a recognized offset line in time.
        self._cancel_offset_query_timer()
        self._printer.commands(["M851"])
        self._logger.info("Queued M851 to capture CR Touch offsets before probing.")
        try:
            self._offset_query_timer = threading.Timer(8.0, self._on_offset_query_timeout)
            self._offset_query_timer.daemon = True
            self._offset_query_timer.start()
        except Exception:  # pragma: no cover - threading.Timer is best-effort
            self._offset_query_timer = None

        return flask.jsonify(
            {
                "ok": True,
                "message": "Querying CR Touch offsets, then starting the heated probe workflow.",
                "state": self._public_state(),
            }
        )

    def _cancel_offset_query_timer(self):
        timer = self._offset_query_timer
        self._offset_query_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # pragma: no cover
                pass

    def _cancel_assist_trigger_monitor(self):
        timer = self._assist_trigger_monitor
        self._assist_trigger_monitor = None
        self._assist_trigger_corner = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # pragma: no cover
                pass

    def _schedule_assist_trigger_monitor(self, corner_id, delay_seconds=2.0):
        # Poll M119 periodically to detect when the user loosens the wheel enough
        # to trigger the deployed CR Touch. With the Z-offset fix the probe pin is
        # positioned above the bed at the target height, so a trigger can only happen
        # when the user physically raises the bed into it.
        self._cancel_assist_trigger_monitor()
        self._assist_trigger_corner = corner_id

        def _poll_trigger():
            with self._state_lock:
                if (
                    self._assist_trigger_corner != corner_id
                    or self._state.get("stage") != "guided"
                    or self._active_assist_corner != corner_id
                ):
                    return
            self._printer.commands(["M119"])
            # Re-schedule the next poll
            self._assist_trigger_monitor = threading.Timer(1.5, _poll_trigger)
            self._assist_trigger_monitor.daemon = True
            self._assist_trigger_monitor.start()

        self._logger.info(
            "Scheduling assist trigger monitor for %s (polling M119 every 1.5s after %.1fs delay).",
            corner_id,
            delay_seconds,
        )
        self._assist_trigger_monitor = threading.Timer(delay_seconds, _poll_trigger)
        self._assist_trigger_monitor.daemon = True
        self._assist_trigger_monitor.start()

    def _queue_full_reprobe(self, status_text, history_event):
        self._logger.info(
            "Queueing full reprobe: reason=%s stage=%s assist_corner=%s",
            history_event, self._state.get("stage"), self._active_assist_corner,
        )
        self._cancel_assist_trigger_monitor()
        self._guide_avoid_corner_id = None
        self._begin_corner_probe_pass()
        self._state["status"] = status_text
        self._state["assist"] = None
        self._active_assist_corner = None
        self._touch_state()
        self._append_history(history_event)

        commands = ["M420 S0", "G90"] + self._next_probe_commands()
        self._log_command_batch("reprobe", commands)
        self._send_commands_async(commands)

    def _on_offset_query_timeout(self):
        with self._state_lock:
            if self._pending_start_bed_temp is None:
                pending_z_request = self._pending_z_calibration_request
                if pending_z_request is None:
                    return
                self._pending_z_calibration_request = None
                self._z_calibration_running = False
                if "z_calibration" in self._state:
                    self._state["z_calibration"]["running"] = False
                self._state["status"] = (
                    "Marlin did not echo the current M851 Z offset in time. Z calibration was not started so the printed labels do not drift away from the real printer baseline."
                )
                self._touch_state()
                return
            offset_x = self._probe_offset_x_mm if self._probe_offset_x_mm is not None else 0.0
            offset_y = self._probe_offset_y_mm if self._probe_offset_y_mm is not None else 0.0
            offset_z = self._probe_offset_z_mm if self._probe_offset_z_mm is not None else 0.0
            self._probe_offset_x_mm = offset_x
            self._probe_offset_y_mm = offset_y
            self._probe_offset_z_mm = offset_z
            self._probe_points = self._compute_probe_points()
            self._state["probe_points"] = copy.deepcopy(self._probe_points)
            self._state["status"] = (
                "Marlin did not echo M851 in time. Continuing with cached or zero probe offsets - check the "
                "probe XY in the panel before relying on results."
            )
            self._touch_state()
        self._logger.warning(
            "M851 echo not received within timeout; proceeding with offsets x=%.3f y=%.3f.",
            self._probe_offset_x_mm,
            self._probe_offset_y_mm,
        )
        self._begin_workflow_after_offsets()

    def _configured_probe_targets(self):
        # The normal runtime path is inset-only. Explicit screw coordinates are
        # intentionally disabled here because stale or partially calibrated
        # values can send the probe to the wrong physical location.
        return None

    def _probe_point_status_text(self):
        return f"{self._settings.get_float(['corner_inset_mm']):.0f} mm in from each bed wheel"

    def _begin_workflow_after_offsets(self):
        with self._state_lock:
            bed_temp = self._pending_start_bed_temp
            probe_noise_sample_count = self._pending_start_noise_samples
            if bed_temp is None or probe_noise_sample_count is None:
                return
            self._pending_start_bed_temp = None
            self._pending_start_noise_samples = None

            self._probe_points = self._compute_probe_points()
            self._probe_noise_remaining = probe_noise_sample_count
            self._probe_noise_samples = []
            self._pending_probe_ids = []
            self._active_assist_corner = None
            self._cancel_assist_trigger_monitor()
            self._state.update(
                {
                    "bed_temp": bed_temp,
                    "stage": "probe_noise",
                    "status": (
                        f"Heating the bed to {bed_temp}C, homing, then measuring CR Touch repeatability "
                        f"with {probe_noise_sample_count} center probes."
                    ),
                    "probe_points": copy.deepcopy(self._probe_points),
                    "probe_offset": {
                        "x": self._probe_offset_x_mm,
                        "y": self._probe_offset_y_mm,
                    },
                    "probe_noise": {
                        "expected_samples": probe_noise_sample_count,
                        "sample_count": 0,
                        "samples": [],
                        "sigma_mm": None,
                        "recommended_tolerance_mm": None,
                        "passed": None,
                        "hard_fail_sigma_mm": self._settings.get_float(["probe_noise_hard_fail_sigma_mm"]),
                    },
                }
            )
            self._touch_state()

        commands = [
            f"M140 S{bed_temp}",
            f"M190 S{bed_temp}",
            "G28",
            "M420 S0",
            "G90",
            self._safe_z_move_command(),
        ]
        commands.extend(self._next_probe_noise_commands())

        self._logger.info(
            "Starting guided leveling workflow: bed_temp=%s probe_noise_samples=%s probe_offset=(%.3f, %.3f)",
            bed_temp,
            probe_noise_sample_count,
            self._probe_offset_x_mm,
            self._probe_offset_y_mm,
        )
        self._log_command_batch("start_workflow", commands)
        self._send_commands_async(commands)

    def _handle_reprobe(self):
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        with self._state_lock:
            if self._probe_noise_remaining:
                return (
                    flask.jsonify({"ok": False, "error": "Wait for the probe noise check to finish first"}),
                    409,
                )
            if self._pending_probe_ids:
                return (
                    flask.jsonify({"ok": False, "error": "A probe pass is already running"}),
                    409,
                )
            if not self._state.get("results") or not self._state["results"].get("corners"):
                return (
                    flask.jsonify({"ok": False, "error": "Run the full workflow before re-probing"}),
                    409,
                )

            self._queue_full_reprobe(
                "Re-probing all 5 points to refresh the target plane.",
                "manual_reprobe_requested",
            )

        return flask.jsonify(
            {
                "ok": True,
                "message": "Queued a full 5-point re-probe.",
                "state": self._public_state(),
            }
        )

    def _handle_z_calibrate(self, data):
        center_offset = None
        if data and data.get("center_offset") is not None:
            try:
                center_offset = float(data["center_offset"])
            except (TypeError, ValueError):
                pass

        if center_offset is None:
            center_offset = self._probe_offset_z_mm if self._probe_offset_z_mm is not None else -0.95

        step_mm = 0.02
        if data and data.get("step_mm") is not None:
            try:
                step_mm = float(data["step_mm"])
            except (TypeError, ValueError):
                pass

        result = self._queue_z_calibration(center_offset, step_mm)
        if isinstance(result, tuple):
            return result

        if result.get("pending_offset_query"):
            return flask.jsonify({
                "ok": True,
                "message": "Querying the printer's current M851 Z offset before starting the 5-square calibration.",
                "pending_offset_query": True,
                "center_offset": center_offset,
                "step_mm": step_mm,
            })

        return flask.jsonify({
            "ok": True,
            "message": "Printing 5 test squares. Pick the best one (1=most squished, 5=least).",
            "offsets": result["offsets"],
            "center_offset": center_offset,
            "step_mm": step_mm,
        })

    def _handle_z_search_start(self, data):
        center_offset = self._coerce_float(data, "center_offset", -0.70)
        coarse_step_mm = abs(self._coerce_float(data, "coarse_step_mm", 0.10))
        refine_step_mm = abs(self._coerce_float(data, "refine_step_mm", 0.02))

        search_state = self._default_z_search_state()
        search_state.update(
            {
                "active": True,
                "pass_index": 1,
                "mode": "coarse",
                "coarse_step_mm": coarse_step_mm,
                "refine_step_mm": refine_step_mm,
                "status": (
                    "Coarse Z-offset search printed. Pick the best square to run a tighter refinement pass."
                ),
            }
        )

        result = self._queue_z_calibration(center_offset, coarse_step_mm, search_state=search_state)
        if isinstance(result, tuple):
            return result

        if result.get("pending_offset_query"):
            return flask.jsonify({
                "ok": True,
                "message": "Querying the printer's current M851 Z offset before starting the coarse Z-offset search pass.",
                "pending_offset_query": True,
                "center_offset": center_offset,
                "step_mm": coarse_step_mm,
                "search": copy.deepcopy(self._z_search_state),
            })

        return flask.jsonify({
            "ok": True,
            "message": "Printed the coarse Z-offset search pass. Pick the best square to refine.",
            "offsets": result["offsets"],
            "center_offset": center_offset,
            "step_mm": coarse_step_mm,
            "search": copy.deepcopy(self._z_search_state),
        })

    def _handle_z_search_refine(self, data):
        square = self._parse_square_choice(data)
        if isinstance(square, tuple):
            return square

        if not self._z_cal_offsets:
            return flask.jsonify({"ok": False, "error": "Run a Z calibration or Z search pass first"}), 409

        chosen_offset = self._z_cal_offsets[square - 1][1]
        refine_step_mm = abs(self._coerce_float(data, "step_mm", self._z_search_state.get("refine_step_mm", 0.02)))

        search_state = copy.deepcopy(self._z_search_state) if self._z_search_state else self._default_z_search_state()
        search_state.update(
            {
                "active": True,
                "pass_index": int(search_state.get("pass_index") or 1) + 1,
                "mode": "refine",
                "refine_step_mm": refine_step_mm,
                "selected_square": square,
                "selected_offset": chosen_offset,
                "status": (
                    f"Refining around square {square} at Z {chosen_offset:.3f} with ±{refine_step_mm * 2:.3f} mm coverage."
                ),
            }
        )

        result = self._queue_z_calibration(chosen_offset, refine_step_mm, search_state=search_state)
        if isinstance(result, tuple):
            return result

        if result.get("pending_offset_query"):
            return flask.jsonify({
                "ok": True,
                "message": f"Querying the printer's current M851 Z offset before starting the refinement pass around square {square}.",
                "pending_offset_query": True,
                "center_offset": chosen_offset,
                "step_mm": refine_step_mm,
                "search": copy.deepcopy(self._z_search_state),
            })

        return flask.jsonify({
            "ok": True,
            "message": f"Printed a refinement pass centered on square {square} ({chosen_offset:.3f}).",
            "offsets": result["offsets"],
            "center_offset": chosen_offset,
            "step_mm": refine_step_mm,
            "search": copy.deepcopy(self._z_search_state),
        })

    def _queue_z_calibration(self, center_offset, step_mm, search_state=None):
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        with self._state_lock:
            if self._z_calibration_running:
                return (
                    flask.jsonify({
                        "ok": False,
                        "error": "A Z-offset calibration pass is already running. Wait for it to finish before starting another one.",
                    }),
                    409,
                )
            if self._probe_offset_z_mm is None:
                pending_search_state = copy.deepcopy(search_state) if search_state is not None else self._default_z_search_state()
                pending_search_state.update(
                    {
                        "active": True,
                        "status": "Reading the printer's current M851 Z offset before starting this pass.",
                    }
                )
                self._z_calibration_running = True
                self._pending_z_calibration_request = {
                    "center_offset": center_offset,
                    "step_mm": step_mm,
                    "search_state": copy.deepcopy(search_state) if search_state is not None else None,
                }
                self._state["z_calibration"] = {
                    "offsets": [],
                    "center_offset": center_offset,
                    "running": True,
                    "step_mm": step_mm,
                    "search": pending_search_state,
                }
                self._state["status"] = "Querying the printer's current M851 Z offset before starting Z calibration so the printed labels match the real baseline."
                self._touch_state()
                self._cancel_offset_query_timer()
                self._printer.commands(["M851"])
                try:
                    self._offset_query_timer = threading.Timer(8.0, self._on_offset_query_timeout)
                    self._offset_query_timer.daemon = True
                    self._offset_query_timer.start()
                except Exception:
                    self._offset_query_timer = None
                return {
                    "offsets": [],
                    "center_offset": center_offset,
                    "pending_offset_query": True,
                    "step_mm": step_mm,
                }
            self._z_calibration_running = True

        return self._execute_z_calibration(center_offset, step_mm, search_state=search_state)

    def _execute_z_calibration(self, center_offset, step_mm, search_state=None):
        from .zoffset_cal import generate_z_calibration_gcode

        bed_temp = int(self._settings.get_int(["bed_temp"]))
        try:
            result = generate_z_calibration_gcode(
                center_z_offset=center_offset,
                step_mm=step_mm,
                current_z_offset=self._probe_offset_z_mm,
                bed_temp=bed_temp,
                nozzle_temp=220,
                bed_x=self._settings.get_float(["bed_size_x_mm"]),
                bed_y=self._settings.get_float(["bed_size_y_mm"]) - self._settings.get_float(["mesh_inset_mm"]),
            )

            self._printer.commands(result["gcode"])
            self._logger.info(
                "Z-offset calibration started: center=%.3f step=%.3f offsets=%s",
                center_offset, step_mm, result["offsets"],
            )
        except Exception:
            with self._state_lock:
                self._z_calibration_running = False
                if "z_calibration" in self._state:
                    self._state["z_calibration"]["running"] = False
            raise

        with self._state_lock:
            self._set_z_calibration_state(result, search_state=search_state)

        return result

    def _resume_pending_z_calibration(self):
        with self._state_lock:
            pending_request = self._pending_z_calibration_request
            self._pending_z_calibration_request = None

        if pending_request is None:
            return

        try:
            result = self._execute_z_calibration(
                pending_request["center_offset"],
                pending_request["step_mm"],
                search_state=pending_request["search_state"],
            )
        except Exception:
            with self._state_lock:
                self._z_calibration_running = False
                if "z_calibration" in self._state:
                    self._state["z_calibration"]["running"] = False
                self._state["status"] = "Failed to start Z calibration after reading M851."
                self._touch_state()
            raise

        if isinstance(result, tuple):
            with self._state_lock:
                self._z_calibration_running = False
                if "z_calibration" in self._state:
                    self._state["z_calibration"]["running"] = False
                self._state["status"] = result[0].get("error", "Failed to start Z calibration after reading M851.")
                self._touch_state()

    def _set_z_calibration_state(self, result, search_state=None):
        self._z_cal_offsets = list(result["offsets"])
        self._z_search_state = copy.deepcopy(search_state) if search_state is not None else self._default_z_search_state()
        self._state["z_calibration"] = {
            "offsets": [
                {"square": square_number, "z_offset": z_offset}
                for square_number, z_offset in self._z_cal_offsets
            ],
            "center_offset": result["center_offset"],
            "running": self._z_calibration_running,
            "step_mm": result["step_mm"],
            "search": copy.deepcopy(self._z_search_state),
        }
        self._state["status"] = (
            self._z_search_state["status"]
            if self._z_search_state.get("active")
            else "Printing Z-offset test squares. Pick the best square when the print finishes."
        )
        self._touch_state()

    def _coerce_float(self, data, key, default):
        if data and data.get(key) is not None:
            try:
                return float(data[key])
            except (TypeError, ValueError):
                return default
        return default

    def _parse_square_choice(self, data):
        if not data or data.get("square") is None:
            return flask.jsonify({"ok": False, "error": "Specify which square (1-5) looked best"}), 400

        try:
            square = int(data["square"])
        except (TypeError, ValueError):
            return flask.jsonify({"ok": False, "error": "square must be a number 1-5"}), 400

        if square < 1 or square > 5:
            return flask.jsonify({"ok": False, "error": "square must be 1-5"}), 400

        return square

    def _default_z_search_state(self):
        return {
            "active": False,
            "pass_index": 0,
            "mode": None,
            "coarse_step_mm": 0.10,
            "refine_step_mm": 0.02,
            "selected_square": None,
            "selected_offset": None,
            "status": "Run Z Search to print a wide pass, then refine around the best square.",
        }

    def _handle_apply_z_offset(self, data):
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        square = self._parse_square_choice(data)
        if isinstance(square, tuple):
            return square

        if not self._z_cal_offsets:
            return flask.jsonify({"ok": False, "error": "Run z_calibrate first"}), 409

        chosen_offset = self._z_cal_offsets[square - 1][1]
        self._printer.commands([
            f"M851 Z{chosen_offset:.3f}",
            "M500",
            f"M117 Z offset: {chosen_offset:.3f}",
        ])
        self._probe_offset_z_mm = chosen_offset
        self._logger.info("Applied Z offset from calibration: square=%d offset=%.3f", square, chosen_offset)
        with self._state_lock:
            self._z_search_state.update(
                {
                    "active": False,
                    "mode": None,
                    "selected_square": square,
                    "selected_offset": chosen_offset,
                    "status": f"Applied Z offset {chosen_offset:.3f} from square {square}.",
                }
            )
            if "z_calibration" in self._state:
                self._state["z_calibration"]["running"] = False
                self._state["z_calibration"]["search"] = copy.deepcopy(self._z_search_state)
            self._state["status"] = self._z_search_state["status"]
            self._touch_state()

        return flask.jsonify({
            "ok": True,
            "message": f"Applied Z offset {chosen_offset:.3f} mm from square {square} and saved to EEPROM.",
            "z_offset": chosen_offset,
            "square": square,
        })

    def _handle_abort_workflow(self):
        with self._state_lock:
            workflow_active = bool(
                self._probe_noise_remaining
                or self._pending_probe_ids
                or self._active_assist_corner is not None
                or self._pending_start_bed_temp is not None
                or self._state.get("stage") not in {"idle", "error", "done"}
            )
            if not workflow_active:
                return (
                    flask.jsonify({"ok": False, "error": "No leveling workflow is active"}),
                    409,
                )

            bed_temp = self._state.get("bed_temp", int(self._settings.get_int(["bed_temp"])))
            self._cancel_offset_query_timer()
            self._probe_noise_remaining = 0
            self._probe_noise_samples = []
            self._pending_probe_ids = []
            self._active_assist_corner = None
            self._cancel_assist_trigger_monitor()
            self._pending_start_bed_temp = None
            self._pending_start_noise_samples = None
            self._guide_avoid_corner_id = None
            self._state = self._make_idle_state()

            self._state["bed_temp"] = bed_temp
            self._state["status"] = "Workflow aborted. Start again from a loose-wheel baseline when ready."
            self._touch_state()
            self._append_history("workflow_aborted")

        commands = ["M117 Test Level Abort", "M402", self._safe_z_move_command()]
        self._log_command_batch("abort_workflow", commands)
        self._send_commands_async(commands)
        return flask.jsonify(
            {
                "ok": True,
                "message": "Aborted the active leveling workflow",
                "state": self._public_state(),
            }
        )

    def _ensure_printer_ready(self):
        if not self._printer.is_operational():
            self._logger.warning("Printer readiness check failed: printer is not operational")
            return flask.jsonify({"ok": False, "error": "Printer is not operational"}), 409

        if self._printer.is_printing() or self._printer.is_paused():
            self._logger.warning("Printer readiness check failed: printer is busy (printing=%s paused=%s)", self._printer.is_printing(), self._printer.is_paused())
            return flask.jsonify({"ok": False, "error": "Printer must be idle before leveling"}), 409

        return None

    def _parse_bed_temp(self, raw_value):
        default_temp = int(self._settings.get_int(["bed_temp"]))
        if raw_value in (None, ""):
            self._logger.info("No bed temperature override supplied; using default=%sC", default_temp)
            return default_temp

        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            self._logger.warning("Invalid bed temperature override=%r; using default=%sC", raw_value, default_temp)
            return default_temp

        clamped = min(max(parsed, 0), 110)
        if clamped != parsed:
            self._logger.warning("Clamped requested bed temperature from %sC to %sC", parsed, clamped)
        else:
            self._logger.info("Using requested bed temperature=%sC", clamped)
        return clamped

    def _minimum_initial_tightening_turns(self):
        configured_turns = float(self._settings.get_float(["minimum_initial_tightening_turns"]))
        if abs(configured_turns - 0.5) < 1e-9:
            configured_turns = 0.0
            settings_set = getattr(self._settings, "set", None)
            settings_save = getattr(self._settings, "save", None)
            if callable(settings_set):
                settings_set(["minimum_initial_tightening_turns"], 0.0)
                if callable(settings_save):
                    settings_save()

        return max(0.0, configured_turns)

    def _target_plane_strategy_text(self):
        minimum_turns = self._minimum_initial_tightening_turns()
        if minimum_turns < 0.01:
            return (
                "The plugin will probe the same 5 points, build the target plane from the measured loose "
                "baseline, and only ask for the tightening that the measurements actually need."
            )

        return (
            "The plugin will probe the same 5 points and build a target plane that requires at least "
            f"{minimum_turns:.2f} turns of tightening from the loose baseline."
        )

    def _minimum_initial_tightening_mm(self):
        return self._minimum_initial_tightening_turns() * self._settings.get_float(["screw_pitch_mm"])

    def _refresh_idle_probe_points_from_settings(self):
        if not hasattr(self, "_settings") or self._settings is None:
            return
        if self._state.get("stage") != "idle":
            return

        refreshed_points = self._compute_probe_points()
        if refreshed_points == self._probe_points:
            return

        self._probe_points = refreshed_points
        self._state["probe_points"] = copy.deepcopy(self._probe_points)
        self._touch_state()

    def _public_state(self):
        with self._state_lock:
            self._refresh_idle_probe_points_from_settings()
            return copy.deepcopy(self._state)

    def _make_idle_state(self):
        return {
            "assist": None,
            "bed_temp": int(self._settings.get_int(["bed_temp"])) if hasattr(self, "_settings") else 60,
            "defaults": {
                "lower_label": self._settings.get(["lower_label"]) if hasattr(self, "_settings") else "clockwise",
                "raise_label": self._settings.get(["raise_label"]) if hasattr(self, "_settings") else "counter-clockwise",
                "screw_pitch_mm": self._settings.get_float(["screw_pitch_mm"]) if hasattr(self, "_settings") else 0.7,
                "minimum_initial_tightening_turns": self._minimum_initial_tightening_turns() if hasattr(self, "_settings") else 0.0,
                "tolerance_mm": self._settings.get_float(["tolerance_mm"]) if hasattr(self, "_settings") else 0.03,
            },
            "effective_tolerance_mm": self._settings.get_float(["tolerance_mm"]) if hasattr(self, "_settings") else 0.03,
            "error": None,
            "guide": None,
            "history": copy.deepcopy(self._history),
            "measurements": [],
            "probe_noise": None,
            "probe_points": copy.deepcopy(self._probe_points),
            "results": {},
            "target_profile": None,
            "stage": "idle",
            "status": (
                "Press Run to start. The plugin will probe all 5 points and walk you corner-by-corner until the bed is parallel to the gantry."
            ),
            "updated_at": time.time(),
            "z_calibration": {
                "offsets": [],
                "center_offset": None,
                "running": False,
                "step_mm": None,
                "search": copy.deepcopy(self._z_search_state) if hasattr(self, "_z_search_state") else self._default_z_search_state(),
            },
        }

    def _touch_state(self):
        self._state["updated_at"] = time.time()
        stage = self._state.get("stage")
        status = self._state.get("status")
        if stage != self._last_logged_stage:
            self._logger.info("State transition: %s -> %s", self._last_logged_stage, stage)
            self._last_logged_stage = stage
        if status != self._last_logged_status:
            self._logger.info("State status: %s", status)
            self._last_logged_status = status

    def _history_file_path(self):
        if not hasattr(self, "get_plugin_data_folder"):
            return None
        try:
            folder = self.get_plugin_data_folder()
            os.makedirs(folder, exist_ok=True)
            return os.path.join(folder, "workflow-history.jsonl")
        except Exception:  # pragma: no cover
            return None

    def _append_history(self, event_type, **details):
        entry = {
            "event": event_type,
            "run_id": self._current_run_id,
            "stage": self._state.get("stage") if hasattr(self, "_state") else None,
            "timestamp": time.time(),
        }
        entry.update(details)

        with self._state_lock:
            self._history.append(entry)
            self._history = self._history[-200:]
            if hasattr(self, "_state"):
                self._state["history"] = copy.deepcopy(self._history)

        history_path = self._history_file_path()
        if history_path is None:
            return
        try:
            with open(history_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
        except Exception as exc:  # pragma: no cover
            self._logger.warning("Failed to append workflow history entry: %s", exc)

    def _compute_probe_points(self):
        """Build the 5 nozzle XY targets so the probe tip lands `inset` mm in from each wheel.

        Uses settings for bed size and corner inset, and the cached M851
        offsets if known (else 0). Safe to call before `self._settings` exists
        (during ``__init__``) - we fall back to the documented defaults so the
        idle state can render without crashing.
        """
        if hasattr(self, "_settings") and self._settings is not None:
            bed_x = self._settings.get_float(["bed_size_x_mm"])
            bed_y = self._settings.get_float(["bed_size_y_mm"])
            inset = self._settings.get_float(["corner_inset_mm"])
        else:
            bed_x = 235.0
            bed_y = 235.0
            inset = 25.0

        offset_x = self._probe_offset_x_mm if self._probe_offset_x_mm is not None else 0.0
        offset_y = self._probe_offset_y_mm if self._probe_offset_y_mm is not None else 0.0

        # Keep the probe well inside the firmware's right-edge probe boundary.
        # Live serial-log checks on this printer showed:
        #   - G30 X180 Y25 -> "Z Probe Past Bed"
        #   - G30 X170 Y25 -> success
        # with M851 X-45 Y-6.5. That means the safe right-side nozzle ceiling
        # is about 20 mm inside the nominal 235 mm travel max.
        # Apply the same margin on Y to keep rear points within the firmware
        # probe area.
        nozzle_edge_margin_x_mm = 20.0
        nozzle_edge_margin_y_mm = 20.0

        return compute_probe_points(
            bed_size_x_mm=bed_x,
            bed_size_y_mm=bed_y,
            corner_inset_mm=inset,
            probe_offset_x_mm=offset_x,
            probe_offset_y_mm=offset_y,
            nozzle_max_x_mm=bed_x - nozzle_edge_margin_x_mm,
            nozzle_max_y_mm=bed_y - nozzle_edge_margin_y_mm,
            explicit_probe_targets=self._configured_probe_targets(),
        )

    def _point_by_id(self, point_id):
        return next(point for point in self._probe_points if point["id"] == point_id)

    def _safe_z_move_command(self):
        safe_z = self._settings.get_float(["safe_z"])
        z_feedrate = self._settings.get_float(["z_feedrate"])
        return f"G1 Z{safe_z:.2f} F{z_feedrate:.0f}"

    def _build_g29_command(self):
        """Build a G29 command with L/R/F/B boundaries to keep the mesh probe
        away from bed clips.

        Marlin's G29 L/R/F/B parameters specify the probe-tip coordinate
        boundaries. We derive them from the bed size and mesh_inset_mm setting,
        then clamp the right side to the same safe limit the plugin uses for
        its own corner probes (accounting for the X probe offset and firmware
        probe-area limits).
        """
        bed_x = self._settings.get_float(["bed_size_x_mm"])
        bed_y = self._settings.get_float(["bed_size_y_mm"])
        inset = self._settings.get_float(["mesh_inset_mm"])

        # Probe-tip boundaries
        front = inset
        back = bed_y - inset
        left = inset
        right = bed_x - inset

        # Apply the same right-side clamp the plugin uses for corner probes.
        # With M851 X-45, probe_x = nozzle_x + offset_x, and nozzle travel is
        # limited. The plugin already proved that probe_x=170 is the safe max.
        nozzle_edge_margin_x_mm = 20.0
        offset_x = self._probe_offset_x_mm if self._probe_offset_x_mm is not None else 0.0
        max_probe_x = bed_x - nozzle_edge_margin_x_mm + offset_x
        if right > max_probe_x:
            right = max_probe_x

        self._logger.info(
            "G29 mesh boundaries: L=%.1f R=%.1f F=%.1f B=%.1f (inset=%.1f offset_x=%.1f)",
            left, right, front, back, inset, offset_x,
        )
        return f"G29 L{left:.0f} R{right:.0f} F{front:.0f} B{back:.0f}"

    def _assist_target_height_command(self, target_z):
        """Compute the nozzle Z that places the deployed probe pin at target_z.

        G30 reports the nozzle Z at which the probe triggered. This already has
        the M851 Z offset baked in by Marlin. But when we deploy the probe with
        M401 and then do a plain G1 Z move, the *nozzle* goes to that Z while
        the probe pin hangs below by |M851 Z|.

        To place the probe pin at target_z we need:
            nozzle_z = target_z - probe_z_offset
        Since probe_z_offset is negative (pin is below nozzle), this raises the
        nozzle above target_z by the offset magnitude.
        """
        approach_feedrate = max(120.0, self._settings.get_float(["z_feedrate"]) / 2.0)
        minimum_assist_target_z = self._settings.get_float(["minimum_assist_target_z"])

        # probe_z_offset is typically negative (e.g. -1.8). Subtracting it
        # raises the nozzle so the pin tip lands at target_z.
        probe_z_offset = self._probe_offset_z_mm if self._probe_offset_z_mm is not None else 0.0
        nozzle_z = float(target_z) - probe_z_offset

        clamped_nozzle_z = max(nozzle_z, minimum_assist_target_z)
        return f"G1 Z{clamped_nozzle_z:.3f} F{approach_feedrate:.0f}"

    def _assist_mode(self, recommendation):
        """Pick the gantry-staging mode for a corner.

        - ``raise_to_trigger``: the bed is *low* at this corner and needs to come
          up. The plugin parks the CR Touch *deployed* at the target Z so the
          user can loosen the wheel until the probe just triggers.
        - ``tighten``: the bed is *high* at this corner and needs to come down.
          The plugin parks the gantry at safe Z with the probe stowed and tells
          the user how many turns clockwise to make.
        - ``hold``: the corner is within tolerance, no movement needed.
        """
        delta = recommendation.get("delta_mm", 0.0)
        if delta > 0:
            return "raise_to_trigger"
        if delta < 0:
            return "tighten"
        return "hold"

    def _guided_corner_instruction_text(self, recommendation):
        assist_mode = recommendation.get("assist_mode")
        if assist_mode == "raise_to_trigger":
            return (
                f"The CR Touch has been positioned over {recommendation['label']} at the target height. "
                f"Slowly turn that wheel {recommendation['rotation_label']} to loosen it until the CR Touch "
                "just triggers, then press Re-probe."
            )

        if assist_mode == "tighten":
            return (
                f"Tighten {recommendation['label']} by turning it {recommendation['rotation_label']} about "
                f"{recommendation['turns_text']}, then press Re-probe. If you go past the target the next "
                "pass will line up the CR Touch so you can back it off precisely."
            )

        return recommendation.get("human_instruction") or ""

    def _guided_corner_status(self, recommendation):
        assist_mode = recommendation.get("assist_mode")
        if assist_mode == "raise_to_trigger":
            return (
                f"{recommendation['label']}: probe is at the target height. Loosen the wheel "
                f"{recommendation['rotation_label']} until the CR Touch just triggers, then press Re-probe."
            )

        if assist_mode == "tighten":
            return (
                f"{recommendation['label']}: tighten {recommendation['rotation_label']} by about "
                f"{recommendation['turns_text']}, then press Re-probe."
            )

        return f"{recommendation['label']}: within tolerance."

    def _travel_command(self, point):
        travel_feedrate = self._settings.get_float(["travel_feedrate"])
        return f"G1 X{point['x']:.2f} Y{point['y']:.2f} F{travel_feedrate:.0f}"

    def _safe_travel_preamble(self):
        z_feedrate = self._settings.get_float(["z_feedrate"])
        pre_travel_lift_z = max(0.0, self._settings.get_float(["pre_travel_lift_z"]))
        return [
            "M402",
            "G91",
            f"G1 Z{pre_travel_lift_z:.2f} F{z_feedrate:.0f}",
            "G90",
            self._safe_z_move_command(),
        ]

    def _assist_positioning_commands(self, point, target_z, recommendation=None):
        commands = self._safe_travel_preamble() + [self._travel_command(point)]

        # Build the LCD message: prefer a rich one if we have the recommendation,
        # otherwise fall back to the short "Assist FL" form so the LCD still
        # reflects what the printer is doing.
        lcd_text = None
        if recommendation is not None and self._settings.get_boolean(["show_on_printer_lcd"]):
            viewpoint = self._settings.get(["viewpoint"]) or "from_below"
            lcd_text = format_lcd_instruction(
                point["short_label"],
                recommendation.get("delta_mm", 0.0),
                recommendation.get("turns", 0.0),
                viewpoint=viewpoint,
            )

        commands.append(f"M117 {lcd_text}" if lcd_text else f"M117 Assist {point['short_label']}")
        commands.append("M401 R1")
        commands.append(self._assist_target_height_command(target_z))

        if recommendation is not None and self._settings.get_boolean(["wait_for_encoder_click"]):
            # M0 with a message: pauses the queue until the printer encoder
            # is clicked. Message is also subject to the LCD char budget.
            click_msg = f"Done? Click {point['short_label']}"[:LCD_STATUS_MAX_CHARS]
            commands.append(f"M0 {click_msg}")

        return commands

    def _probe_command(self, point):
        return f"G30 X{point['probe_x']:.2f} Y{point['probe_y']:.2f}"

    def _probe_noise_point(self):
        return self._point_by_id("center")

    def _begin_corner_probe_pass(self):
        self._pending_probe_ids = [point["id"] for point in self._probe_points]
        self._state["stage"] = "probing"
        self._state["status"] = "Probe repeatability OK. Starting the full corner and center probe pass from the loose-wheel baseline."

    def _effective_tolerance_mm(self):
        probe_noise = self._state.get("probe_noise") or {}
        recommended = probe_noise.get("recommended_tolerance_mm")
        if recommended is not None:
            return float(recommended)
        return self._settings.get_float(["tolerance_mm"])

    def _next_probe_noise_commands(self):
        if self._probe_noise_remaining <= 0:
            return [self._safe_z_move_command()]

        point = self._probe_noise_point()
        sample_number = len(self._probe_noise_samples) + 1
        total_samples = max(sample_number, len(self._probe_noise_samples) + self._probe_noise_remaining)
        return self._safe_travel_preamble() + [
            self._travel_command(point),
            f"M117 Noise {sample_number}/{total_samples}",
            self._probe_command(point),
        ]

    def _next_probe_commands(self):
        if not self._pending_probe_ids:
            return [self._safe_z_move_command()]

        point = self._point_by_id(self._pending_probe_ids[0])
        return self._safe_travel_preamble() + [
            self._travel_command(point),
            f"M117 Probing {point['short_label']}",
            self._probe_command(point),
        ]

    def _record_probe_result(self, point, measured_z, reported_x=None, reported_y=None):
        measurement = {
            "id": point["id"],
            "label": point["label"],
            "x": point["x"],
            "y": point["y"],
            "reported_x": reported_x,
            "reported_y": reported_y,
            "z": measured_z,
        }

        self._state["measurements"] = [
            item for item in self._state.get("measurements", []) if item["id"] != point["id"]
        ] + [measurement]
        self._logger.info(
            "Recorded probe result: point=%s reported=(%.3f, %.3f) target=(%.3f, %.3f) z=%.3f",
            point["id"],
            reported_x if reported_x is not None else point["x"],
            reported_y if reported_y is not None else point["y"],
            point["x"],
            point["y"],
            measured_z,
        )
        self._append_history(
            "probe_result",
            point_id=point["id"],
            reported_x=reported_x if reported_x is not None else point["x"],
            reported_y=reported_y if reported_y is not None else point["y"],
            target_x=point["x"],
            target_y=point["y"],
            z=measured_z,
        )
        self._touch_state()

    def _send_commands_async(self, commands):
        """Send printer commands from a daemon thread to avoid re-entrancy issues
        when called from within a gcode received hook callback."""
        self._log_command_batch("async_dispatch", commands)
        threading.Thread(
            target=lambda: self._printer.commands(commands),
            daemon=True,
            name="testlevel-cmd-dispatch",
        ).start()

    def _handle_probe_line(self, measured_x, measured_y, measured_z):
        self._logger.info(
            "Probe line received: x=%.3f y=%.3f z=%.3f  pending=%s  assist=%s",
            measured_x,
            measured_y,
            measured_z,
            self._pending_probe_ids,
            self._active_assist_corner,
        )
        commands_to_send = None
        with self._state_lock:
            if self._probe_noise_remaining:
                point = self._probe_noise_point()
                self._probe_noise_samples.append(measured_z)
                self._probe_noise_remaining -= 1
                summary = build_probe_noise_summary(
                    self._probe_noise_samples,
                    hard_fail_sigma_mm=self._settings.get_float(["probe_noise_hard_fail_sigma_mm"]),
                )
                summary["expected_samples"] = len(self._probe_noise_samples) + self._probe_noise_remaining
                self._state["probe_noise"] = summary
                self._state["effective_tolerance_mm"] = self._effective_tolerance_mm()
                if self._probe_noise_remaining:
                    self._state["stage"] = "probe_noise"
                    self._state["status"] = (
                        f"Probe repeatability check: collected {summary['sample_count']}/{summary['expected_samples']} "
                        "center samples."
                    )
                    commands_to_send = self._next_probe_noise_commands()
                else:
                    sigma_mm = summary["sigma_mm"]
                    self._state["effective_tolerance_mm"] = summary["recommended_tolerance_mm"]
                    if summary["passed"]:
                        self._begin_corner_probe_pass()
                        self._state["status"] = (
                            f"Probe repeatability OK (sigma {sigma_mm:.3f} mm). "
                            f"Using {summary['recommended_tolerance_mm']:.3f} mm tolerance for this run."
                        )
                        commands_to_send = self._next_probe_commands()
                    else:
                        self._state["error"] = (
                            f"CR Touch repeatability is too noisy (sigma {sigma_mm:.3f} mm > "
                            f"{summary['hard_fail_sigma_mm']:.3f} mm). Check probe pin movement, bed wobble, "
                            "gantry play, and hotend/carriage looseness before retrying."
                        )
                        self._state["status"] = self._state["error"]
                        self._state["stage"] = "error"
                        self._pending_probe_ids = []
                        self._probe_noise_remaining = 0
                        commands_to_send = [self._safe_z_move_command()]
                self._touch_state()

            elif self._pending_probe_ids:
                point_id = self._pending_probe_ids.pop(0)
                point = self._point_by_id(point_id)
                self._record_probe_result(point, measured_z, reported_x=measured_x, reported_y=measured_y)
                self._state["stage"] = "probing"
                remaining = len(self._pending_probe_ids)
                if remaining:
                    self._state["status"] = f"Captured {point['label']}. {remaining} probe points remaining."
                    commands_to_send = self._next_probe_commands()
                    self._logger.info("Queuing next probe commands: %s", commands_to_send)
                else:
                    avoid_corner_id = self._guide_avoid_corner_id
                    self._guide_avoid_corner_id = None
                    self._state["results"] = self._build_results(avoid_corner_id=avoid_corner_id)
                    self._state["guide"] = copy.deepcopy(self._state["results"].get("guide"))
                    self._log_results_summary(self._state["results"])
                    commands_to_send = self._stage_guided_corner_after_pass()
                self._touch_state()

        if commands_to_send is not None:
            self._send_commands_async(commands_to_send)
            return

    def _stage_guided_corner_after_pass(self):
        """Called once a full 5-point probe pass has finished.

        Picks the worst out-of-tolerance corner, auto-stages the gantry, and
        returns the command batch to execute. If every corner is in tolerance
        the gantry is parked and the workflow transitions to ``done``.
        """
        results = self._state.get("results") or {}
        guide = results.get("guide") or {}
        current_corner = guide.get("current_corner")

        if current_corner is None or current_corner.get("within_tolerance"):
            self._active_assist_corner = None
            self._state["assist"] = None
            self._cancel_assist_trigger_monitor()

            if self._settings.get_boolean(["auto_mesh_on_done"]):
                self._state["stage"] = "meshing"
                self._state["status"] = (
                    "All corners within tolerance. Running a full mesh probe (G29) to update firmware compensation."
                )
                self._touch_state()
                self._append_history("mesh_probe_started")
                self._logger.info("All corners in tolerance — starting auto mesh probe (G29).")
                g29_command = self._build_g29_command()
                self._logger.info("Mesh command: %s", g29_command)
                return [
                    "M402",
                    self._safe_z_move_command(),
                    "G28",
                    "M420 S0",
                    g29_command,
                    "M500",
                    "M420 V",
                    self._safe_z_move_command(),
                    "M117 Mesh saved",
                    "M118 TESTLEVEL_MESH_DONE",
                ]

            self._state["stage"] = "done"
            self._state["status"] = (
                "All corners are within tolerance. The bed is level - leave the wheels alone."
            )
            self._touch_state()
            self._append_history("workflow_done")
            return [self._safe_z_move_command()]

        corner_id = current_corner["id"]
        point = self._point_by_id(corner_id)
        target_z = current_corner.get("target_z")
        assist_mode = current_corner.get("assist_mode") or self._assist_mode(current_corner)

        self._active_assist_corner = corner_id
        self._state["stage"] = "guided"
        self._state["assist"] = {
            "corner": corner_id,
            "corner_label": current_corner["label"],
            "instruction": self._assist_instruction_text(current_corner),
            "measured_z": current_corner["z"],
            "remaining_mm": current_corner["delta_mm"],
            "remaining_turns": current_corner["turns"],
            "remaining_turns_text": current_corner["turns_text"],
            "rotation_label": current_corner["rotation_label"],
            "target_z": target_z,
            "mode": assist_mode,
            "within_tolerance": current_corner["within_tolerance"],
            "feedback_title": "Loosen until CR Touch triggers" if assist_mode == "raise_to_trigger" else "Tighten this wheel",
            "feedback_text": self._assist_instruction_text(current_corner),
        }
        self._state["status"] = self._guided_corner_status(current_corner)
        self._state["error"] = None

        self._logger.info(
            "Auto-staging corner after probe pass: corner=%s mode=%s delta=%.3f turns=%.3f target_z=%s",
            corner_id,
            assist_mode,
            current_corner["delta_mm"],
            current_corner["turns"],
            "n/a" if target_z is None else f"{target_z:.3f}",
        )
        self._append_history(
            "guided_corner_staged",
            corner_id=corner_id,
            mode=assist_mode,
            delta_mm=current_corner["delta_mm"],
            turns=current_corner["turns"],
            target_z=target_z,
        )

        if assist_mode == "raise_to_trigger" and target_z is not None:
            self._schedule_assist_trigger_monitor(corner_id, delay_seconds=5.0)
            return self._assist_positioning_commands(point, target_z, recommendation=current_corner)

        self._cancel_assist_trigger_monitor()

        # Tighten mode: park the gantry at safe Z above the corner so the user
        # can see exactly which wheel to turn, but do not deploy the probe.
        commands = self._safe_travel_preamble() + [self._travel_command(point)]
        if self._settings.get_boolean(["show_on_printer_lcd"]):
            viewpoint = self._settings.get(["viewpoint"]) or "from_below"
            lcd_text = format_lcd_instruction(
                point["short_label"],
                current_corner.get("delta_mm", 0.0),
                current_corner.get("turns", 0.0),
                viewpoint=viewpoint,
            )
            commands.append(f"M117 {lcd_text}")
        else:
            commands.append(f"M117 Tighten {point['short_label']}")
        return commands

    def _build_results(self, avoid_corner_id=None):
        measurements = {item["id"]: item for item in self._state.get("measurements", [])}
        corner_points = [measurements[point["id"]] for point in self._probe_points[:4] if point["id"] in measurements]
        center_point = measurements.get("center")

        if len(corner_points) < 4:
            return {}

        plane = self._solve_plane(corner_points)
        measured_plane_points = [
            dict(point, plane_z=self._evaluate_plane(plane, point["x"], point["y"]))
            for point in corner_points
        ]

        # Target: all corners should read the same Z so the bed is parallel to the
        # gantry. Use the mean of all four corner probe readings. This distributes
        # adjustments equally and works from any starting wheel position.
        target_z = sum(pt["z"] for pt in corner_points) / len(corner_points)

        self._logger.info(
            "Build results: corner readings=[%s] mean_target_z=%.4f",
            ", ".join(f"{pt['id']}={pt['z']:.3f}" for pt in corner_points),
            target_z,
        )

        corner_results = []
        for point in measured_plane_points:
            recommendation = self._build_corner_recommendation(point, point["z"], target_z)
            recommendation["plane_z"] = point["plane_z"]
            corner_results.append(recommendation)
            self._logger.info(
                "  Corner %s: measured=%.3f target=%.3f delta=%.3f mode=%s tol=%s",
                point["id"], point["z"], target_z,
                recommendation["delta_mm"], recommendation["assist_mode"],
                recommendation["within_tolerance"],
            )

        center_result = None
        if center_point is not None:
            center_plane_z = self._evaluate_plane(plane, center_point["x"], center_point["y"])
            center_result = {
                "x": center_point["x"],
                "y": center_point["y"],
                "z": center_point["z"],
                "plane_z": center_plane_z,
                "warp_delta_mm": center_point["z"] - center_plane_z,
            }

        instructions = [
            "1. Press Run from any starting wheel position.",
            "2. Wait for the heated CR Touch repeatability check.",
            "3. The plugin probes all 5 points and calculates the mean corner height as the target.",
            "4. For low corners the CR Touch is placed at the target height — loosen the wheel until it triggers.",
            "5. For high corners tighten by the shown number of turns, then press Re-probe.",
        ]

        return {
            "center": center_result,
            "corners": corner_results,
            "guide": self._build_guide_state(corner_results, avoid_corner_id=avoid_corner_id),
            "instructions": instructions,
            "plane": {
                "a": plane["a"],
                "b": plane["b"],
                "c": plane["c"],
                "x_tilt_per_100mm": plane["a"] * 100.0,
                "y_tilt_per_100mm": plane["b"] * 100.0,
            },
            "target_z": round(target_z, 3),
            "tolerance_mm": self._effective_tolerance_mm(),
        }

    def _build_guide_state(self, corner_results, preferred_corner_id=None, avoid_corner_id=None):
        ordered_corners = sorted(
            list(corner_results),
            key=lambda item: (item["within_tolerance"], -abs(item["delta_mm"])),
        )
        all_within_tolerance = all(item["within_tolerance"] for item in ordered_corners) if ordered_corners else False
        current_corner = None
        blocked_corner_ids = []

        if preferred_corner_id is not None:
            preferred_corner = next((item for item in ordered_corners if item["id"] == preferred_corner_id), None)
            if preferred_corner is not None and not preferred_corner["within_tolerance"]:
                current_corner = preferred_corner

        if current_corner is None:
            current_corner = next((item for item in ordered_corners if not item["within_tolerance"]), None)

        if current_corner is None and ordered_corners:
            current_corner = ordered_corners[0]

        if current_corner is not None and avoid_corner_id is not None and current_corner["id"] == avoid_corner_id:
            cleanup_band_mm = max(self._effective_tolerance_mm() * 2.0, 0.05)
            alternative_corner = next(
                (item for item in ordered_corners if item["id"] != avoid_corner_id and not item["within_tolerance"]),
                None,
            )
            if alternative_corner is not None and abs(current_corner.get("delta_mm", 0.0)) <= cleanup_band_mm:
                blocked_corner_ids = [avoid_corner_id]
                current_corner = alternative_corner

        return {
            "all_within_tolerance": all_within_tolerance,
            "blocked_corner_ids": blocked_corner_ids,
            "completed_count": len([item for item in ordered_corners if item["within_tolerance"]]),
            "current_corner": copy.deepcopy(current_corner) if current_corner is not None else None,
            "current_corner_id": current_corner["id"] if current_corner is not None else None,
            "ordered_corner_ids": [item["id"] for item in ordered_corners],
            "remaining_count": len([item for item in ordered_corners if not item["within_tolerance"]]),
        }

    def _refresh_guide_state(self, results, preferred_corner_id=None, avoid_corner_id=None):
        if results is None:
            return

        results["guide"] = self._build_guide_state(
            results.get("corners", []),
            preferred_corner_id=preferred_corner_id,
            avoid_corner_id=avoid_corner_id,
        )
        self._state["guide"] = copy.deepcopy(results["guide"])
        guide = results["guide"]
        current_corner = guide.get("current_corner") or {}
        self._logger.info(
            "Guide updated: preferred=%s current=%s remaining=%s completed=%s all_within_tolerance=%s",
            preferred_corner_id,
            current_corner.get("id"),
            guide.get("remaining_count"),
            guide.get("completed_count"),
            guide.get("all_within_tolerance"),
        )

    def _direction_name(self, delta_mm):
        if abs(delta_mm) < 1e-9:
            return "hold"
        return "raise" if delta_mm > 0 else "lower"

    def _build_corner_recommendation(self, point, measured_z, target_z):
        delta_mm = target_z - measured_z
        screw_pitch_mm = self._settings.get_float(["screw_pitch_mm"])
        turns = abs(delta_mm) / screw_pitch_mm
        viewpoint = self._settings.get(["viewpoint"]) or "from_below"
        human_instruction = build_human_instruction(
            point["label"], delta_mm, turns, viewpoint=viewpoint
        )
        effective_tolerance = self._effective_tolerance_mm()
        assist_mode = self._assist_mode({"delta_mm": delta_mm})
        return {
            "id": point["id"],
            "label": point["label"],
            "x": point["x"],
            "y": point["y"],
            "z": measured_z,
            "target_z": target_z,
            "delta_mm": delta_mm,
            "direction": self._direction_name(delta_mm),
            "rotation_label": self._rotation_label(delta_mm),
            "turns": turns,
            "turns_text": self._format_turns(turns),
            "assist_mode": assist_mode,
            "within_tolerance": abs(delta_mm) <= effective_tolerance,
            "human_instruction": human_instruction,
            "mechanical_limit": None,
            "mechanical_limit_message": None,
        }

    def _assist_instruction_text(self, recommendation):
        if recommendation["within_tolerance"]:
            return f"{recommendation['label']} is already within tolerance."

        assist_mode = self._assist_mode(recommendation)
        if assist_mode == "raise_to_trigger":
            return (
                f"The bed needs to come up at {recommendation['label']}. The CR Touch has been deployed at the "
                f"target trigger height. Slowly turn that wheel {recommendation['rotation_label']} to loosen it "
                "until the CR Touch just triggers, then press Re-probe."
            )

        if assist_mode == "tighten":
            return (
                f"Tighten {recommendation['label']} by turning it {recommendation['rotation_label']} about "
                f"{recommendation['turns_text']}, then press Re-probe. If the corner overshoots, the next pass "
                "will line up the CR Touch so you can back the wheel off to the exact height."
            )

        return recommendation.get("human_instruction") or ""

    def _rotation_label(self, delta_mm):
        if abs(delta_mm) < 1e-9:
            return "no change"
        viewpoint = self._settings.get(["viewpoint"]) or "from_below"
        return direction_for_delta(delta_mm, viewpoint=viewpoint)["rotation"]

    def _format_turns(self, turns):
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

    def _solve_plane(self, points):
        sum_x = sum(point["x"] for point in points)
        sum_y = sum(point["y"] for point in points)
        sum_z = sum(point["z"] for point in points)
        sum_xx = sum(point["x"] * point["x"] for point in points)
        sum_xy = sum(point["x"] * point["y"] for point in points)
        sum_yy = sum(point["y"] * point["y"] for point in points)
        sum_xz = sum(point["x"] * point["z"] for point in points)
        sum_yz = sum(point["y"] * point["z"] for point in points)
        count = float(len(points))

        matrix = [
            [sum_xx, sum_xy, sum_x, sum_xz],
            [sum_xy, sum_yy, sum_y, sum_yz],
            [sum_x, sum_y, count, sum_z],
        ]

        solution = self._solve_linear_system(matrix)
        return {"a": solution[0], "b": solution[1], "c": solution[2]}

    def _solve_linear_system(self, matrix):
        working = [row[:] for row in matrix]
        size = len(working)

        for pivot_index in range(size):
            pivot_row = max(range(pivot_index, size), key=lambda row_index: abs(working[row_index][pivot_index]))
            working[pivot_index], working[pivot_row] = working[pivot_row], working[pivot_index]
            pivot = working[pivot_index][pivot_index]
            if abs(pivot) < 1e-12:
                return [0.0, 0.0, 0.0]

            for column_index in range(pivot_index, size + 1):
                working[pivot_index][column_index] /= pivot

            for row_index in range(size):
                if row_index == pivot_index:
                    continue
                factor = working[row_index][pivot_index]
                for column_index in range(pivot_index, size + 1):
                    working[row_index][column_index] -= factor * working[pivot_index][column_index]

        return [working[row_index][size] for row_index in range(size)]

    def _evaluate_plane(self, plane, x_value, y_value):
        return plane["a"] * x_value + plane["b"] * y_value + plane["c"]

    def _set_error_state(self, message):
        with self._state_lock:
            self._cancel_offset_query_timer()
            self._cancel_assist_trigger_monitor()
            self._probe_noise_remaining = 0
            self._probe_noise_samples = []
            self._pending_probe_ids = []
            self._active_assist_corner = None
            self._pending_start_bed_temp = None
            self._pending_start_noise_samples = None
            self._guide_avoid_corner_id = None
            self._state["error"] = message
            self._state["stage"] = "error"
            self._state["status"] = message
            self._touch_state()
            self._append_history("error", message=message)
        self._logger.error("Test Level entered error state: %s", message)

    def _log_command_batch(self, source, commands):
        self._logger.info("Command batch queued: source=%s commands=%s", source, commands)

    def _log_results_summary(self, results):
        corners = results.get("corners", []) if results else []
        corner_summary = [
            "%s:z=%.3f delta=%.3f turns=%.3f tol=%s" % (
                item["id"],
                item["z"],
                item["delta_mm"],
                item["turns"],
                item["within_tolerance"],
            )
            for item in corners
        ]
        center = results.get("center") if results else None
        self._logger.info(
            "Results summary: corners=%s center_warp=%s target_reference_z=%s",
            corner_summary,
            None if center is None else round(center.get("warp_delta_mm", 0.0), 3),
            None if results is None else round(results.get("target_reference_z", 0.0), 3),
        )
        self._append_history(
            "results_summary",
            corners=corner_summary,
            center_warp=None if center is None else round(center.get("warp_delta_mm", 0.0), 3),
            target_reference_z=None if results is None else round(results.get("target_reference_z", 0.0), 3),
        )

    # =====================================================================
    # PID Tune Handlers
    # =====================================================================

    def _handle_pid_start(self, data):
        """Start PID autotune at the given temperature."""
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        if self._pid_running:
            return flask.jsonify({"ok": False, "error": "PID autotune is already running"}), 409

        try:
            temp = int((data or {}).get("target_temp", 215))
        except (TypeError, ValueError):
            return flask.jsonify({"ok": False, "error": "target_temp must be a whole number"}), 400
        if temp < 1 or temp > 300:
            return flask.jsonify({"ok": False, "error": "target_temp must be between 1 and 300C"}), 400

        self._pid_running = True
        self._pid_results = None
        self._printer.commands(["M303 E0 S{} C8".format(temp)])
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "pid_progress", "phase": "running", "message": "PID autotune running at {}°C (8 cycles, ~2 min)...".format(temp)},
        )
        return flask.jsonify({"status": "started", "target": temp})

    def _handle_pid_apply(self):
        """Apply PID results via M301 and save to EEPROM."""
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        if self._pid_results is None:
            return flask.jsonify({"error": "No PID results available. Run autotune first."}), 400
        kp = self._pid_results["kp"]
        ki = self._pid_results["ki"]
        kd = self._pid_results["kd"]
        self._printer.commands([
            "M301 P{:.2f} I{:.2f} D{:.2f}".format(kp, ki, kd),
            "M500",
        ])
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "pid_progress", "phase": "saved", "message": "PID values applied and saved to EEPROM."},
        )
        return flask.jsonify({"status": "saved", "kp": kp, "ki": ki, "kd": kd})

    def _check_pid_line(self, line):
        """Parse PID autotune results from serial output."""
        if not self._pid_running:
            return

        # Marlin outputs: "Kp: 22.35 Ki: 1.59 Kd: 78.53" or
        # "#define DEFAULT_Kp 22.35" style
        import re
        # Try "Kp: X Ki: Y Kd: Z" format
        match = re.search(r"Kp[:\s]+([\d.]+)\s+Ki[:\s]+([\d.]+)\s+Kd[:\s]+([\d.]+)", line)
        if match:
            self._pid_results = {
                "kp": float(match.group(1)),
                "ki": float(match.group(2)),
                "kd": float(match.group(3)),
            }
            self._pid_running = False
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {
                    "type": "pid_progress",
                    "phase": "done",
                    "message": "PID autotune complete! Kp={:.2f} Ki={:.2f} Kd={:.2f}".format(
                        self._pid_results["kp"], self._pid_results["ki"], self._pid_results["kd"]
                    ),
                    "results": self._pid_results,
                },
            )
            return

        # Detect failure
        if "PID Autotune failed" in line:
            self._pid_running = False
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"type": "pid_progress", "phase": "error", "message": "PID autotune failed. Check hotend wiring and thermistor."},
            )

    # =====================================================================
    # E-Steps Calibration Handlers
    # =====================================================================

    def _handle_esteps_start(self, data):
        """Single command: read E-steps, heat to temp (blocking), then extrude 100mm."""
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        if self._esteps_waiting:
            return flask.jsonify({"ok": False, "error": "E-steps calibration is already running"}), 409

        try:
            temp = int((data or {}).get("target_temp", 200))
        except (TypeError, ValueError):
            return flask.jsonify({"ok": False, "error": "target_temp must be a whole number"}), 400
        if temp < 1 or temp > 300:
            return flask.jsonify({"ok": False, "error": "target_temp must be between 1 and 300C"}), 400

        self._esteps_target_temp = temp
        self._esteps_waiting = True
        self._esteps_current = None

        # Home, raise Z to 100mm so extruded filament doesn't hit the bed,
        # heat and wait, read E-steps, then extrude 100mm slowly.
        # Hotend stays hot so user can re-run without waiting.
        self._printer.commands([
            "G28",
            "G1 Z100 F600",
            "M503",
            "M109 S{}".format(temp),
            "M118 ESTEPS_AT_TEMP",
            "M83",
            "G1 E100 F100",
            "M82",
            "M118 ESTEPS_EXTRUDE_DONE",
        ])

        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "esteps_progress", "phase": "heating", "message": "Heating to {}°C...".format(temp)},
        )
        return flask.jsonify({"status": "started", "target": temp})

    def _handle_esteps_calculate_and_apply(self, data):
        """Calculate new E-steps from measurement and immediately apply via M92."""
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        try:
            measured_remaining = float((data or {}).get("measured_remaining", 0))
        except (TypeError, ValueError):
            return flask.jsonify({"error": "measured_remaining must be a number"}), 400
        if not math.isfinite(measured_remaining) or not 0 <= measured_remaining <= 120:
            return flask.jsonify({"error": "measured_remaining must be between 0 and 120 mm"}), 400

        mark_distance = 120
        extrude_length = 100
        actual_extruded = mark_distance - measured_remaining

        if self._esteps_current is None:
            return flask.jsonify({"error": "E-steps not read yet. Please run Heat & Extrude first."}), 400

        try:
            result = calculate_new_esteps(self._esteps_current, extrude_length, actual_extruded)
        except ValueError as exc:
            return flask.jsonify({"error": str(exc)}), 400

        # Apply immediately
        new_esteps = result["new_esteps"]
        self._printer.commands(["M92 E{:.2f}".format(new_esteps)])
        self._esteps_current = new_esteps
        result["applied"] = True
        return flask.jsonify(result)

    def _handle_esteps_save(self):
        error_response = self._ensure_printer_ready()
        if error_response is not None:
            return error_response

        self._printer.commands(["M500"])
        return flask.jsonify({"status": "saved"})

    def _check_esteps_line(self, line):
        """Check if a received line contains E-steps info from M503 and notify the UI."""
        if not self._esteps_waiting:
            return
        value = parse_esteps_from_m503(line)
        if value is not None:
            self._esteps_current = value
            self._esteps_waiting = False

        # Detect progress sentinels sent by _handle_esteps_start
        if "ESTEPS_AT_TEMP" in line:
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"type": "esteps_progress", "phase": "extruding", "message": "At temperature. Extruding 100mm (~60s)..."},
            )
        elif "ESTEPS_EXTRUDE_DONE" in line:
            self._plugin_manager.send_plugin_message(
                self._identifier,
                {"type": "esteps_progress", "phase": "done", "message": "Done! Measure the remaining filament and enter below."},
            )

    def handle_received_line(self, comm_instance, line, *args, **kwargs):
        self._check_esteps_line(line)
        self._check_pid_line(line)
        lower_line = line.lower()
        if "z probe past bed" in lower_line:
            self._set_error_state(
                "The configured probe points exceed the safe probe area for the current CR Touch offset. Move the right-side points inward and rerun the workflow."
            )
            return line

        if "TESTLEVEL_Z_CAL_DONE" in line:
            with self._state_lock:
                self._z_calibration_running = False
                if "z_calibration" in self._state:
                    self._state["z_calibration"]["running"] = False
                self._state["status"] = (
                    self._z_search_state["status"]
                    if self._z_search_state.get("active")
                    else "Z-offset test squares finished. Pick the best square when ready."
                )
                self._touch_state()
            self._logger.info("Z-offset calibration pass completed.")
            return line

        # Detect mesh probe completion sentinel echoed back by Marlin via M118.
        if "TESTLEVEL_MESH_DONE" in line:
            with self._state_lock:
                if self._state.get("stage") == "meshing":
                    self._state["stage"] = "done"
                    self._state["status"] = (
                        "All corners within tolerance and mesh saved to EEPROM. The bed is level."
                    )
                    self._touch_state()
                    self._append_history("mesh_probe_done")
                    self._logger.info("Mesh probe complete — workflow done.")
            return line

        probe_state_match = Z_PROBE_STATE_PATTERN.search(line)
        if probe_state_match is not None:
            self._handle_z_probe_state_line(probe_state_match.group(1).upper())
            return line

        offsets = parse_probe_offset_line(line)
        if offsets is not None:
            self._handle_probe_offset_line(offsets[0], offsets[1], offsets[2])
            return line

        match = PROBE_RESULT_PATTERN.search(line)
        if not match:
            return line

        measured_x = float(match.group(1))
        measured_y = float(match.group(2))
        measured_z = float(match.group(3))
        self._logger.info("Probe result matched from serial line: %r", line.strip())
        self._handle_probe_line(measured_x, measured_y, measured_z)
        return line

    def _handle_probe_offset_line(self, offset_x, offset_y, offset_z):
        with self._state_lock:
            previous = (self._probe_offset_x_mm, self._probe_offset_y_mm, self._probe_offset_z_mm)
            self._probe_offset_x_mm = float(offset_x)
            self._probe_offset_y_mm = float(offset_y)
            self._probe_offset_z_mm = float(offset_z)
            self._probe_points = self._compute_probe_points()
            self._state["probe_points"] = copy.deepcopy(self._probe_points)
            self._state["probe_offset"] = {
                "x": self._probe_offset_x_mm,
                "y": self._probe_offset_y_mm,
                "z": self._probe_offset_z_mm,
            }
            pending_start = self._pending_start_bed_temp is not None
            self._touch_state()

        self._logger.info(
            "Parsed CR Touch offsets from Marlin: prev=%s new=(%.3f, %.3f, %.3f) pending_start=%s",
            previous,
            offset_x,
            offset_y,
            offset_z,
            pending_start,
        )
        self._append_history("probe_offsets_detected", offset_x=offset_x, offset_y=offset_y, offset_z=offset_z, pending_start=pending_start)

        if pending_start:
            self._cancel_offset_query_timer()
            self._begin_workflow_after_offsets()
            return

        if self._pending_z_calibration_request is not None:
            self._cancel_offset_query_timer()
            self._resume_pending_z_calibration()

    def _handle_z_probe_state_line(self, probe_state):
        with self._state_lock:
            assist = self._state.get("assist") or {}
            stage = self._state.get("stage")
            assist_mode = assist.get("mode")
            active_corner = self._active_assist_corner

            self._logger.info(
                "M119 z_probe state received: %s  assist_mode=%s  active_corner=%s  stage=%s",
                probe_state,
                assist_mode,
                active_corner,
                stage,
            )

            # Auto-reprobe: if the probe triggers while we are in guided/raise_to_trigger
            # mode, the user has loosened the wheel enough for the bed to push the
            # probe pin up. With the Z-offset fix the pin only reaches the bed when
            # the user physically raises it, so a TRIGGERED state here is always a
            # genuine adjustment signal.
            if (
                probe_state == "TRIGGERED"
                and stage == "guided"
                and assist_mode == "raise_to_trigger"
                and active_corner is not None
                and not self._pending_probe_ids
            ):
                self._logger.info(
                    "CR Touch triggered during raise_to_trigger assist on %s — auto-reprobing.",
                    active_corner,
                )
                self._queue_full_reprobe(
                    f"CR Touch triggered at {assist.get('corner_label', active_corner)}. "
                    "Re-probing all 5 points to refresh the target plane.",
                    "assist_triggered_reprobe_requested",
                )
                self._guide_avoid_corner_id = active_corner

        return

    def handle_error_message(self, comm_instance, error_message, *args, **kwargs):
        self._logger.warning("Firmware error line received: %s", error_message)
        if "unknown command" in error_message.lower() and "g30" in error_message.lower():
            self._set_error_state(
                "The printer reported that G30 is unavailable. Enable single-point probing in Marlin before using this workflow."
            )
        if "z probe past bed" in error_message.lower():
            self._set_error_state(
                "The configured probe points exceed the safe probe area for the current CR Touch offset. Move the right-side points inward and rerun the workflow."
            )
        return False


__plugin_name__ = "Printer Setup"
__plugin_pythoncompat__ = ">=3.8,<4"
__plugin_implementation__ = TestLevelPlugin()
__plugin_hooks__ = {
    "octoprint.comm.protocol.gcode.error": __plugin_implementation__.handle_error_message,
    "octoprint.comm.protocol.gcode.received": __plugin_implementation__.handle_received_line,
}
