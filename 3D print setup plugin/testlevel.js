$(function () {
    function TestLevelViewModel(parameters) {
        this.loginState = parameters[0];
        this.bedTemp = ko.observable(60);
        this.isSubmitting = ko.observable(false);
        this.state = ko.observable({
            assist: null,
            defaults: {
                lower_label: "clockwise",
                raise_label: "counter-clockwise",
                screw_pitch_mm: 0.7,
                tolerance_mm: 0.03
            },
            error: null,
            measurements: [],
            results: {},
            stage: "idle",
            status: "Loading..."
        });

        this.hasResults = ko.pureComputed(() => {
            const currentState = this.state();
            return !!(currentState.results && currentState.results.corners && currentState.results.corners.length);
        });

        this.cornerResults = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.results && currentState.results.corners ? currentState.results.corners : [];
        });

        this.instructions = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.results && currentState.results.instructions ? currentState.results.instructions : [];
        });

        this.guideState = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.results && currentState.results.guide ? currentState.results.guide : null;
        });

        this.guidedCorner = ko.pureComputed(() => {
            const guide = this.guideState();
            return guide && guide.current_corner ? guide.current_corner : null;
        });

        this.hasGuidedCorner = ko.pureComputed(() => !!this.guidedCorner());

        this.strategy = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.results ? currentState.results.strategy || null : null;
        });

        this.centerResult = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.results ? currentState.results.center : null;
        });

        this.probeNoise = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.probe_noise || null;
        });

        this.effectiveTolerance = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.effective_tolerance_mm;
        });

        this.planeResult = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.results ? currentState.results.plane : null;
        });

        this.zCalibration = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState.z_calibration || { offsets: [], search: {} };
        });

        this.zCalibrationOffsets = ko.pureComputed(() => {
            const calibration = this.zCalibration();
            return calibration && calibration.offsets ? calibration.offsets : [];
        });

        this.zSearchState = ko.pureComputed(() => {
            const calibration = this.zCalibration();
            return calibration && calibration.search ? calibration.search : {};
        });

        this.isZCalibrationRunning = ko.pureComputed(() => {
            const calibration = this.zCalibration();
            return !!(calibration && calibration.running);
        });

        this.currentZBaseline = ko.pureComputed(() => {
            const currentState = this.state();
            return currentState && currentState.probe_offset ? currentState.probe_offset.z : null;
        });

        this.zSearchButtonLabel = ko.pureComputed(() => {
            if (this.isSubmitting()) {
                return "Sending...";
            }

            if (this.isZCalibrationRunning()) {
                return "Reading M851 / printing current pass...";
            }

            return "Start coarse Z search (-0.90 to -0.50)";
        });

        this.hasZCalibrationOffsets = ko.pureComputed(() => this.zCalibrationOffsets().length > 0);

        this.canStartZSearch = ko.pureComputed(() => !this.isSubmitting() && !this.isZCalibrationRunning());

        this.probeNoiseSummary = ko.pureComputed(() => {
            const probeNoise = this.probeNoise();
            if (!probeNoise) {
                return null;
            }

            if (probeNoise.passed === false) {
                return {
                    title: "Probe repeatability failed",
                    text: "The CR Touch is too noisy for trustworthy leveling guidance. Check mechanics, then run again."
                };
            }

            if (probeNoise.passed === true) {
                return {
                    title: "Probe repeatability OK",
                    text: "Sigma and tolerance were measured automatically for this run."
                };
            }

            return {
                title: "Checking probe repeatability",
                text: probeNoise.sample_count + "/" + probeNoise.expected_samples + " center samples captured."
            };
        });

        this.defaultGuidedInstruction = ko.pureComputed(() => {
            const corner = this.guidedCorner();
            if (!corner) {
                return "";
            }

            if (corner.within_tolerance) {
                return corner.label + " is already within tolerance on this pass.";
            }

            if (corner.assist_mode === "raise_to_trigger") {
                return "The CR Touch is at the target height for " + corner.label + ". Slowly turn that wheel " + corner.rotation_label + " to loosen it until the CR Touch just triggers, then press Re-probe.";
            }

            if (corner.assist_mode === "tighten") {
                return "Tighten " + corner.label + " by turning it " + corner.rotation_label + " about " + corner.turns_text + ", then press Re-probe.";
            }

            return "Adjust " + corner.label + " and press Re-probe.";
        });

        this.refreshState = () => {
            OctoPrint.simpleApiGet("testlevel")
                .done((response) => {
                    this.state(response);
                    if (response && response.bed_temp !== undefined && response.bed_temp !== null) {
                        this.bedTemp(response.bed_temp);
                    }
                });
        };

        this.runCommand = (command, payload, defaultError) => {
            if (this.isSubmitting()) {
                return;
            }

            this.isSubmitting(true);

            OctoPrint.simpleApiCommand("testlevel", command, payload || {})
                .done((response) => {
                    this.refreshState();
                    const message = response && response.message ? response.message : "Command queued";
                    new PNotify({
                        title: "Test Level",
                        text: message,
                        type: "success"
                    });
                })
                .fail((xhr) => {
                    let message = defaultError;

                    if (xhr && xhr.responseJSON && xhr.responseJSON.error) {
                        message = xhr.responseJSON.error;
                    }

                    new PNotify({
                        title: "Test Level",
                        text: message,
                        type: "error"
                    });
                })
                .always(() => {
                    this.isSubmitting(false);
                });
        };

        this.sendHome = () => {
            this.runCommand("home", {}, "Failed to queue homing command");
        };

        this.startWorkflow = () => {
            this.runCommand(
                "start_workflow",
                { bed_temp: parseInt(this.bedTemp(), 10) || 0 },
                "Failed to start the bed leveling workflow"
            );
        };

        this.abortWorkflow = () => {
            this.runCommand(
                "abort_workflow",
                {},
                "Failed to abort the bed leveling workflow"
            );
        };

        this.reprobe = () => {
            this.runCommand(
                "reprobe",
                {},
                "Failed to queue the full re-probe"
            );
        };

        this.startZSearch = () => {
            this.runCommand(
                "z_search_start",
                {},
                "Failed to start the coarse Z-offset search"
            );
        };

        this.refineZSearch = (offsetEntry) => {
            if (!offsetEntry) {
                return;
            }

            this.runCommand(
                "z_search_refine",
                { square: offsetEntry.square },
                "Failed to print the refinement Z-offset search pass"
            );
        };

        this.applyZOffset = (offsetEntry) => {
            if (!offsetEntry) {
                return;
            }

            this.runCommand(
                "apply_z_offset",
                { square: offsetEntry.square },
                "Failed to apply the selected Z offset"
            );
        };

        this.onStartupComplete = () => {
            this.refreshState();
            this._pollHandle = window.setInterval(() => {
                this.refreshState();
            }, 2000);
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: TestLevelViewModel,
        dependencies: ["loginStateViewModel"],
        elements: ["#tab_plugin_testlevel"]
    });
});
