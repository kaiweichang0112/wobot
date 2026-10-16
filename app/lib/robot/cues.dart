/// What a sentence's words say about how to move with it, read from the text alone:
/// the robot hears no prosody, only the sentence and its feeling.
library;

final _question = RegExp(r'([?？]|[嗎呢])[\s。！!～~…」』）)]*$');
final _exclamation = RegExp(r'[!！][\s」』）)]*$');

/// "X 是…的中心", "X is a … company": a sentence that says what something is.
final _definition = RegExp(
  r'是.{0,24}(中心|公司|團隊|實驗室|研究室|大學|產品|平台|系統)|'
  r'\b(is|are) (a|an|the)\b.{0,48}\b(cent(er|re)|company|team|lab(oratory)?|university|product)s?\b',
  caseSensitive: false,
);

/// The centre and the company the sources are about, and what they make
/// (`knowledge/profiles.py` lists their pages).
const _names = [
  'GRC',
  '老人福祉科技研究中心',
  'Gerontechnology Research Center',
  '元智',
  '世大智科',
  'G-Tech',
  'WhizPad',
  'WhizToys',
];

/// Asks something: the head tilts as it ends.
bool asks(String text) => _question.hasMatch(text.trim());

/// Ends in an exclamation.
bool exclaims(String text) => _exclamation.hasMatch(text.trim());

/// Introduces the centre, the company or one of their products.
bool introduces(String text) =>
    _definition.hasMatch(text) ||
    _names.any((name) => text.toLowerCase().contains(name.toLowerCase()));
