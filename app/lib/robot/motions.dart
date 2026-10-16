import 'dart:math';

import 'protocol.dart';

/// The robot's motions (spec 26, DEC-103): what the head does for each feeling,
/// for speaking without one, and in each state of Avatar mode. A motion is data;
/// the director plays it. Poses are offsets from neutral (see [Pose]): a negative
/// tilt raises the head, and it can nose down only 15°.
///
/// A motion's quick part, its accent, is a chain of `M` moves: an S-curve between
/// two poses starts and ends at rest, so a chain of them is a smooth wave whose
/// first direction is known. What goes on while a sentence lasts, its carrier, is
/// a slow `O`: the firmware ramps an amplitude at 30 °/s, which adds acceleration
/// that only a slow oscillation can afford.

/// How much larger motions are played than written: the written sizes looked too
/// small on the rig (2026-10-16, the first round of tuning). Poses and oscillations
/// grow by it; a move it makes too hard for the [Budget] is slowed instead.
const gain = 1.8;

/// Roll grows twice as much again: head tilts were still too small to read (the
/// second round). It pays in time: the quickest rolls are slowed the most.
const rollGain = 3.6;

/// How far the head may nose down, ° from neutral: tilt's soft limit, 5° short of
/// the mechanical stop. A larger written dip is played as this.
const noseDown = 15.0;

double gainOf(RobotAxis axis) => axis == RobotAxis.roll ? rollGain : gain;

/// A written pose as it is played.
Pose amplify(Pose pose) => Pose(
  pan: pose.pan * gain,
  tilt: min(pose.tilt * gain, noseDown),
  roll: pose.roll * rollGain,
);

/// A written carrier as it is played.
Carrier amplifyCarrier(Carrier carrier) => {
  for (final MapEntry(key: axis, value: (amplitude, period)) in carrier.entries)
    axis: (amplitude * gainOf(axis), period),
};

/// A motion as it is played from neutral: [gain] larger, and each move slowed as
/// much as the [Budget] needs with the oscillations of the motion and of [carrier]
/// going on.
Motion fitted(Motion motion, {Carrier carrier = const {}}) {
  final load = <RobotAxis, double>{};
  void swing(RobotAxis axis, double amplitude, int period) => load[axis] = max(
    load[axis] ?? 0,
    Budget.swingAcceleration(amplitude * gainOf(axis), period),
  );
  for (final MapEntry(key: axis, value: (amplitude, period))
      in carrier.entries) {
    swing(axis, amplitude, period);
  }
  for (final step in motion.steps.whereType<Swing>()) {
    swing(step.axis, step.amplitude, step.periodMs);
  }
  var at = Pose.neutral;
  return Motion(motion.id, mirror: motion.mirror, still: motion.still, [
    for (final step in motion.steps)
      switch (step) {
        Go(:final pose, :final ms) => () {
          final to = amplify(pose);
          final safe = Go(to, Budget.safeMs(at, to, ms, load));
          at = to;
          return safe;
        }(),
        Swing(:final axis, :final amplitude, :final periodMs) => Swing(
          axis,
          amplitude * gainOf(axis),
          periodMs,
        ),
        Wait() => step,
      },
  ]);
}

/// One step of a motion.
sealed class Step {
  const Step();

  Step scaled(double amplitude, double time, int side);
}

/// `M`: eases the head to a pose over [ms], then the next step starts.
class Go extends Step {
  const Go(this.pose, this.ms);

  final Pose pose;
  final int ms;

  @override
  Go scaled(double amplitude, double time, int side) => Go(
    Pose(
      pan: pose.pan * amplitude * side,
      tilt: pose.tilt * amplitude,
      roll: pose.roll * amplitude * side,
    ),
    (ms * time).round(),
  );
}

/// `O`: oscillates one axis, at once; amplitude 0 eases it to rest.
class Swing extends Step {
  const Swing(this.axis, this.amplitude, this.periodMs);

  final RobotAxis axis;
  final double amplitude;
  final int periodMs;

  @override
  Swing scaled(double amplitude, double time, int side) =>
      Swing(axis, this.amplitude * amplitude, (periodMs * time).round());
}

/// Holds the pose for [ms].
class Wait extends Step {
  const Wait(this.ms);

  final int ms;

  @override
  Wait scaled(double amplitude, double time, int side) =>
      Wait((ms * time).round());
}

class Motion {
  const Motion(this.id, this.steps, {this.mirror = false, this.still = false});

  /// Such as `joy.bounce`.
  final String id;
  final List<Step> steps;

  /// Whether it may be played turned the other way, pan and roll negated.
  final bool mirror;

  /// Whether every oscillation stops while it plays.
  final bool still;

  /// How long its moves and waits take.
  int get ms => steps.fold(
    0,
    (total, step) =>
        total +
        switch (step) {
          Go(:final ms) || Wait(:final ms) => ms,
          Swing() => 0,
        },
  );

  /// The same motion, smaller by [amplitude], slower by [time] and turned to
  /// [side] (1 or -1, only when it may be mirrored).
  Motion scaled({double amplitude = 1, double time = 1, int side = 1}) =>
      Motion(id, [
        for (final step in steps)
          step.scaled(amplitude, time, mirror ? side : 1),
      ], still: still);
}

/// What goes on while a sentence lasts, one oscillation per axis listed; the
/// axes left out are at rest.
typedef Carrier = Map<RobotAxis, (double amplitude, int periodMs)>;

/// The seven feelings of the avatar's faces (DEC-042), each with the motions its
/// sentences start with and what goes on after. Neutral is [beats].
const feelings = {
  'joy': [
    Motion('joy.bounce', [
      Go(Pose(tilt: -8), 300),
      Go(Pose(tilt: -3), 160),
      Go(Pose(tilt: -8), 160),
      Go(Pose(tilt: -3), 160),
      Go(Pose(tilt: -5), 200),
    ]),
    Motion('joy.tilt', mirror: true, [
      Go(Pose(tilt: -6, roll: 8), 600),
      Go(Pose(pan: 4, tilt: -6, roll: 8), 300),
      Go(Pose(pan: 4, tilt: -6, roll: 5), 400),
    ]),
    Motion('joy.nods', [
      Go(Pose(tilt: -6), 250),
      Go(Pose(tilt: 2), 180),
      Go(Pose(tilt: -5), 180),
      Go(Pose(tilt: 2), 180),
      Go(Pose(tilt: -3), 220),
    ]),
    Motion('joy.look', mirror: true, [
      Go(Pose(pan: 10, tilt: -7, roll: -3), 380),
      Go(Pose(pan: -8, tilt: -6, roll: 3), 520),
      Go(Pose(tilt: -5), 420),
    ]),
    Motion('joy.wiggle', [
      Go(Pose(tilt: -6), 280),
      Go(Pose(pan: 4, tilt: -6), 200),
      Go(Pose(pan: -4, tilt: -6), 200),
      Go(Pose(pan: 4, tilt: -6), 200),
      Go(Pose(tilt: -5), 220),
    ]),
  ],
  'sadness': [
    Motion('sad.droop', mirror: true, [Go(Pose(tilt: 8, roll: 4), 1200)]),
    Motion('sad.sigh', mirror: true, [
      Go(Pose(tilt: -3), 600),
      Go(Pose(pan: 3, tilt: 8), 1500),
    ]),
    Motion('sad.shake', [
      Go(Pose(tilt: 6), 800),
      Go(Pose(pan: 4, tilt: 6), 700),
      Go(Pose(pan: -4, tilt: 6), 900),
      Go(Pose(tilt: 6), 800),
    ]),
    Motion('sad.away', mirror: true, [
      Go(Pose(pan: 12, tilt: 6, roll: -3), 1300),
      Wait(700),
      Go(Pose(pan: 6, tilt: 5), 900),
    ]),
  ],
  // Mostly in jest (DEC-093): a huff, never a threat.
  'anger': [
    Motion('anger.hmph', mirror: true, [
      Go(Pose(pan: 14, tilt: -7, roll: -5), 350),
      Wait(600),
      Go(Pose(pan: 5, tilt: -2, roll: -2), 600),
    ]),
    Motion('anger.shake', [
      Go(Pose(tilt: 3), 200),
      Go(Pose(pan: 5, tilt: 3), 180),
      Go(Pose(pan: -5, tilt: 3), 180),
      Go(Pose(pan: 5, tilt: 3), 180),
      Go(Pose(pan: -5, tilt: 3), 180),
      Go(Pose(tilt: 1), 220),
    ]),
    Motion('anger.stomp', [
      Go(Pose(tilt: 6), 160),
      Go(Pose(), 240),
      Go(Pose(tilt: 6), 160),
      Go(Pose(), 260),
    ]),
    Motion('anger.glare', still: true, [
      Go(Pose(tilt: 5), 700),
      Wait(900),
      Go(Pose(tilt: 2), 600),
    ]),
  ],
  'fear': [
    Motion('fear.flinch', mirror: true, [
      Go(Pose(pan: 5, tilt: -9), 250),
      Swing(RobotAxis.pan, 0.8, 320),
      Wait(1200),
      Swing(RobotAxis.pan, 0, 320),
      Go(Pose(pan: 3, tilt: -5), 500),
    ]),
    Motion('fear.hide', mirror: true, [
      Go(Pose(pan: 10, tilt: 6, roll: 4), 900),
      Wait(400),
      Go(Pose(pan: 4, tilt: 3, roll: 2), 600),
    ]),
    Motion('fear.glance', [
      Go(Pose(tilt: -4), 200),
      Go(Pose(pan: -7, tilt: -4), 260),
      Go(Pose(pan: 7, tilt: -4), 360),
      Go(Pose(tilt: -2), 300),
    ]),
    // A shiver on two axes at once, at periods a little apart: a single small
    // one did not read as fear (the second round).
    Motion('fear.freeze', [
      Go(Pose(tilt: -5), 300),
      Swing(RobotAxis.pan, 1.8, 400),
      Swing(RobotAxis.tilt, 1.2, 420),
      Wait(1500),
      Swing(RobotAxis.pan, 0, 400),
      Swing(RobotAxis.tilt, 0, 420),
    ]),
  ],
  'surprise': [
    Motion('surprise.jolt', [
      Go(Pose(tilt: -12), 220),
      Wait(350),
      Go(Pose(tilt: -5), 700),
    ]),
    Motion('surprise.double', mirror: true, [
      Go(Pose(pan: 9), 260),
      Go(Pose(tilt: -10), 220),
      Wait(300),
      Go(Pose(tilt: -5), 600),
    ]),
    Motion('surprise.wow', mirror: true, [
      Go(Pose(tilt: -9, roll: 8), 650),
      Wait(400),
      Go(Pose(tilt: -5, roll: 4), 700),
    ]),
    Motion('surprise.lean', [
      Go(Pose(tilt: -10), 240),
      Wait(250),
      Go(Pose(tilt: 3), 600),
      Go(Pose(), 500),
    ]),
  ],
  'thinking': [
    Motion('think.up', mirror: true, [
      Go(Pose(pan: 9, tilt: -9, roll: 4), 900),
    ]),
    Motion('think.tilt', mirror: true, [
      Go(Pose(tilt: -4, roll: 7), 800),
      Go(Pose(tilt: -1, roll: 7), 500),
      Go(Pose(tilt: -4, roll: 7), 500),
    ]),
    Motion('think.drift', mirror: true, [
      Go(Pose(tilt: -7), 700),
      Go(Pose(pan: 7, tilt: -7), 1300),
      Go(Pose(pan: -4, tilt: -7), 1300),
    ]),
    Motion('think.weigh', mirror: true, [
      Go(Pose(tilt: 3), 500),
      Go(Pose(), 500),
      Go(Pose(tilt: 2), 500),
      Go(Pose(pan: 5, tilt: -5), 800),
    ]),
  ],
};

/// What goes on after each feeling's accent, until the next sentence.
const carriers = <String, Carrier>{
  'joy': {RobotAxis.tilt: (1.5, 1500), RobotAxis.pan: (2, 2400)},
  'sadness': {RobotAxis.pan: (1, 4200)},
  'anger': {RobotAxis.tilt: (1, 1600)},
  'fear': {},
  'surprise': {RobotAxis.tilt: (1, 1800)},
  'thinking': {RobotAxis.pan: (1, 3600), RobotAxis.roll: (1.5, 4200)},
};

/// How a sentence without a feeling starts: chat's neutral sentences, and every
/// sentence of the paths that answer from the sources (DEC-097). One in seven or so
/// has none, so not every sentence moves the same way.
const beats = [
  // A nod as large as tilt allows for a beat: 4° played 7° looked too small (the
  // second round).
  Motion('n.nod', [Go(Pose(tilt: 7), 240), Go(Pose(), 320)]),
  Motion('n.double', [
    Go(Pose(tilt: 5), 200),
    Go(Pose(), 220),
    Go(Pose(tilt: 4), 200),
    Go(Pose(), 260),
  ]),
  Motion('n.shift', mirror: true, [Go(Pose(pan: 5, roll: 2), 500)]),
  Motion('n.lean', [Go(Pose(tilt: 4), 450)]),
  Motion('n.lift', [Go(Pose(tilt: -5), 450), Go(Pose(tilt: -2), 500)]),
  Motion('n.tilt', mirror: true, [Go(Pose(tilt: 1, roll: 4), 550)]),
  Motion('n.none', []),
];

/// The odds a sentence without a feeling starts with no beat.
const noBeat = 0.15;

/// The speaking carrier's ranges; each sentence takes its own values, and the two
/// periods are never in a whole ratio, so the sum does not repeat.
const speakingTilt = ((1.2, 2.0), (1300, 1900));
const speakingPan = ((1.0, 2.5), (2900, 4300));

/// Knowledge answers that introduce the centre, the company or what they make:
/// showing it, calm and a little proud (DEC-103).
const introductions = [
  Motion('intro.present', mirror: true, [
    Go(Pose(pan: -6, tilt: -4), 400),
    Go(Pose(pan: 6, tilt: -4), 1000),
    Go(Pose(tilt: 2), 250),
    Go(Pose(), 250),
  ]),
  Motion('intro.proud', [
    Go(Pose(tilt: -6), 500),
    Wait(400),
    Go(Pose(tilt: 1), 250),
    Go(Pose(tilt: -2), 300),
  ]),
  Motion('intro.point', mirror: true, [
    Go(Pose(pan: 10, tilt: -3), 400),
    Wait(300),
    Go(Pose(tilt: 2), 300),
    Go(Pose(), 250),
  ]),
  Motion('intro.bow', [Go(Pose(tilt: 6), 600), Go(Pose(), 600)]),
];

/// A question: the head tilts as the sentence ends, as if waiting for the answer.
const asking = Motion('cue.ask', mirror: true, [
  Go(Pose(tilt: -3, roll: 6), 600),
]);

/// An exclamation: one firmer nod.
const exclaiming = Motion('cue.exclaim', [
  Go(Pose(tilt: 5), 180),
  Go(Pose(), 260),
]);

/// A list's items: each moves where the head speaks from one place along.
const itemPlaces = [-6.0, 0.0, 6.0];

Motion item(double pan) => Motion('cue.item', [
  Go(Pose(pan: pan, tilt: 3), 300),
  Go(Pose(pan: pan), 300),
]);

/// The states of Avatar mode (spec 22).
const waking = Motion('state.wake', [
  Go(Pose(), 1000),
  Go(Pose(tilt: -8), 400),
  Go(Pose(tilt: 2), 300),
  Go(Pose(), 350),
]);

const resting = Motion('state.idle', [Go(Pose(), 700)]);
const breathing = <RobotAxis, (double, int)>{
  RobotAxis.tilt: (0.8, 4200),
  RobotAxis.pan: (0.8, 6100),
};

/// A glance aside while idle, every [glanceEvery].
const glancing = Motion('state.glance', mirror: true, [
  Go(Pose(pan: 3), 900),
  Wait(1200),
  Go(Pose(), 900),
]);
const glanceEvery = (9000, 16000);

/// Leaning in while the user speaks. No head tilt: one to a side, seen once a
/// question, read as the same lean every time (2026-10-16).
const leaningIn = Motion('state.listen', [Go(Pose(tilt: 3), 700)]);
const listeningCarrier = <RobotAxis, (double, int)>{RobotAxis.tilt: (1, 2600)};

/// While the reply is awaited the head ponders, looking away: every [ponderEvery]
/// it takes another of these or of the thinking feeling's motions, never one of the
/// last two, turned to each side in turn.
const pondering = [
  Motion('state.ponder.up', mirror: true, [
    Go(Pose(pan: 9, tilt: -9, roll: 4), 900),
  ]),
  Motion('state.ponder.tilt', mirror: true, [Go(Pose(tilt: -5, roll: 7), 800)]),
  Motion('state.ponder.drift', mirror: true, [
    Go(Pose(pan: -5, tilt: -7), 1200),
  ]),
];
const ponderingCarrier = <RobotAxis, (double, int)>{RobotAxis.pan: (1.2, 3400)};
const ponderEvery = (2200, 3000);

/// The reply is over: a small nod hands the turn back.
const handingOver = Motion('state.handover', [
  Go(Pose(tilt: 3), 250),
  Go(Pose(), 350),
]);

/// Something went wrong.
const erring = Motion('state.error', [
  Go(Pose(tilt: 3, roll: -4), 600),
  Wait(1200),
]);

/// Every motion, for Robot lab and the tests.
List<Motion> get everyMotion => [
  for (final variants in feelings.values) ...variants,
  ...beats,
  ...introductions,
  asking,
  exclaiming,
  for (final place in itemPlaces) item(place),
  waking,
  resting,
  glancing,
  leaningIn,
  ...pondering,
  handingOver,
  erring,
];

/// How hard the head may move (`firmware/README.md`, "Rig measurements"): the
/// worst case of an axis is its fastest move, its oscillation and its gaze added
/// up, and stays under 80% of the measured ceiling. Roll is the one that matters:
/// the phone slips on its magnet, for good.
abstract final class Budget {
  /// The 80% ceilings, °/s²: pan never wobbled up to 3300, tilt wobbles past 2700,
  /// roll slips past 800.
  static const ceiling = {
    RobotAxis.pan: 2640.0,
    RobotAxis.tilt: 2160.0,
    RobotAxis.roll: 640.0,
  };

  /// What the firmware's gaze may add (its `L` acceleration caps).
  static const gaze = {
    RobotAxis.pan: 600.0,
    RobotAxis.tilt: 400.0,
    RobotAxis.roll: 0.0,
  };

  /// The fastest roll, °/s: a third of the 180 °/s at which `osc roll 20 700` ran
  /// a minute without a slip. Acceleration is what slips the phone; this keeps
  /// rolls from snapping (raised from 25 in the second round, for larger rolls).
  static const rollSpeed = 60.0;

  /// The shortest roll oscillation, ms: a faster one ramps too hard.
  static const rollPeriod = 1400;

  /// The firmware's amplitude ramp, °/s (`AMP_SLEW_DEG_PER_S`).
  static const slew = 30.0;

  /// A move's peak acceleration and speed, as `reportMove()` prints them.
  static double moveAcceleration(double degrees, int ms) {
    final t = ms / 1000;
    return degrees * pi * pi / (2 * t * t);
  }

  static double moveSpeed(double degrees, int ms) =>
      degrees * pi / (2 * ms / 1000);

  /// An oscillation's peak acceleration, as `reportOsc()` prints it, plus what its
  /// amplitude ramp adds while it starts or stops.
  static double swingAcceleration(double amplitude, int periodMs) {
    if (amplitude == 0) return 0;
    final w = 2 * pi / (periodMs / 1000);
    return amplitude * w * w + 2 * slew * w;
  }

  /// The shortest a move from [from] to [to] may take, given the oscillations
  /// going on: [ms], or longer when that would be too hard.
  static int safeMs(
    Pose from,
    Pose to,
    int ms,
    Map<RobotAxis, double> swinging,
  ) {
    var shortest = ms.toDouble();
    for (final axis in RobotAxis.values) {
      final degrees = (to[axis] - from[axis]).abs();
      if (degrees == 0) continue;
      final room = ceiling[axis]! - gaze[axis]! - (swinging[axis] ?? 0);
      if (room <= 0) return max(ms, 2000);
      shortest = max(shortest, 1000 * sqrt(degrees * pi * pi / (2 * room)));
      if (axis == RobotAxis.roll) {
        shortest = max(shortest, 1000 * degrees * pi / (2 * rollSpeed));
      }
    }
    return shortest.ceil();
  }
}

/// What a motion asks of each axis, played from [from] with [carrier] going on.
class Peaks {
  Peaks(Motion motion, {Pose from = Pose.neutral, Carrier carrier = const {}}) {
    var at = from;
    for (final (axis, (amplitude, period)) in carrier.entries.map(
      (e) => (e.key, e.value),
    )) {
      _swing(axis, amplitude, period);
    }
    for (final step in motion.steps) {
      switch (step) {
        case Go(:final pose, :final ms):
          for (final axis in RobotAxis.values) {
            final degrees = (pose[axis] - at[axis]).abs();
            move[axis] = max(move[axis]!, Budget.moveAcceleration(degrees, ms));
            speed[axis] = max(speed[axis]!, Budget.moveSpeed(degrees, ms));
          }
          at = pose;
        case Swing(:final axis, :final amplitude, :final periodMs):
          _swing(axis, amplitude, periodMs);
        case Wait():
      }
    }
  }

  final move = {for (final axis in RobotAxis.values) axis: 0.0};
  final swing = {for (final axis in RobotAxis.values) axis: 0.0};
  final speed = {for (final axis in RobotAxis.values) axis: 0.0};
  int shortestRollPeriod = 1 << 30;

  void _swing(RobotAxis axis, double amplitude, int periodMs) {
    swing[axis] = max(
      swing[axis]!,
      Budget.swingAcceleration(amplitude, periodMs),
    );
    if (axis == RobotAxis.roll && amplitude > 0) {
      shortestRollPeriod = min(shortestRollPeriod, periodMs);
    }
  }

  /// The worst case of an axis: its hardest move, oscillation and gaze at once.
  double worst(RobotAxis axis) =>
      move[axis]! + swing[axis]! + Budget.gaze[axis]!;

  /// What exceeds the budget, in words; empty when the motion is within it.
  List<String> get over => [
    for (final axis in RobotAxis.values)
      if (worst(axis) > Budget.ceiling[axis]!)
        '${axis.name} ${worst(axis).round()} °/s² > ${Budget.ceiling[axis]!.round()}',
    if (speed[RobotAxis.roll]! > Budget.rollSpeed)
      'roll ${speed[RobotAxis.roll]!.round()} °/s > ${Budget.rollSpeed.round()}',
    if (shortestRollPeriod < Budget.rollPeriod)
      'roll period $shortestRollPeriod ms < ${Budget.rollPeriod}',
  ];
}
