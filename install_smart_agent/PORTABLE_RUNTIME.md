# Portable runtime

Release builds may include `.venv/portable-runtime/manifest.json` and `runtime.zip.partNNN` files. `install_smart_agent/launchers/ensure_portable_runtime.bat` verifies every part and the aggregate archive before first extraction into a fresh payload-only `.venv`. It never replaces an existing runtime. A newer immutable payload requires extracting the release into a new directory. The portable runtime needs an existing Chrome or Edge browser for WebDirect.

Build from a clean builder using `build_portable_runtime.py --root <release> --build-root <release>\localdata\portable_build`. The build downloads the official CPython 3.13.16 x64 embedded archive and binary wheels, then writes immutable, hash-verified payload files. The normal full installer remains the online installation path; this bootstrap is used by a release that includes the payload.
