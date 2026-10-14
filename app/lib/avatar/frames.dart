/// The avatar's frames: seven expressions, each with three mouths (spec 21). The same
/// keys as the backend's `wobot/assets/frames.py`; a new set is a new schema version.
const expressions = [
  'neutral',
  'joy',
  'sadness',
  'anger',
  'fear',
  'surprise',
  'thinking',
];
const mouths = ['closed', 'half', 'wide'];
final frameKeys = [
  for (final expression in expressions)
    for (final mouth in mouths) '${expression}_$mouth',
];

/// Drawn from the photo; every other frame is drawn from it, directly or not.
const baseFrame = 'neutral_closed';

String frameKey(String expression, String mouth) => '${expression}_$mouth';

/// The frame a frame is drawn from: the base from the photo, each expression's closed
/// frame from the base, and its half and wide frames from that closed frame.
String? parentOf(String key) {
  if (key == baseFrame) return null;
  final [expression, mouth] = key.split('_');
  return mouth == 'closed' ? baseFrame : '${expression}_closed';
}

/// The frames drawn from this one, directly or not: drawn again with it.
List<String> descendantsOf(String key) {
  final children = [
    for (final k in frameKeys)
      if (parentOf(k) == key) k,
  ];
  return [...children, for (final c in children) ...descendantsOf(c)];
}
