# Third-party components / Composants tiers

Apache-2.0 applies to the original Nexus Audio code, not to the independent audio engines or their dependencies. This release distributes Nexus Audio source. The Dockerfile downloads and builds upstream components; it does not relicense them.

| Component | Version/source | License/attribution |
| --- | --- | --- |
| aiosendspin | 9.1.0, https://pypi.org/project/aiosendspin/9.1.0/ | Apache-2.0; upstream license included |
| audioop-lts | 0.2.2, https://pypi.org/project/audioop-lts/0.2.2/ | PSF-2.0; compatibility module for Python 3.13+ |
| librespot | v0.6.0, commit `383a6f6969f23b3e3cbc693747101cb9c92463dc`, https://github.com/librespot-org/librespot | MIT, Copyright (c) 2015 Paul Lietar; license included |
| Inferno / inferno2pipe | commit `9abfab97c609bd8a5a3a4cae21b919972fd44937`, https://github.com/salanki/inferno | Upstream offers GPL-3.0-or-later OR AGPL-3.0-or-later. GPL-3.0-or-later selected here; Copyright (C) 2023–2025 Teodor Wozniak; upstream notice and GPL text included |
| Shairport Sync | Alpine package, https://github.com/mikebrady/shairport-sync | Multiple upstream notices; LICENSES included. Copyright James Laird, Mike Brady and other contributors |
| FFmpeg and system packages | Alpine repositories, https://ffmpeg.org/legal.html | Each package retains its own license, including LGPL/GPL components according to build configuration |
| Home Assistant base | 2026.08.0, https://github.com/home-assistant/docker-base | Upstream base image and bundled packages retain their own licenses |

Statime and Spin2Dante are external services and are not bundled by Nexus Audio. Their upstream licenses continue to apply.

The exact Inferno and librespot source commits are recorded in the Dockerfile. If redistributing built container images/binaries, provide the corresponding source and required license notices for those components and their dependencies. This source prerelease does not publish a prebuilt container image.

FR : les moteurs tiers conservent leurs licences. Statime et Spin2Dante restent externes. Une redistribution d'images compilées doit également fournir les sources correspondantes et mentions requises ; cette préversion publie le code source de Nexus Audio.
