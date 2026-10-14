import 'package:flutter/material.dart';

/// Wobot's look: warm dark grays, light text, and orange only for what acts or is
/// alive: the send button, links, the thinking mark.
abstract final class WobotColors {
  static const background = Color(0xFF1F1E1C);
  static const sidebar = Color(0xFF191817);
  static const surface = Color(0xFF2B2A27);
  static const raised = Color(0xFF363431);
  static const outline = Color(0xFF423F3B);
  static const text = Color(0xFFF2F0EB);
  static const muted = Color(0xFFA29F97);
  static const faint = Color(0xFF76736C);
  static const accent = Color(0xFFE07A3F);
  static const danger = Color(0xFFE8746A);
}

ThemeData wobotTheme() {
  const scheme = ColorScheme.dark(
    primary: WobotColors.accent,
    onPrimary: Color(0xFF1A0E06),
    secondary: WobotColors.accent,
    onSecondary: Color(0xFF1A0E06),
    surface: WobotColors.background,
    onSurface: WobotColors.text,
    onSurfaceVariant: WobotColors.muted,
    surfaceContainerLowest: WobotColors.sidebar,
    surfaceContainerLow: WobotColors.sidebar,
    surfaceContainer: WobotColors.surface,
    surfaceContainerHigh: WobotColors.surface,
    surfaceContainerHighest: WobotColors.raised,
    outline: WobotColors.outline,
    outlineVariant: WobotColors.outline,
    error: WobotColors.danger,
    onError: Color(0xFF1A0E06),
  );
  final base = ThemeData(colorScheme: scheme, useMaterial3: true);
  final text = base.textTheme.apply(
    bodyColor: WobotColors.text,
    displayColor: WobotColors.text,
  );
  return base.copyWith(
    scaffoldBackgroundColor: WobotColors.background,
    textTheme: text.copyWith(
      bodyLarge: text.bodyLarge?.copyWith(fontSize: 16, height: 1.55),
      bodyMedium: text.bodyMedium?.copyWith(height: 1.45),
    ),
    appBarTheme: AppBarTheme(
      backgroundColor: WobotColors.background,
      surfaceTintColor: Colors.transparent,
      scrolledUnderElevation: 0,
      elevation: 0,
      centerTitle: true,
      titleTextStyle: text.titleMedium?.copyWith(
        fontWeight: FontWeight.w600,
        color: WobotColors.text,
      ),
      iconTheme: const IconThemeData(color: WobotColors.text),
    ),
    drawerTheme: const DrawerThemeData(
      backgroundColor: WobotColors.sidebar,
      surfaceTintColor: Colors.transparent,
    ),
    dividerTheme: const DividerThemeData(
      color: WobotColors.outline,
      thickness: 0.5,
      space: 1,
    ),
    listTileTheme: const ListTileThemeData(
      iconColor: WobotColors.muted,
      textColor: WobotColors.text,
    ),
    dialogTheme: const DialogThemeData(
      backgroundColor: WobotColors.raised,
      surfaceTintColor: Colors.transparent,
    ),
    bottomSheetTheme: const BottomSheetThemeData(
      backgroundColor: WobotColors.raised,
      surfaceTintColor: Colors.transparent,
      showDragHandle: true,
    ),
    snackBarTheme: SnackBarThemeData(
      behavior: SnackBarBehavior.floating,
      backgroundColor: WobotColors.raised,
      contentTextStyle: text.bodyMedium,
    ),
    textSelectionTheme: TextSelectionThemeData(
      cursorColor: WobotColors.accent,
      selectionColor: WobotColors.accent.withValues(alpha: 0.35),
      selectionHandleColor: WobotColors.accent,
    ),
    inputDecorationTheme: const InputDecorationTheme(
      hintStyle: TextStyle(color: WobotColors.faint),
      border: InputBorder.none,
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        backgroundColor: WobotColors.accent,
        foregroundColor: scheme.onPrimary,
        textStyle: const TextStyle(fontWeight: FontWeight.w600),
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(foregroundColor: WobotColors.accent),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: WobotColors.text,
        side: const BorderSide(color: WobotColors.outline),
      ),
    ),
  );
}
