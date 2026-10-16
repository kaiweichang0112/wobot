import 'dart:math';

import 'package:fake_async/fake_async.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/robot/cues.dart';
import 'package:wobot/robot/director.dart';
import 'package:wobot/robot/motions.dart';
import 'package:wobot/robot/pose_model.dart';
import 'package:wobot/robot/protocol.dart';

/// A command as sent, and when.
typedef Sent = ({int at, String line});

Spoken said(
  String emotion, {
  int index = 1,
  int ms = 4000,
  String text = '這是一句夠長的測試句子。',
  String? route,
  bool reading = false,
}) => Spoken(
  index: index,
  text: text,
  emotion: emotion,
  length: Duration(milliseconds: ms),
  route: route,
  reading: reading,
);

void main() {
  late List<Sent> sent;
  late Director director;

  /// Runs [body] on a fake clock, with a director whose commands are logged.
  void run(void Function(FakeAsync async) body, {int seed = 7}) {
    fakeAsync((async) {
      final start = DateTime(2026, 10, 16);
      sent = [];
      director = Director(
        (command) =>
            sent.add((at: async.elapsed.inMilliseconds, line: command.line)),
        random: Random(seed),
        now: () => start.add(async.elapsed),
      );
      body(async);
      director.dispose();
      async.flushTimers();
    });
  }

  /// Says [lines], each for its own length.
  List<String?> speak(FakeAsync async, List<Spoken> lines) {
    final accents = <String?>[];
    for (final line in lines) {
      director.speaking(line);
      accents.add(director.accent);
      async.elapse(line.length);
    }
    return accents;
  }

  Iterable<Sent> moves() => sent.where((s) => s.line.startsWith('M '));

  test('a move never starts while the one before is under way', () {
    run((async) {
      director.speaking(said('joy'));
      async.elapse(const Duration(milliseconds: 90));
      director.speaking(said('surprise')); // cuts in mid-move
      async.elapse(const Duration(milliseconds: 40));
      director.listening();
      async.elapse(const Duration(milliseconds: 500));
      director.waiting();
      async.elapse(const Duration(seconds: 6));
      director.speaking(said('anger', index: 0));
      async.elapse(const Duration(seconds: 3));
      director.rested();
      async.elapse(const Duration(seconds: 2));
    });
    final list = moves().toList();
    expect(list.length, greaterThan(5));
    for (var i = 1; i < list.length; i++) {
      final before = list[i - 1];
      final takes = int.parse(before.line.split(' ').last);
      expect(
        list[i].at,
        greaterThanOrEqualTo(before.at + takes),
        reason: '${before.line} at ${before.at}, then ${list[i].line}',
      );
    }
  });

  test('a feeling never plays the same motion twice running', () {
    late List<String?> accents;
    run((async) {
      final lines = [
        for (var i = 0; i < 40; i++) ...[said('joy'), said('neutral')],
      ];
      accents = speak(async, lines);
    });
    final joys = [for (var i = 0; i < accents.length; i += 2) accents[i]];
    for (var i = 1; i < joys.length; i++) {
      expect(joys[i], isNot(joys[i - 1]));
    }
    expect(joys.toSet().length, greaterThanOrEqualTo(3));
    expect(joys.every((id) => id!.startsWith('joy.')), isTrue);
  });

  test('a feeling said again calms down; a change starts over', () {
    late List<String?> accents;
    run((async) {
      accents = speak(async, [
        said('surprise'),
        said('surprise'),
        said('surprise'),
        said('surprise'),
        said('fear'),
      ]);
    });
    expect(accents[0], startsWith('surprise.'));
    expect(accents[1], startsWith('surprise.'));
    expect(accents.sublist(2, 4), ['n.nod', 'n.nod']); // only a small beat
    expect(accents[4], startsWith('fear.'));
  });

  test('a short sentence only nods', () {
    late List<String?> accents;
    run((async) {
      accents = speak(async, [said('joy', ms: 500), said('anger', text: '哼！')]);
    });
    expect(accents, ['n.nod', 'n.nod']);
  });

  test('a motion too long for its sentence is left out', () {
    late List<String?> accents;
    run((async) {
      accents = speak(async, [
        for (var i = 0; i < 12; i++) ...[
          said('sadness', ms: 1700),
          said('neutral'),
        ],
      ]);
    });
    // 1.7 s leaves room only for the droop (1.2 s); the others take longer.
    final sad = [for (var i = 0; i < accents.length; i += 2) accents[i]];
    expect(sad.toSet().difference({'sad.droop', 'n.nod'}), isEmpty);
  });

  test('each path speaks its own way', () {
    late List<String?> knowledge, list, reading, recommend;
    run((async) {
      knowledge = speak(async, [
        said('neutral', index: 0, route: 'knowledge'),
        said('neutral', index: 1, route: 'knowledge', text: '它的產品包括 WhizPad。'),
        said('neutral', index: 2, route: 'knowledge', text: '世大智科是一家照護科技的公司。'),
      ]);
      list = speak(async, [
        said('neutral', index: 0, route: 'list'),
        said('neutral', index: 1, route: 'list'),
        said('neutral', index: 2, route: 'list'),
      ]);
      reading = speak(async, [said('neutral', index: 0, reading: true)]);
      recommend = speak(async, [
        for (var i = 0; i < 6; i++)
          said('neutral', index: i, route: 'recommend'),
      ]);
    });
    expect(knowledge[0], startsWith('intro.'));
    expect(knowledge[1], isNot(startsWith('intro.'))); // one sentence apart
    expect(knowledge[2], startsWith('intro.'));
    expect(list[0], isNot('cue.item'));
    expect(list.sublist(1), ['cue.item', 'cue.item']);
    expect(reading, ['cue.item']);
    expect(recommend.every((id) => id!.startsWith('n.')), isTrue);
  });

  test('without a route a sentence gets a beat', () {
    late List<String?> accents;
    run((async) {
      accents = speak(async, [
        for (var i = 0; i < 20; i++) said('neutral', index: i),
      ]);
    });
    expect(accents.every((id) => id!.startsWith('n.')), isTrue);
    expect(accents.toSet().length, greaterThanOrEqualTo(4));
  });

  test('a question tilts the head as it ends', () {
    run((async) {
      director.speaking(said('neutral', ms: 3000, text: '你想先聽哪一個呢？'));
      async.elapse(const Duration(milliseconds: 3000));
    });
    final tilts = moves().where((s) {
      final roll = int.parse(s.line.split(' ')[3]);
      return (roll - 90).abs() >= 5;
    });
    expect(tilts, isNotEmpty);
    expect(tilts.first.at, greaterThanOrEqualTo(3000 - 900));
  });

  test('an interruption lets the move end, then leans in to listen', () {
    run((async) {
      director.speaking(said('surprise'));
      async.elapse(const Duration(milliseconds: 60));
      director.listening();
      async.elapse(const Duration(seconds: 2));
    });
    final first = moves().first;
    final takes = int.parse(first.line.split(' ').last);
    final next = moves().elementAt(1);
    expect(next.at, greaterThanOrEqualTo(takes));
    expect(next.line, startsWith('M 90 165 ')); // leaning in, tilt +3 × 1.8
    expect(sent.map((s) => s.line), contains('O t 1.8 2600 0'));
  });

  test('waiting ponders a new way each time, holding the gaze', () {
    late bool held;
    run((async) {
      director.waiting();
      held = director.holding.value;
      async.elapse(const Duration(seconds: 12));
    });
    expect(held, isTrue);
    final poses = moves().map((s) => s.line.split(' ').sublist(1, 4).join(' '));
    expect(poses.length, greaterThanOrEqualTo(4));
    final list = poses.toList();
    for (var i = 1; i < list.length; i++) {
      expect(list[i], isNot(list[i - 1]));
    }
  });

  test('the gaze is held only through an accent', () {
    final held = <bool>[];
    run((async) {
      director.holding.addListener(() => held.add(director.holding.value));
      director.speaking(said('joy'));
      async.elapse(const Duration(seconds: 4));
    });
    expect(held, [true, false]);
  });

  test('the end of a reply nods, then breathes and glances now and then', () {
    run((async) {
      director.rested();
      async.elapse(const Duration(seconds: 40));
    });
    final lines = sent.map((s) => s.line).toList();
    expect(lines.first, startsWith('M 90 165 90')); // the nod, 3° × 1.8
    expect(lines, contains('O t 1.4 4200 0'));
    expect(lines, contains('O p 1.4 6100 0'));
    expect(
      moves().where(
        (s) => s.line.startsWith('M 95 ') || s.line.startsWith('M 85 '),
      ),
      isNotEmpty,
    );
  });

  test('every move takes at least what the budget allows', () {
    run((async) {
      director.idle();
      async.elapse(const Duration(seconds: 1));
      director.listening(); // tilt +3
      async.elapse(const Duration(seconds: 1));
      for (var i = 0; i < 10; i++) {
        director.speaking(said(i.isEven ? 'surprise' : 'sadness', index: i));
        async.elapse(const Duration(milliseconds: 2500));
      }
    });
    var from = Pose.neutral;
    for (final move in moves()) {
      final p = move.line.split(' ').skip(1).map(int.parse).toList();
      final to = Pose(pan: p[0] - 90.0, tilt: p[1] - 160.0, roll: p[2] - 90.0);
      // Each end is rounded to a whole degree on the wire: up to a degree less.
      double closer(double a, double b) =>
          (b - a).abs() <= 1 ? a : b - (b - a).sign;
      final least = Budget.safeMs(
        from,
        Pose(
          pan: closer(from.pan, to.pan),
          tilt: closer(from.tilt, to.tilt),
          roll: closer(from.roll, to.roll),
        ),
        1,
        const {},
      );
      expect(p[3], greaterThanOrEqualTo(least), reason: move.line);
      from = to;
    }
  });

  test('every command sent fits one write', () {
    run((async) {
      for (final feeling in [
        'joy',
        'sadness',
        'anger',
        'fear',
        'surprise',
        'thinking',
      ]) {
        director.speaking(said(feeling));
        async.elapse(const Duration(seconds: 4));
      }
      director.waiting();
      async.elapse(const Duration(seconds: 5));
    });
    for (final s in sent) {
      expect(s.line.length, lessThanOrEqualTo(maxCommandBytes), reason: s.line);
    }
  });

  test('a connected robot greets, then goes on where Avatar mode is', () {
    run((async) {
      director.listening();
      async.elapse(const Duration(seconds: 1));
      sent.clear();
      director.connected();
      async.elapse(const Duration(seconds: 4));
    });
    final lines = sent.map((s) => s.line).toList();
    expect(lines.first, 'M 90 160 90 1000');
    expect(lines[1], 'M 90 146 90 400');
    expect(
      lines.where((l) => l.startsWith('M 90 165 ')),
      isNotEmpty,
    ); // listening
  });

  test('the model follows the S-curve of a move', () {
    fakeAsync((async) {
      final start = DateTime(2026);
      final model = CentreModel(() => start.add(async.elapsed));
      model.moved(const Pose(tilt: -10), 1000);
      expect(model.moving, isTrue);
      async.elapse(const Duration(milliseconds: 500));
      expect(model.at().tilt, closeTo(-5, 0.01));
      async.elapse(const Duration(milliseconds: 250));
      expect(model.at().tilt, closeTo(-10 * (1 - cos(pi * 0.75)) / 2, 0.01));
      async.elapse(const Duration(milliseconds: 300));
      expect(model.moving, isFalse);
      expect(model.at().tilt, -10);
    });
  });

  test('cues read a question, an exclamation and an introduction', () {
    expect(asks('你想先聽哪一個呢？'), isTrue);
    expect(asks('要不要試試看嗎'), isTrue);
    expect(asks('Which one would you like?'), isTrue);
    expect(asks('這是答案。'), isFalse);
    expect(exclaims('太好了！'), isTrue);
    expect(exclaims('Great!'), isTrue);
    expect(exclaims('好。'), isFalse);
    expect(introduces('GRC 是元智大學的研究中心。'), isTrue);
    expect(introduces('世大智科是一家照護科技的公司。'), isTrue);
    expect(
      introduces('G-Tech is a company that makes care technology.'),
      isTrue,
    );
    expect(introduces('它的產品包括 whizpad。'), isTrue);
    expect(introduces('論文發表於 2024 年。'), isFalse);
  });
}
