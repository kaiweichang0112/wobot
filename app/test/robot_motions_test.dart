import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/robot/motions.dart';
import 'package:wobot/robot/protocol.dart';

/// The carrier a motion is played with: its feeling's, or the speaking carrier at
/// its liveliest for the rest.
Carrier carrierOf(Motion motion) {
  for (final MapEntry(key: feeling, value: variants) in feelings.entries) {
    if (variants.contains(motion)) return carriers[feeling]!;
  }
  if (motion.id.startsWith('state.listen')) return listeningCarrier;
  if (motion.id.startsWith('state.ponder')) return ponderingCarrier;
  if (motion.id.startsWith('state.')) return breathing;
  return {
    RobotAxis.tilt: (speakingTilt.$1.$2, speakingTilt.$2.$1),
    RobotAxis.pan: (speakingPan.$1.$2, speakingPan.$2.$1),
  };
}

void main() {
  test('every feeling has at least four motions, and neutral six beats', () {
    expect(feelings.keys.toSet(), {
      'joy',
      'sadness',
      'anger',
      'fear',
      'surprise',
      'thinking',
    });
    for (final MapEntry(key: feeling, value: variants) in feelings.entries) {
      expect(variants.length, greaterThanOrEqualTo(4), reason: feeling);
      expect(carriers, contains(feeling));
    }
    expect(
      beats.where((b) => b.steps.isNotEmpty).length,
      greaterThanOrEqualTo(6),
    );
    expect(introductions.length, greaterThanOrEqualTo(3));
    final ids = everyMotion.map((m) => m.id).where((id) => id != 'cue.item');
    expect(ids.toSet().length, ids.length, reason: 'ids are unique');
  });

  test('every motion, either way round, stays within the budget', () {
    for (final motion in everyMotion) {
      for (final side in [1, -1]) {
        final played = motion.scaled(side: side);
        final peaks = Peaks(played, carrier: carrierOf(motion));
        expect(peaks.over, isEmpty, reason: '${motion.id} side $side');
      }
    }
  });

  test('played at the gain, every motion stays within the budget', () {
    for (final motion in everyMotion) {
      for (final side in [1, -1]) {
        final carrier = carrierOf(motion);
        final played = fitted(motion.scaled(side: side), carrier: carrier);
        final peaks = Peaks(played, carrier: amplifyCarrier(carrier));
        expect(peaks.over, isEmpty, reason: '${motion.id} side $side');
        for (final step in played.steps.whereType<Go>()) {
          // Tilt has only 15° of nose-down travel from neutral.
          expect(
            step.pose.tilt,
            inInclusiveRange(-25, noseDown),
            reason: motion.id,
          );
          expect(step.pose.pan.abs(), lessThanOrEqualTo(30), reason: motion.id);
          expect(
            step.pose.roll.abs(),
            lessThanOrEqualTo(30), // well inside roll's ±45° soft limits
            reason: motion.id,
          );
        }
      }
    }
  });

  test('the gain is applied, and slows only what it must', () {
    final jolt = feelings['surprise']!.firstWhere(
      (m) => m.id == 'surprise.jolt',
    );
    final played = fitted(jolt, carrier: carriers['surprise']!);
    final first = played.steps.first as Go;
    expect(first.pose.tilt, closeTo(-12 * gain, 1e-9));
    expect(first.ms, greaterThan(220)); // 21.6° in 220 ms is too hard
    final droop = fitted(feelings['sadness']!.first);
    expect((droop.steps.first as Go).ms, 1200); // gentle enough as written
  });

  test('the carriers stay within the budget on their own', () {
    for (final carrier in [
      ...carriers.values,
      breathing,
      listeningCarrier,
      ponderingCarrier,
    ]) {
      expect(
        Peaks(
          const Motion('carrier', []),
          carrier: amplifyCarrier(carrier),
        ).over,
        isEmpty,
        reason: '$carrier',
      );
    }
  });

  test('a scaled motion is only gentler', () {
    final motion = feelings['joy']!.first;
    final played = motion.scaled(amplitude: 0.85, time: 1.15);
    for (final axis in RobotAxis.values) {
      expect(
        Peaks(played).worst(axis),
        lessThanOrEqualTo(Peaks(motion).worst(axis)),
      );
    }
    expect(played.ms, greaterThan(motion.ms));
  });

  test('poses leave room for the gaze and stay inside the soft limits', () {
    for (final motion in everyMotion) {
      for (final step in motion.steps.whereType<Go>()) {
        final pose = step.pose;
        expect(pose.pan.abs(), lessThanOrEqualTo(15), reason: motion.id);
        expect(pose.tilt, inInclusiveRange(-12, 8), reason: motion.id);
        expect(pose.roll.abs(), lessThanOrEqualTo(8), reason: motion.id);
        expect(
          MoveTo(pose, step.ms).bytes.length,
          lessThanOrEqualTo(maxCommandBytes),
        );
      }
    }
  });

  test('a move too hard for the budget is slowed down', () {
    const from = Pose(tilt: -12);
    const to = Pose(tilt: 8);
    final ms = Budget.safeMs(from, to, 100, const {});
    expect(ms, greaterThan(100));
    final room = Budget.ceiling[RobotAxis.tilt]! - Budget.gaze[RobotAxis.tilt]!;
    expect(Budget.moveAcceleration(20, ms), lessThanOrEqualTo(room + 1));
    // A gentle one keeps its time.
    expect(
      Budget.safeMs(Pose.neutral, const Pose(tilt: 2), 600, const {}),
      600,
    );
    // Roll is held to its speed too.
    expect(
      Budget.moveSpeed(
        8,
        Budget.safeMs(Pose.neutral, const Pose(roll: 8), 100, const {}),
      ),
      lessThanOrEqualTo(Budget.rollSpeed + 0.1),
    );
  });
}
