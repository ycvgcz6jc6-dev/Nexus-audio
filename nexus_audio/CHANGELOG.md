# Changelog

## 1.0.0-rc3

- Preserve AirPlay, Spotify Connect, native Dante RX, AES67, FR/EN interface, source controls, watchdog, diagnostics and the inactive experimental Cast path.
- Import RC2 diagnostic logging and SAP/SDP cleanup; RC3 Spotify 44.1 → 48 kHz normalization.
- Fix Sendspin 9.1 API imports, Spotify autoplay arguments and partial PCM-frame handling.
- Redact pairing credentials in exported source diagnostics.
- Package as a Home Assistant repository, include Apache-2.0 and third-party attribution, and supply a default base image and Python audioop compatibility.
- Hardware audio, PTP and Music Assistant acceptance testing remain required; this is a prerelease.
