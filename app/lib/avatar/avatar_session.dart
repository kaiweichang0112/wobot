import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../api.dart';
import '../chat.dart';
import '../robot/director.dart';
import '../robot/robot.dart';
import '../sse.dart';
import '../voice/listener.dart';
import '../voice/speech_queue.dart';

/// What Avatar mode is doing, as its one button shows it.
enum AvatarState {
  /// Nothing heard or played: a tap listens.
  idle,

  /// Hearing a question: a tap sends it.
  listening,

  /// The question is sent and no sentence is heard yet: the avatar thinks.
  waiting,

  /// Sentences are being played: a tap stops them and listens.
  speaking,
}

/// One conversation spoken with the avatar (spec 22): hears a question, sends it in
/// Avatar mode on the chat's conversation, plays the reply's sentences and shows each
/// one's expression as it starts.
///
/// Every question, interruption or reading on starts a new generation. Speech from an
/// earlier one, a sentence still arriving or a timer still running, finds its
/// generation over and is dropped: the same idea as the server's compare-and-swap.
/// An interruption stops only the speech; the text is still written (P19).
class AvatarSession extends ChangeNotifier {
  AvatarSession({
    required this._api,
    required this._chat,
    required this._listener,
    required this._queue,
    required this._locale,
    this.robot,
    this.previousWait = const Duration(seconds: 15),
    this.hold = const Duration(milliseconds: 1600),
  }) {
    _chat.speech = _onSpeech;
    _queue.onStart = _onStart;
    _listener.addListener(notifyListeners);
  }

  final WobotApi _api;
  final ChatController _chat;
  final QuestionListener _listener;
  final SpeechQueue _queue;
  final Future<String> Function() _locale;

  /// The robot the phone sits on, if any: connected while Avatar mode is open.
  final Robot? robot;

  /// How long a question waits for the reply before it to be written, before that
  /// reply is stopped, keeping its words.
  final Duration previousWait;

  /// How long the last expression stays after the last sentence.
  final Duration hold;

  AvatarState state = AvatarState.idle;

  /// The expression shown: one of the seven of `frames.dart`.
  String expression = 'neutral';

  /// What went wrong, for the reader; cleared by the next tap.
  String? error;

  int _generation = 0;

  /// The turn whose speech is being played; null once it is over or interrupted.
  ChatTurn? _speaking;

  /// The turn last asked here: its list may be read on.
  ChatTurn? _last;
  Completer<void>? _reading;
  Timer? _rest;
  bool _disposed = false;

  /// Whether a sentence is being heard: the avatar's mouth moves only then.
  ValueListenable<bool> get speaking => _queue.playing;

  /// The words heard so far, while listening.
  String get words => _listener.words;

  /// A list reply has more to read, and the user may ask for it.
  bool get canReadOn => _last?.next != null && state == AvatarState.idle;

  /// Sets the phone up to play speech, before the first question, and starts
  /// looking for the robot.
  Future<void> prepare() {
    unawaited(robot?.open());
    return _queue.prepare();
  }

  /// The one button: sends the question being heard, or stops what is being said
  /// and listens.
  Future<void> talk() async {
    if (_listener.listening) return _listener.finish();
    await _silence();
    final generation = _generation;
    _set(AvatarState.listening, 'neutral');
    robot?.listening();
    final heard = await _listener.listen(localeId: await _locale());
    if (generation != _generation) return;
    if (heard == null) return _idle(error: _listener.error);
    if (heard.empty) return _idle(error: 'No speech detected. Try again.');
    await _ask(heard.text, generation);
  }

  /// Reads the next items of the last list reply (DEC-096).
  Future<void> readOn() async {
    final turn = _last;
    final from = turn?.next;
    if (turn == null || from == null) return;
    await _silence();
    final generation = _generation;
    _set(AvatarState.waiting, 'neutral');
    robot?.waiting();
    final abort = _reading = Completer<void>();
    try {
      await for (final event in _api.continueReading(
        turn.id,
        attempt: turn.attempt,
        from: from,
        abort: abort.future,
      )) {
        if (generation != _generation) return;
        if (event.name == 'speech') _queue.add(Sentence.fromEvent(event.data));
        if (event.name == 'completed') turn.next = event.data['next'] as int?;
      }
    } on http.RequestAbortedException {
      return;
    } on Exception catch (e) {
      if (generation == _generation) _idle(error: describe(e));
      return;
    }
    await _queue.idle;
    if (generation == _generation) _rested();
  }

  /// Out of sight, or leaving for Text: nothing is heard or played any more. The
  /// text of a reply being written goes on.
  Future<void> pause() async {
    // Not awaited: an unreachable robot must not hold up leaving.
    unawaited(robot?.pause());
    await _silence();
    await _listener.cancel();
    _idle();
  }

  Future<void> _ask(String text, int generation) async {
    _set(AvatarState.waiting, 'thinking');
    robot?.waiting();
    if (!await _previousEnded(generation)) return;
    final before = _chat.turns.length;
    final sending = _chat.send(text, mode: 'avatar');
    if (_chat.turns.length == before) return _idle();
    final turn = _speaking = _last = _chat.turns.last;
    await sending;
    if (generation != _generation) return;
    if (turn.state != TurnState.completed) {
      _speaking = null;
      return _idle(error: turn.error);
    }
    await _queue.idle;
    if (generation != _generation) return;
    _speaking = null;
    _report(turn, heard: _queue.heard);
    _rested();
  }

  /// Waits for the reply before to be written; one stuck past [previousWait] is
  /// stopped, keeping its words. False when this question was given up meanwhile.
  Future<bool> _previousEnded(int generation) async {
    final deadline = DateTime.now().add(previousWait);
    while (_chat.busy && generation == _generation) {
      if (!DateTime.now().isBefore(deadline)) {
        await _chat.stop();
        break;
      }
      await Future<void>.delayed(const Duration(milliseconds: 100));
    }
    return generation == _generation;
  }

  /// Stops what is being said, at once, here first: the server hears of it after.
  Future<void> _silence() async {
    _generation++;
    _rest?.cancel();
    error = null;
    final reading = _reading;
    if (reading != null && !reading.isCompleted) reading.complete();
    _reading = null;
    final turn = _speaking;
    _speaking = null;
    final heard = _queue.heard;
    await _queue.stop();
    if (turn != null) _report(turn, heard: heard, stopped: true);
  }

  void _onSpeech(ChatTurn turn, SseEvent event) {
    if (!identical(turn, _speaking) || event.name != 'speech') return;
    _queue.add(Sentence.fromEvent(event.data));
  }

  void _onStart(Sentence sentence) {
    _rest?.cancel();
    state = AvatarState.speaking;
    // A few characters is over before a new face could be seen: it would only flicker.
    if (sentence.text.runes.length >= 4) expression = sentence.emotion;
    robot?.speaking(
      Spoken(
        index: sentence.index,
        text: sentence.text,
        emotion: sentence.emotion,
        length: Spoken.lengthOf(sentence.audio.length),
        route: _speaking?.route,
        reading: _reading != null,
      ),
    );
    _notify();
  }

  /// Done speaking: the last expression stays a moment, then the face rests.
  void _rested() {
    state = AvatarState.idle;
    robot?.rested();
    _notify();
    final generation = _generation;
    _rest = Timer(hold, () {
      if (generation != _generation) return;
      expression = 'neutral';
      _notify();
    });
  }

  void _report(ChatTurn turn, {required int heard, bool stopped = false}) {
    if (!turn.saved) return;
    unawaited(
      _api
          .playback(
            turn.id,
            attempt: turn.attempt,
            heard: heard,
            stopped: stopped,
          )
          .then<void>((_) {}, onError: (Object _) {}),
    );
  }

  void _set(AvatarState next, String face) {
    state = next;
    expression = face;
    _notify();
  }

  void _idle({String? error}) {
    state = AvatarState.idle;
    expression = 'neutral';
    this.error = error;
    robot?.idle(error: error != null);
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _generation++;
    _rest?.cancel();
    if (_chat.speech == _onSpeech) _chat.speech = null;
    _listener.removeListener(notifyListeners);
    _listener.dispose();
    unawaited(_queue.dispose());
    unawaited(robot?.dispose());
    super.dispose();
  }
}
