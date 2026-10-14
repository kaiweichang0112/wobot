import 'package:flutter/gestures.dart';
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../theme.dart';

/// A web address in a reply: a product page, a talk's PDF. Ends before whitespace and
/// the punctuation that closes a Chinese or English sentence around it.
final _address = RegExp(r'https?://[^\s<>"，。、；！？）」』】]+');

/// The label a reply puts before an address, as in `PDF：`, `產品網頁：` or
/// `Product page: `: a few words since the last break, then a colon.
final _label = RegExp(r'([^\s，,。；;、（(：:][^\n，,。；;、（(：:]{0,19})(：\s*|:\s+)$');

/// Splits text into plain runs and links. An address after a label is shown as the
/// label, so a reply reads `PDF` rather than a long address; an address without one
/// is shown as its site.
List<({String text, Uri? link})> linkify(String text) {
  final runs = <({String text, Uri? link})>[];
  var at = 0;
  for (final match in _address.allMatches(text)) {
    var address = match.group(0)!;
    // A sentence may end right after an address.
    while (address.isNotEmpty &&
        '.,;:!?)'.contains(address[address.length - 1])) {
      address = address.substring(0, address.length - 1);
    }
    final uri = Uri.tryParse(address);
    if (uri == null || uri.host.isEmpty) continue;
    var before = text.substring(at, match.start);
    final labelled = _label.firstMatch(before);
    final shown = labelled?.group(1)?.trim() ?? _site(uri);
    if (labelled != null) before = before.substring(0, labelled.start);
    if (before.isNotEmpty) runs.add((text: before, link: null));
    runs.add((text: shown, link: uri));
    at = match.start + address.length;
  }
  if (at < text.length) runs.add((text: text.substring(at), link: null));
  return runs;
}

String _site(Uri uri) =>
    uri.host.startsWith('www.') ? uri.host.substring(4) : uri.host;

/// Plain text whose web addresses open in the browser. Replies are plain text
/// (DEC-082): written to be read aloud, they carry no Markdown.
class LinkedText extends StatefulWidget {
  const LinkedText(this.text, {super.key, this.style});

  final String text;
  final TextStyle? style;

  @override
  State<LinkedText> createState() => _LinkedTextState();
}

class _LinkedTextState extends State<LinkedText> {
  final _taps = <TapGestureRecognizer>[];

  @override
  void dispose() {
    _release();
    super.dispose();
  }

  void _release() {
    for (final tap in _taps) {
      tap.dispose();
    }
    _taps.clear();
  }

  @override
  Widget build(BuildContext context) {
    _release();
    return Text.rich(
      TextSpan(
        style: widget.style,
        children: [
          for (final run in linkify(widget.text))
            if (run.link case final link?)
              TextSpan(
                text: run.text,
                style: const TextStyle(
                  color: WobotColors.accent,
                  decoration: TextDecoration.underline,
                  decorationColor: WobotColors.accent,
                ),
                recognizer:
                    (TapGestureRecognizer()
                          ..onTap = () => launchUrl(
                            link,
                            mode: LaunchMode.externalApplication,
                          ))
                        .also(_taps.add),
              )
            else
              TextSpan(text: run.text),
        ],
      ),
    );
  }
}

extension _Also<T> on T {
  T also(void Function(T) use) {
    use(this);
    return this;
  }
}
