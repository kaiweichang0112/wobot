import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/pages/history_drawer.dart';
import 'package:wobot/widgets/linked_text.dart';
import 'package:wobot/widgets/thinking.dart';
import 'package:wobot/widgets/w_mark.dart';

void main() {
  test('an address after its label is shown as the label', () {
    final runs = linkify(
      '1. 2020/01/18　樂齡科技，TCA 教師英雄會，PDF：https://drive.google.com/file/d/1O/view?usp=share_link\n'
      '產品網頁：https://example.com/p/1。\n'
      'Product page: https://example.com/p/2, Full text: https://doi.org/10.1/x',
    );

    expect(runs.map((r) => r.text), [
      '1. 2020/01/18　樂齡科技，TCA 教師英雄會，',
      'PDF',
      '\n',
      '產品網頁',
      '。\n',
      'Product page',
      ', ',
      'Full text',
    ]);
    expect(
      runs[1].link,
      Uri.parse('https://drive.google.com/file/d/1O/view?usp=share_link'),
    );
    expect(runs[3].link, Uri.parse('https://example.com/p/1'));
    expect(runs[7].link, Uri.parse('https://doi.org/10.1/x'));
  });

  test('an address without a label is shown as its site', () {
    final runs = linkify('詳見 https://www.example.com/a/b 的說明');

    expect(runs.map((r) => r.text), ['詳見 ', 'example.com', ' 的說明']);
    expect(runs[1].link, Uri.parse('https://www.example.com/a/b'));
  });

  test('text without an address stays one run', () {
    expect(linkify('沒有連結').single.link, isNull);
  });

  test('a chat says when it was last active', () {
    final now = DateTime(2026, 10, 13, 18);

    expect(when(DateTime(2026, 10, 13, 9, 5), now: now), '09:05');
    expect(when(DateTime(2026, 10, 12, 23), now: now), 'Yesterday');
    expect(when(DateTime(2026, 3, 2), now: now), '3/2');
    expect(when(DateTime(2025, 12, 31), now: now), '2025/12/31');
  });

  test(
    'the W waves inside its square, each corner a little after the last',
    () {
      for (var t = 0.0; t < 1; t += 0.05) {
        final corners = wCorners(100, t: t);
        expect(corners, hasLength(5));
        // Its stroke, 11 wide, stays inside the square.
        for (final c in corners) {
          expect(c.dy, inInclusiveRange(5.5, 94.5));
        }
      }
      final rest = wCorners(100);
      final start = wCorners(100, t: 0);
      final later = wCorners(100, t: 0.25);
      expect(later.map((c) => c.dx), rest.map((c) => c.dx));
      expect(later[0].dy, isNot(start[0].dy));
      expect(
        start[0].dy - rest[0].dy,
        isNot(closeTo(start[1].dy - rest[1].dy, 0.01)),
      );
    },
  );

  test('the starting W writes itself, holds, then fades', () {
    expect(wStroke(0).written, 0);
    expect(wStroke(0.15).written, inExclusiveRange(0.5, 1));
    expect(wStroke(0.3), (written: 1.0, opacity: 1.0));
    expect(wStroke(0.9).opacity, closeTo(0.5, 0.001));
  });
}
