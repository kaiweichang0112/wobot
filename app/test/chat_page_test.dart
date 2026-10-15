import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:wobot/api.dart';
import 'package:wobot/chat.dart';
import 'package:wobot/pages/chat_page.dart';
import 'package:wobot/theme.dart';
import 'package:wobot/widgets/thinking.dart';

import 'fake_server.dart';

const _me = Me(
  accountId: 'uid-1',
  email: 'member@example.com',
  settings: AssistantSettings(chatbotName: 'Wobot', version: 0),
);

void main() {
  late FakeServer server;
  late StreamController<List<int>> reply;
  late HistoryController history;
  late ChatController chat;

  setUp(() {
    SharedPreferences.setMockInitialValues({});
    reply = StreamController<List<int>>();
    server = FakeServer()
      ..reply('GET /health', 200, '{"status": "ok"}')
      ..reply('POST /v1/conversations', 201, jsonEncode(newConversation));
    server.routes['POST /v1/conversations/c1/turns'] = () async =>
        http.StreamedResponse(reply.stream, 200);
    final api = server.api();
    history = HistoryController(api);
    chat = ChatController(api, history);
  });

  Future<void> show(WidgetTester tester) => tester.pumpWidget(
    MaterialApp(
      theme: wobotTheme(),
      home: ChatPage(
        api: server.api(),
        me: _me,
        idToken: () async => 'token',
        chat: chat,
        history: history,
      ),
    ),
  );

  void send(List<(String, Map<String, Object>)> events) =>
      reply.add(utf8.encode(sse(events)));

  testWidgets('a reply is awaited with the thinking mark, then streams in', (
    tester,
  ) async {
    await show(tester);
    expect(find.text('How can I help?'), findsOneWidget);

    await tester.enterText(find.byType(TextField), '你好');
    await tester.pump();
    await tester.tap(find.byTooltip('Send'));
    await tester.pump();
    await tester.pump();

    // Waiting: the mark breathes, the label says what is happening, Stop replaces Send.
    expect(find.text('你好'), findsOneWidget);
    expect(find.byType(ThinkingMark), findsOneWidget);
    expect(find.text('Thinking…'), findsOneWidget);
    expect(find.byTooltip('Stop'), findsOneWidget);

    send([
      ('accepted', {'turn_id': chat.turns.single.id, 'attempt': 1}),
      ('status', {'stage': 'searching'}),
    ]);
    await tester.pump();
    expect(find.text('Looking it up…'), findsOneWidget);

    send([
      ('text_delta', {'text': '你好，我是'}),
    ]);
    await tester.pump();
    expect(find.textContaining('你好，我是', findRichText: true), findsOneWidget);

    send([
      ('reply', {'text': '你好，我是 Wobot。產品網頁：https://example.com/p'}),
      ('title', {'title': '打招呼'}),
      ('completed', {}),
    ]);
    await reply.close();
    await tester.pump();
    await tester.pump();

    expect(
      find.textContaining('我是 Wobot。', findRichText: true),
      findsOneWidget,
    );
    expect(find.text('打招呼'), findsOneWidget); // the chat's title, in the bar
    expect(find.byTooltip('Copy'), findsOneWidget);
    expect(find.byTooltip('Stop'), findsNothing);
    expect(find.byType(ThinkingMark), findsNothing);
  });

  testWidgets('a tap outside the input puts the keyboard away', (tester) async {
    await show(tester);

    await tester.tap(find.byType(TextField));
    await tester.pump();
    expect(tester.testTextInput.isVisible, isTrue);
    await tester.tap(find.text('How can I help?'));
    await tester.pump();

    expect(tester.testTextInput.isVisible, isFalse);
  });

  testWidgets('Avatar mode first asks for what is not set up', (tester) async {
    server.reply(
      'GET /v1/me',
      200,
      meJson(
        voice: {
          'active': {'id': 'v1', 'status': 'active'},
          'candidate': null,
        },
      ),
    );
    await show(tester);

    await tester.tap(find.byTooltip('Avatar mode'));
    await tester.pumpAndSettle();

    expect(find.text('Avatar mode'), findsOneWidget);
    expect(find.text('Set up Avatar'), findsOneWidget);
    expect(find.text('Set up Voice'), findsNothing); // the voice is in use
  });

  testWidgets('settings hold the name, the voice and the avatar', (
    tester,
  ) async {
    server
      ..reply('GET /v1/me', 200, meJson())
      ..reply(
        'GET /v1/conversations',
        200,
        jsonEncode({'conversations': [], 'next_cursor': null}),
      );
    await show(tester);

    await tester.tap(find.byTooltip('Chats'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('member@example.com'));
    await tester.pumpAndSettle();

    expect(find.text('Settings'), findsOneWidget);
    expect(find.text('Name'), findsOneWidget);
    expect(find.text('Voice'), findsOneWidget);
    expect(find.text('Avatar'), findsOneWidget);
    expect(find.text('Not set up'), findsNWidgets(2));
    await tester.scrollUntilVisible(find.text('Question language'), 200);
    expect(find.text('中文'), findsOneWidget);
    await tester.scrollUntilVisible(find.text('Sign out'), 200);
    expect(find.text('Sign out'), findsOneWidget);
  });

  testWidgets('the drawer lists the chats', (tester) async {
    server.reply(
      'GET /v1/conversations',
      200,
      jsonEncode({
        'conversations': [
          {...newConversation, 'title': 'GRC 介紹'},
        ],
        'next_cursor': null,
      }),
    );
    await history.refresh();
    await show(tester);

    await tester.tap(find.byTooltip('Chats'));
    await tester.pumpAndSettle();

    expect(find.text('New chat'), findsOneWidget);
    expect(find.text('Recents'), findsOneWidget);
    expect(find.text('GRC 介紹'), findsOneWidget);
    expect(find.text('member@example.com'), findsOneWidget);
  });
}
