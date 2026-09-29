import 'package:firebase_auth/firebase_auth.dart';
import 'package:google_sign_in/google_sign_in.dart';

/// Signs in with Google and exchanges the Google ID token for a Firebase session.
///
/// Returns false when the user cancels.
Future<bool> signInWithGoogle() async {
  final GoogleSignInAccount account;
  try {
    account = await GoogleSignIn.instance.authenticate();
  } on GoogleSignInException catch (e) {
    if (e.code == GoogleSignInExceptionCode.canceled) return false;
    rethrow;
  }
  final credential = GoogleAuthProvider.credential(
    idToken: account.authentication.idToken,
  );
  await FirebaseAuth.instance.signInWithCredential(credential);
  return true;
}

Future<void> signOut() async {
  await FirebaseAuth.instance.signOut();
  await GoogleSignIn.instance.signOut();
}
