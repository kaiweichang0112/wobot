import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/voice/cer.dart';
import 'package:wobot/voice/listener.dart';

import 'fake_speech.dart';

void main() {
  late FakeEngine engine;
  late QuestionListener listener;

  setUp(() {
    engine = FakeEngine();
    listener = QuestionListener(
      engine,
      pause: const Duration(milliseconds: 150),
      silence: const Duration(milliseconds: 400),
      limit: const Duration(milliseconds: 1000),
      finalWait: const Duration(milliseconds: 50),
    );
  });

  tearDown(() => listener.dispose());

  test(
    'a pause after the words ends the question, with the final text',
    () async {
      engine.finalText = 'GRC 是哪一年成立的？';
      final heard = listener.listen();
      await pumpEventQueue();
      engine.say('GRC 是哪一年');
      await Future<void>.delayed(const Duration(milliseconds: 20));
      engine.say('GRC 是哪一年成立的');

      final result = (await heard)!;

      expect(result.reason, EndReason.pause);
      expect(result.text, 'GRC 是哪一年成立的？');
      expect(result.finalArrived, isTrue);
      expect(result.lastWordsToEnd!.inMilliseconds, greaterThanOrEqualTo(150));
      expect(engine.stops, 1);
    },
  );

  test('words that keep coming hold the end off', () async {
    final heard = listener.listen();
    await pumpEventQueue();
    for (var i = 1; i <= 5; i++) {
      engine.say('一' * i);
      await Future<void>.delayed(const Duration(milliseconds: 25));
    }
    expect(listener.listening, isTrue);

    expect((await heard)!.text, '一' * 5);
  });

  test('without the final result, the last words stand', () async {
    final heard = listener.listen();
    await pumpEventQueue();
    engine.say('在家裡');

    final result = (await heard)!;

    expect(result.text, '在家裡');
    expect(result.finalArrived, isFalse);
  });

  test('nothing said ends with nothing heard', () async {
    final result = (await listener.listen())!;

    expect(result.reason, EndReason.silence);
    expect(result.empty, isTrue);
    expect(result.firstWords, isNull);
  });

  test('a tap, the pause and the limit at once end it once', () async {
    final heard = listener.listen();
    await pumpEventQueue();
    engine.say('都可以');

    await Future.wait([listener.finish(), listener.finish()]);
    final result = (await heard)!;
    await Future<void>.delayed(const Duration(milliseconds: 1100));

    expect(result.reason, EndReason.manual);
    expect(engine.stops, 1);
  });

  test(
    'a question cancelled is never sent, and late words are dropped',
    () async {
      final heard = listener.listen();
      await pumpEventQueue();
      engine.say('今天好熱');

      await listener.cancel();
      engine.say('今天好熱，有點不想出門');
      await Future<void>.delayed(const Duration(milliseconds: 100));

      expect(await heard, isNull);
      expect(engine.cancels, 1);
      expect(engine.stops, 0);
      expect(listener.words, '今天好熱');
    },
  );

  test('words from an earlier question never reach the next', () async {
    final first = listener.listen();
    await pumpEventQueue();
    final stale = engine.onResult!;
    final second = listener.listen();
    await pumpEventQueue();

    stale('舊的問題', false);
    engine.say('新的問題');

    expect(await first, isNull);
    expect((await second)!.text, '新的問題');
  });

  test('the recognizer ending by itself ends the question', () async {
    final heard = listener.listen();
    await pumpEventQueue();
    engine.say('When was GRC founded?', isFinal: true);

    final result = (await heard)!;

    expect(result.reason, EndReason.engine);
    expect(result.text, 'When was GRC founded?');
    expect(engine.stops, 0);
  });

  test('a phone without recognition says so', () async {
    engine.available = false;

    expect(await listener.listen(), isNull);
    expect(listener.error, contains('unavailable'));
  });

  group('CER', () {
    test('counts edits per character said', () {
      expect(cer('徐業良教授', '許業良教授'), closeTo(0.2, 1e-9));
      expect(cer('在家裡', ''), 1);
      expect(cer('在家裡', '在家裡了'), closeTo(1 / 3, 1e-9));
    });

    test('ignores punctuation, spaces, case and full width', () {
      expect(cer('WhizPad 的布套可以拆下來洗嗎？', 'whiz pad的布套可以拆下來洗嗎'), 0);
      expect(cer('GRC 2021、2022 年', 'ＧＲＣ２０２１ ２０２２年'), 0);
    });

    test('counts simplified characters as errors', () {
      expect(cer('長輩', '长辈'), 1);
    });
  });
}
