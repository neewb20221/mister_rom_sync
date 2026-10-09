# Changelog

All notable changes are documented here. Release downloads: [GitHub Releases](https://github.com/neewb20221/mister_rom_sync/releases).

## [0.2.0] - 2026-10-09

- Dual progress bars: current operation and overall scan/transfer
- Faster / safer identify: CRC-first matching, parallel workers, serial hashing for large files
- Primary MiSTer folder chosen from official core paths (e.g. MegaDrive over Genesis)
- Output matching lists the full library with cheap size/name filters, then CRC only for hits
- UI: Actions beside Options; tighter Methods/Options layout

## [0.1.0] - 2026-10-03

First packaged release.

- Scan / plan / transfer ROMs into MiSTer `games/`
- DAT matching (MiSTer Organize + No-Intro / Redump via libretro-database)
- Built-in default DAT catalogue before first update
- CRC cache, smart Unpack defaults, CD cue-set path coalescing
- About dialog with version, donate (DonatePay), Latest Releases link
- Windows standalone build packaged as `MiSTerRomSync-<version>-windows.zip`
