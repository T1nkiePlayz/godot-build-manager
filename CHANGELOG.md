# Changelog

## 1.1.0

First public release.

- **Editor snapshots**: every successful editor build is kept; switch the launcher to any earlier build and back.
- **Update checks after opening Godot**: a background fetch with a desktop notification when upstream has new changes.
- **Readable changelog**: incoming changes shown as pull request titles, grouped by source area, with watched areas highlighted.
- **Build history**: duration chart per build step, ccache hit rates, and time-left estimates while building.
- **Encryption key backup**: passphrase-protected (GnuPG, AES-256) backup, verify and restore.
- **Storage page**: sizes of caches, objects, snapshots and installed templates, with cleanup.
- **First-run setup**: choose a Godot checkout or clone one from GitHub; all paths configurable.
- Host architecture detection (x86_64 and arm64 Linux hosts), clang or GCC.

## 1.0.0

- GTK 4 / libadwaita front end for `godot_build.py`: editor and export template builds, PCK encryption,
  live build activity, logs, diagnostics, settings and desktop integration.
