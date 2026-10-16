import 'package:shared_preferences/shared_preferences.dart';

/// The language questions are spoken in, for the phone's recognizer (DEC-092). Kept on
/// this phone, chosen in Settings, never on the Avatar screen (VOI-012).
abstract final class QuestionLanguage {
  static const chinese = 'zh-TW';
  static const english = 'en-US';
  static const _key = 'question_locale';

  /// Chinese until English is chosen, or when the setting cannot be read.
  static Future<String> read() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final locale = prefs.getString(_key);
      return locale == english ? english : chinese;
    } on Exception {
      return chinese;
    }
  }

  static Future<void> write(String locale) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_key, locale);
  }

  /// Forgets the choice, as signing out does: the next account starts in Chinese.
  static Future<void> clear() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_key);
  }
}
