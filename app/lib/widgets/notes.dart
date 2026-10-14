import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../theme.dart';

/// A boxed remark under a step: what happens to a photo or a recording.
class InfoNote extends StatelessWidget {
  const InfoNote(this.text, {super.key});

  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: WobotColors.surface,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(
        text,
        style: const TextStyle(
          color: WobotColors.muted,
          fontSize: 13,
          height: 1.5,
        ),
      ),
    );
  }
}

/// The numbered steps a page starts with.
class Steps extends StatelessWidget {
  const Steps(this.steps, {super.key});

  final List<String> steps;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Column(
      children: [
        for (final (i, step) in steps.indexed)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '${i + 1}',
                  style: const TextStyle(
                    color: WobotColors.accent,
                    fontWeight: FontWeight.w600,
                  ),
                ),
                const SizedBox(width: 14),
                Expanded(
                  child: Text(
                    step,
                    style: text.bodyLarge?.copyWith(color: WobotColors.muted),
                  ),
                ),
              ],
            ),
          ),
      ],
    );
  }
}

/// An error in the page's flow, with what to do about it.
class ErrorNote extends StatelessWidget {
  const ErrorNote(this.message, {super.key, this.action});

  final String message;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            message,
            style: const TextStyle(color: WobotColors.danger, height: 1.45),
          ),
          ?action,
        ],
      ),
    );
  }
}

/// iOS asks for the microphone or the camera once; after a refusal only Settings can
/// allow it.
class PermissionNote extends StatelessWidget {
  const PermissionNote(this.what, {super.key});

  final String what;

  @override
  Widget build(BuildContext context) {
    return ErrorNote(
      'Wobot may not use the $what. Allow it in Settings, then try again.',
      action: TextButton(
        style: TextButton.styleFrom(padding: EdgeInsets.zero),
        onPressed: () => launchUrl(Uri.parse('app-settings:')),
        child: const Text('Open Settings'),
      ),
    );
  }
}
