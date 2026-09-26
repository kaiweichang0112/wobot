# Firmware — Raspberry Pi Pico W

Single sketch: `wobot_robot/wobot_robot.ino`. `README.md` has the wiring,
the full protocol, the bring-up procedure and the rig measurements.

## Building

Built and flashed from the **Arduino IDE**, not a CLI — there is no build to run
from the terminal, so changes cannot be compiled or tested here.

- Core: **earlephilhower "Raspberry Pi Pico/RP2040"**. The Arduino Mbed core will
  not work — it has no Pico W and no `BTstackLib`.
- Board: *Raspberry Pi Pico W*, with a Tools → IP/Bluetooth Stack option that
  includes Bluetooth.

## Constraints

- **The firmware knows motion primitives, not meanings.** `M` (move centres),
  `O` (oscillate an axis), `X` (stop → neutral). Poses such as "happy" or
  "listening" belong to the BLE client; do not add them here.
- **One parser for both transports.** BLE writes and Serial lines both go
  through `handleCommand()`; BLE changes the command source, not the motion.
  The BLE write callback may run in BTstack context, so it only enqueues —
  parsing and servo mutation stay in `loop()`.
- **The lease is the only disconnect-safety mechanism.** Only BLE `M`/`O` arm
  it; `DEFAULT_LEASE_MS` (8000) applies until an `O` supplies its own value.
  Do not add behaviour to the disconnect callback instead.
- **Status (`6b9a1002-…`) is Read only — no NOTIFY.** It reports the computed
  pose, not measured positions; the hardware has no position feedback.
- **The wire format is a contract.** ASCII command lines, one per BLE write.
  Changing a command's name or arguments breaks every client.
- **Motion limits are measured, not guessed.** Neutral pose, soft limits and the
  roll slip ceiling (≈ 800 °/s²) come from rig measurements in `README.md`.
  Never widen a soft limit or raise an acceleration without a new measurement.
