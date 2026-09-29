import 'dart:async';

import 'package:firebase_auth/firebase_auth.dart';
import 'package:flutter/material.dart';

import 'api.dart';
import 'auth.dart';

class HomePage extends StatefulWidget {
  const HomePage({super.key});

  @override
  State<HomePage> createState() => _HomePageState();
}

class _HomePageState extends State<HomePage> {
  final _api = WobotApi();
  late final StreamSubscription<User?> _authChanges;
  User? _user;
  Future<Me>? _me;
  bool _signingIn = false;
  String? _signInError;

  @override
  void initState() {
    super.initState();
    // Also fires at launch, when Firebase restores the previous session.
    _authChanges = FirebaseAuth.instance.authStateChanges().listen((user) {
      setState(() {
        _user = user;
        _me = user == null ? null : _loadMe(user);
      });
    });
  }

  @override
  void dispose() {
    _authChanges.cancel();
    super.dispose();
  }

  Future<Me> _loadMe(User user) async {
    final idToken = await user.getIdToken();
    if (idToken == null) throw StateError('Firebase returned no ID token.');
    return _api.me(idToken);
  }

  Future<void> _signIn() async {
    setState(() {
      _signingIn = true;
      _signInError = null;
    });
    String? error;
    try {
      await signInWithGoogle();
    } on Exception catch (e) {
      error = 'Sign-in failed: $e';
    }
    if (!mounted) return;
    setState(() {
      _signingIn = false;
      _signInError = error;
    });
  }

  @override
  Widget build(BuildContext context) {
    final user = _user;
    return Scaffold(
      appBar: AppBar(
        title: const Text('Wobot'),
        actions: [
          if (user != null)
            TextButton(onPressed: signOut, child: const Text('Sign out')),
        ],
      ),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: user == null ? _signedOut() : _signedIn(user),
        ),
      ),
    );
  }

  Widget _signedOut() {
    if (apiBaseUrl.isEmpty) {
      return const Text(
        'API_BASE_URL is not set. Run with --dart-define=API_BASE_URL=<url>.',
        textAlign: TextAlign.center,
      );
    }
    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        FilledButton(
          onPressed: _signingIn ? null : _signIn,
          child: const Text('Sign in with Google'),
        ),
        if (_signInError case final error?) ...[
          const SizedBox(height: 16),
          Text(error, textAlign: TextAlign.center),
        ],
      ],
    );
  }

  Widget _signedIn(User user) {
    // Asks the API again, so an allowlist change shows up without signing out.
    void checkAgain() => setState(() => _me = _loadMe(user));

    return FutureBuilder<Me>(
      future: _me,
      builder: (context, snapshot) {
        if (snapshot.connectionState != ConnectionState.done) {
          return const CircularProgressIndicator();
        }
        if (snapshot.hasError) {
          final error = snapshot.error;
          return Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                error is ApiException
                    ? error.message
                    : 'Could not reach Wobot: $error',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 16),
              OutlinedButton(
                onPressed: checkAgain,
                child: const Text('Try again'),
              ),
            ],
          );
        }
        final me = snapshot.requireData;
        return Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text('Signed in as ${me.email}', textAlign: TextAlign.center),
            const SizedBox(height: 8),
            Text(
              'Account ${me.accountId}',
              style: Theme.of(context).textTheme.bodySmall,
            ),
            const SizedBox(height: 16),
            OutlinedButton(
              onPressed: checkAgain,
              child: const Text('Check again'),
            ),
          ],
        );
      },
    );
  }
}
