import 'package:flutter/material.dart';

import '../api.dart';
import '../chat.dart';
import '../theme.dart';
import '../widgets/thinking.dart';

/// The account's chats, most recently active first: open one, start one, delete one.
class HistoryDrawer extends StatelessWidget {
  const HistoryDrawer({
    super.key,
    required this.history,
    required this.chat,
    required this.email,
    required this.onSettings,
  });

  final HistoryController history;
  final ChatController chat;
  final String email;
  final VoidCallback onSettings;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Drawer(
      shape: const RoundedRectangleBorder(),
      child: SafeArea(
        child: ListenableBuilder(
          listenable: Listenable.merge([history, chat]),
          builder: (context, _) => Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 16, 20, 8),
                child: Row(
                  children: [
                    const ThinkingMark(size: 22, animate: false),
                    const SizedBox(width: 10),
                    Text(
                      'Wobot',
                      style: text.titleLarge?.copyWith(
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
                child: ListTile(
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(12),
                  ),
                  leading: const Icon(
                    Icons.edit_square,
                    color: WobotColors.accent,
                  ),
                  title: const Text('New chat'),
                  onTap: () {
                    Navigator.of(context).pop();
                    chat.startNew();
                  },
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(28, 12, 20, 4),
                child: Text(
                  'Recents',
                  style: text.labelMedium?.copyWith(color: WobotColors.faint),
                ),
              ),
              Expanded(child: _list(context)),
              const Divider(),
              ListTile(
                contentPadding: const EdgeInsets.symmetric(horizontal: 20),
                leading: CircleAvatar(
                  radius: 16,
                  backgroundColor: WobotColors.raised,
                  child: Text(
                    email.isEmpty ? '?' : email[0].toUpperCase(),
                    style: const TextStyle(color: WobotColors.accent),
                  ),
                ),
                title: Text(email, overflow: TextOverflow.ellipsis),
                trailing: const Icon(Icons.settings_outlined),
                onTap: () {
                  Navigator.of(context).pop();
                  onSettings();
                },
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _list(BuildContext context) {
    final chats = history.conversations;
    if (chats.isEmpty) {
      final note = history.loading
          ? null
          : history.error ?? 'Your chats will appear here.';
      return Center(
        child: note == null
            ? const ThinkingMark(size: 22)
            : Padding(
                padding: const EdgeInsets.all(24),
                child: Text(
                  note,
                  textAlign: TextAlign.center,
                  style: const TextStyle(color: WobotColors.muted),
                ),
              ),
      );
    }
    return RefreshIndicator(
      color: WobotColors.accent,
      onRefresh: history.refresh,
      child: ListView.builder(
        padding: const EdgeInsets.symmetric(horizontal: 12),
        itemCount: chats.length + (history.hasMore ? 1 : 0),
        itemBuilder: (context, i) {
          if (i == chats.length) {
            return TextButton(
              onPressed: history.loading ? null : history.loadMore,
              child: const Text('Show more'),
            );
          }
          final c = chats[i];
          return ListTile(
            dense: true,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(12),
            ),
            selected: c.id == chat.conversation?.id,
            selectedTileColor: WobotColors.raised,
            selectedColor: WobotColors.text,
            title: Text(
              c.title ?? 'New chat',
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: const TextStyle(fontSize: 15),
            ),
            subtitle: Text(
              when(c.updatedAt),
              style: const TextStyle(color: WobotColors.faint),
            ),
            onTap: () {
              Navigator.of(context).pop();
              chat.open(c);
            },
            onLongPress: () => _confirmDelete(context, c),
          );
        },
      ),
    );
  }

  Future<void> _confirmDelete(BuildContext context, Conversation c) async {
    final messenger = ScaffoldMessenger.of(context);
    final delete = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('Delete chat?'),
        content: Text(
          '"${c.title ?? 'New chat'}" and its messages will be deleted. '
          'This cannot be undone.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(false),
            child: const Text(
              'Cancel',
              style: TextStyle(color: WobotColors.text),
            ),
          ),
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(true),
            child: const Text(
              'Delete',
              style: TextStyle(color: WobotColors.danger),
            ),
          ),
        ],
      ),
    );
    if (delete != true) return;
    try {
      if (chat.conversation?.id == c.id) await chat.startNew();
      await history.delete(c.id);
    } on Exception catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(describe(e))));
    }
  }
}

/// When a chat was last active, as a list of chats says it.
String when(DateTime time, {DateTime? now}) {
  final local = time.toLocal();
  final today = now ?? DateTime.now();
  String two(int n) => n.toString().padLeft(2, '0');
  final days = DateTime(
    today.year,
    today.month,
    today.day,
  ).difference(DateTime(local.year, local.month, local.day)).inDays;
  if (days == 0) return '${two(local.hour)}:${two(local.minute)}';
  if (days == 1) return 'Yesterday';
  if (local.year == today.year) return '${local.month}/${local.day}';
  return '${local.year}/${local.month}/${local.day}';
}
