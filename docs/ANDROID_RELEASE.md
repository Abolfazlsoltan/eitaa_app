# Android release checklist

This project has two different APK paths:

- The regular **Build APK** workflow is for CI validation. Without signing secrets it may produce a debug-signed APK; do not distribute it as a production release.
- **Signed Android Release** is the release path. It runs tests, checks that the Git tag matches `pyproject.toml`, requires a configured release keystore, builds the APK and attaches it to a GitHub Release.

## One-time GitHub setup

Add these repository Actions secrets before attempting a release:

- `ANDROID_KEYSTORE_BASE64`: base64-encoded Android keystore file.
- `ANDROID_KEYSTORE_PASSWORD`: keystore password.
- `ANDROID_KEY_ALIAS`: key alias inside the keystore.
- `ANDROID_KEY_PASSWORD`: password for the key.

Keep a secure offline backup of the keystore and passwords. Losing the signing key can prevent in-place updates for users of an already-published APK. Never commit the keystore or passwords to Git.

## Before publishing a version

1. Confirm the backend is deployed over HTTPS and its environment variables use production-only secrets.
2. Revoke and rotate any credentials that may have been committed in the past. Removing a secret from the current tree does not erase it from Git history.
3. Test sign-in, bot connection, channel permissions, publishing, media upload, schedule execution, retry/error handling and account roles on a real Android device.
4. Confirm privacy disclosures accurately describe AI-provider requests, local storage and backend data retention.
5. Increment `version` in both `pyproject.toml` and `src/config.py` to the same release version.
6. Push a tag that exactly matches the project version, for example `v2.2.2`. The release workflow intentionally fails if the versions do not match.
7. Install the resulting APK on a clean device and as an update over the prior signed version. Verify the installed package, signature and critical workflows before announcing the release.

## Current limitations to verify

- A green CI run proves automated tests and build steps passed; it does not prove the app works on every device.
- Local scheduling only runs while the app process is active. Reliable scheduling while the app is closed requires a healthy deployed backend worker.
- Public AI endpoints and third-party Eitaa methods can change or be rate-limited. Do not report an operation as successful until the provider confirms it.
- The release workflow cannot create a production backend, provision API credentials, or perform physical-device QA on your behalf.
