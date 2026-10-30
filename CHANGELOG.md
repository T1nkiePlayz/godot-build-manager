# Changelog

## 1.2.0

- **Toolchain installer** (Diagnostics → Cross-Compile Toolchains, `godot-build toolchain`):
  - llvm-mingw for Windows, downloaded from its latest release, followed by Godot's Windows dependencies.
  - osxcross for macOS, built from an Xcode `.xip` or a packaged `MacOSX*.sdk.tar.xz`.
  - MoltenVK, so macOS templates can use Vulkan.
- **Vulkan on macOS** setting: MoltenVK is linked into macOS templates automatically once installed.
- Export Templates shows **Set Up Toolchains…** when Windows or macOS is unavailable.
- Diagnostics explain how to install a missing toolchain instead of only asking for an environment variable.
- The osxcross installer names any missing build tools, with the install command for your distribution.

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
