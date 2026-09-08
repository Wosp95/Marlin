# Remote Firmware Deployment

Build the transfer environment first:

```powershell
pio run -e STM32F103RE_creality_xfer
```

The target firmware must already have `BINARY_FILE_TRANSFER` and
`CUSTOM_FIRMWARE_UPLOAD` enabled. The Pi needs an isolated environment with
`pyserial` and `heatshrink2`, plus an SSH key for the deployment account.

Run the deployment from the repository root:

```powershell
python buildroot/share/scripts/deploy_firmware.py `
  .pio/build-deploy/STM32F103RE_creality_xfer/firmware-YYYYMMDD-HHMMSS.bin `
  --host PiPrinter --user kieran `
  --key $env:USERPROFILE/.ssh/octoprint_ed25519_new
```

The workflow disconnects OctoPrint, transfers the image using Marlin's binary
protocol, explicitly closes binary mode, sends `M997`, reconnects OctoPrint,
and waits for `Operational`. It reports the post-reboot firmware log line and
removes temporary Pi-side files. It does not start a print.

Test the complete transport without writing the image or rebooting:

```powershell
python buildroot/share/scripts/deploy_firmware.py `
  .pio/build-deploy/STM32F103RE_creality_xfer/firmware-YYYYMMDD-HHMMSS.bin `
  --host PiPrinter --user kieran `
  --key $env:USERPROFILE/.ssh/octoprint_ed25519_new `
  --dry-run
```