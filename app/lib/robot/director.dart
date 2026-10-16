import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';

import 'cues.dart';
import 'motions.dart';
import 'pose_model.dart';
import 'protocol.dart';

/// A sentence as it starts to be heard (DEC-095): the robot moves with it then,
/// the moment the avatar shows its face.
class Spoken {
  const Spoken({
    required this.index,
    required this.text,
    required this.emotion,
    required this.length,
    this.route,
    this.reading = false,
  });

  /// Its place in the reply, from 0.
  final int index;
  final String text;

  /// One of the avatar's seven faces; neutral on every path but chat (DEC-097).
  final String emotion;

  /// How long it is heard.
  final Duration length;

  /// The path the reply took (`chat`, `knowledge`, `list`, `recommend`), when the
  /// server said; null from one that does not.
  final String? route;

  /// Whether it is read on from a list (DEC-096).
  final bool reading;

  /// How long an MP3 sentence lasts: the speech is 64 kb/s (`mp3_44100_64`,
  /// `speech/tts.py`), 8 bytes a millisecond.
  static Duration lengthOf(int mp3Bytes) =>
      Duration(milliseconds: mp3Bytes ~/ 8);
}

enum _Cue { idle, listening, waiting, speaking }

/// Plays the robot's motions as Avatar mode goes (spec 26, DEC-103): leaning in
/// while the user speaks, pondering while the reply is awaited, moving with each
/// sentence, and breathing in between.
///
/// Every cue starts a new generation, as Avatar mode's own do: a motion of an
/// earlier one stops at its next step. A move under way is never cut into, since
/// the next would start from rest where the head happens to be: a new motion waits
/// for it to end. Every move is slowed, if need be, to stay within the [Budget].
class Director {
  Director(this._send, {Random? random, DateTime Function()? now})
    : _random = random ?? Random(),
      _now = now ?? DateTime.now {
    model = CentreModel(_now);
  }

  final void Function(RobotCommand command) _send;
  final Random _random;
  final DateTime Function() _now;
  late final CentreModel model;

  /// True while the head turns away on purpose, in an accent or pondering: face
  /// tracking holds its aim meanwhile, so it does not undo the motion.
  final holding = ValueNotifier(false);

  /// The motion the last sentence started with, for tests and Robot lab.
  String? accent;

  /// Each axis's oscillation as last sent.
  final _swings = <RobotAxis, (double, int)>{};
  int _generation = 0;
  _Cue _cue = _Cue.idle;
  Timer? _later;

  // What keeps it from looking the same: the motions used last, the sides turned
  // to last, how many sentences in a row had the same feeling.
  final _recent = <String, List<String>>{};
  final _sides = <int>[];
  String? _feeling;
  int _run = 0;
  int _introduced = -10;
  int _place = 0;
  String? _pondered;

  /// The robot has just been connected and attached: it greets, then goes on
  /// where Avatar mode is.
  void connected() {
    model.reset();
    _swings.clear();
    final cue = _cue;
    final generation = _begin();
    unawaited(() async {
      await _play(generation, waking.steps);
      if (generation != _generation) return;
      switch (cue) {
        case _Cue.listening:
          listening();
        case _Cue.waiting:
          waiting();
        case _Cue.idle || _Cue.speaking:
          idle();
      }
    }());
  }

  /// The user is speaking: lean in, to one side and then the other.
  void listening() {
    _cue = _Cue.listening;
    final generation = _begin();
    _resetRun();
    _carry(listeningCarrier);
    unawaited(_play(generation, leaningIn.scaled(side: _side()).steps));
  }

  /// The reply is awaited: the head looks away, pondering, a new way every few
  /// seconds, until the first sentence.
  void waiting() {
    _cue = _Cue.waiting;
    final generation = _begin();
    _resetRun();
    holding.value = true;
    _carry(ponderingCarrier);
    unawaited(_ponder(generation));
  }

  /// A sentence starts to be heard.
  void speaking(Spoken line) {
    _cue = _Cue.speaking;
    final generation = _begin();
    final (motion, carrier) = _choose(line);
    accent = motion.id;
    unawaited(_speak(generation, line, motion, carrier));
  }

  /// The reply is over: a nod hands the turn back.
  void rested() {
    _cue = _Cue.idle;
    final generation = _begin();
    _resetRun();
    unawaited(() async {
      await _play(generation, handingOver.steps);
      if (generation == _generation) _breathe(generation);
    }());
  }

  /// Nothing heard or said, after a question that failed when [error].
  void idle({bool error = false}) {
    _cue = _Cue.idle;
    final generation = _begin();
    _resetRun();
    unawaited(() async {
      await _play(generation, (error ? erring : resting).steps);
      if (generation == _generation) _breathe(generation);
    }());
  }

  /// One motion as it is, for Robot lab; then idle.
  void play(Motion motion, {int side = 1}) {
    final generation = _begin();
    unawaited(() async {
      if (motion.still) _carry(const {});
      await _play(generation, motion.scaled(side: side).steps);
      if (generation == _generation) idle();
    }());
  }

  /// Stops everything: the robot was let go.
  void stop() {
    _begin();
    _swings.clear();
    model.reset();
  }

  void dispose() {
    stop();
    holding.dispose();
  }

  int _begin() {
    _later?.cancel();
    _later = null;
    holding.value = false;
    return ++_generation;
  }

  void _resetRun() {
    _feeling = null;
    _run = 0;
  }

  /// Which motion a sentence starts with, and what goes on after it.
  (Motion, Carrier) _choose(Spoken line) {
    if (line.index == 0) {
      _introduced = -10;
      _place = 0;
    }
    final length = line.length.inMilliseconds;
    final feeling = feelings.containsKey(line.emotion) ? line.emotion : null;
    if (feeling == _feeling) {
      _run++;
    } else {
      _feeling = feeling;
      _run = 1;
    }
    final carrier = feeling == null ? _speakingCarrier() : carriers[feeling]!;
    // A few characters are over before a motion could be seen, as with the face.
    if (length < 600 || line.text.runes.length < 4) return (_small(), carrier);
    bool fits(Motion m) => fitted(m).ms * 1.15 <= 0.8 * length;

    if (feeling != null) {
      if (_run >= 3) return (_small(), carrier);
      final motion = _pick(feeling, feelings[feeling]!, fits);
      if (motion == null) return (_small(), carrier);
      return (_vary(motion, _run == 2 ? 0.8 : 1), carrier);
    }
    if (line.reading || (line.route == 'list' && line.index >= 1)) {
      return (item(itemPlaces[_place++ % itemPlaces.length]), carrier);
    }
    if (line.route == 'knowledge' &&
        (line.index == 0 || introduces(line.text)) &&
        line.index - _introduced >= 2) {
      final motion = _pick('intro', introductions, fits);
      if (motion != null) {
        _introduced = line.index;
        return (_vary(motion, 1), carrier);
      }
    }
    if (exclaims(line.text) && fits(exclaiming)) return (exclaiming, carrier);
    return (_beat(fits), carrier);
  }

  Future<void> _speak(
    int generation,
    Spoken line,
    Motion accent,
    Carrier carrier,
  ) async {
    final start = _now();
    final length = line.length.inMilliseconds;
    holding.value = accent.steps.isNotEmpty;
    if (accent.still) _carry(const {});
    await _play(generation, accent.steps);
    if (generation != _generation) return;
    // A moment more for the head to settle before tracking aims it again.
    await _until(generation, start, _elapsed(start) + 200);
    if (generation != _generation) return;
    holding.value = false;
    _carry(carrier);

    // A long sentence gets a second beat halfway, so the head never freezes.
    if (length > 4000 && line.emotion == 'neutral') {
      await _until(generation, start, length ~/ 2);
      if (generation != _generation) return;
      await _play(generation, _beat((_) => true).steps);
    }
    // A question tilts the head as it ends, as if waiting for the answer.
    if (asks(line.text) && length > 1500) {
      await _until(generation, start, length - 900);
      if (generation != _generation) return;
      await _play(generation, asking.scaled(side: _side()).steps);
    }
    // No next sentence soon after this one ends: calm down to breathing.
    await _until(generation, start, length + 800);
    if (generation == _generation) _carry(breathing);
  }

  Future<void> _ponder(int generation) async {
    while (generation == _generation) {
      final choices = pondering.where((m) => m.id != _pondered).toList();
      final motion = choices[_random.nextInt(choices.length)];
      _pondered = motion.id;
      await _play(generation, _vary(motion, 1).steps);
      final (low, high) = ponderEvery;
      await _sleep(low + _random.nextInt(high - low));
    }
  }

  /// Rests and breathes, glancing aside now and then.
  void _breathe(int generation) {
    _carry(breathing);
    final (low, high) = glanceEvery;
    _later = Timer(
      Duration(milliseconds: low + _random.nextInt(high - low)),
      () {
        if (generation != _generation) return;
        unawaited(() async {
          await _play(generation, glancing.scaled(side: _side()).steps);
          if (generation == _generation) _breathe(generation);
        }());
      },
    );
  }

  /// Plays steps in order; stops at the first step after the generation moved on.
  Future<void> _play(int generation, List<Step> steps) async {
    for (final step in steps) {
      if (generation != _generation) return;
      switch (step) {
        case Go(:final pose, :final ms):
          if (model.moving) {
            // Never cut into a move: the next would start from rest mid-way.
            await _sleep(model.endsAt.difference(_now()).inMilliseconds + 20);
            if (generation != _generation) return;
          }
          final to = amplify(pose);
          final safe = Budget.safeMs(model.at(), to, ms, _load());
          _send(MoveTo(to, safe));
          model.moved(to, safe);
          // A little after it ends: a write may arrive a few ms late.
          await _sleep(safe + 15);
        case Swing(:final axis, :final amplitude, :final periodMs):
          _swing(axis, amplitude * gainOf(axis), periodMs);
        case Wait(:final ms):
          await _sleep(ms);
      }
    }
  }

  /// Sets the oscillations to [carrier]: the axes it leaves out come to rest.
  void _carry(Carrier carrier) {
    for (final axis in RobotAxis.values) {
      final wanted = carrier[axis];
      if (wanted != null) {
        _swing(axis, wanted.$1 * gainOf(axis), wanted.$2);
      } else if (_swings[axis] case (
        final amplitude,
        final period,
      ) when amplitude > 0) {
        _swing(axis, 0, period);
      }
    }
  }

  void _swing(RobotAxis axis, double amplitude, int periodMs) {
    final now = _swings[axis];
    if (now != null && now.$1 == amplitude && now.$2 == periodMs) return;
    if (now == null && amplitude == 0) return;
    _swings[axis] = (amplitude, periodMs);
    _send(Sway(axis, amplitude, periodMs, 0));
  }

  /// What the oscillations going on take of each axis's budget.
  Map<RobotAxis, double> _load() => {
    for (final MapEntry(key: axis, value: (amplitude, period))
        in _swings.entries)
      axis: Budget.swingAcceleration(amplitude, period),
  };

  /// One of [variants], not one of the last used, and one that fits; null when
  /// none fits.
  Motion? _pick(
    String family,
    List<Motion> variants,
    bool Function(Motion) fits,
  ) {
    final fitting = variants.where(fits).toList();
    if (fitting.isEmpty) return null;
    final recent = _recent.putIfAbsent(family, () => []);
    var choices = fitting.where((m) => !recent.contains(m.id)).toList();
    if (choices.isEmpty) {
      choices = fitting.where((m) => m.id != recent.lastOrNull).toList();
    }
    if (choices.isEmpty) choices = fitting;
    final motion = choices[_random.nextInt(choices.length)];
    recent.add(motion.id);
    // The last two are kept from coming back, while there are others to take.
    while (recent.length > min(2, variants.length - 1)) {
      recent.removeAt(0);
    }
    return motion;
  }

  /// A beat for a sentence without a feeling; sometimes none.
  Motion _beat(bool Function(Motion) fits) {
    if (_random.nextDouble() < noBeat) return beats.last;
    final moving = beats.where((m) => m.steps.isNotEmpty).toList();
    return _vary(_pick('beat', moving, fits) ?? _small(), 1);
  }

  /// A small nod, for a short sentence or a feeling said once too often.
  Motion _small() => beats.first.scaled(amplitude: 0.8);

  /// The motion as played this time: a little smaller and slower at random, and
  /// turned to a side.
  Motion _vary(Motion motion, double amplitude) => motion.scaled(
    amplitude: amplitude * (0.9 + 0.1 * _random.nextDouble()),
    time: 1 + 0.15 * _random.nextDouble(),
    side: motion.mirror ? _side() : 1,
  );

  /// Left or right at random, but never the same side three times running.
  int _side() {
    var side = _random.nextBool() ? 1 : -1;
    if (_sides.length >= 2 && _sides.every((s) => s == _sides.last)) {
      side = -_sides.last;
    }
    _sides.add(side);
    if (_sides.length > 2) _sides.removeAt(0);
    return side;
  }

  /// The speaking carrier, with values of its own for each sentence.
  Carrier _speakingCarrier() {
    double within((double, double) range) =>
        range.$1 + (range.$2 - range.$1) * _random.nextDouble();
    int between((int, int) range) =>
        range.$1 + _random.nextInt(range.$2 - range.$1 + 1);
    final tiltPeriod = between(speakingTilt.$2);
    var panPeriod = between(speakingPan.$2);
    final ratio = panPeriod / tiltPeriod;
    // Two periods in a whole ratio would trace the same figure over and over.
    if ((ratio - ratio.round()).abs() < 0.15) panPeriod += 250;
    return {
      RobotAxis.tilt: ((within(speakingTilt.$1) * 10).round() / 10, tiltPeriod),
      RobotAxis.pan: ((within(speakingPan.$1) * 10).round() / 10, panPeriod),
    };
  }

  int _elapsed(DateTime start) => _now().difference(start).inMilliseconds;

  /// Waits until [ms] after [start].
  Future<void> _until(int generation, DateTime start, int ms) async {
    final wait = ms - _elapsed(start);
    if (wait > 0 && generation == _generation) await _sleep(wait);
  }

  Future<void> _sleep(int ms) =>
      Future<void>.delayed(Duration(milliseconds: max(0, ms)));
}
