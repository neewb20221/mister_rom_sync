# Changelog

All notable changes are documented here. Release downloads: [GitHub Releases](https://github.com/neewb20221/mister_rom_sync/releases).

## [0.2.0] - 2026-10-09

Scan, CRC, and UI improvements for large MiSTer libraries over Samba.

- Dual progress bars: Current (files/CRC) and Overall (phase)
- CRC for all files; hybrid workers — full pool for small files, serial CRC for large (>=32 MB)
- SMB stall detect / CancelIoEx; live Current progress while hashing
- Primary MiSTer folder from official docs (MegaDrive over Genesis, TGFX16, NES over LightGun/2P, …)
- Output match: list entire Output, cheap size/name filter, CRC only hits (finds Genesis copies under games/)
- Source=Output reuses listing/CRCs where possible
- Actions panel beside Options; tighter Methods/Options layout
- DAT identify: CRC-first (SHA-1 only on miss for smaller files)

## [0.1.0] - 2026-10-03

First packaged release.

- Scan / plan / transfer ROMs into MiSTer `games/`
- DAT matching (MiSTer Organize + No-Intro / Redump via libretro-database)
- Built-in default DAT catalogue before first update
- CRC cache, smart Unpack defaults, CD cue-set path coalescing
- About dialog with version, donate (DonatePay), Latest Releases link
- Windows standalone build packaged as `MiSTerRomSync-<version>-windows.zip`
