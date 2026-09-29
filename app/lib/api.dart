import 'dart:convert';

import 'package:http/http.dart' as http;

/// Set at build time: `--dart-define=API_BASE_URL=https://...`.
const apiBaseUrl = String.fromEnvironment('API_BASE_URL');

/// The signed-in account as the API sees it.
class Me {
  const Me({required this.accountId, required this.email});

  final String accountId;
  final String email;
}

/// An error from the API, read from its `{"error": {"code", "message"}}` envelope.
class ApiException implements Exception {
  const ApiException(this.statusCode, this.code, this.message);

  final int statusCode;
  final String code;
  final String message;

  @override
  String toString() => 'ApiException($statusCode, $code): $message';
}

class WobotApi {
  WobotApi({http.Client? client, this.baseUrl = apiBaseUrl})
    : _client = client ?? http.Client();

  final http.Client _client;
  final String baseUrl;

  /// Registers the signed-in account with the API and returns it.
  Future<Me> me(String idToken) async {
    final response = await _client
        .get(
          Uri.parse('$baseUrl/v1/me'),
          headers: {'Authorization': 'Bearer $idToken'},
        )
        // Long enough for a cold start of a scaled-to-zero service.
        .timeout(const Duration(seconds: 30));
    final body = _decode(response.body);
    if (response.statusCode == 200 && body != null) {
      return Me(
        accountId: body['account_id'] as String,
        email: body['email'] as String,
      );
    }
    final error = body?['error'];
    if (error is Map<String, dynamic>) {
      throw ApiException(
        response.statusCode,
        error['code'] as String,
        error['message'] as String,
      );
    }
    throw ApiException(
      response.statusCode,
      'UNEXPECTED_RESPONSE',
      'Unexpected response (HTTP ${response.statusCode}).',
    );
  }

  static Map<String, dynamic>? _decode(String body) {
    try {
      final value = jsonDecode(body);
      return value is Map<String, dynamic> ? value : null;
    } on FormatException {
      return null;
    }
  }
}
