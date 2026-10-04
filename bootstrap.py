#!/usr/bin/env python3
"""Install the owner-local CLI router using the existing Codex login."""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import shutil
import sys
import tempfile

import manage


def native_binary(explicit: Path | None = None) -> Path:
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    candidate = explicit or (Path(shutil.which("codex")) if shutil.which("codex") else bundled)
    if not candidate.is_absolute():
        raise ValueError("--native must be an absolute path")
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ValueError("native Codex CLI missing; install Codex and sign in first")
    if manage.ROOT in candidate.resolve().parents:
        raise ValueError("Effortlane is already installed; use effortlane status")
    return candidate


def prepare_codex_link(bin_dir: Path, native: Path) -> bool:
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    link = bin_dir / "codex"
    if link.is_symlink():
        return False
    if link.exists():
        raise ValueError("~/.local/bin/codex is not a symlink; refusing to overwrite it")
    link.symlink_to(native)
    return True


def key_file(explicit: Path | None = None) -> Path:
    key = explicit or Path.home() / ".config/jev-codex-router/typesafe-api-key"
    if key.is_file():
        if key.stat().st_mode & 0o077:
            raise ValueError("TypeSafe key file must be owner-only (chmod 600)")
        return key
    if explicit:
        raise ValueError("TypeSafe key file does not exist")
    if not os.isatty(0):
        raise ValueError("TypeSafe key missing; run interactively or pass --key-file")
    value = getpass.getpass("TypeSafe/Jev API key (input hidden): ").strip()
    if not value or len(value) > 4096 or "\n" in value:
        raise ValueError("invalid TypeSafe key")
    manage.atomic_write(key, (value + "\n").encode(), 0o600)
    return key


def preflight() -> None:
    if sys.platform != "darwin":
        raise ValueError("macOS is required for Effortlane installation")
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required for Effortlane installation")


def existing_install(root: Path) -> bool:
    """Check an existing installation without changing its files or settings."""
    if not (root / "manifest.json").exists():
        return False
    try:
        result = manage.doctor(root)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        raise ValueError(
            "Effortlane is already installed, but its health check could not run: "
            f"{exc}. Run ~/.local/bin/effortlane doctor for details; do not reinstall over it."
        ) from exc
    if result.get("ok"):
        print("Effortlane is already installed; checks passed. No changes made.")
        return True
    issues = result.get("issues")
    detail = "; ".join(str(issue) for issue in issues) if issues else "an unknown check failed"
    raise ValueError(
        "Effortlane is already installed, but checks failed: " + detail
        + ". Run ~/.local/bin/effortlane doctor for details; fix the reported issue before reinstalling."
    )


def run(native: Path | None = None, key: Path | None = None) -> None:
    if existing_install(manage.ROOT):
        return
    preflight()
    selected = native_binary(native)
    config = manage.CODEX_CONFIG
    if not config.is_file():
        raise ValueError("Codex config missing; run native Codex and sign in first")
    auth = config.parent / "auth.json"
    if not auth.is_file():
        raise ValueError("Codex authentication missing; run native Codex and sign in first")
    selected_key = key_file(key)
    catalog = manage.fetch_account_catalog(selected, auth)
    bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    desktop_binary = bundled if bundled.is_file() and os.access(bundled, os.X_OK) else selected
    desktop_catalog = (manage.fetch_account_catalog(desktop_binary, auth)
                       if desktop_binary != selected else catalog)
    created_link = prepare_codex_link(manage.BIN, selected)
    try:
        with tempfile.TemporaryDirectory(prefix="jev-install-") as directory:
            cache = Path(directory) / "desktop-models.json"
            manage.write_json(cache, desktop_catalog)
            manage.install(cache_path=cache, execution_binary=desktop_binary, key_path=selected_key)
            if selected != desktop_binary:
                cli_cache = Path(directory) / "cli-models.json"
                manage.write_json(cli_cache, catalog)
                try:
                    manage.cli_set_target(selected)
                    manage.cli_refresh_catalog(catalog_path=cli_cache)
                except (OSError, ValueError, RuntimeError):
                    print("CLI catalog setup incomplete; native fallback remains available. "
                          "Run effortlane doctor and cli-refresh-models.", file=sys.stderr)
    except Exception:
        link = manage.BIN / "codex"
        if created_link and link.is_symlink() and os.readlink(link) == str(selected):
            link.unlink()
        raise
    policy = manage.load_json(manage.ROOT / "config.json")
    policy["mode"] = "auto"
    manage.write_json(manage.ROOT / "config.json", policy)
    print("Effortlane CLI Auto installed. Native ChatGPT/Codex login preserved.")
    print("Verify: ~/.local/bin/effortlane doctor")
    if Path(shutil.which("codex") or "").resolve() != (manage.BIN / "codex").resolve():
        print("Put ~/.local/bin before other Codex binaries in PATH, then open a new terminal.")
    print("Desktop stays native; experimental Desktop adapter is opt-in.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", type=Path, help="existing native Codex executable")
    parser.add_argument("--key-file", type=Path, help="owner-only TypeSafe/Jev key file")
    args = parser.parse_args()
    try:
        run(args.native, args.key_file)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, "Effortlane install: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
