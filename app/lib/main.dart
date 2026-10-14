import 'package:firebase_auth/firebase_auth.dart';
import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:google_sign_in/google_sign_in.dart';

import 'api.dart';
import 'auth.dart';
import 'chat.dart';
import 'firebase_options.dart';
import 'pages/chat_page.dart';
import 'pages/sign_in_page.dart';
import 'theme.dart';
import 'widgets/thinking.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final options = DefaultFirebaseOptions.currentPlatform;
  await Firebase.initializeApp(options: options);
  // The iOS OAuth client comes from the generated Firebase options, so no client
  // ID is written into Info.plist.
  await GoogleSignIn.instance.initialize(clientId: options.iosClientId);
  runApp(const WobotApp());
}

class WobotApp extends StatelessWidget {
  const WobotApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Wobot',
      debugShowCheckedModeBanner: false,
      theme: wobotTheme(),
      home: StreamBuilder<User?>(
        // Also fires at launch, when Firebase restores the previous session.
        stream: FirebaseAuth.instance.authStateChanges(),
        builder: (context, snapshot) {
          if (snapshot.connectionState == ConnectionState.waiting) {
            return const _Waiting();
          }
          final user = snapshot.data;
          if (user == null || apiBaseUrl.isEmpty) return const SignInPage();
          return SignedIn(key: ValueKey(user.uid), user: user);
        },
      ),
    );
  }
}

/// A signed-in account: its API client, what the API knows of it, and its chats. All
/// of it goes when the account signs out (AC-IAM-03).
class SignedIn extends StatefulWidget {
  const SignedIn({super.key, required this.user});

  final User user;

  @override
  State<SignedIn> createState() => _SignedInState();
}

class _SignedInState extends State<SignedIn> {
  late final WobotApi _api = WobotApi(
    token: ({bool refresh = false}) async {
      // Cached until near its expiry; a refused one is replaced once.
      final token = await widget.user.getIdToken(refresh);
      if (token == null) throw StateError('Firebase returned no ID token.');
      return token;
    },
  );
  late final HistoryController _history = HistoryController(_api);
  late final ChatController _chat = ChatController(_api, _history);
  late Future<Me> _me = _api.me();

  @override
  void initState() {
    super.initState();
    _history.refresh();
  }

  @override
  void dispose() {
    _chat.dispose();
    _history.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<Me>(
      future: _me,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return const _Waiting();
        }
        if (snapshot.error case final error?) {
          return _Refused(
            message: describe(error),
            tryAgain: () => setState(() => _me = _api.me()),
          );
        }
        return ChatPage(
          api: _api,
          me: snapshot.requireData,
          idToken: widget.user.getIdToken,
          chat: _chat,
          history: _history,
        );
      },
    );
  }
}

class _Waiting extends StatelessWidget {
  const _Waiting();

  @override
  Widget build(BuildContext context) {
    return const Scaffold(body: Center(child: ThinkingMark(size: 36)));
  }
}

/// The API refused the account, such as one not on the allowlist, or was unreachable.
class _Refused extends StatelessWidget {
  const _Refused({required this.message, required this.tryAgain});

  final String message;
  final VoidCallback tryAgain;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const ThinkingMark(size: 36, animate: false),
                const SizedBox(height: 24),
                Text(
                  message,
                  textAlign: TextAlign.center,
                  style: Theme.of(context).textTheme.bodyLarge,
                ),
                const SizedBox(height: 24),
                FilledButton(
                  onPressed: tryAgain,
                  child: const Text('Try again'),
                ),
                const SizedBox(height: 8),
                TextButton(
                  onPressed: () => signOutAll(),
                  child: const Text('Sign out'),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
