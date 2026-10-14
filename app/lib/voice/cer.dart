/// Character error rate: how far what was heard is from what was said, in edits per
/// character said. Compared after normalizing, so that punctuation, spaces, case and
/// full-width forms, which change nothing for the reply, are not counted as errors.
double cer(String said, String heard) {
  final reference = normalize(said).runes.toList();
  final hypothesis = normalize(heard).runes.toList();
  if (reference.isEmpty) return hypothesis.isEmpty ? 0 : 1;
  return edits(reference, hypothesis) / reference.length;
}

/// Letters and digits only, half-width and lower case.
String normalize(String text) {
  final halfWidth = String.fromCharCodes(
    text.runes.map(
      (c) => c >= 0xFF01 && c <= 0xFF5E
          ? c - 0xFEE0
          : c == 0x3000
          ? 0x20
          : c,
    ),
  );
  return halfWidth.toLowerCase().replaceAll(_notWord, '');
}

final _notWord = RegExp(r'[^\p{L}\p{N}]', unicode: true);

/// Levenshtein distance: the fewest insertions, deletions and substitutions.
int edits(List<int> a, List<int> b) {
  var previous = List<int>.generate(b.length + 1, (j) => j);
  for (var i = 1; i <= a.length; i++) {
    final current = List<int>.filled(b.length + 1, 0)..[0] = i;
    for (var j = 1; j <= b.length; j++) {
      final substitution = previous[j - 1] + (a[i - 1] == b[j - 1] ? 0 : 1);
      current[j] = [
        previous[j] + 1,
        current[j - 1] + 1,
        substitution,
      ].reduce((x, y) => x < y ? x : y);
    }
    previous = current;
  }
  return previous[b.length];
}
