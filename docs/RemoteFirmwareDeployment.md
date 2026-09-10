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
  --key $env:USERPROFILE/.ssh/octoprint_ed25519_new `
  --power-cycle `
  --ha-url $env:HOME_ASSISTANT_URL `
  --ha-token-file $env:USERPROFILE/.homeassistant/3d-printer-agent.token
```

The workflow disconnects OctoPrint, transfers the image using Marlin's binary
protocol, explicitly closes binary mode, sends `M997`, reconnects OctoPrint,
performs the required Home Assistant smart-plug cold cycle, reconnects
OctoPrint, and verifies the compiler date from the post-cycle firmware log.
Temporary Pi-side files are removed. It does not start a print.

Use the plain command without `--power-cycle` only for transport diagnostics.
It cannot prove firmware activation because the Creality bootloader requires a
cold power cycle after `M997`.

For Creality V4.2.x boards, the helper follows Marlin's upstream upload rule:
all root-level `.BIN` files are removed before transfer, and the new image is
written with a fresh 8.3 `FW-XXXXX.BIN` name. This avoids the Creality
bootloader ignoring a previously used filename. The old firmware should be
kept as a local rollback artifact, not left on the printer SD card.

This method requires the currently running firmware to have
`BINARY_FILE_TRANSFER` and `CUSTOM_FIRMWARE_UPLOAD` enabled. If the board does
not return after `M997`, use a physical FAT32 SD card with the same fresh 8.3
filename and power-cycle the printer; OctoPrint's ordinary file API is not a
firmware flashing mechanism.

Test the complete transport without writing the image or rebooting:

```powershell
python buildroot/share/scripts/deploy_firmware.py `
  .pio/build-deploy/STM32F103RE_creality_xfer/firmware-YYYYMMDD-HHMMSS.bin `
  --host PiPrinter --user kieran `
  --key $env:USERPROFILE/.ssh/octoprint_ed25519_new `
  --dry-run
```