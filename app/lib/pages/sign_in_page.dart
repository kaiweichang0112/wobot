import 'package:flutter/material.dart';

import '../api.dart';
import '../auth.dart';
import '../theme.dart';
import '../widgets/thinking.dart';

class SignInPage extends StatefulWidget {
  const SignInPage({super.key});

  @override
  State<SignInPage> createState() => _SignInPageState();
}

class _SignInPageState extends State<SignInPage> {
  bool _signingIn = false;
  String? _error;

  Future<void> _signIn() async {
    setState(() {
      _signingIn = true;
      _error = null;
    });
    String? error;
    try {
      await signInWithGoogle();
    } on Exception {
      error = 'Sign-in did not finish. Try again.';
    }
    if (!mounted) return;
    setState(() {
      _signingIn = false;
      _error = error;
    });
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 32),
          child: Column(
            children: [
              const Spacer(flex: 3),
              const ThinkingMark(size: 52, animate: false),
              const SizedBox(height: 24),
              Text(
                'Wobot',
                style: text.headlineMedium?.copyWith(
                  fontWeight: FontWeight.w600,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'Ask about GRC, its products and research, or anything else.',
                textAlign: TextAlign.center,
                style: text.bodyLarge?.copyWith(color: WobotColors.muted),
              ),
              const Spacer(flex: 4),
              if (apiBaseUrl.isEmpty)
                Text(
                  'API_BASE_URL is not set. Run with '
                  '--dart-define=API_BASE_URL=<url>.',
                  textAlign: TextAlign.center,
                  style: text.bodyMedium?.copyWith(color: WobotColors.danger),
                )
              else
                SizedBox(
                  width: double.infinity,
                  height: 52,
                  child: FilledButton.icon(
                    onPressed: _signingIn ? null : _signIn,
                    icon: _signingIn
                        ? const SizedBox.square(
                            dimension: 18,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          )
                        : const Icon(Icons.login),
                    label: const Text('Continue with Google'),
                    style: FilledButton.styleFrom(
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(14),
                      ),
                    ),
                  ),
                ),
              if (_error case final error?) ...[
                const SizedBox(height: 16),
                Text(
                  error,
                  textAlign: TextAlign.center,
                  style: text.bodyMedium?.copyWith(color: WobotColors.danger),
                ),
              ],
              const SizedBox(height: 48),
            ],
          ),
        ),
      ),
    );
  }
}
