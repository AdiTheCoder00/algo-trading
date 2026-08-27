// Deployment-specific values: your relay address and its token.
//
// This is the template. `secrets.example.dart` is the committed template --
// copy it to `secrets.dart` and fill in your own values to build.
//
// The token ends up inside the built APK, which is acceptable for a personal
// build: it guards nothing but gold quotes, and the relay is only reachable over
// the tailnet. It must not end up in a public repository, which is the whole
// reason for this split.

const kRelayDefault = 'http://your-relay-host:8787';
const kRelayFallback = 'http://100.x.y.z:8787';
const kTokenDefault = '';
