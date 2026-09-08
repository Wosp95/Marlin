$(function () {
    function TestLevelEStepsViewModel(parameters) {
        var self = this;

        // Update temp label when input changes
        $("#esteps_target_temp").on("change input", function () {
            $("#esteps_temp_label").text($(this).val());
        });

        // Single button: heat (M109 waits), read E-steps, then extrude
        $("#esteps_btn_start").on("click", function () {
            var temp = parseInt($("#esteps_target_temp").val());
            $(this).prop("disabled", true);
            $("#esteps_progress").show();
            $("#esteps_progress_text").text("Heating to " + temp + "\u00B0C, then extruding...").removeClass("label-success").addClass("label-warning");
            $("#esteps_results").hide();

            OctoPrint.simpleApiCommand("testlevel", "esteps_start", { target_temp: temp })
                .done(function () {
                    // Progress updates come via plugin messages
                })
                .fail(function (xhr) {
                    var resp = xhr.responseJSON || {};
                    alert(resp.error || "Failed to start");
                    $("#esteps_btn_start").prop("disabled", false);
                    $("#esteps_progress").hide();
                });
        });

        // Calculate, apply, and show result
        $("#esteps_btn_done").on("click", function () {
            var measured = parseFloat($("#esteps_measured").val());
            if (isNaN(measured) || measured < 0 || measured > 120) {
                alert("Please enter a valid measurement between 0 and 120 mm");
                return;
            }
            OctoPrint.simpleApiCommand("testlevel", "esteps_calculate_and_apply", { measured_remaining: measured })
                .done(function (data) {
                    if (data.error) {
                        alert(data.error);
                        return;
                    }
                    $("#res_actual").text(data.actual_extruded + " mm");
                    $("#res_pct_error").text(data.pct_error + "%");
                    $("#res_current").text(data.current_esteps + " steps/mm");
                    $("#res_new").text(data.new_esteps + " steps/mm");
                    $("#esteps_results").show();

                    if (Math.abs(data.pct_error) < 2) {
                        $("#esteps_result_status").html('<span class="label label-success">Within 2% \u2014 already good! Applied anyway for precision.</span>');
                    } else {
                        $("#esteps_result_status").html('<span class="label label-success">New E-steps applied (M92). Run again to verify, then save to EEPROM.</span>');
                    }
                })
                .fail(function (xhr) {
                    var resp = xhr.responseJSON || {};
                    alert(resp.error || "Calculation failed");
                });
        });

        // Save to EEPROM
        $("#esteps_btn_save").on("click", function () {
            if (!confirm("Save current E-steps to EEPROM? This persists across reboots.")) return;
            OctoPrint.simpleApiCommand("testlevel", "esteps_save")
                .done(function () {
                    $("#esteps_result_status").html('<span class="label label-success">Saved to EEPROM! Done.</span>');
                });
        });

        // Run again
        $("#esteps_btn_again").on("click", function () {
            $("#esteps_results").hide();
            $("#esteps_measured").val("");
            $("#esteps_btn_start").prop("disabled", false);
            $("#esteps_progress").hide();
            alert("Cut/mark your filament at 120mm again, then press 'Heat & Extrude 100mm'.");
        });

        // Listen for plugin messages via OctoPrint's socket
        OctoPrint.socket.onMessage("plugin", function (msg) {
            if (!msg.data || msg.data.plugin !== "testlevel") return;
            var data = msg.data.data;

            if (data.type === "esteps_progress") {
                $("#esteps_progress").show();
                var cls = data.phase === "done" ? "label-success" : "label-warning";
                $("#esteps_progress_text").text(data.message).removeClass("label-warning label-success").addClass(cls);
                if (data.phase === "done") {
                    $("#esteps_btn_start").prop("disabled", false);
                }
            }
        });
    }

    // Initialize immediately - no viewmodel registration needed since
    // we use jQuery events, not Knockout bindings.
    new TestLevelEStepsViewModel();

    // =====================================================================
    // PID Tune
    // =====================================================================
    $("#pid_btn_start").on("click", function () {
        var temp = parseInt($("#pid_target_temp").val());
        $(this).prop("disabled", true);
        $("#pid_progress").show();
        $("#pid_progress_text").text("Running PID autotune at " + temp + "\u00B0C...");
        $("#pid_results").hide();
        OctoPrint.simpleApiCommand("testlevel", "pid_start", { target_temp: temp })
            .fail(function (xhr) {
                var resp = xhr.responseJSON || {};
                alert(resp.error || "Failed to start PID tune");
                $("#pid_btn_start").prop("disabled", false);
                $("#pid_progress").hide();
            });
    });

    $("#pid_btn_apply").on("click", function () {
        OctoPrint.simpleApiCommand("testlevel", "pid_apply")
            .done(function (data) {
                $("#pid_apply_status").text("Saved! Kp=" + data.kp.toFixed(2) + " Ki=" + data.ki.toFixed(2) + " Kd=" + data.kd.toFixed(2));
            })
            .fail(function (xhr) {
                var resp = xhr.responseJSON || {};
                alert(resp.error || "Failed to apply PID values");
            });
    });

    OctoPrint.socket.onMessage("plugin", function (msg) {
        if (!msg.data || msg.data.plugin !== "testlevel") return;
        var data = msg.data.data;

        if (data.type === "pid_progress") {
            $("#pid_progress").show();
            $("#pid_progress_text").text(data.message);

            if (data.phase === "done" && data.results) {
                $("#pid_btn_start").prop("disabled", false);
                $("#pid_kp").text(data.results.kp.toFixed(2));
                $("#pid_ki").text(data.results.ki.toFixed(2));
                $("#pid_kd").text(data.results.kd.toFixed(2));
                $("#pid_results").show();
            } else if (data.phase === "error") {
                $("#pid_btn_start").prop("disabled", false);
            } else if (data.phase === "saved") {
                $("#pid_apply_status").text("Applied and saved to EEPROM!");
            }
        }
    });
});
