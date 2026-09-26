# Wobot robot firmware

Raspberry Pi Pico W firmware for a three-axis robot head that carries a phone.
It exposes the servos over a BLE GATT service and a Serial console that share
one command parser, so everything a BLE client does can also be typed by hand.

## Hardware

| Axis | Pico W pin | Servo | Notes |
|------|-----------|-------|-------|
| pan  | GP2  | MG995 | base rotation |
| tilt | GP6  | MG995 | pitch — carries the phone's full weight at the end of the arm |
| roll | GP10 | SG90  | the only axis whose errors are unrecoverable (magnetic mount can slip) |

The three pins land on three different RP2040 PWM slices (1, 3, 5), so the axes
never contend for a timer.

Servos run from a **separate 5 V 2 A supply**, not from the Pico, with a
2200 µF electrolytic across the servo rail near the servos.

> **The servo supply ground must be tied to the Pico ground, star-grounded at a
> single point.** Without a shared reference the PWM signal is meaningless —
> servos jitter or stay dead. Without the *star* ground, the SG90 jitters
> whenever the MG995s draw current (ground bounce).

## Building

**Core matters — install the right one.** This firmware needs the
**earlephilhower "Raspberry Pi Pico/RP2040"** core, *not* the Arduino "Mbed OS
RP2040" core. The Mbed core has no Pico W board and no `BTstackLib`, so BLE will
not even find its headers.

| | earlephilhower (use this) | Arduino Mbed (won't work for BLE) |
|--|--|--|
| Boards Manager name | Raspberry Pi Pico/RP2040 · by Earle F. Philhower | Arduino Mbed OS RP2040 Boards |
| Pico W board | yes | no (only plain Pico) |
| BLE | BTstack, built in | none for Pico W |

1. Arduino IDE → Settings → *Additional boards manager URLs*, add
   `https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json`
2. Tools → Board → Boards Manager → search *pico* → install
   **Raspberry Pi Pico/RP2040 by Earle F. Philhower**.
3. Tools → Board → Raspberry Pi Pico/RP2040 → **Raspberry Pi Pico W** (with the W).
4. Tools → **IP/Bluetooth Stack** → an option that includes **Bluetooth**
   (e.g. *IPv4 + Bluetooth*). Without it the sketch will not link.

Then open `wobot_robot/wobot_robot.ino`, compile and flash.

## First boot

Open the Serial Monitor at **115200 baud**, line ending **Newline**. The banner
prints the advertised BLE name and the full command list (`help` reprints it).

Servos boot **detached** and the firmware assumes the head is at the **neutral
pose** (90, 160, 90). `attach` — over Serial or BLE — engages them at full speed
toward that assumed position. If the head was left somewhere else, run
`setpos <p> <t> <r>` first so `attach` does not snap across the gap with the
phone on board.

## Motion model

A pose is a **centre** plus a per-axis **oscillation** that compose:

```
output = centre + amp · sin(phase)
```

- The centre eases to its target with an ease-in-out S-curve over a given duration.
- The oscillation amplitude slews toward its target at 30 °/s, and the phase
  runs continuously, so starting, stopping or re-shaping an oscillation never
  steps the servos.
- Output is clamped to each axis's soft limits and written at 50 Hz.

## BLE protocol

**GATT layout** — service `6b9a1000-8f3e-4b7a-9c2d-1f5e7a0b3c11`

| Characteristic | UUID | Properties | Payload |
|----------------|------|-----------|---------|
| Command | `6b9a1001-…` | Write / Write Without Response (also Read) | one ASCII command line |
| Status  | `6b9a1002-…` | **Read only — no Notify** | `pan tilt roll` as integers |

Status reports the angles the motion model last computed, not measured servo
positions — there is no position feedback.

The device advertises as `Wobot-Robot-XXXX`, where `XXXX` is the last two bytes
of the Pico's unique board ID, so multiple robots never collide.

The wire format is ASCII lines: human-typable in nRF Connect for manual testing
and parsed by the same tokeniser as the Serial console. One BLE write is one
whole command; trailing CR/LF/spaces are trimmed.

**Commands**

```
attach                            engage servos — send once after connecting
M <pan> <tilt> <roll> <ms>        ease all three centres to a pose over <ms>
O <axis> <amp> <period> <lease>   oscillate one axis around its centre
X                                 ease back to neutral, stop oscillating
```

- `<axis>` is `p` | `t` | `r`. All values are degrees and milliseconds.
- To enact a pose, send one `M`, then an `O` per oscillating axis, then stay
  silent — the oscillation runs by itself.
- `O` with `amp 0` stops that axis oscillating (the offset eases back to 0).
- Every Serial command also works over BLE, but only `M`/`O` interact with the lease.

**Lease** — the only disconnect-safety mechanism:

- Only BLE `M` and `O` arm or refresh it; Serial never arms it.
- `O`'s `<lease>` (ms, if > 0) replaces the timeout, and later `M`s refresh with
  that value. The default before any `O` is 8000 ms.
- On expiry the robot eases back to neutral, so a crashed or disconnected client
  cannot leave it moving. A BLE disconnect by itself does nothing.
- `X`, `stop` and `detach` disarm it.
- A frozen pose is held alive by re-sending its `M` before the lease expires —
  there is no separate keep-alive.

**Delivery limits**

- Writes are queued in an 8-slot ring (7 pending commands); a full queue drops
  the write. Lines longer than 63 bytes are truncated.
- An accepted write is not an executed command: there is no command ID, ACK or
  error channel.

Example session in nRF Connect (value type **Text / UTF-8**, written to Command):

```
attach
M 90 160 90 800        -> to neutral over 0.8 s
O t 5 1500 8000        -> nod: tilt oscillates ±5°, 1.5 s period, 8 s lease
O p 3 2000 8000        -> add a gentle pan sway on top
X                      -> stop, return to neutral
```

## Serial command reference

```
attach / detach          engage / release servos (detached = phone drops)
pos                      print current angles
neutral                  go to the neutral pose (90, 160, 90)
datum                    all axes to 90 — phone mount faces the ceiling
setpos <p> <t> <r>       declare where the head is, without moving it
p|t|r <a>                move one axis to angle a
p|t|r +<d> | -<d>        nudge one axis
go <p> <t> <r>           move all three together
speed <ms>               default move duration
move <axis> <a> <ms>     timed move, prints peak vel/acc
osc <axis> <amp> <ms>    oscillate, prints peak vel/acc
stop                     stop all motion, hold position
lim <axis> <min> <max>   set soft limits
limits                   print soft limits
M / O / X                the BLE protocol, typeable here (no lease)
```

`<axis>` is `pan` | `tilt` | `roll` (or `p` | `t` | `r`).

Widen soft limits with `lim` only as you map each axis — driving a servo into a
mechanical stop makes it stall, overheat and eventually burn out.

## Verifying a build

**1. Motion logic over Serial.** `M`/`O`/`X` typed in the console do not arm the
lease, so the motion core can be tested with no BLE involved:

```
attach
M 90 155 90 800        centre eases: head lifts slightly
O t 5 1500 8000        tilt nods, riding on the centre
O p 3 2000 8000        add a pan sway on top — both compose
O t 0 1500 8000        tilt oscillation eases back to rest, pan continues
X                      stop everything, return to neutral
```

**2. BLE transport.** With **nRF Connect** (iOS/Android) or any generic BLE tool:

1. Scan and connect to the name printed in the Serial banner.
2. On the Command characteristic, set the value type to **Text / UTF-8** (not
   hex) and write `attach`. The Serial Monitor echoes `[BLE] attach`.
3. Write `osc tilt 5 1500` — tilt nods exactly as it does over Serial. Write `X`.
4. Read the Status characteristic — it returns the current angles.

If Serial works but BLE writes do nothing: check the Tools stack setting, that
the value was sent as **text not hex**, and that you wrote to Command (`…1001`).
Serial keeps working throughout, so a BLE-only failure is isolated to the
transport, not the motion core.

**3. Lease.** Write `O t 5 1500 4000` over BLE and send nothing else. After ~4 s
the robot eases back to neutral and the Serial Monitor prints
`[BLE] lease expired -> neutral`. Another `M` or `O` before then refreshes it.

## Rig measurements

These three numbers are the inputs to every pose parameter. Each `move` and
`osc` prints its peak angular velocity and acceleration; the numbers printed
when a failure appeared are the recorded limits.

### Neutral pose — measured 2026-07-23

**Neutral is `pan 90, tilt 160, roll 90`**, compiled into the sketch.

Servos are assembled at the 90° **assembly datum**, which is not the neutral
pose — at the datum the phone mount faces the ceiling. Neutral tilts it up to
20° short of vertical, so the screen leans back into the face of someone looking
down at the robot. Every robot is assembled at the same datum, so this offset is
a shared constant, not a per-robot calibration.

| Axis | Angle increasing |
|------|-----------------|
| pan  | head turns to the **viewer's right** |
| tilt | head noses **down** (90 = mount faces ceiling, 180 = upright) |
| roll | screen rotates **counter-clockwise** as the viewer sees it |

Tilt's mechanical stop is at **180°**, so from neutral there is only **20° of
nose-down travel** but **70° of nose-up** — poses should express themselves
upward. Tilt's soft limits are 90–175°, staying clear of the stop. Pan and roll
stops are not measured yet and keep conservative 45–135° soft limits.

### Wobble limit — measured 2026-07-24

The base is not fixed to the table and the centre of mass is high, so motion is
limited by whether the rig rocks, not by servo torque.

| Axis | Last clean | Wobble onset | Safe ceiling |
|------|-----------|--------------|--------------|
| tilt | `osc tilt 25 600` ≈ 2740 °/s² | `osc tilt 30 500` ≈ 4740 °/s² | ≈ 2700 °/s² |
| pan  | `osc pan 30 600` ≈ 3290 °/s² (still clean) | not reached | > 3300 °/s² |

For scale, a gentle speaking nod (tilt ±4° over 1.5 s) peaks near **70 °/s²** —
about 40× below the wobble onset. Wobble does not bind pan or tilt in practice.

### Roll slip limit — measured 2026-07-24

The phone is held magnetically. Magnets resist being pulled straight off well,
but resist *rotation* only through friction. If roll accelerates too hard the
phone slips and stays crooked — with no position feedback the error accumulates
silently and is only fixed by physically re-seating the phone.

Tested with tape across the phone case and mount, each setting run for about a
minute (slip accumulates; one cycle will not show it):

| Roll | Last clean | Slip onset | Safe ceiling |
|------|-----------|-----------|--------------|
| SG90 | `osc roll 20 700` ≈ 1610 °/s² | `osc roll 20 500` ≈ 3160 °/s² | **≈ 800 °/s²** |

Roll is the tightest and only unrecoverable axis: keep roll motions small and
well under 800 °/s².
