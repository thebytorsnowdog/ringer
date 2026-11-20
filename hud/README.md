# Ringside

The native mission-control HUD for [Ringer](../README.md). Tauri v2 — one codebase builds macOS, Windows, and Linux.

## Build

```bash
cargo install tauri-cli --locked   # once
cargo tauri build                  # bundle lands in target/release/bundle/
cargo tauri dev                    # live-reload dev mode
```

The frontend is shared with the browser: `dashboard/ringside.html`, `dashboard/ringside.css`, `dashboard/ringside.js`, and `dashboard/assets/`, plus the native transport in `frontend/hud.js`. Both `scripts/sync-dist.sh` and `build.rs` copy these sources before embedding them. Edit the sources, never `dist/`. Native Models and Open folder use the local browser HUD; keep `ringer.py hud` running for those actions.

## Linux GLib backport

Tauri's GTK 0.18 stack requires glib 0.18. The Cargo patch uses the crates.io `glib-0.18.5` archive (SHA-256 `233daaf6e83ae6a12a52055f568f9d7cf4671dabb78ff9560ab6da230ce00ee5`) with the two-line fix for RUSTSEC-2024-0429 applied to `src/variant_iter.rs`. The crate retains its upstream version, so version-only scans may still report the advisory. Remove the override once the Linux dependency tree resolves entirely to a fixed upstream release. [gtk-rs-core #1343](https://github.com/gtk-rs/gtk-rs-core/pull/1343)

Run `cargo test --locked --release --test ringer_rust` on Linux to check the string iterator with serialized GLib fixtures under optimization.

## Behavior

- Frameless, always-on-top, visible on all Spaces/desktops; drag anywhere that isn't a control.
- Watches the Ringer state dir (`~/.ringer/runs/`, or `state_dir` from `~/.config/ringer/config.toml`) and renders every swarm: live, finished, and died (orchestrator gone without finishing).
- Tray icon: show/hide, version, quit. Closing the window hides it; the app stays in the tray.
- macOS-style close control top-left; ESC also hides.
