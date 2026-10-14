import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:wobot/sse.dart';

Stream<List<int>> chunks(List<String> parts) =>
    Stream.fromIterable(parts.map(utf8.encode));

void main() {
  test('reads events, whatever the chunks split', () async {
    final events = await parseSse(
      chunks([
        'event: acc',
        'epted\ndata: {"attempt": 1}\n',
        '\nevent: text_delta\r\ndata: {"text": "中',
        '文"}\r\n\r\n',
      ]),
    ).toList();

    expect(events.map((e) => e.name), ['accepted', 'text_delta']);
    expect(events[1].data, {'text': '中文'});
  });

  test('skips keepalive comments and data that is not a JSON object', () async {
    final events = await parseSse(
      chunks([': ping\n\n', 'data: not json\n\n', 'data: {"a": 1}\n\n']),
    ).toList();

    expect(events.single.name, 'message');
    expect(events.single.data, {'a': 1});
  });

  test('joins data spread over several lines', () async {
    final events = await parseSse(
      chunks(['event: reply\ndata: {"text":\ndata: "x"}\n\n']),
    ).toList();

    expect(events.single.data, {'text': 'x'});
  });
}
