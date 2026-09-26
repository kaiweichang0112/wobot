/*
 * Wobot robot - stage 2b firmware (M/O/X compose protocol + lease).
 *
 * Stage 1 measured the rig; stage 2a proved the BLE transport. Stage 2b is the
 * protocol itself: a pose is a CENTRE (moved by M) plus per-axis OSCILLATION
 * (set by O) that compose - output = centre + amp*sin(phase). A single lease
 * timer, armed only by BLE commands, returns the robot to neutral if the app
 * stops refreshing it. All of this is testable over Serial first:
 * M/O/X can be typed in the console, where they do not arm the lease.
 *   M <pan> <tilt> <roll> <ms>          move all centres to a pose
 *   O <axis> <amp> <period_ms> <lease>  oscillate one axis (amp 0 = stop it)
 *   X                                   stop, return to neutral
 *
 * Requires Arduino IDE: Tools -> "IP/Bluetooth Stack" set to an option that
 * includes Bluetooth (e.g. "IPv4 + Bluetooth"), or BLE will not link.
 *
 * Purpose of the earlier Serial stage: measure the three numbers every later
 * pose parameter depends on.
 *
 *   1. The real angles of the NEUTRAL POSE (phone upright, screen facing the user).
 *      Servos are assembled at the 90 deg assembly datum, which is NOT the neutral
 *      pose - at the datum the phone lies flat facing the ceiling.
 *   2. The amplitude / speed at which the rig starts to wobble on the table.
 *   3. The angular acceleration at which the magnetically mounted phone starts to
 *      slip on the roll axis. Slip is cumulative and unrecoverable: there is no
 *      position feedback, so a slipped phone stays crooked forever.
 *
 * The motion core (move-to-target, oscillate) is shared: BLE writes and Serial
 * lines both flow through handleCommand(). BLE changes the command source, not
 * the motion.
 *
 * Board: Raspberry Pi Pico W, Arduino-Pico core (earlephilhower).
 * Wiring: pan=GP2 (MG995), tilt=GP6 (MG995), roll=GP10 (SG90). Servos on a
 *         separate 5V supply, star-grounded to a single point shared with the
 *         Pico ground, with a 2200uF cap across the servo rail near the servos.
 *         Without the shared star ground the SG90 jitters whenever the MG995s
 *         draw current (ground bounce) - this was diagnosed and fixed 2026-07-24.
 *
 * Stage 1 measurements (2026-07-24), the inputs to every later pose parameter:
 *   - neutral pose:  pan 90, tilt 160, roll 90  (compiled in below)
 *   - tilt wobble onset ~4740 deg/s^2, safe ceiling ~2700 deg/s^2
 *   - pan  wobble not reached below ~3300 deg/s^2 (very stiff)
 *   - roll magnetic-slip onset ~3160 deg/s^2, safe ceiling ~800 deg/s^2
 *     roll is the only unrecoverable axis - keep its accelerations lowest.
 */

#include <Servo.h>
#include <BTstackLib.h>
#include <pico/unique_id.h>

// ---------------------------------------------------------------- configuration

static const int PIN_PAN = 2;
static const int PIN_TILT = 6;
static const int PIN_ROLL = 10;

// Arduino's default hobby-servo pulse range, mapped across 0..180 degrees.
// Widen only if a servo visibly fails to reach an angle you know it can hit -
// driving past a servo's internal stop makes it stall and overheat.
static const int SERVO_MIN_US = 544;
static const int SERVO_MAX_US = 2400;

static const float ASSEMBLY_DATUM = 90.0f;  // every robot is assembled here

// Measured 2026-07-23. At the datum the phone mount faces the ceiling; the
// neutral pose tilts it up to vertical less 20 deg, so the screen leans back
// into the face of someone looking down at the robot.
static const float NEUTRAL_PAN = 90.0f;
static const float NEUTRAL_TILT = 160.0f;
static const float NEUTRAL_ROLL = 90.0f;

// Direction convention, measured on the rig:
//   pan  increasing -> head turns to the VIEWER'S RIGHT
//   tilt increasing -> head noses DOWN (90 = mount faces ceiling, 180 = upright)
//   roll increasing -> screen rotates COUNTER-CLOCKWISE as the viewer sees it
//
// Soft limits. Tilt's mechanical stop is at 180, so the ceiling is 5 deg short
// of it — never let a bug drive a servo into a stop, it stalls and overheats.
// Pan and roll stops are not measured yet, so they stay conservative.
static const float PAN_MIN = 45.0f, PAN_MAX = 135.0f;
static const float TILT_MIN = 90.0f, TILT_MAX = 175.0f;
static const float ROLL_MIN = 45.0f, ROLL_MAX = 135.0f;

static const unsigned long UPDATE_INTERVAL_MS = 20;  // 50 Hz
static const unsigned long DEFAULT_MOVE_MS = 800;

// Oscillation amplitude is eased toward its target at this rate so starting or
// stopping an oscillation (O with amp 0) never steps the output. Roll is the
// slip-prone axis; keeping the slew gentle also keeps its accelerations low.
static const float AMP_SLEW_DEG_PER_S = 30.0f;

// Lease / liveness: a single timer, armed and refreshed by BLE M and O commands.
// On expiry the robot returns to neutral so a crashed or disconnected app
// cannot leave it moving. Serial commands do not arm it (a wired console needs
// no dead-man's switch).
static const unsigned long DEFAULT_LEASE_MS = 8000;

// ------------------------------------------------------------------- axis model

// A pose composes a CENTRE and an OSCILLATION that ride on top of each other:
//   output = centre (eased by M / move) + amp * sin(phase) (set by O / osc)
// The centre eases to its target over a duration; the oscillation's phase is
// advanced every frame so changing its period or amplitude never steps output.
struct Axis {
  Servo servo;
  int pin;
  const char *name;

  bool attached;
  float current;    // last output written = centre + oscillation offset
  float neutral;
  float minLimit;
  float maxLimit;

  // Centre, eased ease-in-out toward centerTarget over centerDuration.
  float center;
  float centerFrom;
  float centerTarget;
  unsigned long centerStart;
  unsigned long centerDuration;
  bool centerMoving;

  // Oscillation riding on the centre.
  float oscPhase;      // radians, advanced per frame
  float oscAmp;        // current eased amplitude (deg); 0 = at rest
  float oscAmpTarget;  // commanded amplitude
  float oscPeriodMs;
};

static Axis axes[3];
static const int PAN = 0;
static const int TILT = 1;
static const int ROLL = 2;

static unsigned long defaultMoveMs = DEFAULT_MOVE_MS;
static unsigned long lastUpdate = 0;

// Lease timer, armed only by BLE commands (see DEFAULT_LEASE_MS).
static bool livenessArmed = false;
static unsigned long livenessDeadline = 0;
static unsigned long livenessTimeoutMs = DEFAULT_LEASE_MS;

// ------------------------------------------------------------------- primitives

static int degToUs(float deg) {
  float span = SERVO_MAX_US - SERVO_MIN_US;
  return (int)lroundf(SERVO_MIN_US + (deg / 180.0f) * span);
}

static void initAxis(Axis &a, int pin, const char *name, float neutral,
                     float minLimit, float maxLimit) {
  a.pin = pin;
  a.name = name;
  a.attached = false;
  // Boot at neutral, not at the datum: every abnormal stop returns the robot to
  // neutral via the lease, so this is where the head almost always already is, and
  // attaching there means the servos barely move.
  a.current = neutral;
  a.neutral = neutral;
  a.minLimit = minLimit;
  a.maxLimit = maxLimit;
  a.center = neutral;
  a.centerFrom = neutral;
  a.centerTarget = neutral;
  a.centerMoving = false;
  a.oscPhase = 0.0f;
  a.oscAmp = 0.0f;
  a.oscAmpTarget = 0.0f;
  a.oscPeriodMs = 1000.0f;
}

/// Primitive: ease the centre to a target angle over a duration (ease-in-out).
static void moveCenter(Axis &a, float target, unsigned long durationMs) {
  a.centerFrom = a.center;
  a.centerTarget = constrain(target, a.minLimit, a.maxLimit);
  a.centerStart = millis();
  a.centerDuration = durationMs == 0 ? 1 : durationMs;
  a.centerMoving = true;
}

/// Primitive: set the oscillation riding on the centre. amp 0 eases to rest.
/// Phase is left running so the amplitude/period change never steps the output.
static void setOscillation(Axis &a, float amplitude, float periodMs) {
  a.oscAmpTarget = amplitude;
  if (periodMs >= 1.0f) a.oscPeriodMs = periodMs;
}

/// Declare where the head physically is, without moving it (use while detached).
static void setAxisPosition(Axis &a, float value) {
  value = constrain(value, a.minLimit, a.maxLimit);
  a.centerMoving = false;
  a.center = value;
  a.centerTarget = value;
  a.oscAmp = 0.0f;
  a.oscAmpTarget = 0.0f;
  a.current = value;
}

/// "stop": hold the current centre and damp any oscillation to rest.
static void haltAxis(Axis &a) {
  a.centerMoving = false;
  a.centerTarget = a.center;
  a.oscAmpTarget = 0.0f;
}

/// Ease every axis back to the neutral pose and let its oscillation settle.
static void goNeutral(unsigned long durationMs) {
  for (int i = 0; i < 3; i++) {
    moveCenter(axes[i], axes[i].neutral, durationMs);
    axes[i].oscAmpTarget = 0.0f;
  }
}

static void refreshLiveness() {
  livenessDeadline = millis() + livenessTimeoutMs;
  livenessArmed = true;
}

static void updateLiveness(unsigned long now) {
  if (livenessArmed && (long)(now - livenessDeadline) >= 0) {
    livenessArmed = false;
    goNeutral(DEFAULT_MOVE_MS);
    Serial.println(F("[BLE] lease expired -> neutral"));
  }
}

static void updateAxis(Axis &a, float dt, unsigned long now) {
  // Centre easing (time-based S-curve).
  if (a.centerMoving) {
    unsigned long elapsed = now - a.centerStart;
    if (elapsed >= a.centerDuration) {
      a.center = a.centerTarget;
      a.centerMoving = false;
    } else {
      float u = (float)elapsed / (float)a.centerDuration;
      float eased = (1.0f - cosf(PI * u)) * 0.5f;
      a.center = a.centerFrom + (a.centerTarget - a.centerFrom) * eased;
    }
  }

  // Ease amplitude toward its target so oscillation starts and stops smoothly.
  float da = a.oscAmpTarget - a.oscAmp;
  float step = AMP_SLEW_DEG_PER_S * dt;
  if (fabsf(da) <= step) a.oscAmp = a.oscAmpTarget;
  else a.oscAmp += (da > 0.0f) ? step : -step;

  // Advance the phase (rate set by period) and compute the offset.
  float offset = 0.0f;
  if (a.oscAmp != 0.0f) {
    a.oscPhase += 2.0f * PI * dt / (a.oscPeriodMs / 1000.0f);
    if (a.oscPhase > 2.0f * PI) a.oscPhase -= 2.0f * PI;
    offset = a.oscAmp * sinf(a.oscPhase);
  }

  float out = constrain(a.center + offset, a.minLimit, a.maxLimit);
  a.current = out;
  if (a.attached) {
    a.servo.writeMicroseconds(degToUs(out));
  }
}

// ------------------------------------------------------- measurement reporting

/*
 * Peak angular velocity and acceleration are what actually cause wobble and
 * magnetic slip - not the amplitude on its own. Printing them turns this sketch
 * into a measuring instrument: whatever command was running when the rig wobbled
 * or the phone slipped, its printed numbers are the limit you record.
 *
 *   ease-in-out move over duration T:  peak vel = A*PI/(2T),  peak acc = A*PI^2/(2T^2)
 *   sine oscillation of period P:      peak vel = A*2PI/P,    peak acc = A*(2PI/P)^2
 */
static void reportMove(float amplitudeDeg, float durationS) {
  float peakVel = amplitudeDeg * PI / (2.0f * durationS);
  float peakAcc = amplitudeDeg * PI * PI / (2.0f * durationS * durationS);
  Serial.print(F("    peak vel "));
  Serial.print(peakVel, 1);
  Serial.print(F(" deg/s, peak acc "));
  Serial.print(peakAcc, 1);
  Serial.println(F(" deg/s^2"));
}

static void reportOsc(float amplitudeDeg, float periodS) {
  float w = 2.0f * PI / periodS;
  Serial.print(F("    peak vel "));
  Serial.print(amplitudeDeg * w, 1);
  Serial.print(F(" deg/s, peak acc "));
  Serial.print(amplitudeDeg * w * w, 1);
  Serial.println(F(" deg/s^2"));
}

// ---------------------------------------------------------------- serial plumbing

static void printPose() {
  Serial.print(F("pose  pan="));
  Serial.print(axes[PAN].current, 1);
  Serial.print(F("  tilt="));
  Serial.print(axes[TILT].current, 1);
  Serial.print(F("  roll="));
  Serial.println(axes[ROLL].current, 1);
}

static void printLimits() {
  for (int i = 0; i < 3; i++) {
    Serial.print(F("limit "));
    Serial.print(axes[i].name);
    Serial.print(F(" ["));
    Serial.print(axes[i].minLimit, 1);
    Serial.print(F(", "));
    Serial.print(axes[i].maxLimit, 1);
    Serial.println(F("]"));
  }
}

static void printHelp() {
  Serial.println(F("--- Wobot robot bring-up -----------------------------------"));
  Serial.println(F("attach            engage servos (they jump to their current target)"));
  Serial.println(F("detach            release servos - THE PHONE WILL DROP under gravity"));
  Serial.println(F("pos               print current angles"));
  Serial.println(F("neutral           go to the neutral pose (90, 160, 90)"));
  Serial.println(F("datum             all axes to 90 - phone mount faces the ceiling"));
  Serial.println(F("setpos <p> <t> <r>   declare where the head is, without moving it"));
  Serial.println(F(""));
  Serial.println(F("p|t|r <a>         move one axis to angle a"));
  Serial.println(F("p|t|r +<d>|-<d>   nudge one axis by d degrees"));
  Serial.println(F("go <p> <t> <r>    move all three axes together"));
  Serial.println(F("speed <ms>        default move duration (now: see below)"));
  Serial.println(F("move <axis> <a> <ms>   timed move, prints peak vel/acc"));
  Serial.println(F(""));
  Serial.println(F("osc <axis> <amp> <period_ms>   oscillate, prints peak vel/acc"));
  Serial.println(F("stop              stop all motion, hold position"));
  Serial.println(F(""));
  Serial.println(F("--- BLE protocol (also typeable here for testing) ---"));
  Serial.println(F("M <p> <t> <r> <ms>          move all centres to a pose"));
  Serial.println(F("O <axis> <amp> <period> <lease>   oscillate one axis, ms lease"));
  Serial.println(F("X                           stop, return to neutral"));
  Serial.println(F("  centre + oscillation compose; over BLE a lease auto-returns"));
  Serial.println(F("  to neutral if not refreshed. Serial does not arm the lease."));
  Serial.println(F(""));
  Serial.println(F("lim <axis> <min> <max>   set soft limits"));
  Serial.println(F("limits            print soft limits"));
  Serial.println(F("help              this text"));
  Serial.println(F("  <axis> is pan | tilt | roll"));
  Serial.print(F("  default move duration: "));
  Serial.print(defaultMoveMs);
  Serial.println(F(" ms"));
  Serial.println(F("-------------------------------------------------------------"));
}

static int axisFromName(const char *s) {
  if (strcmp(s, "pan") == 0 || strcmp(s, "p") == 0) return PAN;
  if (strcmp(s, "tilt") == 0 || strcmp(s, "t") == 0) return TILT;
  if (strcmp(s, "roll") == 0 || strcmp(s, "r") == 0) return ROLL;
  return -1;
}

static void attachAll() {
  for (int i = 0; i < 3; i++) {
    if (!axes[i].attached) {
      axes[i].servo.attach(axes[i].pin, SERVO_MIN_US, SERVO_MAX_US);
      axes[i].attached = true;
    }
    axes[i].servo.writeMicroseconds(degToUs(axes[i].current));
  }
  Serial.println(F("attached"));
  printPose();
}

static void detachAll() {
  for (int i = 0; i < 3; i++) {
    axes[i].centerMoving = false;
    axes[i].oscAmp = 0.0f;
    axes[i].oscAmpTarget = 0.0f;
    if (axes[i].attached) {
      axes[i].servo.detach();
      axes[i].attached = false;
    }
  }
  livenessArmed = false;
  Serial.println(F("detached - servos are limp"));
}

static void handleCommand(char *line, bool fromBLE) {
  char *tok[5];
  int n = 0;
  char *p = strtok(line, " \t");
  while (p != NULL && n < 5) {
    tok[n++] = p;
    p = strtok(NULL, " \t");
  }
  if (n == 0) return;

  // --- BLE protocol commands (M / O / X). Only BLE writes arm the lease. ---

  if (strcmp(tok[0], "M") == 0 && n >= 5) {
    unsigned long ms = (unsigned long)atol(tok[4]);
    moveCenter(axes[PAN], atof(tok[1]), ms);
    moveCenter(axes[TILT], atof(tok[2]), ms);
    moveCenter(axes[ROLL], atof(tok[3]), ms);
    if (fromBLE) refreshLiveness();
    return;
  }

  if (strcmp(tok[0], "O") == 0 && n >= 5) {
    int a = axisFromName(tok[1]);
    if (a < 0) { Serial.println(F("? unknown axis")); return; }
    float amp = atof(tok[2]);
    float period = atof(tok[3]);
    if (period < 1.0f) period = 1.0f;
    setOscillation(axes[a], amp, period);
    if (fromBLE) {
      unsigned long lease = (unsigned long)atol(tok[4]);
      if (lease > 0) livenessTimeoutMs = lease;
      refreshLiveness();
    }
    return;
  }

  if (strcmp(tok[0], "X") == 0) {
    goNeutral(DEFAULT_MOVE_MS);
    livenessArmed = false;
    return;
  }

  if (strcmp(tok[0], "help") == 0 || strcmp(tok[0], "?") == 0) {
    printHelp();
    return;
  }
  if (strcmp(tok[0], "attach") == 0) { attachAll(); return; }
  if (strcmp(tok[0], "detach") == 0) { detachAll(); return; }
  if (strcmp(tok[0], "pos") == 0) { printPose(); return; }
  if (strcmp(tok[0], "limits") == 0) { printLimits(); return; }

  if (strcmp(tok[0], "stop") == 0) {
    for (int i = 0; i < 3; i++) haltAxis(axes[i]);
    livenessArmed = false;
    Serial.println(F("stopped"));
    printPose();
    return;
  }

  if (strcmp(tok[0], "neutral") == 0) {
    goNeutral(defaultMoveMs);
    Serial.println(F("-> neutral pose"));
    return;
  }

  if (strcmp(tok[0], "datum") == 0) {
    for (int i = 0; i < 3; i++) moveCenter(axes[i], ASSEMBLY_DATUM, defaultMoveMs);
    Serial.println(F("-> assembly datum (90, 90, 90) - phone mount faces ceiling"));
    return;
  }

  // Tell the firmware where the head actually is, without moving it. Only
  // meaningful while detached: use it before 'attach' when the head was left
  // somewhere unexpected, so attaching does not snap across a large gap.
  if (strcmp(tok[0], "setpos") == 0 && n >= 4) {
    for (int i = 0; i < 3; i++) {
      setAxisPosition(axes[i], atof(tok[i + 1]));
    }
    if (axes[0].attached) Serial.println(F("(attached - servos will jump there)"));
    printPose();
    return;
  }

  if (strcmp(tok[0], "speed") == 0 && n >= 2) {
    defaultMoveMs = (unsigned long)atol(tok[1]);
    if (defaultMoveMs == 0) defaultMoveMs = 1;
    Serial.print(F("default move duration "));
    Serial.print(defaultMoveMs);
    Serial.println(F(" ms"));
    return;
  }

  if (strcmp(tok[0], "go") == 0 && n >= 4) {
    moveCenter(axes[PAN], atof(tok[1]), defaultMoveMs);
    moveCenter(axes[TILT], atof(tok[2]), defaultMoveMs);
    moveCenter(axes[ROLL], atof(tok[3]), defaultMoveMs);
    Serial.println(F("moving"));
    return;
  }

  if (strcmp(tok[0], "move") == 0 && n >= 4) {
    int a = axisFromName(tok[1]);
    if (a < 0) { Serial.println(F("? unknown axis")); return; }
    float target = atof(tok[2]);
    unsigned long ms = (unsigned long)atol(tok[3]);
    if (ms == 0) ms = 1;
    float delta = fabsf(constrain(target, axes[a].minLimit, axes[a].maxLimit) - axes[a].center);
    moveCenter(axes[a], target, ms);
    Serial.print(F("move "));
    Serial.print(axes[a].name);
    Serial.print(F(" by "));
    Serial.print(delta, 1);
    Serial.print(F(" deg over "));
    Serial.print(ms);
    Serial.println(F(" ms"));
    reportMove(delta, ms / 1000.0f);
    return;
  }

  if (strcmp(tok[0], "osc") == 0 && n >= 4) {
    int a = axisFromName(tok[1]);
    if (a < 0) { Serial.println(F("? unknown axis")); return; }
    float amp = atof(tok[2]);
    float period = atof(tok[3]);
    if (period < 1.0f) period = 1.0f;
    setOscillation(axes[a], amp, period);
    Serial.print(F("osc "));
    Serial.print(axes[a].name);
    Serial.print(F(" amp "));
    Serial.print(amp, 1);
    Serial.print(F(" deg, period "));
    Serial.print(period, 0);
    Serial.println(F(" ms"));
    reportOsc(amp, period / 1000.0f);
    return;
  }

  if (strcmp(tok[0], "lim") == 0 && n >= 4) {
    int a = axisFromName(tok[1]);
    if (a < 0) { Serial.println(F("? unknown axis")); return; }
    axes[a].minLimit = atof(tok[2]);
    axes[a].maxLimit = atof(tok[3]);
    printLimits();
    return;
  }

  // Single-axis shorthand: "p 95", "t +2", "r -1.5"
  int a = axisFromName(tok[0]);
  if (a >= 0 && n >= 2) {
    float value = atof(tok[1]);
    bool relative = (tok[1][0] == '+' || tok[1][0] == '-');
    float target = relative ? axes[a].center + value : value;
    moveCenter(axes[a], target, defaultMoveMs);
    Serial.print(F("-> "));
    Serial.print(axes[a].name);
    Serial.print(F(" "));
    Serial.println(constrain(target, axes[a].minLimit, axes[a].maxLimit), 1);
    return;
  }

  Serial.println(F("? unknown command - type help"));
}

static void readSerial() {
  static char buf[64];
  static int len = 0;

  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      buf[len] = '\0';
      if (len > 0) handleCommand(buf, false);  // Serial: no lease
      len = 0;
    } else if (len < (int)sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}

// ------------------------------------------------------------------------- BLE
//
// A BLE peripheral whose Command characteristic hands each written ASCII line
// to handleCommand() - the very parser the Serial console uses. A read-only
// Status characteristic reports the current angles; it has no notifications.
//
// One base UUID with an incrementing 16-bit field, the usual convention.
static const char *SERVICE_UUID     = "6b9a1000-8f3e-4b7a-9c2d-1f5e7a0b3c11";
static const char *CMD_CHAR_UUID    = "6b9a1001-8f3e-4b7a-9c2d-1f5e7a0b3c11";
static const char *STATUS_CHAR_UUID = "6b9a1002-8f3e-4b7a-9c2d-1f5e7a0b3c11";
// NOTE: this BTstackLib passes the ATT value handle to the read/write callbacks,
// not the characteristic_id given here - so callbacks cannot guard on these ids.
// We rely instead on the invariant that only the Command char is writable and
// only the Status char is meaningfully readable.
static const uint16_t CMD_CHAR_ID = 1;
static const uint16_t STATUS_CHAR_ID = 2;

static char deviceName[24];

// Single-producer (BLE callback) / single-consumer (loop) ring of command
// lines. The write callback may run in BTstack context, so it only enqueues;
// all parsing and servo mutation stay in loop() via handleCommand().
static const int CMD_QUEUE_LEN = 8;
static const int CMD_LINE_MAX = 64;
static volatile char cmdQueue[CMD_QUEUE_LEN][CMD_LINE_MAX];
static volatile uint8_t cmdHead = 0;
static volatile uint8_t cmdTail = 0;

static void buildDeviceName() {
  pico_unique_board_id_t id;
  pico_get_unique_board_id(&id);
  // Last two bytes of the board id keep every robot's name distinct.
  snprintf(deviceName, sizeof(deviceName), "Wobot-Robot-%02X%02X",
           id.id[6], id.id[7]);
}

static void enqueueCommand(const char *data, uint16_t size) {
  uint8_t next = (uint8_t)((cmdTail + 1) % CMD_QUEUE_LEN);
  if (next == cmdHead) return;  // queue full - drop, latest attempts retry
  uint16_t n = size;
  if (n > CMD_LINE_MAX - 1) n = CMD_LINE_MAX - 1;
  // A BLE write is one whole command; trim any trailing CR/LF/space.
  while (n > 0 && (data[n - 1] == '\n' || data[n - 1] == '\r' || data[n - 1] == ' ')) {
    n--;
  }
  if (n == 0) return;
  for (uint16_t i = 0; i < n; i++) cmdQueue[cmdTail][i] = data[i];
  cmdQueue[cmdTail][n] = '\0';
  cmdTail = next;
}

static void drainCommandQueue() {
  while (cmdHead != cmdTail) {
    char line[CMD_LINE_MAX];
    uint8_t h = cmdHead;
    for (int i = 0; i < CMD_LINE_MAX; i++) {
      line[i] = cmdQueue[h][i];
      if (line[i] == '\0') break;
    }
    line[CMD_LINE_MAX - 1] = '\0';
    Serial.print(F("[BLE] "));
    Serial.println(line);
    handleCommand(line, true);  // BLE: arms the lease
    cmdHead = (uint8_t)((h + 1) % CMD_QUEUE_LEN);
  }
}

static int gattWriteCallback(uint16_t characteristic_id, uint8_t *buffer, uint16_t size) {
  // The Command char is the only writable characteristic, so any write is a
  // command. (The callback's id is an ATT handle, not CMD_CHAR_ID - see note
  // above.) drainCommandQueue() echoes the line, so no logging is needed here.
  (void)characteristic_id;
  enqueueCommand((const char *)buffer, size);
  return 0;  // 0 = write accepted
}

static uint16_t gattReadCallback(uint16_t characteristic_id, uint8_t *buffer, uint16_t buffer_size) {
  // Status is the only characteristic read for data; returning the pose for any
  // read is harmless. (The id is an ATT handle, not STATUS_CHAR_ID - see note.)
  (void)characteristic_id;
  char tmp[24];
  int n = snprintf(tmp, sizeof(tmp), "%d %d %d",
                   (int)lroundf(axes[PAN].current),
                   (int)lroundf(axes[TILT].current),
                   (int)lroundf(axes[ROLL].current));
  if (n < 0) return 0;
  if (buffer) {  // called once with buffer=NULL to query length, then to fill
    uint16_t copy = (uint16_t)n;
    if (copy > buffer_size) copy = buffer_size;
    memcpy(buffer, tmp, copy);
  }
  return (uint16_t)n;
}

static void bleConnected(BLEStatus status, BLEDevice *device) {
  (void)status;
  (void)device;
  Serial.println(F("[BLE] central connected"));
}

static void bleDisconnected(BLEDevice *device) {
  (void)device;
  // Disconnecting changes nothing by itself: if the central stops refreshing
  // the lease, updateLiveness() returns the robot to neutral.
  Serial.println(F("[BLE] central disconnected"));
}

static void setupBLE() {
  buildDeviceName();
  BTstack.setBLEDeviceConnectedCallback(bleConnected);
  BTstack.setBLEDeviceDisconnectedCallback(bleDisconnected);
  BTstack.setGATTCharacteristicRead(gattReadCallback);
  BTstack.setGATTCharacteristicWrite(gattWriteCallback);
  BTstack.addGATTService(new UUID(SERVICE_UUID));
  // READ is included to match the known-good LEPeripheral example shape; some
  // BTstackLib versions register the dynamic write handler differently for a
  // write-only characteristic.
  BTstack.addGATTCharacteristicDynamic(new UUID(CMD_CHAR_UUID),
      ATT_PROPERTY_READ | ATT_PROPERTY_WRITE | ATT_PROPERTY_WRITE_WITHOUT_RESPONSE,
      CMD_CHAR_ID);
  BTstack.addGATTCharacteristicDynamic(new UUID(STATUS_CHAR_UUID),
      ATT_PROPERTY_READ, STATUS_CHAR_ID);
  BTstack.setup(deviceName);
  BTstack.startAdvertising();
}

// ------------------------------------------------------------------------ setup

void setup() {
  Serial.begin(115200);

  initAxis(axes[PAN], PIN_PAN, "pan", NEUTRAL_PAN, PAN_MIN, PAN_MAX);
  initAxis(axes[TILT], PIN_TILT, "tilt", NEUTRAL_TILT, TILT_MIN, TILT_MAX);
  initAxis(axes[ROLL], PIN_ROLL, "roll", NEUTRAL_ROLL, ROLL_MIN, ROLL_MAX);

  unsigned long waitStart = millis();
  while (!Serial && millis() - waitStart < 3000) {
    delay(10);
  }

  setupBLE();

  Serial.println();
  Serial.println(F("Wobot robot - stage 2b (M/O/X compose protocol + lease)"));
  Serial.print(F("Advertising as: "));
  Serial.println(deviceName);
  Serial.println(F("Servos are DETACHED and assumed to be at the NEUTRAL pose."));
  Serial.println(F("Write 'attach' (Serial or the BLE Command characteristic) to"));
  Serial.println(F("engage them. Every Serial command also works over BLE."));
  Serial.println();
  printHelp();
}

void loop() {
  BTstack.loop();
  drainCommandQueue();
  readSerial();

  unsigned long now = millis();
  if (now - lastUpdate >= UPDATE_INTERVAL_MS) {
    float dt = (now - lastUpdate) / 1000.0f;
    lastUpdate = now;
    updateLiveness(now);
    for (int i = 0; i < 3; i++) {
      updateAxis(axes[i], dt, now);
    }
  }
}
