import 'dart:async';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import 'api.dart';
import 'sse.dart';

enum TurnState { running, completed, cancelled, failed }

/// A question and its reply as the chat shows them.
class ChatTurn {
  ChatTurn({
    required this.id,
    required this.question,
    this.attempt = 1,
    this.state = TurnState.running,
    this.text = '',
    this.saved = false,
  });

  factory ChatTurn.fromRecord(TurnRecord record) => ChatTurn(
    id: record.turnId,
    question: record.userText,
    attempt: record.attempt,
    state: _stateOf(record.status),
    text: record.replyText ?? '',
    saved: true,
  )..error = record.status == 'failed' ? _failureOf(record.errorCode) : null;

  final String id;
  final String question;
  int attempt;
  TurnState state;

  /// The reply so far: the words streamed, then the whole reply that replaces them.
  String text;

  /// `searching` or `writing` while running, as the server last said.
  String? stage;

  /// Why the reply failed, for the reader.
  String? error;

  /// Whether the server has the turn: a retry of one it never got sends it again.
  bool saved;

  bool get running => state == TurnState.running;
}

/// One conversation: its turns, the one being written, and sending, stopping and
/// retrying. The conversation is started on the first message, so the history never
/// fills with empty ones.
class ChatController extends ChangeNotifier {
  ChatController(this._api, this._history);

  final WobotApi _api;
  final HistoryController _history;

  Conversation? conversation;
  final List<ChatTurn> turns = [];
  String? _earlier;
  bool loading = false;
  String? loadError;

  ChatTurn? _active;
  Completer<void>? _abort;
  bool _disposed = false;

  /// Whether a reply is being written: one at a time.
  bool get busy => _active != null;
  bool get hasEarlier => _earlier != null;

  /// The last turn, if it may be tried again: stopped or failed.
  ChatTurn? get retryable {
    final last = turns.isEmpty ? null : turns.last;
    if (last == null || busy) return null;
    return last.state == TurnState.failed || last.state == TurnState.cancelled
        ? last
        : null;
  }

  Future<void> open(Conversation chosen) async {
    await stop();
    conversation = chosen;
    turns.clear();
    _earlier = null;
    await reload();
  }

  Future<void> startNew() async {
    await stop();
    conversation = null;
    turns.clear();
    _earlier = null;
    loadError = null;
    _notify();
  }

  /// Reads the conversation's turns again, as they stand on the server: after the
  /// app returns to the foreground, or a stream that ended without saying how.
  Future<void> reload() async {
    final chosen = conversation;
    if (chosen == null || busy) return;
    loading = true;
    loadError = null;
    _notify();
    try {
      final page = await _api.turns(chosen.id);
      if (!identical(conversation, chosen)) return;
      turns
        ..clear()
        ..addAll(page.items.map(ChatTurn.fromRecord));
      _earlier = page.nextCursor;
    } on Exception catch (e) {
      loadError = describe(e);
    } finally {
      loading = false;
      _notify();
    }
  }

  Future<void> loadEarlier() async {
    final chosen = conversation;
    final cursor = _earlier;
    if (chosen == null || cursor == null || loading) return;
    loading = true;
    _notify();
    try {
      final page = await _api.turns(chosen.id, cursor: cursor);
      turns.insertAll(0, page.items.map(ChatTurn.fromRecord));
      _earlier = page.nextCursor;
    } on Exception catch (e) {
      loadError = describe(e);
    } finally {
      loading = false;
      _notify();
    }
  }

  Future<void> send(String text) async {
    final message = text.trim();
    if (message.isEmpty || busy) return;
    final turn = ChatTurn(id: newTurnId(), question: message);
    turns.add(turn);
    _active = turn;
    _notify();
    try {
      conversation ??= await _history.start();
    } on Exception catch (e) {
      _fail(turn, describe(e));
      return;
    }
    await _play(turn, (abort) {
      return _api.sendTurn(
        conversation!.id,
        turnId: turn.id,
        text: message,
        abort: abort,
      );
    });
  }

  Future<void> retry() async {
    final turn = retryable;
    if (turn == null) return;
    final expected = turn.attempt;
    turn
      ..state = TurnState.running
      ..text = ''
      ..error = null
      ..stage = null;
    _active = turn;
    _notify();
    await _play(
      turn,
      (abort) => turn.saved
          ? _api.retryTurn(turn.id, expectedAttempt: expected, abort: abort)
          : _api.sendTurn(
              conversation!.id,
              turnId: turn.id,
              text: turn.question,
              abort: abort,
            ),
    );
  }

  /// Stops the reply being written, keeping the words shown (CHAT-012). On Cloud Run
  /// closing the stream alone stops nothing: the cancel request is the stop.
  Future<void> stop() async {
    final turn = _active;
    if (turn == null) return;
    _closeStream();
    _active = null;
    turn
      ..state = TurnState.cancelled
      ..stage = null;
    _notify();
    // Sent even before `accepted`: the server may have the turn already.
    try {
      final status = await _api.cancelTurn(
        turn.id,
        attempt: turn.attempt,
        shownText: turn.text,
      );
      if (status != 'cancelled') await _sync(turn); // it ended first
    } on Exception {
      // Not there yet, or unreachable: the server ends the turn by its time limit,
      // and the next reload shows how.
    }
  }

  void _closeStream() {
    final abort = _abort;
    if (abort != null && !abort.isCompleted) abort.complete();
  }

  Future<void> _play(
    ChatTurn turn,
    Stream<SseEvent> Function(Future<void> abort) open,
  ) async {
    final abort = Completer<void>();
    _abort = abort;
    try {
      await for (final event in open(abort.future)) {
        if (abort.isCompleted || !identical(_active, turn)) break;
        _apply(turn, event);
        _notify();
      }
      if (turn.running && identical(_active, turn)) {
        // The stream ended without saying how: the turn ended elsewhere.
        _active = null;
        await _sync(turn);
      }
    } on http.RequestAbortedException {
      // Stopped: stop() has said so.
    } on ApiException catch (e) {
      if (e.code == 'TURN_EXISTS') {
        turn.saved = true;
        _active = null;
        await _sync(turn);
      } else {
        _fail(turn, e.message);
      }
    } on Exception {
      if (identical(_active, turn)) {
        // The connection was lost; the server may still be writing the reply.
        _active = null;
        await _follow(turn);
      }
    } finally {
      if (identical(_active, turn)) _active = null;
      if (identical(_abort, abort)) _abort = null;
      _notify();
    }
  }

  void _apply(ChatTurn turn, SseEvent event) {
    final data = event.data;
    switch (event.name) {
      case 'accepted':
        turn
          ..saved = true
          ..attempt = data['attempt'] as int;
      case 'status':
        turn.stage = data['stage'] as String?;
      case 'text_delta':
        turn
          ..stage = 'writing'
          ..text += data['text'] as String;
      case 'reply':
        turn.text = data['text'] as String? ?? turn.text;
      case 'title':
        final title = data['title'] as String;
        conversation = conversation?.copyWith(title: title);
        _history.rename(conversation!.id, title);
      case 'completed':
        turn
          ..state = TurnState.completed
          ..stage = null;
        _active = null;
        _history.touch(conversation!);
      case 'error':
        _fail(turn, data['message'] as String? ?? 'Something went wrong.');
    }
  }

  void _fail(ChatTurn turn, String message) {
    turn
      ..state = TurnState.failed
      ..stage = null
      ..error = message;
    if (identical(_active, turn)) _active = null;
    _notify();
  }

  /// Reads one turn back from the server, as it ended there.
  Future<void> _sync(ChatTurn turn) async {
    final chosen = conversation;
    if (chosen == null) return;
    try {
      final page = await _api.turns(chosen.id);
      final record = page.items.where((t) => t.turnId == turn.id).firstOrNull;
      if (record == null) return;
      final saved = ChatTurn.fromRecord(record);
      turn
        ..attempt = saved.attempt
        ..state = saved.state
        ..text = saved.text.isEmpty ? turn.text : saved.text
        ..error = saved.error
        ..saved = true;
    } on Exception {
      if (turn.running) _fail(turn, 'The connection was lost. Try again.');
    }
    _notify();
  }

  /// After a lost connection: reads the turn until the server has ended it, up to
  /// its time limit.
  Future<void> _follow(ChatTurn turn) async {
    turn.stage = null;
    for (var i = 0; i < 50 && !_disposed; i++) {
      await _sync(turn);
      if (!turn.running) return;
      await Future<void>.delayed(const Duration(seconds: 2));
    }
    if (turn.running) _fail(turn, 'The connection was lost. Try again.');
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _closeStream();
    super.dispose();
  }
}

/// The account's conversations, most recently active first.
class HistoryController extends ChangeNotifier {
  HistoryController(this._api);

  final WobotApi _api;
  final List<Conversation> conversations = [];
  String? _next;
  bool loading = false;
  String? error;
  bool _disposed = false;

  bool get hasMore => _next != null;

  Future<void> refresh() async {
    loading = true;
    error = null;
    _notify();
    try {
      final page = await _api.conversations();
      conversations
        ..clear()
        ..addAll(page.items);
      _next = page.nextCursor;
    } on Exception catch (e) {
      error = describe(e);
    } finally {
      loading = false;
      _notify();
    }
  }

  Future<void> loadMore() async {
    final cursor = _next;
    if (cursor == null || loading) return;
    loading = true;
    _notify();
    try {
      final page = await _api.conversations(cursor: cursor);
      conversations.addAll(page.items);
      _next = page.nextCursor;
    } on Exception catch (e) {
      error = describe(e);
    } finally {
      loading = false;
      _notify();
    }
  }

  Future<Conversation> start() async {
    final conversation = await _api.startConversation();
    conversations.insert(0, conversation);
    _notify();
    return conversation;
  }

  void rename(String id, String title) {
    final i = conversations.indexWhere((c) => c.id == id);
    if (i >= 0) conversations[i] = conversations[i].copyWith(title: title);
    _notify();
  }

  /// Moves the conversation to the top: it was active just now.
  void touch(Conversation conversation) {
    final i = conversations.indexWhere((c) => c.id == conversation.id);
    final current = i >= 0 ? conversations.removeAt(i) : conversation;
    conversations.insert(0, current.copyWith(updatedAt: DateTime.now()));
    _notify();
  }

  Future<void> delete(String id) async {
    await _api.deleteConversation(id);
    conversations.removeWhere((c) => c.id == id);
    _notify();
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    super.dispose();
  }
}

/// What to tell the reader of an error.
String describe(Object error) => switch (error) {
  ApiException(:final message) => message,
  TimeoutException() => 'Wobot took too long to answer. Try again.',
  _ => 'Could not reach Wobot. Check the connection and try again.',
};

TurnState _stateOf(String status) => switch (status) {
  'completed' => TurnState.completed,
  'cancelled' => TurnState.cancelled,
  'failed' => TurnState.failed,
  _ => TurnState.running,
};

String _failureOf(String? code) => switch (code) {
  'PROVIDER_UNAVAILABLE' => 'The reply could not be written just now.',
  'TURN_TIMEOUT' => 'The reply took too long.',
  'STREAM_INTERRUPTED' => 'The reply was interrupted.',
  _ => 'Something went wrong.',
};

final _random = Random.secure();

/// A random UUID (version 4): the app names each turn, so a message sent again after
/// a lost connection is never answered twice.
String newTurnId() {
  final bytes = List<int>.generate(16, (_) => _random.nextInt(256));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  final hex = bytes.map((b) => b.toRadixString(16).padLeft(2, '0')).join();
  return '${hex.substring(0, 8)}-${hex.substring(8, 12)}-'
      '${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}
