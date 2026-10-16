import 'package:firebase_auth/firebase_auth.dart';
import 'package:google_sign_in/google_sign_in.dart';

import 'avatar/frame_cache.dart';
import 'voice/question_language.dart';

/// Signs in with Google and exchanges the Google ID token for a Firebase session.
///
/// Returns false when the user cancels.
Future<bool> signInWithGoogle() async {
  final credential = await _googleCredential();
  if (credential == null) return false;
  await FirebaseAuth.instance.signInWithCredential(credential);
  return true;
}

/// Signs in with Google again, as the account already signed in, so the next ID token
/// says the sign-in is recent: deleting the account asks for one (spec 04).
///
/// Returns false when the user cancels.
Future<bool> signInAgain() async {
  final user = FirebaseAuth.instance.currentUser;
  if (user == null) return false;
  final credential = await _googleCredential();
  if (credential == null) return false;
  await user.reauthenticateWithCredential(credential);
  return true;
}

Future<AuthCredential?> _googleCredential() async {
  final GoogleSignInAccount account;
  try {
    account = await GoogleSignIn.instance.authenticate();
  } on GoogleSignInException catch (e) {
    if (e.code == GoogleSignInExceptionCode.canceled) return null;
    rethrow;
  }
  return GoogleAuthProvider.credential(idToken: account.authentication.idToken);
}

/// Signs out of Firebase and Google. Whatever the account had on screen goes with
/// it: its widgets are keyed by the account. What this phone kept for it goes too.
Future<void> signOutAll() async {
  await _forgetOnPhone();
  await FirebaseAuth.instance.signOut();
  await GoogleSignIn.instance.signOut();
}

/// Deletes the Firebase user once Wobot has deleted the account, then signs out. A
/// user that cannot be deleted, such as when the sign-in is no longer recent, is only
/// signed out: Wobot refuses its account from now on all the same.
Future<void> deleteFirebaseUser() async {
  try {
    await FirebaseAuth.instance.currentUser?.delete();
  } on FirebaseAuthException {
    // Signed out below; the account is already deleted on Wobot's side.
  }
  await signOutAll();
}

Future<void> _forgetOnPhone() async {
  final accountId = FirebaseAuth.instance.currentUser?.uid;
  try {
    if (accountId != null) await FileFrameCache(accountId).clear();
    await QuestionLanguage.clear();
  } on Exception {
    // Signing out goes on; the frames are the account's own, under its ID.
  }
}
