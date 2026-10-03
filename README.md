<img width="1101" height="1153" alt="image" src="https://github.com/user-attachments/assets/407af2bb-3110-4e65-9650-4dda2683f59a" /># MiSTer ROM Sync

Windows app to identify ROMs (DAT CRC / extension / headers), plan paths for MiSTer `games/`, and copy files to your MiSTer library.

**Current version:** [0.1.0](https://github.com/neewb20221/mister_rom_sync/releases/tag/v0.1.0)

## Download

Download **`MiSTerRomSync.exe`** from the latest [Release](https://github.com/neewb20221/mister_rom_sync/releases/latest).

Put the exe in any folder. On first run it creates next to itself: `dats\`, `logs\`, and `config_mister_rom_sync.json`.

> Source code in this repository is for development. End users only need the exe from Releases.

## How to use

<img width="661" height="692" alt="image" src="https://github.com/user-attachments/assets/cee8d87d-0383-414e-be1f-d0e8c12153f4" />

1. Run **MiSTerRomSync.exe**.
2. Set **Source** (your ROM dump) and **Output** (MiSTer `games`, e.g. `\\MISTER\sdcard\games`).
3. Click **Update all DATs** (or open **DAT files…**) so the built-in catalogue can download.
4. **Scan Source + Output** → review the plan (Transfer / Unpack / platform).
5. **Run transfer** (use Test-run first if you want a dry run).

DAT files are not shipped inside the exe; they are downloaded from the default online sources when you update.

See [CHANGELOG.md](CHANGELOG.md) for what changed between versions.

<p>
  <a href="https://donatepay.ru/don/neewb20221">
    <img src="assets/donate_qr.png" alt="Donate QR" width="120">
  </a>
</p>

## Support

If the tool helps you: [DonatePay](https://donatepay.ru/don/neewb20221)

## Acknowledgments

Thanks to the people and projects behind the default DAT catalogues this app updates from:

- **[MiSTer Organize](https://github.com/MiSTerOrganize/MiSTer_Organize)** — path-oriented DAT packs aligned with MiSTer `games/` layouts
- **[No-Intro](https://datomatic.no-intro.org/)** — console ROM DAT standards
- **[Redump](http://redump.org/)** — optical disc dump DAT standards
- **[libretro-database](https://github.com/libretro/libretro-database)** — public No-Intro / Redump DAT mirror used for updates

And to the **[MiSTer FPGA](https://github.com/MiSTer-devel)** community. App icon uses **MiSTer-kun** (hewhoisred) / logo (Conrad Fenech) from [MkDocs_MiSTer](https://github.com/MiSTer-devel/MkDocs_MiSTer).

DAT files remain under their respective owners’ terms; this app only downloads them for local matching.

## License

[MIT](LICENSE)
