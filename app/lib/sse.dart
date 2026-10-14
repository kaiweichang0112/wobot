import 'dart:async';
import 'dart:convert';

/// One server-sent event: its name and its JSON data.
class SseEvent {
  const SseEvent(this.name, this.data);

  final String name;
  final Map<String, dynamic> data;

  @override
  String toString() => 'SseEvent($name, $data)';
}

/// Reads a server-sent event stream: `event:` and `data:` lines, each event ended by
/// a blank line. Comments (the server's `: ping` keepalive) and events whose data is
/// not a JSON object are skipped.
Stream<SseEvent> parseSse(Stream<List<int>> bytes) async* {
  var name = 'message';
  final data = <String>[];
  await for (final line
      in bytes.transform(utf8.decoder).transform(const LineSplitter())) {
    if (line.isEmpty) {
      if (data.isNotEmpty) {
        final event = _event(name, data.join('\n'));
        if (event != null) yield event;
      }
      name = 'message';
      data.clear();
    } else if (line.startsWith(':')) {
      continue;
    } else {
      final colon = line.indexOf(':');
      final field = colon < 0 ? line : line.substring(0, colon);
      var value = colon < 0 ? '' : line.substring(colon + 1);
      if (value.startsWith(' ')) value = value.substring(1);
      if (field == 'event') name = value;
      if (field == 'data') data.add(value);
    }
  }
}

SseEvent? _event(String name, String data) {
  try {
    final value = jsonDecode(data);
    return value is Map<String, dynamic> ? SseEvent(name, value) : null;
  } on FormatException {
    return null;
  }
}
