#!/usr/bin/env python3
"""Owner-local Jev/Codex installation and conservative CLI launcher."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.request


from core import ALIASES, normalize_alias


ROOT = Path.home() / ".local/share/jev-codex-router"
BIN = Path.home() / ".local/bin"
CODEX_CONFIG = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
AGENT = Path.home() / "Library/LaunchAgents/com.local.jev-codex-router.plist"
LABEL = "com.local.jev-codex-router"
DESKTOP_AGENT = Path.home() / "Library/LaunchAgents/com.local.jev-codex-router.desktop-env.plist"
DESKTOP_LABEL = "com.local.jev-codex-router.desktop-env"
DESKTOP_PYTHON = Path("/opt/homebrew/bin/python3.14")
MANAGED_KEYS = ("model", "model_reasoning_effort", "openai_base_url", "model_catalog_json")
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z_0-9-]*)\s*=")
NATIVE_URL = "https://chatgpt.com/backend-api/codex"
SOL = "gpt-6-sol"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp-" + secrets.token_hex(6))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def write_json(path: Path, value: dict) -> None:
    atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode())


def toml_string(value: str) -> str:
    return json.dumps(value)


def split_root(text: str) -> tuple[list[str], list[str]]:
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.lstrip().startswith("["):
            return lines[:i], lines[i:]
    return lines, []


def root_fields(text: str) -> dict[str, str]:
    root, _ = split_root(text)
    fields: dict[str, str] = {}
    for line in root:
        match = KEY_RE.match(line)
        if match and match.group(1) in MANAGED_KEYS:
            key = match.group(1)
            if key in fields:
                raise ValueError("duplicate root key: " + key)
            fields[key] = line
    return fields


def edit_root(text: str, expected: dict[str, str | None], replacement: dict[str, str | None]) -> str:
    root, suffix = split_root(text)
    found = root_fields(text)
    for key, before in expected.items():
        if found.get(key) != before:
            raise ValueError(f"config changed at {key}; refusing to overwrite")
    kept: list[str] = []
    for line in root:
        match = KEY_RE.match(line)
        if match and match.group(1) in replacement:
            new = replacement[match.group(1)]
            if new is not None:
                kept.append(new)
        else:
            kept.append(line)
    for key in MANAGED_KEYS:
        if key not in found and replacement.get(key) is not None:
            kept.append(replacement[key])
    result = "".join(kept + suffix)
    tomllib.loads(result)
    return result


def restore_owned_root(text: str, owned: dict[str, str], original: dict[str, str | None]) -> tuple[str, list[str]]:
    current = root_fields(text)
    safe = {key: value for key, value in owned.items() if current.get(key) == value}
    conflicts = [key for key, value in owned.items() if current.get(key) != value]
    # Do not leave a live router URL while stopping its daemon.
    if "openai_base_url" in conflicts and "127.0.0.1:43191" in (current.get("openai_base_url") or ""):
        raise ValueError("openai_base_url still targets router; refusing to stop")
    return edit_root(text, safe, {key: original.get(key) for key in safe}), conflicts


def save_config(path: Path, text: str) -> None:
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    atomic_write(path, text.encode(), mode)


def native_catalog(cache_path: Path) -> dict:
    catalog = load_json(cache_path)
    models = catalog.get("models")
    if not isinstance(models, list) or not models:
        raise ValueError("native catalog has no models")
    slugs = [x.get("slug") for x in models if isinstance(x, dict)]
    if not any(re.fullmatch(r"gpt-\d+(?:\.\d+)*-sol", slug or "") for slug in slugs):
        raise ValueError("Sol is absent from native catalog")
    return {"models": models}


def select_sol(catalog: dict) -> str:
    candidates = [x["slug"] for x in catalog["models"] if isinstance(x, dict) and x.get("visibility") == "list"
                  and isinstance(x.get("slug"), str) and re.fullmatch(r"gpt-\d+(?:\.\d+)*-sol", x["slug"])]
    if not candidates:
        raise ValueError("no visible Sol model")
    return max(candidates, key=lambda slug: tuple(int(n) for n in slug.split("-")[1].split(".")))


def managed_catalog(native: dict) -> dict:
    models = copy.deepcopy(native["models"])
    sol = next(x for x in models if x.get("slug") == select_sol(native))
    for slug, name in (("effortlane-auto", "Effortlane Auto"), ("effortlane-shadow", "Effortlane Shadow")):
        if any(x.get("slug") == slug for x in models):
            raise ValueError("native catalog already owns " + slug)
        alias = copy.deepcopy(sol)
        alias["slug"] = slug
        alias["display_name"] = name
        alias["description"] = ("Effortlane chooses the execution model and reasoning effort; "
                                "the displayed effort is not the execution effort."
                                if slug == "effortlane-auto" else
                                "Runs the latest available Sol at the selected effort; "
                                "Effortlane independently proposes a model and effort.")
        # Auto ignores the visible effort; Shadow applies it to Sol only.
        advertised = [level for level in sol.get("supported_reasoning_levels", [])
                      if isinstance(level, dict) and level.get("effort") == "medium"] if slug == "effortlane-auto" else [
                          level for level in sol.get("supported_reasoning_levels", [])
                          if isinstance(level, dict) and level.get("effort") in ("low", "medium", "high", "xhigh", "max", "ultra")]
        if not advertised:
            advertised = [level for level in sol.get("supported_reasoning_levels", [])
                          if isinstance(level, dict) and isinstance(level.get("effort"), str)][:1]
        if not advertised:
            raise ValueError("Sol has no supported reasoning levels")
        alias["default_reasoning_level"] = ("medium" if any(level["effort"] == "medium" for level in advertised)
                                            else advertised[0]["effort"])
        alias["supported_reasoning_levels"] = advertised
        models.append(alias)
    return {"models": models}



def is_managed_catalog(current: dict, native: dict) -> bool:
    """Recognize exact generated catalogs, including the former picker labels."""
    expected = managed_catalog(native)
    if current == expected:
        return True
    for old_ids in (False, True):
        for old_names, old_descriptions in ((False, False), (True, False), (False, True), (True, True)):
            legacy = copy.deepcopy(expected)
            for model in legacy["models"]:
                if model.get("slug") in ALIASES:
                    if old_names:
                        model["display_name"] = "Jev Auto" if model["slug"] == "effortlane-auto" else "Jev Shadow"
                    if old_descriptions:
                        model["description"] = model["description"].replace("Effortlane", "Jev")
                    if old_ids:
                        model["slug"] = model["slug"].replace("effortlane-", "jev-", 1)
            if current == legacy:
                return True
    return False


def plist_content(root: Path, python: Path) -> bytes:
    import plistlib

    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(python), str(root / "transport.py"), "--root", str(root)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(root / "state/daemon.log"),
        "StandardErrorPath": str(root / "state/daemon.log"),
    })


def desktop_plist_content(wrapper: Path) -> bytes:
    import plistlib

    return plistlib.dumps({
        "Label": DESKTOP_LABEL,
        "ProgramArguments": ["/bin/launchctl", "setenv", "CODEX_CLI_PATH", str(wrapper)],
        "RunAtLoad": True,
    })


def desktop_wrapper_content(root: Path, native: Path, python: Path) -> bytes:
    argv = [str(python), str(root / "desktop_bootstrap.py"), "--native", str(native), "--root", str(root), "--"]
    adapter = root / "rpc_adapter.py"
    bootstrap = root / "desktop_bootstrap.py"
    direct = "openai_base_url=" + toml_string(NATIVE_URL)
    return ("#!/bin/sh\n"
            + f"if [ -x {shlex.quote(str(python))} ] && [ -r {shlex.quote(str(adapter))} ] && [ -r {shlex.quote(str(bootstrap))} ]; then\n"
            + "  exec " + " ".join(map(shlex.quote, [*argv, "-c", direct])) + ' "$@"\n'
            + "fi\n"
            + "exec " + " ".join(map(shlex.quote, [str(native), "-c", direct])) + ' "$@"\n').encode()


def legacy_desktop_wrapper_content(root: Path, native: Path, python: Path) -> bytes:
    """Recognize only our previous Python-parented wrapper for safe upgrade."""
    argv = [str(python), str(root / "rpc_adapter.py"), "--native", str(native), "--root", str(root), "--"]
    direct = "openai_base_url=" + toml_string(NATIVE_URL)
    adapter = root / "rpc_adapter.py"
    return ("#!/bin/sh\n"
            + f"if [ -x {shlex.quote(str(python))} ] && [ -r {shlex.quote(str(adapter))} ]; then\n"
            + "  exec " + " ".join(map(shlex.quote, [*argv, "-c", direct])) + ' "$@"\n'
            + "fi\n"
            + "exec " + " ".join(map(shlex.quote, [str(native), "-c", direct])) + ' "$@"\n').encode()


def native_desktop_wrapper_content(native: Path) -> bytes:
    direct = "openai_base_url=" + toml_string(NATIVE_URL)
    return ("#!/bin/sh\nexec " + " ".join(map(shlex.quote, [str(native), "-c", direct]))
            + ' "$@"\n').encode()


def _make_desktop_wrapper_native(root: Path, manifest: dict) -> bool:
    """Leave a safe launcher at a path Desktop may keep using after disable."""
    desktop = manifest.get("desktop", {})
    wrapper = Path(desktop.get("wrapper_path", root / "app-server-wrapper"))
    if not wrapper.exists():
        return False
    if wrapper.is_symlink():
        manifest.setdefault("preserved_user_changes", []).append("app-server-wrapper")
        return False
    native = Path(manifest["native_target"])
    python = Path(desktop.get("python", DESKTOP_PYTHON))
    current = wrapper.read_bytes()
    safe = native_desktop_wrapper_content(native)
    if current == safe:
        return False
    if current not in (desktop_wrapper_content(root, native, python),
                       legacy_desktop_wrapper_content(root, native, python)):
        manifest.setdefault("preserved_user_changes", []).append("app-server-wrapper")
        return False
    backup = root / "backups" / ("desktop-wrapper-disable-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "app-server-wrapper", current)
    atomic_write(wrapper, safe, 0o700)
    return True


def desktop_wrapper_issue(root: Path, manifest: dict, check_native: bool = True) -> str | None:
    desktop = manifest.get("desktop", {})
    if not desktop.get("enabled"):
        return None
    wrapper = Path(desktop.get("wrapper_path", root / "app-server-wrapper"))
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        return f"Desktop wrapper missing or not executable: {wrapper}"
    expected = desktop_wrapper_content(root, Path(manifest["native_target"]),
                                       Path(desktop.get("python", DESKTOP_PYTHON)))
    if wrapper.read_bytes() != expected:
        return (f"Desktop wrapper differs from manifest.native_target ({manifest['native_target']}); "
                "it may be stale or manually edited. Refusing automatic overwrite")
    native = Path(manifest["native_target"])
    if check_native and (not native.is_file() or not os.access(native, os.X_OK)):
        return f"Desktop wrapper points to missing or non-executable native_target: {native}"
    return None


def desktop_refresh_native(native: Path, root: Path = ROOT) -> dict:
    """Refresh an owned Desktop wrapper after the app moves its bundled CLI."""
    if not native.is_absolute() or not native.is_file() or not os.access(native, os.X_OK):
        raise ValueError(f"new native Codex binary missing or not executable: {native}")
    manifest_path = root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    desktop = manifest.get("desktop", {})
    if not desktop.get("enabled"):
        raise ValueError("Desktop adapter is not enabled")
    wrapper = Path(desktop.get("wrapper_path", root / "app-server-wrapper"))
    if wrapper.is_symlink() or not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        raise ValueError(f"Desktop wrapper missing, linked, or not executable: {wrapper}")
    if desktop_env() != str(wrapper):
        raise ValueError("CODEX_CLI_PATH changed outside router; refusing to overwrite")
    issue = desktop_wrapper_issue(root, manifest, check_native=False)
    if issue:
        raise ValueError(issue)
    old_content = wrapper.read_bytes()
    if manifest["native_target"] == str(native):
        return {"changed": False, "native_target": str(native)}
    python = Path(desktop.get("python", DESKTOP_PYTHON))
    new_content = desktop_wrapper_content(root, native, python)
    backup = root / "backups" / ("desktop-native-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "manifest.json", manifest_bytes)
    atomic_write(backup / "app-server-wrapper", old_content, 0o700)
    if wrapper.read_bytes() != old_content or manifest_path.read_bytes() != manifest_bytes:
        raise ValueError("Desktop files changed during refresh; refusing to overwrite")
    if manifest.get("cli_target", manifest["native_target"]) == manifest["native_target"]:
        manifest["cli_target"] = str(native)
    manifest["native_target"] = str(native)
    atomic_write(wrapper, new_content, 0o700)
    try:
        write_json(manifest_path, manifest)
    except Exception:
        atomic_write(wrapper, old_content, 0o700)
        raise
    return {"changed": True, "native_target": str(native), "backup": str(backup)}


def desktop_env() -> str | None:
    result = subprocess.run(["launchctl", "getenv", "CODEX_CLI_PATH"], capture_output=True, text=True, timeout=2)
    if result.returncode:
        return None
    return result.stdout.removesuffix("\n")


def start_desktop_agent(agent_path: Path) -> None:
    domain = f"gui/{os.getuid()}"
    launchctl("bootstrap", domain, str(agent_path))
    launchctl("enable", domain + "/" + DESKTOP_LABEL)
    launchctl("kickstart", "-k", domain + "/" + DESKTOP_LABEL)


def stop_desktop_agent(agent_path: Path) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(agent_path)], capture_output=True, timeout=2)


def restore_desktop_env(previous: str | None) -> None:
    if previous is None:
        launchctl("unsetenv", "CODEX_CLI_PATH")
    else:
        launchctl("setenv", "CODEX_CLI_PATH", previous)


def python_executable() -> Path:
    stable = Path("/opt/homebrew/bin/python3")
    return stable if stable.exists() else Path(sys.executable)


def launchctl(*args: str) -> None:
    result = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"launchctl {args[0]} failed: {result.stderr.strip()[:300]}")


def start_agent() -> None:
    domain = f"gui/{os.getuid()}"
    # A disabled or absent agent can both be bootstrapped safely.
    subprocess.run(["launchctl", "bootout", domain, str(AGENT)], capture_output=True)
    launchctl("bootstrap", domain, str(AGENT))
    launchctl("enable", domain + "/" + LABEL)
    launchctl("kickstart", "-k", domain + "/" + LABEL)


def stop_agent() -> None:
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(AGENT)], capture_output=True)


def source_dir() -> Path:
    return Path(__file__).resolve().parent


def cli_target(manifest: dict) -> str:
    """Use an independently updated CLI, with the Desktop bundle as fallback."""
    configured = manifest.get("cli_target", manifest["native_target"])
    path = Path(configured)
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return manifest["native_target"]


def cli_catalog_path(root: Path, name: str) -> Path:
    """Use a complete CLI catalog generation, keeping Desktop on its own files."""
    manifest = load_json(root / "manifest.json")
    generation = manifest.get("cli_catalog_generation")
    if isinstance(generation, str) and re.fullmatch(r"cli-[0-9TZ]{16}-[a-f0-9]{8}", generation):
        directory = root / "catalogs" / generation
        if all((directory / item).is_file() for item in ("native-models.json", "models.json")):
            return directory / name
    return root / name


def fetch_account_catalog(native: Path, auth: Path) -> dict:
    """Ask native Codex for its account catalog in an isolated temporary home."""
    if not auth.is_file() or auth.stat().st_size > 1024 * 1024:
        raise ValueError("Codex auth file missing or too large; login with native Codex first")
    with tempfile.TemporaryDirectory(prefix="jev-catalog-") as directory:
        isolated = Path(directory)
        atomic_write(isolated / "auth.json", auth.read_bytes())
        env = os.environ.copy()
        env["CODEX_HOME"] = str(isolated)
        env.pop("OPENAI_BASE_URL", None)
        env.pop("CODEX_CLI_PATH", None)
        try:
            result = subprocess.run([str(native), "debug", "models"], env=env,
                                    capture_output=True, timeout=30, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("native Codex model catalog fetch timed out") from exc
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise ValueError("native Codex model catalog fetch failed; CLI catalog unchanged")
        try:
            return native_catalog_from_response(json.loads(result.stdout))
        except (ValueError, TypeError) as exc:
            raise ValueError("native Codex returned an invalid model catalog; CLI catalog unchanged") from exc


def cli_account_catalog(root: Path = ROOT) -> dict:
    manifest = load_json(root / "manifest.json")
    auth = Path(manifest["config_path"]).parent / "auth.json"
    return fetch_account_catalog(Path(cli_target(manifest)), auth)


def cli_refresh_catalog(catalog_path: Path | None = None, root: Path = ROOT) -> dict:
    """Install native account catalog for CLI without changing Desktop."""
    if catalog_path is None:
        native = cli_account_catalog(root)
    else:
        if not catalog_path.is_file() or catalog_path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("CLI account catalog missing or too large")
        native = native_catalog(catalog_path)
    managed = managed_catalog(native)
    if (native == load_json(cli_catalog_path(root, "native-models.json"))
            and managed == load_json(cli_catalog_path(root, "models.json"))):
        return {"changed": False, "sol": select_sol(native)}
    generation = "cli-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(4)
    directory = root / "catalogs" / generation
    directory.mkdir(mode=0o700, parents=True)
    write_json(directory / "native-models.json", native)
    write_json(directory / "models.json", managed)
    manifest_path = root / "manifest.json"
    previous = manifest_path.read_bytes()
    backup = root / "backups" / generation
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "manifest.json", previous)
    manifest = json.loads(previous)
    manifest["cli_catalog_generation"] = generation
    if manifest_path.read_bytes() != previous:
        raise ValueError("router manifest changed during CLI catalog refresh; refusing to overwrite")
    write_json(manifest_path, manifest)
    return {"changed": True, "sol": select_sol(native), "backup": str(backup)}


def desktop_refresh_catalog(root: Path = ROOT) -> dict:
    """Refresh Desktop aliases from its own authenticated native Codex binary."""
    manifest = load_json(root / "manifest.json")
    binary = Path(manifest["native_target"])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Desktop native Codex is missing or not executable")
    native_path, managed_path = root / "native-models.json", root / "models.json"
    previous_native, previous_managed = native_path.read_bytes(), managed_path.read_bytes()
    current_native = native_catalog(native_path)
    if not is_managed_catalog(load_json(managed_path), current_native):
        raise ValueError("Desktop model catalog was modified; refusing to overwrite it")
    auth = Path(manifest["config_path"]).parent / "auth.json"
    native = fetch_account_catalog(binary, auth)
    managed = managed_catalog(native)
    with tempfile.TemporaryDirectory(prefix="jev-desktop-catalog-check-") as directory:
        candidate = Path(directory) / "models.json"
        write_json(candidate, managed)
        try:
            result = subprocess.run([str(binary), "-c", "model_catalog_json=" + toml_string(str(candidate)),
                                     "debug", "models"], capture_output=True, timeout=20, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Desktop native Codex catalog validation timed out") from exc
        if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
            raise ValueError("Desktop native Codex rejected the refreshed catalog")
        try:
            validated = json.loads(result.stdout)
            slugs = {item.get("slug") for item in validated["models"] if isinstance(item, dict)}
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("Desktop native Codex returned invalid refreshed metadata") from exc
        if select_sol(native) not in slugs or "effortlane-shadow" not in slugs or "effortlane-auto" not in slugs:
            raise ValueError("Desktop native Codex did not load the refreshed models")
    if (native == current_native and managed == load_json(managed_path)):
        return {"changed": False, "sol": select_sol(native)}
    backup = root / "backups" / ("desktop-models-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "native-models.json", previous_native)
    atomic_write(backup / "models.json", previous_managed)
    if native_path.read_bytes() != previous_native or managed_path.read_bytes() != previous_managed:
        raise ValueError("Desktop model catalog changed during refresh; refusing to overwrite")
    try:
        write_json(native_path, native)
        write_json(managed_path, managed)
    except BaseException:
        atomic_write(native_path, previous_native)
        atomic_write(managed_path, previous_managed)
        raise
    return {"changed": True, "sol": select_sol(native), "backup": str(backup),
            "restart_required": bool(manifest.get("desktop", {}).get("enabled"))}


def cli_update_and_refresh(root: Path = ROOT) -> int:
    """Run the native CLI updater, then refresh only the CLI account catalog."""
    native = cli_target(load_json(root / "manifest.json"))
    command = [native, "update"]
    resolved = Path(native).resolve()
    if tuple(part.name for part in list(resolved.parents)[:4]) == ("bin", "codex", "@openai", "node_modules"):
        npm = shutil.which("npm")
        if npm:
            npm_root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, check=False)
            if npm_root.returncode == 0 and Path(npm_root.stdout.strip()).resolve() == resolved.parents[3]:
                command = [npm, "install", "-g", "@openai/codex@latest"]
    result = subprocess.run(command, check=False)
    if result.returncode:
        return result.returncode
    try:
        refreshed = cli_refresh_catalog(root=root)
        print("Effortlane CLI catalog: " + ("updated" if refreshed["changed"] else "already current")
              + " (Sol " + refreshed["sol"] + ")")
    except (OSError, ValueError, RuntimeError):
        print("Effortlane CLI catalog refresh failed; native Codex remains usable. "
              "Retry with effortlane cli-refresh-models.", file=sys.stderr)
    return 0


def cli_set_target(native: Path, root: Path = ROOT) -> dict:
    """Switch only terminal execution; never change the signed Desktop binary."""
    if not native.is_absolute() or not native.is_file() or not os.access(native, os.X_OK):
        raise ValueError("CLI binary missing or not executable: " + str(native))
    if native.resolve() in ((root / "jev-codex").resolve(), (root / "codex-native").resolve()):
        raise ValueError("CLI target cannot point to an Effortlane wrapper")
    manifest_path = root / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest.get("cli_target", manifest["native_target"]) == str(native):
        return {"changed": False, "cli_target": str(native)}
    backup = root / "backups" / ("cli-target-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "manifest.json", manifest_path.read_bytes())
    manifest["cli_target"] = str(native)
    write_json(manifest_path, manifest)
    return {"changed": True, "cli_target": str(native), "backup": str(backup)}


def install(root: Path = ROOT, config_path: Path = CODEX_CONFIG, bin_dir: Path = BIN,
            cache_path: Path | None = None, agent_path: Path = AGENT,
            start: bool = True, key_path: Path | None = None,
            execution_binary: Path | None = None) -> None:
    if (root / "manifest.json").exists():
        raise ValueError("already installed; use status/update or rollback")
    cache_path = cache_path or config_path.parent / "models_cache.json"
    codex = bin_dir / "codex"
    if not codex.is_symlink():
        raise ValueError("expected existing codex symlink; refusing to replace it")
    original_link = os.readlink(codex)
    native_target = execution_binary or (Path(original_link) if os.path.isabs(original_link) else codex.parent / original_link)
    if not native_target.resolve(strict=True).is_file() or not os.access(native_target, os.X_OK):
        raise ValueError("native codex is not executable")
    if any((bin_dir / name).exists() or (bin_dir / name).is_symlink()
           for name in ("codex-native", "jev-codex", "effortlane")):
        raise ValueError("codex-native, effortlane, or legacy command already exists")
    if not config_path.exists():
        raise ValueError("Codex config missing")
    key_file = key_path or Path.home() / ".config/jev-codex-router/typesafe-api-key"
    if not key_file.is_file() or (key_file.stat().st_mode & 0o077):
        raise ValueError("TypeSafe key file missing or not owner-only")
    if agent_path.exists():
        raise ValueError("LaunchAgent already exists: " + str(agent_path))
    for filename in ("manage.py", "core.py", "costs.py", "metrics.py", "trials.py", "transport.py", "rpc_adapter.py", "desktop_bootstrap.py", "cli_chat.py", "cli_bridge.py", "claude_shadow.py", "native_shadow.py"):
        if not (source_dir() / filename).exists():
            raise ValueError("missing source: " + filename)
    original_text = config_path.read_text()
    tomllib.loads(original_text)
    before = {key: root_fields(original_text).get(key) for key in MANAGED_KEYS}
    native = native_catalog(cache_path)
    sol = select_sol(native)
    generated = managed_catalog(native)
    root.mkdir(parents=True, exist_ok=False)
    os.chmod(root, 0o700)
    (root / "backups").mkdir(mode=0o700)
    (root / "state").mkdir(mode=0o700)
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = root / "backups" / ("config-" + timestamp + ".toml")
    atomic_write(backup, original_text.encode())
    for filename in ("manage.py", "core.py", "costs.py", "metrics.py", "trials.py", "transport.py", "rpc_adapter.py", "desktop_bootstrap.py", "cli_chat.py", "cli_bridge.py", "claude_shadow.py", "native_shadow.py"):
        source = source_dir() / filename
        if not source.exists():
            raise ValueError("missing source: " + filename)
        shutil.copy2(source, root / filename)
        os.chmod(root / filename, 0o600)
    write_json(root / "native-models.json", native)
    write_json(root / "models.json", generated)
    capability = secrets.token_urlsafe(32)
    atomic_write(root / "capability", (capability + "\n").encode())
    port = 43191
    config = {
        "mode": "shadow",
        "auto_roles": ["luna", "terra", "sol"],
        "auto_policy": "completion_v4",
        "large_context_sol_floor_tokens": 48000,
        "shadow_policy": "completion_v4",
        "effort_policy": "jev",
        "fixed_effort": "medium",
        "fallback_model": sol,
        "port": port,
        "capability_file": str(root / "capability"),
        "key_file": str(key_file),
        "native_catalog_path": str(root / "native-models.json"),
        "telemetry_file": str(root / "state/telemetry.jsonl"),
    }
    write_json(root / "config.json", config)
    managed = {
        # Keep Desktop on native models. The CLI wrapper selects Jev aliases
        # per invocation, with its own catalog override.
        "model": before["model"],
        "model_reasoning_effort": before["model_reasoning_effort"],
        # The Desktop adapter and CLI wrapper set their own execution endpoint.
        # A global relay URL can also affect built-in tools such as Image Gen.
        "openai_base_url": before["openai_base_url"],
        "model_catalog_json": before["model_catalog_json"],
    }
    updated = edit_root(original_text, before, managed)
    python = python_executable()
    wrapper = root / "jev-codex"
    atomic_write(wrapper, (f"#!{python}\nimport sys\nsys.path.insert(0, {str(root)!r})\nfrom manage import main\nif __name__ == '__main__': main()\n").encode(), 0o700)
    os.chmod(wrapper, 0o700)
    native_wrapper = root / "codex-native"
    atomic_write(native_wrapper, (f"#!{python}\nimport sys\nsys.path.insert(0, {str(root)!r})\nfrom manage import native_main\nif __name__ == '__main__': native_main()\n").encode(), 0o700)
    os.chmod(native_wrapper, 0o700)
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "original_codex_link": original_link,
        "native_target": str(native_target),
        "cli_target": str(native_target),
        "original_root": before,
        "managed_root": managed,
        "config_state": "enabled",
        "config_path": str(config_path),
        "backup": str(backup),
        "agent_path": str(agent_path),
        "bin_dir": str(bin_dir),
    }
    # All preflight work is complete. Keep the recoverable backup and manifest before global edits.
    write_json(root / "manifest.json", manifest)
    try:
        atomic_write(agent_path, plist_content(root, python), 0o600)
        save_config(config_path, updated)
        (bin_dir / "codex-native").symlink_to(native_wrapper)
        (bin_dir / "jev-codex").symlink_to(wrapper)
        (bin_dir / "effortlane").symlink_to(wrapper)
        codex.unlink()
        codex.symlink_to(wrapper)
        if start:
            start_agent()
    except Exception:
        if codex.is_symlink() and os.readlink(codex) == str(wrapper):
            codex.unlink()
        if not codex.exists() and not codex.is_symlink():
            codex.symlink_to(original_link)
        for name, target in (("codex-native", native_wrapper), ("jev-codex", wrapper), ("effortlane", wrapper)):
            path = bin_dir / name
            if path.is_symlink() and os.readlink(path) == str(target):
                path.unlink()
        try:
            if config_path.exists() and root_fields(config_path.read_text()) == managed:
                save_config(config_path, original_text)
        except (OSError, ValueError):
            pass
        if agent_path.exists() and agent_path.read_bytes() == plist_content(root, python):
            agent_path.unlink()
        if start:
            stop_agent()
        manifest["config_state"] = "failed"
        write_json(root / "manifest.json", manifest)
        raise


def _restore_root(root: Path, manifest: dict) -> None:
    path = Path(manifest["config_path"])
    current = path.read_text()
    if manifest["config_state"] == "enabled":
        updated, conflicts = restore_owned_root(current, manifest["managed_root"], manifest["original_root"])
        if conflicts:
            manifest["preserved_user_changes"] = conflicts
            write_json(root / "manifest.json", manifest)
    else:
        updated = current
    if updated != current:
        save_config(path, updated)


def desktop_enable(root: Path = ROOT, agent_path: Path | None = None,
                   python: Path = DESKTOP_PYTHON) -> None:
    manifest_path = root / "manifest.json"
    manifest = load_json(manifest_path)
    if manifest["config_state"] != "enabled":
        raise ValueError("enable router before Desktop adapter")
    desktop = manifest.get("desktop", {})
    if desktop.get("phase") == "prepared":
        desktop_disable(root)
        manifest = load_json(manifest_path)
        desktop = manifest.get("desktop", {})
    wrapper = root / "app-server-wrapper"
    agent_path = agent_path or Path(desktop.get("agent_path", DESKTOP_AGENT))
    content = desktop_wrapper_content(root, Path(manifest["native_target"]), python)
    if desktop.get("enabled"):
        if desktop_env() != str(wrapper):
            raise ValueError("CODEX_CLI_PATH changed outside router; refusing to overwrite")
        issue = desktop_wrapper_issue(root, manifest)
        if issue:
            raise ValueError(issue)
        return
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Python 3.14 executable missing: " + str(python))
    source_adapter = source_dir() / "rpc_adapter.py"
    source_bootstrap = source_dir() / "desktop_bootstrap.py"
    if not source_adapter.is_file() or not source_bootstrap.is_file():
        raise ValueError("missing Desktop adapter or bootstrap source")
    installed_adapter = root / "rpc_adapter.py"
    installed_bootstrap = root / "desktop_bootstrap.py"
    if installed_adapter.exists() and installed_adapter.read_bytes() != source_adapter.read_bytes():
        raise ValueError("installed rpc_adapter.py differs from source; refusing to overwrite")
    if installed_bootstrap.exists() and installed_bootstrap.read_bytes() != source_bootstrap.read_bytes():
        raise ValueError("installed desktop_bootstrap.py differs from source; refusing to overwrite")
    old_wrapper = wrapper.read_bytes() if wrapper.exists() else None
    if wrapper.is_symlink():
        raise ValueError("Desktop wrapper is a symlink; refusing to overwrite")
    legacy_content = legacy_desktop_wrapper_content(root, Path(manifest["native_target"]), python)
    safe_content = native_desktop_wrapper_content(Path(manifest["native_target"]))
    if old_wrapper not in (None, content, legacy_content, safe_content):
        raise ValueError("Desktop wrapper changed; refusing to overwrite")
    if agent_path.exists():
        raise ValueError("Desktop environment LaunchAgent already exists: " + str(agent_path))
    previous = desktop_env()
    if previous == str(wrapper):
        raise ValueError("CODEX_CLI_PATH already points to an unowned wrapper")
    copied_adapter = not installed_adapter.exists()
    copied_bootstrap = not installed_bootstrap.exists()
    created_wrapper = not wrapper.exists()
    upgraded_wrapper = old_wrapper in (legacy_content, safe_content) and old_wrapper != content
    config_path = Path(manifest["config_path"])
    current_config = config_path.read_bytes()
    current_fields = root_fields(current_config.decode())
    original_catalog = manifest["original_root"].get("model_catalog_json")
    if current_fields.get("model_catalog_json") != original_catalog:
        raise ValueError("Desktop model catalog changed outside router; refusing to overwrite")
    owned_catalog = f'model_catalog_json = {toml_string(str(root / "models.json"))}\n'
    backup = root / "backups" / ("desktop-enable-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "config.toml", current_config)
    atomic_write(backup / "manifest.json", manifest_path.read_bytes())
    if old_wrapper is not None:
        atomic_write(backup / "app-server-wrapper", old_wrapper)
    # Journal the old value before the LaunchAgent or launchctl can change it.
    manifest["desktop"] = {
        "opted_in": bool(desktop.get("opted_in")),
        "enabled": False,
        "phase": "prepared",
        "previous_env": previous,
        "agent_path": str(agent_path),
        "wrapper_path": str(wrapper),
        "python": str(python),
        "env_changed_externally": False,
        "previous_catalog": original_catalog,
    }
    write_json(manifest_path, manifest)
    try:
        if copied_adapter:
            shutil.copy2(source_adapter, installed_adapter)
            os.chmod(installed_adapter, 0o600)
        if copied_bootstrap:
            shutil.copy2(source_bootstrap, installed_bootstrap)
            os.chmod(installed_bootstrap, 0o600)
        if created_wrapper or upgraded_wrapper:
            atomic_write(wrapper, content, 0o700)
        updated_config = edit_root(config_path.read_text(),
                                   {"model_catalog_json": original_catalog},
                                   {"model_catalog_json": owned_catalog})
        save_config(config_path, updated_config)
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(agent_path, desktop_plist_content(wrapper), 0o600)
        start_desktop_agent(agent_path)
        launchctl("setenv", "CODEX_CLI_PATH", str(wrapper))
        manifest["desktop"] = {
            "opted_in": True,
            "enabled": True,
            "phase": "enabled",
            "previous_env": previous,
            "agent_path": str(agent_path),
            "wrapper_path": str(wrapper),
            "python": str(python),
            "env_changed_externally": False,
            "previous_catalog": original_catalog,
        }
        manifest["managed_root"]["model_catalog_json"] = owned_catalog
        write_json(manifest_path, manifest)
    except Exception:
        # If cleanup fails, the prepared manifest remains for a later disable/rollback.
        desktop_disable(root)
        if copied_adapter and installed_adapter.exists() and installed_adapter.read_bytes() == source_adapter.read_bytes():
            installed_adapter.unlink()
        if copied_bootstrap and installed_bootstrap.exists() and installed_bootstrap.read_bytes() == source_bootstrap.read_bytes():
            installed_bootstrap.unlink()
        # Keep the native-only launcher if Desktop cached this path.
        if config_path.read_text() == edit_root(current_config.decode(),
                                               {"model_catalog_json": original_catalog},
                                               {"model_catalog_json": owned_catalog}):
            save_config(config_path, current_config.decode())
        raise


def desktop_disable(root: Path = ROOT) -> None:
    manifest_path = root / "manifest.json"
    manifest = load_json(manifest_path)
    desktop = manifest.get("desktop", {})
    if not desktop.get("enabled") and desktop.get("phase") != "prepared":
        return
    wrapper = Path(desktop["wrapper_path"])
    agent_path = Path(desktop["agent_path"])
    expected = desktop_plist_content(wrapper)
    if agent_path.exists() and agent_path.read_bytes() != expected:
        raise ValueError("Desktop environment LaunchAgent changed; refusing to unload")
    stop_desktop_agent(agent_path)
    current = desktop_env()
    changed = current != str(wrapper) and (desktop.get("phase") != "prepared" or current != desktop.get("previous_env"))
    if not changed:
        if current == str(wrapper):
            restore_desktop_env(desktop.get("previous_env"))
    if agent_path.exists() and agent_path.read_bytes() == expected:
        agent_path.unlink()
    _make_desktop_wrapper_native(root, manifest)
    catalog = manifest.get("managed_root", {}).get("model_catalog_json")
    previous_catalog = desktop.get("previous_catalog")
    config_path = Path(manifest["config_path"])
    if catalog and catalog != previous_catalog:
        current_config = config_path.read_text()
        if root_fields(current_config).get("model_catalog_json") == catalog:
            save_config(config_path, edit_root(current_config,
                                               {"model_catalog_json": catalog},
                                               {"model_catalog_json": previous_catalog}))
        else:
            manifest.setdefault("preserved_user_changes", []).append("model_catalog_json")
        manifest["managed_root"]["model_catalog_json"] = previous_catalog
    desktop["enabled"] = False
    desktop["phase"] = "disabled"
    desktop["env_changed_externally"] = changed
    manifest["desktop"] = desktop
    write_json(manifest_path, manifest)


def desktop_safe(root: Path = ROOT) -> dict:
    """Keep Desktop native and move synthetic model aliases to CLI-only config."""
    manifest_path = root / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    config_path = Path(manifest["config_path"])
    config_bytes = config_path.read_bytes()
    fields = root_fields(config_bytes.decode())
    managed_catalog = manifest["managed_root"].get("model_catalog_json")
    original_catalog = manifest["original_root"].get("model_catalog_json")
    if fields.get("model_catalog_json") not in (managed_catalog, original_catalog):
        raise ValueError("model_catalog_json changed outside router; refusing to overwrite")
    alias_model = fields.get("model") in tuple(f'model = "{alias}"\n' for alias in (*ALIASES, "jev-auto", "jev-shadow"))
    desktop = manifest.get("desktop", {})
    wrapper = Path(desktop.get("wrapper_path", root / "app-server-wrapper"))
    native = Path(manifest["native_target"])
    python = Path(desktop.get("python", DESKTOP_PYTHON))
    wrapper_needs_safety = wrapper.is_file() and wrapper.read_bytes() in (
        desktop_wrapper_content(root, native, python), legacy_desktop_wrapper_content(root, native, python))
    if (not desktop.get("enabled") and not desktop.get("opted_in")
            and fields.get("model_catalog_json") == original_catalog and not alias_model
            and not wrapper_needs_safety):
        return {"changed": False}
    backup = root / "backups" / ("desktop-safe-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "config.toml", config_bytes)
    atomic_write(backup / "manifest.json", manifest_bytes)
    desktop_disable(root)
    manifest = load_json(manifest_path)
    _make_desktop_wrapper_native(root, manifest)
    expected_after_disable = edit_root(config_bytes.decode(),
                                       {"model_catalog_json": fields.get("model_catalog_json")},
                                       {"model_catalog_json": original_catalog})
    if config_path.read_text() not in (config_bytes.decode(), expected_after_disable):
        raise ValueError("Codex config changed during Desktop safety migration; refusing to overwrite")
    current = config_path.read_text()
    current_fields = root_fields(current)
    expected = {"model_catalog_json": current_fields.get("model_catalog_json")}
    replacement = {"model_catalog_json": original_catalog}
    if alias_model:
        expected["model"] = current_fields.get("model")
        replacement["model"] = f'model = {toml_string(load_json(root / "config.json")["fallback_model"])}\n'
    updated = edit_root(current, expected, replacement)
    if updated != current:
        save_config(config_path, updated)
    manifest["managed_root"]["model_catalog_json"] = original_catalog
    manifest.setdefault("desktop", {})["opted_in"] = False
    write_json(manifest_path, manifest)
    return {"changed": True, "backup": str(backup)}


def _remove_desktop_wrapper(root: Path, manifest: dict) -> None:
    desktop = manifest.get("desktop", {})
    if not desktop.get("wrapper_path"):
        return
    wrapper = Path(desktop["wrapper_path"])
    native = Path(manifest["native_target"])
    python = Path(desktop.get("python", DESKTOP_PYTHON))
    owned = (desktop_wrapper_content(root, native, python),
             legacy_desktop_wrapper_content(root, native, python),
             native_desktop_wrapper_content(native))
    if wrapper.exists():
        if wrapper.read_bytes() not in owned:
            manifest.setdefault("preserved_user_changes", []).append("app-server-wrapper")
        else:
            wrapper.unlink()


def disable(root: Path = ROOT, stop: bool = True) -> None:
    if (root / "native-shadow-hook.json").exists():
        from native_shadow import configure
        configure(root, False)
    desktop_disable(root)
    manifest = load_json(root / "manifest.json")
    if manifest["config_state"] == "enabled":
        _restore_root(root, manifest)
        manifest["config_state"] = "disabled"
        write_json(root / "manifest.json", manifest)
    config = load_json(root / "config.json")
    config["mode"] = "off"
    write_json(root / "config.json", config)
    if stop:
        stop_agent()


def enable(root: Path = ROOT, start: bool = True) -> None:
    manifest = load_json(root / "manifest.json")
    if manifest["config_state"] == "disabled":
        path = Path(manifest["config_path"])
        current = path.read_text()
        original = manifest["original_root"]
        managed = manifest["managed_root"].copy()
        # Upgrade installations that previously pinned every Codex process to
        # the local relay. Per-invocation CLI routing remains available.
        if managed.get("openai_base_url") != original.get("openai_base_url"):
            managed["openai_base_url"] = original.get("openai_base_url")
        fields = root_fields(current)
        if "127.0.0.1:43191" in (fields.get("openai_base_url") or ""):
            raise ValueError("global openai_base_url still targets router; remove it before enabling")
        preserved = [key for key in MANAGED_KEYS if fields.get(key) != original.get(key)]
        expected = {key: original.get(key) for key in MANAGED_KEYS if key not in preserved}
        replacement = {key: managed.get(key) for key in expected}
        updated = edit_root(current, expected, replacement)
        save_config(path, updated)
        manifest["managed_root"] = managed
        manifest["preserved_user_changes"] = preserved
        manifest["config_state"] = "enabled"
        write_json(root / "manifest.json", manifest)
    config = load_json(root / "config.json")
    config["mode"] = "shadow"
    write_json(root / "config.json", config)
    if start:
        start_agent()
    # Desktop opt-in is explicit; enabling CLI alone must not change its runtime.


def rollback(root: Path = ROOT, stop: bool = True) -> None:
    desktop_disable(root)
    manifest = load_json(root / "manifest.json")
    bin_dir = Path(manifest["bin_dir"])
    codex = bin_dir / "codex"
    wrapper = root / "jev-codex"
    if not codex.is_symlink() or Path(os.readlink(codex)) != wrapper:
        raise ValueError("codex symlink changed; refusing rollback")
    native = bin_dir / "codex-native"
    if not native.is_symlink() or Path(os.readlink(native)) != root / "codex-native":
        raise ValueError("codex-native symlink changed; refusing rollback")
    _restore_root(root, manifest)
    if stop:
        stop_agent()
    codex.unlink()
    codex.symlink_to(manifest["original_codex_link"])
    native.unlink()
    for name in ("effortlane", "jev-codex"):
        command = bin_dir / name
        if command.is_symlink() and Path(os.readlink(command)) == wrapper:
            command.unlink()
    agent = Path(manifest["agent_path"])
    if agent.exists() and agent.read_bytes() == plist_content(root, python_executable()):
        agent.unlink()
    _remove_desktop_wrapper(root, manifest)
    # Preserve installation snapshot, backup, and telemetry for inspection.
    manifest["config_state"] = "rolled_back"
    write_json(root / "manifest.json", manifest)


def update_catalog(root: Path = ROOT) -> None:
    manifest = load_json(root / "manifest.json")
    cache = Path(manifest["config_path"]).parent / "models_cache.json"
    data = load_json(cache)
    fetched_at = data.get("fetched_at")
    try:
        fetched = dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        age = (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds()
    except (AttributeError, ValueError):
        raise ValueError("native catalog cache has no valid fetch time") from None
    if age < 0 or age > 600:
        raise ValueError(f"native catalog cache is stale ({int(age)}s); refresh it through native Codex before update")
    native = native_catalog(cache)
    generated = managed_catalog(native)
    old_alias = next((x for x in load_json(root / "models.json")["models"] if x.get("slug") == "effortlane-auto"), None)
    new_alias = next(x for x in generated["models"] if x.get("slug") == "effortlane-auto")
    if manifest.get("desktop", {}).get("enabled") and old_alias != new_alias:
        raise ValueError("Desktop alias metadata changed; disable the Desktop adapter before updating catalog")
    current_native = (root / "native-models.json").read_bytes()
    current_managed = (root / "models.json").read_bytes()
    current_config = (root / "config.json").read_bytes()
    if native == json.loads(current_native) and generated == json.loads(current_managed):
        return
    backup = root / "backups" / ("catalog-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                                   + "-" + secrets.token_hex(4))
    backup.mkdir(mode=0o700, parents=True)
    atomic_write(backup / "native-models.json", current_native)
    atomic_write(backup / "models.json", current_managed)
    atomic_write(backup / "config.json", current_config)
    write_json(root / "native-models.json", native)
    write_json(root / "models.json", generated)
    config = load_json(root / "config.json")
    config["fallback_model"] = select_sol(native)
    write_json(root / "config.json", config)


def native_catalog_from_response(data: dict) -> dict:
    models = data.get("models")
    if not isinstance(models, list) or not models or not any(isinstance(x, dict) and x.get("slug", "").endswith("-sol") for x in models):
        raise ValueError("native debug models returned no Sol")
    return {"models": models}


def health(root: Path) -> bool:
    try:
        config = load_json(root / "config.json")
        with urllib.request.urlopen(f"http://127.0.0.1:{int(config['port'])}/health", timeout=0.3) as response:
            return response.status == 200 and json.load(response).get("ok") is True
    except (OSError, ValueError, KeyError):
        return False


def desktop_runtime(native_target: Path | None = None) -> dict:
    """Report active local Desktop transport without logging process arguments."""
    result = {"app_running": False, "adapter_active": False, "direct_native_app_server": False}
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True,
                            text=True, timeout=2, check=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return result
    processes = {}
    for line in ps.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        processes[pid] = (parent, parts[2])
    apps = {pid for pid, (_, command) in processes.items()
            if command.split()[0].endswith("/ChatGPT.app/Contents/MacOS/ChatGPT")}
    result["app_running"] = bool(apps)
    # The primary app-server is spawned by the Desktop main process. Test probes
    # launched from a Codex task also descend from it, but are not its transport.
    adapters = {pid for pid, (parent, command) in processes.items()
                if "rpc_adapter.py --native " in command and parent in apps}
    native_servers = set()
    result["adapter_active"] = bool(adapters)
    for pid, (parent, command) in processes.items():
        executable = command.split()[0]
        bundled_codex = ("/ChatGPT.app/Contents/Resources/" in executable and
                         Path(executable).name == "codex")
        # Desktop may launch its signed CodexCLI.app binary instead of the
        # equivalent bin/codex entry point recorded in the manifest.
        if not (executable == str(native_target) or bundled_codex):
            continue
        if "app-server" not in command:
            continue
        if parent not in apps and parent not in adapters:
            continue
        if parent in apps:
            result["direct_native_app_server"] = True
            native_servers.add(pid)
    sidecars = {pid for pid, (parent, command) in processes.items()
                if parent in native_servers and ("desktop_bootstrap.py --native " in command
                                                 or "desktop_bootstrap.py --sidecar " in command)}
    result["adapter_active"] = bool(adapters or sidecars)
    return result


def status(root: Path = ROOT) -> dict:
    manifest = load_json(root / "manifest.json")
    config = load_json(root / "config.json")
    try:
        codex_config = tomllib.loads(Path(manifest["config_path"]).read_text())
    except (OSError, ValueError, KeyError):
        codex_config = {}
    default_model = codex_config.get("model")
    default_model = normalize_alias(default_model) or default_model
    if not isinstance(default_model, str) or not re.fullmatch(r"(?:effortlane-(?:auto|shadow)|gpt-\d+(?:\.\d+)*(?:-[a-z][a-z0-9]*)?)", default_model):
        default_model = None
    default_effort = codex_config.get("model_reasoning_effort")
    if default_effort not in ("low", "medium", "high", "xhigh", "max", "ultra"):
        default_effort = None
    default_mode = ("off" if manifest["config_state"] != "enabled" or config.get("mode") == "off" else
                    "shadow" if normalize_alias(default_model) == "effortlane-shadow" else
                    "auto" if normalize_alias(default_model) == "effortlane-auto" else "native")
    auto_roles = config.get("auto_roles")
    if not (isinstance(auto_roles, list) and "sol" in auto_roles and
            all(isinstance(role, str) and role in ("luna", "terra", "sol", "astra") for role in auto_roles)):
        auto_roles = ["luna", "terra", "sol"]
    effort_policy = config.get("effort_policy") if config.get("effort_policy") in ("jev", "fixed") else "jev"
    fixed_effort = config.get("fixed_effort") if config.get("fixed_effort") in ("low", "medium", "high", "xhigh", "max", "ultra") else "medium"
    shadow_policy = config.get("shadow_policy") if config.get("shadow_policy") in ("baseline", "completion_v1", "completion_v2", "completion_v3", "completion_v4") else "baseline"
    auto_policy = config.get("auto_policy") if config.get("auto_policy") in ("baseline", "completion_v1", "completion_v2", "completion_v3", "completion_v4") else "baseline"
    catalog = load_json(root / "models.json")
    from core import economical_roles, visible_roles
    available = visible_roles(load_json(root / "native-models.json"))
    effective_roles = economical_roles({role: available[role] for role in auto_roles if role in available},
                                       config.get("allow_dominated_roles") is True)
    process: dict = {"pid": None, "rss_kib": None, "elapsed": None}
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True, timeout=2)
        match = re.search(r"\bpid\s*=\s*(\d+)", out.stdout)
        if match:
            process["pid"] = int(match.group(1))
            ps = subprocess.run(["ps", "-o", "rss=,etime=", "-p", match.group(1)], capture_output=True, text=True, timeout=2)
            values = ps.stdout.split()
            if len(values) >= 2:
                process["rss_kib"] = int(values[0])
                process["elapsed"] = values[1]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    desktop = manifest.get("desktop", {})
    wrapper = desktop.get("wrapper_path", str(root / "app-server-wrapper"))
    native_binary = Path(manifest["native_target"])
    native_executable = native_binary.is_file() and os.access(native_binary, os.X_OK)
    configured_cli = Path(manifest.get("cli_target", manifest["native_target"]))
    cli_executable = configured_cli.is_file() and os.access(configured_cli, os.X_OK)
    cli_native_path = cli_catalog_path(root, "native-models.json")
    wrapper_issue = desktop_wrapper_issue(root, manifest)
    try:
        current_env = desktop_env()
    except (OSError, subprocess.TimeoutExpired):
        current_env = None
    runtime = desktop_runtime(native_binary)
    desktop_status = {
        "opted_in": bool(desktop.get("opted_in")),
        "enabled": bool(desktop.get("enabled")),
        "env_points_to_wrapper": current_env == wrapper,
        "env_present": bool(current_env),
        "env_changed_externally": bool(desktop.get("env_changed_externally")),
        "wrapper_present": Path(wrapper).exists(),
        "wrapper_matches_native_target": wrapper_issue is None,
        "wrapper_issue": wrapper_issue,
        "launch_agent_present": Path(desktop.get("agent_path", DESKTOP_AGENT)).exists(),
        "auto_routing_active": (bool(desktop.get("enabled")) and runtime["adapter_active"]
                                and manifest["config_state"] == "enabled" and config.get("mode") != "off"),
        "shadow_executor_model": available["sol"]["slug"] if "sol" in available else None,
        "limitation": "Desktop hostConfig.codex_cli_command overrides CODEX_CLI_PATH when set",
        "runtime": runtime,
    }
    return {
        "mode": config["mode"], "config_state": manifest["config_state"], "health": health(root),
        "codex_default": {"model": default_model, "effort": default_effort, "routing": default_mode},
        "policy": {"config_file": str(root / "config.json"), "auto_roles": auto_roles,
                   "effective_auto_roles": list(effective_roles),
                   "allow_dominated_roles": config.get("allow_dominated_roles") is True,
                   "effort_policy": effort_policy,
                   "fixed_effort": fixed_effort,
                   "auto_policy": auto_policy,
                   "shadow_policy": shadow_policy,
                   "large_context_sol_floor_tokens": config.get("large_context_sol_floor_tokens", 48000),
                   "astra_auto_allowed": "astra" in auto_roles},
        "port": config["port"], "catalog_models": len(catalog["models"]),
        "models": [normalize_alias(x.get("slug")) for x in catalog["models"] if normalize_alias(x.get("slug"))],
        "native_binary": manifest["native_target"],
        "native_binary_executable": native_executable,
        "native_binary_issue": None if native_executable else f"native_target missing or not executable: {native_binary}",
        "cli_binary": str(configured_cli),
        "cli_binary_executable": cli_executable,
        "cli_fallback_active": not cli_executable,
        "cli_catalog": str(cli_native_path),
        "cli_sol": select_sol(load_json(cli_native_path)),
        "daemon": process,
        "desktop": desktop_status,
        "preserved_user_changes": manifest.get("preserved_user_changes", []),
        "telemetry_bytes": (root / "state/telemetry.jsonl").stat().st_size if (root / "state/telemetry.jsonl").exists() else 0,
    }


def doctor(root: Path = ROOT) -> dict:
    """Read-only compatibility checks after a Codex or Desktop update."""
    current = status(root)
    manifest = load_json(root / "manifest.json")
    native = native_catalog(root / "native-models.json")
    managed = load_json(root / "models.json")
    cli_native = native_catalog(cli_catalog_path(root, "native-models.json"))
    cli_managed = load_json(cli_catalog_path(root, "models.json"))
    expected = managed_catalog(native)
    aliases = {item.get("slug"): item for item in managed.get("models", [])
               if isinstance(item, dict) and item.get("slug") in ("effortlane-auto", "effortlane-shadow")}
    expected_aliases = {item["slug"]: item for item in expected["models"]
                        if item.get("slug") in ("effortlane-auto", "effortlane-shadow")}
    native_binary = Path(manifest.get("native_target", ""))
    cli_binary = Path(manifest.get("cli_target", manifest.get("native_target", "")))
    checks = {
        "native_binary_executable": native_binary.is_file() and os.access(native_binary, os.X_OK),
        "cli_binary_executable": cli_binary.is_file() and os.access(cli_binary, os.X_OK),
        "cli_catalog_matches_latest_sol": {
            item.get("slug"): item for item in cli_managed.get("models", []) if isinstance(item, dict)
            and item.get("slug") in ("effortlane-auto", "effortlane-shadow")
        } == {
            item["slug"]: item for item in managed_catalog(cli_native)["models"]
            if item.get("slug") in ("effortlane-auto", "effortlane-shadow")
        },
        "cli_catalog_generation_valid": (
            not manifest.get("cli_catalog_generation")
            or cli_catalog_path(root, "native-models.json") != root / "native-models.json"),
        "desktop_wrapper_matches_native_target": desktop_wrapper_issue(root, manifest) is None,
        "managed_aliases_match_native_sol": aliases == expected_aliases,
        "gateway_healthy": current["health"],
        "desktop_adapter_active": (
            not current["desktop"]["runtime"]["app_running"]
            or current["desktop"]["runtime"]["adapter_active"] == current["desktop"].get("enabled", False)),
    }
    cache = Path(manifest.get("config_path", "")).parent / "models_cache.json"
    if cache.is_file():
        try:
            cached = load_json(cache)
            fetched = dt.datetime.fromisoformat(cached["fetched_at"].replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - fetched).total_seconds()
            if 0 <= age <= 600:
                refreshed = native_catalog(cache)
                # Description and picker order change often; only execution and
                # capability metadata requires reinstalling a managed catalog.
                def execution_view(catalog: dict) -> list[dict]:
                    return [{key: value for key, value in item.items() if key not in ("description", "priority")}
                            for item in catalog["models"]]
                checks["account_catalog_matches_installed"] = execution_view(refreshed) == execution_view(native)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass  # No usable fresh cache; the explicit refresh command fetches one.
    issues = []
    if not checks["native_binary_executable"]:
        issues.append(f"native_target missing or not executable: {native_binary}")
    wrapper_issue = desktop_wrapper_issue(root, manifest)
    if wrapper_issue:
        issues.append(wrapper_issue)
    if not checks["cli_binary_executable"] and cli_binary != native_binary:
        issues.append(f"cli_target missing or not executable: {cli_binary}; CLI falls back to native_target")
    if not checks["cli_catalog_matches_latest_sol"]:
        issues.append("CLI Effortlane alias metadata differs from its latest Sol model")
    if not checks["cli_catalog_generation_valid"]:
        issues.append("CLI catalog generation is incomplete; CLI falls back to Desktop catalog")
    if not checks["desktop_adapter_active"]:
        issues.append("Desktop runtime does not match Effortlane enable/disable state; fully quit and reopen ChatGPT.app")
    if checks.get("account_catalog_matches_installed") is False:
        issues.append("account model cache differs from installed catalog; refresh native models before updating Effortlane")
    return {"ok": all(checks.values()), "checks": checks, "issues": issues,
            "measurement": measurement_health(root),
            "note": "Static and local-process checks only. Stale account caches are skipped; run desktop-refresh-models to fetch fresh Desktop metadata. A native routed turn and built-in tools still need a smoke test after updates."}


def telemetry_files(root: Path) -> list[Path]:
    state = root / "state"
    archives = sorted(state.glob("telemetry.*.jsonl"))
    current = state / "telemetry.jsonl"
    return archives + ([current] if current.is_file() else [])


def measurement_health(root: Path = ROOT) -> dict:
    """Bound diagnostic work to two log tails; never print their contents."""
    from metrics import measurement_status
    rows = []
    files = telemetry_files(root)
    truncated = len(files) > 2
    for path in files[-2:]:
        try:
            with path.open("rb") as source:
                start = max(0, path.stat().st_size - 1_000_000)
                truncated |= start > 0
                source.seek(start)
                if start:
                    source.readline()
                for line in source:
                    try:
                        row = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(row, dict):
                        rows.append(row)
        except OSError:
            continue
    return {**measurement_status(rows), "bounded_tail_scan": True, "truncated": truncated}


def telemetry_rows(root: Path, cutoff: float | None = None):
    for path in telemetry_files(root):
        try:
            with path.open() as source:
                for line in source:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    timestamp = row.get("ts")
                    if (cutoff is not None and type(timestamp) in (int, float)
                            and 0 <= timestamp <= 4_000_000_000 and timestamp < cutoff):
                        continue
                    yield row
        except FileNotFoundError:  # Rotation may finish after the file list is read.
            continue


def report(root: Path = ROOT, weights: dict | None = None) -> dict:
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - 720 * 3600
    rows = list(telemetry_rows(root, cutoff))
    usage_rows = [row for row in rows if row.get("event", "usage") == "usage"]
    route_rows = [row for row in rows if row.get("event") == "route"]
    by_model: dict[str, dict] = {}
    by_client: dict[str, dict] = {}
    failures = 0
    for row in usage_rows:
        model = str(row.get("model") or "unknown")
        client = str(row.get("client") or "unknown")
        client_bucket = by_client.setdefault(client, {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
        client_bucket["calls"] += 1
        bucket = by_model.setdefault(model, {"calls": 0, "input_tokens": 0, "cached_input_tokens": 0,
                                             "output_tokens": 0, "jev_ms": 0, "efforts": {}})
        bucket["calls"] += 1
        for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
            value = row.get(field)
            if isinstance(value, int) and value >= 0:
                bucket[field] += value
                client_bucket[field] += value
        effort = str(row.get("effort") or "unknown")
        bucket["efforts"][effort] = bucket["efforts"].get(effort, 0) + 1
        failures += row.get("status") in ("failed", "error")
    proposals: dict[str, int] = {}
    reasons: dict[str, int] = {}
    model_bases: dict[str, int] = {}
    by_policy: dict[str, dict] = {}
    jev_ms = 0
    for row in route_rows:
        reason = row.get("reason")
        if isinstance(reason, str) and reason:
            reasons[reason] = reasons.get(reason, 0) + 1
        basis = row.get("model_basis")
        if basis in ("jev_work_shape", "sole_eligible_model"):
            model_bases[basis] = model_bases.get(basis, 0) + 1
        policy = row.get("policy") if row.get("policy") in ("baseline", "completion_v1", "completion_v2", "completion_v3", "completion_v4") else "unknown"
        policy_bucket = by_policy.setdefault(policy, {"decisions": 0, "proposed_models": {}, "work_shapes": {},
                                                      "confidence_samples": 0, "confidence_total": 0.0})
        policy_bucket["decisions"] += 1
        shape = row.get("work_shape")
        if shape in ("mechanical", "routine", "substantive", "unknown", "frontier"):
            shapes = policy_bucket["work_shapes"]
            shapes[shape] = shapes.get(shape, 0) + 1
        confidence = row.get("jev_confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence) and 0 <= confidence <= 1:
            policy_bucket["confidence_samples"] += 1
            policy_bucket["confidence_total"] += confidence
        proposed = row.get("proposed_model")
        if isinstance(proposed, str):
            proposals[proposed] = proposals.get(proposed, 0) + 1
            policy_models = policy_bucket["proposed_models"]
            policy_models[proposed] = policy_models.get(proposed, 0) + 1
        value = row.get("jev_ms")
        if isinstance(value, (int, float)) and value >= 0:
            jev_ms += value
    for bucket in by_policy.values():
        samples = bucket.pop("confidence_samples")
        total = bucket.pop("confidence_total")
        bucket["confidence"] = {"samples": samples, "mean": round(total / samples, 4) if samples else None}
    route_ids = {row.get("route_id"): row for row in route_rows
                 if isinstance(row.get("route_id"), str) and re.fullmatch(r"[a-f0-9]{24}", row["route_id"])}
    linked: dict[str, list[dict]] = {}
    outcome_by_model: dict[str, dict] = {}
    outcome_by_shape: dict[str, dict] = {}
    outcome_by_policy: dict[str, dict] = {}
    def add_outcome(buckets: dict, key: str, calls: list[dict]) -> None:
        bucket = buckets.setdefault(key, {"turns": 0, "completed_turns": 0, "failed_turns": 0,
                                          "cancelled_turns": 0, "command_failures": 0,
                                          "prior_failed_turns": 0, "usage_missing": 0, "model_calls": 0,
                                          "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0})
        bucket["turns"] += 1
        bucket["model_calls"] += len(calls)
        bucket["completed_turns"] += calls[-1].get("status") == "ok"
        bucket["failed_turns"] += calls[-1].get("status") == "error"
        bucket["cancelled_turns"] += calls[-1].get("status") == "cancelled"
        bucket["prior_failed_turns"] += any(call.get("prior_failed") is True for call in calls)
        bucket["usage_missing"] += any(call.get("usage_missing") is True for call in calls)
        for usage in calls:
            failures = usage.get("command_failures")
            if isinstance(failures, int) and not isinstance(failures, bool) and 0 <= failures <= 255:
                bucket["command_failures"] += failures
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                value = usage.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    bucket[field] += value
    for usage in usage_rows:
        route_id = usage.get("route_id")
        route = route_ids.get(route_id) if isinstance(route_id, str) else None
        if (not route or route.get("session") != usage.get("session")
                or route.get("client") != usage.get("client") or route.get("model") != usage.get("model")
                or route.get("effort") != usage.get("effort")):
            continue
        linked.setdefault(route_id, []).append(usage)
    for route_id, calls in linked.items():
        route = route_ids[route_id]
        add_outcome(outcome_by_model, str(route.get("model") or "unknown"), calls)
        add_outcome(outcome_by_shape, str(route.get("work_shape") or "not_classified"), calls)
        add_outcome(outcome_by_policy, str(route.get("policy") or "unknown"), calls)
    totals = {field: sum(row[field] for row in by_model.values()) for field in ("input_tokens", "cached_input_tokens", "output_tokens")}
    comparison: dict = {"token_hold_constant": totals, "weights_source": "none", "actual_units": None,
                        "all_sol_units": None, "all_astra_units": None}
    if weights:
        try:
            models = load_json(root / "native-models.json")
            from core import visible_roles
            roles = visible_roles(models)
            def units(tokens: dict, rate: dict) -> float:
                if not all(isinstance(rate.get(key), (int, float)) and rate[key] >= 0 for key in ("input", "cached_input", "output")):
                    raise ValueError("weights require nonnegative input/cached_input/output")
                uncached = max(0, tokens["input_tokens"] - tokens["cached_input_tokens"])
                return round(uncached * rate["input"] + tokens["cached_input_tokens"] * rate["cached_input"] + tokens["output_tokens"] * rate["output"], 4)
            actual = sum(units(tokens, weights[model]) for model, tokens in by_model.items())
            comparison.update({"weights_source": "user_supplied", "actual_units": round(actual, 4),
                               "all_sol_units": units(totals, weights[roles["sol"]["slug"]]),
                               "all_astra_units": units(totals, weights[roles["astra"]["slug"]])})
        except (KeyError, TypeError, ValueError, ImportError):
            comparison["weights_source"] = "incomplete_user_weights"
    return {
        "coverage": {"window_hours": 720, "files": len(telemetry_files(root)),
                     "first_ts": min((row["ts"] for row in rows if isinstance(row.get("ts"), (int, float))), default=None),
                     "last_ts": max((row["ts"] for row in rows if isinstance(row.get("ts"), (int, float))), default=None)},
        "observed": {"calls": len(usage_rows), "failures": failures,
                     "usage_missing_count": sum(row.get("usage_missing") is True for row in usage_rows),
                     "weak_quality_signals": {
                         "prior_failed_turns": sum(row.get("prior_failed") is True for row in usage_rows),
                         "manual_overrides": sum(row.get("manual_override") is True for row in usage_rows),
                         "nonzero_command_exits": sum(row.get("command_failures", 0) for row in usage_rows
                                                      if isinstance(row.get("command_failures"), int)
                                                      and not isinstance(row.get("command_failures"), bool)
                                                      and 0 <= row["command_failures"] <= 255),
                     },
                     "by_model": by_model, "by_client": by_client},
        "routes": {"decisions": len(route_rows), "switches": sum(row.get("switched") is True for row in route_rows),
                   "jev_ms": jev_ms, "proposed_models": proposals, "reasons": reasons,
                   "model_bases": model_bases, "by_policy": by_policy,
                   "outcomes": {"linked_turns": len(linked), "unlinked_routes": len(route_rows) - len(linked),
                                "by_model": outcome_by_model, "by_work_shape": outcome_by_shape,
                                "by_policy": outcome_by_policy}},
        "counterfactual": comparison,
        "note": "Linked CLI launches sum observed model calls; Desktop native usage may report only the last call of a turn. Completion and command exits are weak signals, not task correctness. Token-hold-constant comparisons are sensitivity estimates, not Pro cost or quality-equivalent savings.",
    }


def evaluate(root: Path = ROOT, hours: int = 24, labels_path: Path | None = None,
             since: float | None = None) -> dict:
    """Audit the active policy's route funnel without treating weak signals as savings."""
    if not isinstance(hours, int) or isinstance(hours, bool) or not 1 <= hours <= 24 * 30:
        raise ValueError("hours must be between 1 and 720")
    cutoff = dt.datetime.now(dt.timezone.utc).timestamp() - hours * 3600
    if since is not None:
        if not isinstance(since, (int, float)) or isinstance(since, bool) or since < 0:
            raise ValueError("since must be a Unix timestamp")
        cutoff = max(cutoff, since)
    rows = [row for row in telemetry_rows(root, cutoff)
            if isinstance(row.get("ts"), (int, float)) and not isinstance(row["ts"], bool)]
    routes = [row for row in rows if row.get("event") == "route" and row.get("policy") == "completion_v4"
              and row.get("mode") in ("auto", "shadow")]
    auto_calls = [row for row in rows if row.get("event") == "usage" and row.get("mode") == "auto"]
    auto_models: dict[str, int] = {}
    auto_clients: dict[str, int] = {}
    for row in auto_calls:
        model = row.get("model") if isinstance(row.get("model"), str) else "unknown"
        client = row.get("client") if isinstance(row.get("client"), str) else "unknown"
        auto_models[model] = auto_models.get(model, 0) + 1
        auto_clients[client] = auto_clients.get(client, 0) + 1
    def counts(key: str, missing: str = "unknown") -> dict[str, int]:
        result: dict[str, int] = {}
        for row in routes:
            value = row.get(key)
            label = value if isinstance(value, str) and value else missing
            result[label] = result.get(label, 0) + 1
        return dict(sorted(result.items()))
    route_ids = {row["route_id"]: row for row in routes
                 if isinstance(row.get("route_id"), str) and re.fullmatch(r"[a-f0-9]{24}", row["route_id"])}
    linked: dict[str, list[dict]] = {}
    for usage in rows:
        if usage.get("event") != "usage" or not isinstance(usage.get("route_id"), str):
            continue
        route_id = usage["route_id"]
        route = route_ids.get(route_id)
        if (route is None or route.get("session") != usage.get("session")
                or route.get("client") != usage.get("client") or route.get("model") != usage.get("model")
                or route.get("effort") != usage.get("effort")):
            continue
        linked.setdefault(route_id, []).append(usage)
    outcomes: dict[str, dict] = {}
    for route_id, calls in linked.items():
        model = route_ids[route_id]["model"]
        bucket = outcomes.setdefault(model, {"turns": 0, "ok": 0, "error": 0, "cancelled": 0,
                                             "usage_missing": 0, "nonzero_command_exits": 0, "model_calls": 0,
                                             "recorded_input_tokens": 0, "recorded_cached_input_tokens": 0,
                                             "recorded_output_tokens": 0})
        bucket["turns"] += 1
        bucket["model_calls"] += len(calls)
        status = calls[-1].get("status")
        if status in ("ok", "error", "cancelled"):
            bucket[status] += 1
        bucket["usage_missing"] += any(call.get("usage_missing") is True for call in calls)
        for usage in calls:
            failures = usage.get("command_failures")
            if isinstance(failures, int) and not isinstance(failures, bool) and 0 <= failures <= 255:
                bucket["nonzero_command_exits"] += failures
            for source_key, target_key in (("input_tokens", "recorded_input_tokens"),
                                           ("cached_input_tokens", "recorded_cached_input_tokens"),
                                           ("output_tokens", "recorded_output_tokens")):
                value = usage.get(source_key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    bucket[target_key] += value
    human_outcomes: dict[str, dict[str, int]] = {}
    if labels_path is not None:
        if labels_path.stat().st_size > 1_000_000:
            raise ValueError("labels file exceeds 1 MB")
        seen_labels: set[str] = set()
        with labels_path.open() as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    label = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError("invalid labels JSONL") from exc
                if not isinstance(label, dict) or not isinstance(label.get("route_id"), str) or not re.fullmatch(r"[a-f0-9]{24}", label["route_id"]) or label.get("outcome") not in ("accepted", "rework", "failed"):
                    raise ValueError("labels require route_id and accepted/rework/failed outcome")
                route_id = label["route_id"]
                if route_id in seen_labels:
                    raise ValueError("duplicate route_id in labels")
                seen_labels.add(route_id)
                if route_id not in linked:
                    continue
                model = route_ids[route_id]["model"]
                bucket = human_outcomes.setdefault(model, {"accepted": 0, "rework": 0, "failed": 0})
                bucket[label["outcome"]] += 1
    return {
        "policy": "completion_v4", "window_hours": hours, "since": cutoff,
        "files": len(telemetry_files(root)), "routes": len(routes),
        "auto_model_calls": {"calls": len(auto_calls), "by_model": auto_models, "by_client": auto_clients,
                             "gateway_blocks": sum(row.get("reason") == "requires_native_model_selection"
                                                   for row in auto_calls)},
        "by_mode": counts("mode"), "by_client": counts("client"),
        "executed_models": counts("model"), "proposed_models": counts("proposed_model"),
        "reasons": counts("reason"), "work_shapes": counts("work_shape", "not_classified"),
        "proposal_held_by_cache": sum(row.get("reason") in ("cache_hysteresis", "shadow_cache_hysteresis")
                                      and row.get("model") != row.get("proposed_model") for row in routes),
        "linked_turns": len(linked), "unlinked_routes": len(routes) - len(linked),
        "outcomes_by_executed_model": outcomes,
        "human_labels": {"linked_labeled_turns": sum(sum(bucket.values()) for bucket in human_outcomes.values()),
                         "by_executed_model": human_outcomes},
        "quality_equivalent_savings": None,
        "note": "Legacy model_calls fields count usage records, not guaranteed individual inferences. Use metrics usage_scope for native-thread turn totals versus last-call or historical samples. gateway_blocks are late proposals that could not safely change the native harness. Command exits and turn status do not establish correctness. Human labels are local subjective outcomes, not paired counterfactuals. No quality-equivalent all-Sol or all-Astra savings estimate exists.",
    }


def trace(thread_id: str, root: Path = ROOT) -> dict:
    """Explain one thread using only hashed, allowlisted local metadata."""
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", thread_id):
        raise ValueError("thread ID must be a UUID")
    digest = hashlib.sha256(thread_id.lower().encode()).hexdigest()
    session = digest[:24]
    intent = {}
    try:
        entry = load_json(root / "state/desktop-intent.json").get(digest[:32], {})
        if isinstance(entry, dict):
            intent = {key: entry[key] for key in ("alias", "actual", "effort", "effort_override")
                      if key in entry and isinstance(entry[key], str)}
    except (OSError, ValueError, TypeError):
        pass
    routes = []
    usage: dict[str, dict] = {}
    usage_events = 0
    last_executor: dict = {}
    for row in telemetry_rows(root, dt.datetime.now(dt.timezone.utc).timestamp() - 720 * 3600):
        if row.get("session") != session:
            continue
        if row.get("event") == "route":
            view = {key: row.get(key) for key in
                    ("ts", "client", "mode", "policy", "model", "effort",
                     "proposed_model", "proposed_effort", "reason", "jev_ms", "router_ms",
                     "jev_confidence", "jev_selected_probability", "jev_model",
                     "jev_effort_confidence", "work_shape", "model_basis")}
            route_id = row.get("route_id")
            view["route_id"] = route_id if isinstance(route_id, str) and re.fullmatch(r"[a-f0-9]{24}", route_id) else None
            routes.append(view)
            routes = routes[-64:]
        elif row.get("event") == "usage":
            usage_events += 1
            key = str(row.get("model") or "unknown") + "/" + str(row.get("effort") or "unknown")
            bucket = usage.setdefault(key, {"calls": 0, "input_tokens": 0,
                                            "cached_input_tokens": 0, "output_tokens": 0})
            bucket["calls"] += 1
            for field in ("input_tokens", "cached_input_tokens", "output_tokens"):
                value = row.get(field)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    bucket[field] += value
        if row.get("event") in ("route", "usage"):
            last_executor = {"model": row.get("model"), "effort": row.get("effort"),
                             "event": row.get("event"), "ts": row.get("ts"),
                             "status": row.get("status")}
    return {"thread_hash": session, "selection": intent, "routes": routes,
            "usage_events": usage_events, "executor_usage": usage, "last_executor": last_executor,
            "note": "Usage events are model calls, not user turns. A native concrete_model usage reason does not override a preceding Auto route."}


def route(thread_id: str, root: Path = ROOT) -> dict:
    """Compact actual route for a Desktop thread, without task content."""
    details = trace(thread_id, root)
    observed = details["last_executor"]
    selection = details["selection"]
    return {"thread_hash": details["thread_hash"],
            "selector": selection.get("alias"),
            "model": observed.get("model") or selection.get("actual"),
            "effort": observed.get("effort") or selection.get("effort"),
            "source": observed.get("event") or "saved_selection",
            "status": observed.get("status"),
            "ts": observed.get("ts")}


def _parse_cli(argv: list[str]) -> tuple[str, str | None, bool]:
    """Return supported command, initial prompt, and explicit override flag."""
    command = "interactive"
    positional: list[str] = []
    explicit = False
    value_options = {"-c", "--config", "-m", "--model", "-p", "--profile", "-i", "--image", "-C", "--cd",
                     "--add-dir", "-s", "--sandbox", "-a", "--ask-for-approval", "--output-schema", "-o",
                     "--output-last-message", "--color", "--thread-source", "--enable", "--disable", "--remote",
                     "--remote-auth-token-env", "--local-provider"}
    safe_switches = {"--json", "--search", "--no-alt-screen", "--skip-git-repo-check", "--ephemeral",
                     "--ignore-rules", "--dangerously-bypass-approvals-and-sandbox", "--dangerously-bypass-hook-trust",
                     "--approve-for-me", "--strict-config", "--all", "--last", "--include-non-interactive", "--worktree"}
    native_commands = {"agents", "review", "login", "logout", "mcp", "plugin", "mcp-server", "app-server",
                       "remote-control", "app", "completion", "update", "doctor", "sandbox", "debug", "apply",
                       "a", "resume", "queue", "archive", "delete", "migrate-rollouts", "unarchive", "fork",
                       "cloud", "exec-server", "features", "help"}
    i = 0
    while i < len(argv):
        item = argv[i]
        if item in ("exec", "e") and command == "interactive" and not positional:
            command = "exec"
        elif item == "resume" and command == "interactive" and not positional:
            command = "resume"
        elif item == "resume" and command == "exec" and not positional:
            command = "exec-resume"
        elif item == "--":
            positional.extend(argv[i + 1:]); break
        elif item in value_options:
            if i + 1 >= len(argv):
                return "passthrough", None, True
            value = argv[i + 1]
            if item in ("-m", "--model", "-p", "--profile", "--remote", "--local-provider"):
                explicit = True
            if item in ("-c", "--config") and value.split("=", 1)[0] in ("model", "model_provider", "model_provider_id", "openai_base_url", "model_catalog_json"):
                explicit = True
            i += 1
        elif item.startswith("--model=") or item.startswith("--profile=") or item.startswith("--remote="):
            explicit = True
        elif item.startswith("--config=") and item.partition("=")[2].split("=", 1)[0] in ("model", "model_provider", "model_provider_id", "openai_base_url", "model_catalog_json"):
            explicit = True
        elif item in ("--oss", "--ignore-user-config"):
            explicit = True
        elif item == "-":
            positional.append(item)
        elif item.startswith("-"):
            if item not in safe_switches:
                return "passthrough", None, True
        else:
            if command == "interactive" and not positional and item in native_commands:
                return "passthrough", None, True
            if command == "exec" and not positional and item in ("review", "fork", "help"):
                return "passthrough", None, True
            positional.append(item)
        i += 1
    if command == "exec-resume":
        # Two positional args: session then prompt. --last permits a prompt alone.
        if "--last" in argv:
            prompt = positional[-1] if positional else None
        else:
            prompt = positional[1] if len(positional) == 2 else None
        return command, prompt, explicit
    if command == "resume":
        return command, None, explicit
    if command in ("interactive", "exec"):
        if len(positional) > 1:
            return "passthrough", None, True
        return command, positional[0] if positional else None, explicit
    return "passthrough", None, True


def _custom_transport(argv: list[str]) -> bool:
    for i, arg in enumerate(argv):
        if arg in ("-p", "--profile", "--oss", "--local-provider", "--remote", "--ignore-user-config") or arg.startswith(("--profile=", "--local-provider=", "--remote=")):
            return True
        value = argv[i + 1] if arg in ("-c", "--config") and i + 1 < len(argv) else arg.partition("=")[2] if arg.startswith("--config=") else ""
        if value.split("=", 1)[0] in ("openai_base_url", "model_provider", "model_provider_id"):
            return True
    return False


def _cli_endpoint_args(root: Path, config: dict, route_token: str | None = None) -> list[str]:
    capability = Path(config["capability_file"]).read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", capability):
        raise ValueError("invalid local capability")
    port = int(config["port"])
    if route_token is not None and not re.fullmatch(r"[0-9a-f]{16}", route_token):
        raise ValueError("invalid route token")
    suffix = "/" + route_token if route_token else ""
    return ["-c", "openai_base_url=" + toml_string(f"http://127.0.0.1:{port}/{capability}/cli{suffix}")]


def _cli_alias_catalog_args(root: Path) -> list[str]:
    return ["-c", "model_catalog_json=" + toml_string(str(cli_catalog_path(root, "models.json")))]


def _cli_native_catalog_args(root: Path) -> list[str]:
    return ["-c", "model_catalog_json=" + toml_string(str(cli_catalog_path(root, "native-models.json")))]


def _cli_effort_override(argv: list[str]) -> str | None:
    for index, arg in enumerate(argv):
        value = argv[index + 1] if arg in ("-c", "--config") and index + 1 < len(argv) else arg.partition("=")[2] if arg.startswith("--config=") else ""
        name, separator, raw = value.partition("=")
        if separator and name == "model_reasoning_effort":
            effort = raw.strip().strip("\"'")
            if effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
                return effort
    return None


def _strip_cli_alias_model(argv: list[str]) -> tuple[list[str], str | None]:
    """Treat a synthetic alias as a route selector, not an executor."""
    clean: list[str] = []
    alias = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        value = argv[index + 1] if index + 1 < len(argv) else None
        if arg in ("-m", "--model") and normalize_alias(value):
            alias, index = normalize_alias(value), index + 2
            continue
        if arg.startswith("--model=") and normalize_alias(arg.partition("=")[2]):
            alias, index = normalize_alias(arg.partition("=")[2]), index + 1
            continue
        config_value = value if arg in ("-c", "--config") else arg.partition("--config=")[2] if arg.startswith("--config=") else None
        if config_value and config_value.startswith("model="):
            selected = config_value.partition("=")[2].strip().strip("\"'")
            if normalize_alias(selected):
                alias, index = normalize_alias(selected), index + (2 if arg in ("-c", "--config") else 1)
                continue
        clean.append(arg)
        index += 1
    return clean, alias


def brand_cli_args(argv: list[str]) -> list[str]:
    """Normalize public flags/model IDs without changing positional prompt text."""
    flags = {"--effortlane-auto": "--jev-auto", "--effortlane-shadow": "--jev-shadow",
             "--effortlane-off": "--jev-off"}
    result = []
    previous = None
    for arg in argv:
        if previous in ("-m", "--model"):
            value = normalize_alias(arg) or arg
        elif arg.startswith("--model="):
            value = "--model=" + (normalize_alias(arg[8:]) or arg[8:])
        elif previous in ("-c", "--config") or arg.startswith("--config="):
            prefix = "--config=" if arg.startswith("--config=") else ""
            config_arg = arg[len(prefix):]
            key, separator, raw = config_arg.partition("=")
            alias = normalize_alias(raw.strip().strip("\"'")) if key == "model" and separator else None
            value = prefix + "model=" + json.dumps(alias) if alias else arg
        else:
            value = flags.get(arg, arg)
        result.append(value)
        previous = arg
    return result


def cli_args(argv: list[str], root: Path = ROOT, stdin_tty: bool = True) -> list[str]:
    argv = brand_cli_args(argv)
    manifest = load_json(root / "manifest.json")
    native = cli_target(manifest)
    bypass = str(Path(manifest["bin_dir"]) / "codex-native")
    mode_flag = next((x for x in argv if x in ("--jev-auto", "--jev-shadow", "--jev-off")), None)
    clean, selected_alias = _strip_cli_alias_model(
        [x for x in argv if x not in ("--jev-auto", "--jev-shadow", "--jev-off")])
    if selected_alias and mode_flag is None:
        mode_flag = "--jev-shadow" if selected_alias == "effortlane-shadow" else "--jev-auto"
    command, prompt, explicit = _parse_cli(clean)
    config = load_json(root / "config.json")
    try:
        codex_config = tomllib.loads(Path(manifest["config_path"]).read_text())
    except (OSError, ValueError, KeyError):
        codex_config = {}
    if mode_flag is None and normalize_alias(codex_config.get("model")):
        mode_flag = "--jev-shadow" if normalize_alias(codex_config["model"]) == "effortlane-shadow" else "--jev-auto"
    logical_alias = "effortlane-shadow" if mode_flag == "--jev-shadow" else "effortlane-auto"
    try:
        sol = select_sol(native_catalog(cli_catalog_path(root, "native-models.json")))
    except (OSError, ValueError, KeyError):
        sol = config.get("fallback_model", SOL)
    if mode_flag == "--jev-off":
        return [native if _custom_transport(clean) else bypass, *clean]
    if _custom_transport(clean):
        return [native, *clean]
    enabled = config.get("mode") != "off" and manifest["config_state"] == "enabled"
    healthy = health(root) if enabled else False
    endpoint = _cli_endpoint_args(root, config) if enabled and healthy and command != "passthrough" else []
    if command == "passthrough":
        # Mobile Remote bypasses our RPC adapter. Give its native daemon a
        # concrete catalog, even while local pickers advertise Effortlane.
        if "remote-control" in clean and enabled:
            concrete = [] if explicit else ["-m", sol]
            return [native, *_cli_native_catalog_args(root), *concrete, *clean]
        return [native if healthy else bypass, *clean]
    if explicit:
        return [native if healthy else bypass, *endpoint, *clean]
    if command == "resume":
        return [native, *endpoint, *_cli_alias_catalog_args(root), *clean] if healthy else [bypass, "-m", sol, *clean]
    if prompt == "-":
        return [native, *endpoint, *_cli_alias_catalog_args(root), "-m", logical_alias, *clean] if healthy else [bypass, "-m", sol, *clean]
    if command == "interactive" and prompt is None:
        return [native, *endpoint, *_cli_alias_catalog_args(root), "-m", logical_alias, *clean] if healthy else [bypass, "-m", sol, *clean]
    if prompt is None:
        return [native if healthy else bypass, *endpoint, "-m", sol, *clean]
    if not enabled:
        return [bypass, *clean]
    if not healthy:
        return [bypass, "-m", sol, *clean]
    if command == "exec-resume":
        return [native, *endpoint, *_cli_alias_catalog_args(root), *clean]
    from core import Router, cli_route_id
    payload = {"model": "effortlane-auto", "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt[:12000]}]}]}
    try:
        router = Router(config_path=root / "config.json", catalog_path=cli_catalog_path(root, "native-models.json"),
                        state_path=root / "state/leases.json", telemetry_path=root / "state/telemetry.jsonl")
        requested_mode = "shadow" if mode_flag == "--jev-shadow" else "auto"
        route_token = secrets.token_hex(8)
        decision = router.decide(payload, client="cli", session_id="cli-" + route_token,
                                 native_selection=True, mode_override=requested_mode)
        model = decision["model"]
        effort = decision.get("effort")
        if not isinstance(model, str) or not model or normalize_alias(model):
            raise ValueError("invalid native selection")
        configured_roles = config.get("auto_roles")
        astra_allowed = (isinstance(configured_roles, list) and "sol" in configured_roles
                         and all(role in ("luna", "terra", "sol", "astra") for role in configured_roles)
                         and "astra" in configured_roles)
        if model.endswith("-astra") and not astra_allowed:
            model, effort = sol, "high"
        requested_effort = _cli_effort_override(clean)
        if requested_mode == "shadow" and not requested_effort:
            global_effort = codex_config.get("model_reasoning_effort")
            if global_effort in ("low", "medium", "high", "xhigh", "max", "ultra"):
                requested_effort = global_effort
        if requested_effort:
            from core import visible_roles
            roles = visible_roles(router._catalog())
            selected = next((item for item in roles.values() if item["slug"] == model), None)
            if selected and requested_effort not in selected["efforts"]:
                model = sol
            effort = requested_effort
        decision["model"], decision["effort"] = model, effort
        decision["client"] = "cli"
        decision["route_id"] = cli_route_id(route_token)
        router.record_usage(decision, None, "ok", event="route")
        extras = ["-m", model]
        if effort and not requested_effort:
            extras.extend(["-c", "model_reasoning_effort=" + toml_string(effort)])
        return [native, *_cli_endpoint_args(root, config, route_token), *extras,
                *_cli_native_catalog_args(root), *clean]
    except Exception:
        return [native, *endpoint, "-m", sol, *_cli_native_catalog_args(root), *clean]


def native_overrides(root: Path) -> list[str]:
    manifest = load_json(root / "manifest.json")
    config = tomllib.loads(Path(manifest["config_path"]).read_text())
    # A redundant base URL override makes Codex warn that the model picker is
    # unsupported. Only force the native endpoint when bypassing a legacy
    # router URL still present in the user's config.
    endpoint = (["-c", "openai_base_url=" + toml_string(NATIVE_URL)]
                if "127.0.0.1:43191" in str(config.get("openai_base_url", "")) else [])
    return [*endpoint, "-c", "model_catalog_json=" + toml_string(str(cli_catalog_path(root, "native-models.json")))]


def native_args(argv: list[str], root: Path = ROOT) -> list[str]:
    manifest = load_json(root / "manifest.json")
    real = cli_target(manifest)
    config = load_json(root / "config.json")
    command, _, _ = _parse_cli(argv)
    explicit_model = False
    for i, arg in enumerate(argv):
        if arg in ("-m", "--model", "-p", "--profile") or arg.startswith(("--model=", "--profile=")):
            explicit_model = True
        if arg in ("-c", "--config") and i + 1 < len(argv) and argv[i + 1].split("=", 1)[0] == "model":
            explicit_model = True
        if arg.startswith("--config=model="):
            explicit_model = True
    # The native shim never inherits a synthetic default alias from global config.
    model = [] if explicit_model or command in ("passthrough", "exec-resume") else ["-m", config.get("fallback_model", SOL)]
    return [real, *native_overrides(root), *model, *argv]


def native_main() -> None:
    args = native_args(sys.argv[1:])
    os.execv(args[0], args)


def cli_bridge_args(argv: list[str], root: Path = ROOT) -> list[str] | None:
    """Use the native TUI with pre-turn routing when an alias is selected."""
    argv = brand_cli_args(argv)
    try:
        manifest = load_json(root / "manifest.json")
        config = load_json(root / "config.json")
        codex_config = tomllib.loads(Path(manifest["config_path"]).read_text())
        if (manifest.get("config_state") != "enabled" or config.get("mode") == "off"
                or not health(root) or _custom_transport(argv) or "--jev-off" in argv
                or codex_config.get("model_provider", "openai") != "openai"
                or codex_config.get("openai_base_url") not in (None, NATIVE_URL)):
            return None
    except (OSError, ValueError, KeyError):
        return None
    args = [arg for arg in argv if arg not in ("--jev-auto", "--jev-shadow")]
    command, _, _ = _parse_cli(args)
    if command not in ("interactive", "resume"):
        return None
    explicit = None
    for index, arg in enumerate(args):
        if arg in ("-m", "--model") and index + 1 < len(args):
            explicit = args[index + 1]
        elif arg.startswith("--model="):
            explicit = arg.partition("=")[2]
        elif arg in ("-c", "--config") and index + 1 < len(args):
            name, sep, value = args[index + 1].partition("=")
            if name == "model" and sep:
                explicit = value.strip().strip("\"'")
        elif arg.startswith("--config=model="):
            explicit = arg.partition("model=")[2].strip().strip("\"'")
    if explicit and explicit not in ("effortlane-auto", "effortlane-shadow"):
        return None
    if "--jev-shadow" in argv:
        alias = "effortlane-shadow"
    elif "--jev-auto" in argv:
        alias = "effortlane-auto"
    elif explicit in ("effortlane-auto", "effortlane-shadow"):
        alias = explicit
    elif normalize_alias(codex_config.get("model")):
        alias = normalize_alias(codex_config["model"])
    else:
        alias = "effortlane-shadow" if config.get("mode") == "shadow" else "effortlane-auto"
    if alias not in ("effortlane-auto", "effortlane-shadow"):
        return None
    return [*_cli_alias_catalog_args(root), *(args if explicit else ["-m", alias, *args])]


def trial_command(args: list[str], root: Path = ROOT) -> dict:
    """Register observations only; never apply a model or routing setting."""
    from trials import start, finish, reopen, report as trial_report
    usage = ("usage: effortlane trial start THREAD --kind mechanical|routine|debugging|design "
             "--scope component|cross_component --risk low|high --uncertainty known|investigation "
             "--effort low|medium|high|xhigh|max|ultra [--check tests|build|review] [--client cli|desktop] [--arm auto|baseline]; "
             "trial finish TASK --outcome accepted|rework|failed --checks passed|failed|not_run; "
             "trial reopen TASK; trial report [--hours 1..720]")
    if not args:
        raise ValueError(usage)
    command = args[0]
    if command == "report":
        if len(args) not in (1, 3) or (len(args) == 3 and args[1] != "--hours"):
            raise ValueError(usage)
        try:
            hours = int(args[2]) if len(args) == 3 else 168
        except ValueError:
            raise ValueError("hours must be 1..720") from None
        if not 1 <= hours <= 720:
            raise ValueError("hours must be 1..720")
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        return trial_report(root, list(telemetry_rows(root, now - hours * 3600)), hours, now=now)
    if command == "reopen" and len(args) == 2:
        return reopen(root, args[1])
    if command not in ("start", "finish") or len(args) < 2:
        raise ValueError(usage)
    options = args[2:]
    allowed = ({"--kind", "--scope", "--risk", "--uncertainty", "--effort", "--check", "--client", "--arm"}
               if command == "start" else {"--outcome", "--checks"})
    required = allowed - {"--check", "--arm", "--client"} if command == "start" else allowed
    if (len(options) % 2 or any(key not in allowed for key in options[::2])
            or len(set(options[::2])) != len(options) // 2 or not required <= set(options[::2])):
        raise ValueError(usage)
    settings = {key[2:]: value for key, value in zip(options[::2], options[1::2])}
    if command == "start":
        return start(root, args[1], project=Path.cwd(), **settings)
    return finish(root, args[1], **settings)


def main() -> None:
    argv = sys.argv[1:]
    commands = {"install", "status", "doctor", "report", "metrics", "cost", "savings", "chat", "evaluate", "trace", "route", "disable", "enable", "rollback", "update", "desktop-refresh-native", "desktop-refresh-models", "cli-set-native", "cli-refresh-models",
                "desktop-enable", "desktop-disable", "desktop-safe", "native-shadow", "claude", "claude-report", "trial"}
    # Only Effortlane and its legacy command manage installation. The transparent codex link always passes native commands.
    invoked = Path(sys.argv[0]).name
    if invoked in ("effortlane", "jev-codex", "manage.py") and argv and argv[0] in commands:
        cmd = argv[0]
        try:
            if cmd == "install":
                options = argv[1:]
                if len(options) % 2 or any(options[i] not in ("--execution-binary", "--key-file") for i in range(0, len(options), 2)):
                    raise ValueError("usage: manage.py install [--execution-binary /absolute/path/to/codex] [--key-file /absolute/path/to/key]")
                settings = dict(zip(options[::2], options[1::2]))
                install(execution_binary=Path(settings["--execution-binary"]) if "--execution-binary" in settings else None,
                        key_path=Path(settings["--key-file"]) if "--key-file" in settings else None)
            elif cmd == "disable": disable()
            elif cmd == "enable": enable()
            elif cmd == "desktop-enable":
                desktop_enable()
            elif cmd == "desktop-disable": desktop_disable()
            elif cmd == "desktop-safe": print(json.dumps(desktop_safe(), indent=2))
            elif cmd == "native-shadow":
                from native_shadow import configure, report as native_report
                if argv[1:] not in (["enable"], ["disable"], ["report"]):
                    raise ValueError("usage: effortlane native-shadow enable|disable|report")
                result = native_report(ROOT) if argv[1] == "report" else configure(ROOT, argv[1] == "enable")
                print(json.dumps(result, indent=2))
            elif cmd == "desktop-refresh-native":
                if len(argv) != 3 or argv[1] != "--native":
                    raise ValueError("usage: effortlane desktop-refresh-native --native /absolute/path/to/codex")
                print(json.dumps(desktop_refresh_native(Path(argv[2])), indent=2))
            elif cmd == "desktop-refresh-models":
                if len(argv) != 1:
                    raise ValueError("usage: effortlane desktop-refresh-models")
                print(json.dumps(desktop_refresh_catalog(), indent=2))
            elif cmd == "cli-set-native":
                if len(argv) != 3 or argv[1] != "--native":
                    raise ValueError("usage: effortlane cli-set-native --native /absolute/path/to/codex")
                print(json.dumps(cli_set_target(Path(argv[2])), indent=2))
            elif cmd == "cli-refresh-models":
                if len(argv) != 1 and (len(argv) != 3 or argv[1] != "--catalog"):
                    raise ValueError("usage: effortlane cli-refresh-models [--catalog /absolute/path/to/native-account-models.json]")
                print(json.dumps(cli_refresh_catalog(Path(argv[2]) if len(argv) == 3 else None), indent=2))
            elif cmd == "rollback": rollback()
            elif cmd == "update": update_catalog()
            elif cmd == "status": print(json.dumps(status(), indent=2))
            elif cmd == "doctor": print(json.dumps(doctor(), indent=2))
            elif cmd == "trial": print(json.dumps(trial_command(argv[1:]), indent=2))
            elif cmd == "report":
                if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--weights"):
                    raise ValueError("usage: effortlane report [--weights path.json]")
                weights = load_json(Path(argv[2])) if len(argv) == 3 else None
                print(json.dumps(report(weights=weights), indent=2))
            elif cmd == "metrics":
                if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--hours"):
                    raise ValueError("usage: effortlane metrics [--hours 1..720]")
                from metrics import metrics_report
                print(json.dumps(metrics_report(list(telemetry_rows(ROOT)), int(argv[2]) if len(argv) == 3 else 168), indent=2))
            elif cmd == "claude":
                from claude_shadow import launch
                options = argv[2:] if argv[1:2] == ["--"] else argv[1:]
                raise SystemExit(launch(options, ROOT))
            elif cmd == "claude-report":
                if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--hours"):
                    raise ValueError("usage: effortlane claude-report [--hours 1..720]")
                from claude_shadow import report as claude_report
                print(json.dumps(claude_report(ROOT, int(argv[2]) if len(argv) == 3 else 168), indent=2))
            elif cmd == "cost":
                if len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--hours"):
                    raise ValueError("usage: effortlane cost [--hours 1..720]")
                from costs import cost_report
                hours = int(argv[2]) if len(argv) == 3 else 168
                print(json.dumps(cost_report(list(telemetry_rows(ROOT)), hours), indent=2))
            elif cmd == "savings":
                options = argv[1:]
                if (len(options) % 2 or any(options[i] not in ("--hours", "--since") for i in range(0, len(options), 2))
                        or len(set(options[::2])) != len(options) // 2):
                    raise ValueError("usage: effortlane savings [--hours 1..720] [--since ISO-8601-UTC]")
                settings = dict(zip(options[::2], options[1::2]))
                since = None
                if "--since" in settings:
                    try:
                        since_date = dt.datetime.fromisoformat(settings["--since"].replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise ValueError("--since must be an ISO-8601 timestamp with timezone") from exc
                    if since_date.tzinfo is None:
                        raise ValueError("--since must include a timezone")
                    since = since_date.timestamp()
                from costs import cost_report, format_savings
                hours = int(settings.get("--hours", 720 if since is not None else 24))
                print(format_savings(cost_report(list(telemetry_rows(ROOT)), hours, since)))
            elif cmd == "chat":
                from cli_chat import run
                raise SystemExit(run(argv[1:], ROOT))
            elif cmd == "evaluate":
                options = argv[1:]
                if len(options) % 2 or any(options[i] not in ("--hours", "--labels", "--since") for i in range(0, len(options), 2)) or len(set(options[::2])) != len(options) // 2:
                    raise ValueError("usage: effortlane evaluate [--hours 1..720] [--since ISO-8601-UTC] [--labels path.jsonl]")
                settings = dict(zip(options[::2], options[1::2]))
                since = None
                if "--since" in settings:
                    try:
                        since_date = dt.datetime.fromisoformat(settings["--since"].replace("Z", "+00:00"))
                    except ValueError as exc:
                        raise ValueError("--since must be an ISO-8601 timestamp with timezone") from exc
                    if since_date.tzinfo is None:
                        raise ValueError("--since must include a timezone")
                    since = since_date.timestamp()
                print(json.dumps(evaluate(hours=int(settings.get("--hours", 720 if since is not None else 24)),
                                          labels_path=Path(settings["--labels"]) if "--labels" in settings else None,
                                          since=since), indent=2))
            elif cmd == "trace":
                if len(argv) != 2:
                    raise ValueError("usage: effortlane trace THREAD_UUID")
                print(json.dumps(trace(argv[1]), indent=2))
            elif cmd == "route":
                if len(argv) != 2:
                    raise ValueError("usage: effortlane route THREAD_UUID")
                print(json.dumps(route(argv[1]), indent=2))
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"Effortlane {cmd}: {exc}", file=sys.stderr)
            raise SystemExit(1)
        return
    if not (ROOT / "manifest.json").exists():
        print("Effortlane is not installed", file=sys.stderr)
        raise SystemExit(1)
    if invoked == "codex" and argv == ["update"]:
        raise SystemExit(cli_update_and_refresh())
    if invoked in ("codex", "effortlane", "jev-codex") and sys.stdin.isatty():
        bridge_args = cli_bridge_args(argv)
        if bridge_args is not None:
            from cli_bridge import run
            manifest = load_json(ROOT / "manifest.json")
            raise SystemExit(run(ROOT, Path(cli_target(manifest)), bridge_args))
    args = cli_args(argv, stdin_tty=sys.stdin.isatty())
    os.execv(args[0], args)


if __name__ == "__main__":
    main()
