#!/bin/sh
# Download Effortlane source and run its user-level installer. No sudo or shell edits.
set -eu
umask 077

fail() { printf '%s\n' "Effortlane install: $*" >&2; exit 1; }
[ "$(uname -s)" = Darwin ] || fail 'macOS is required.'
[ "$(id -u)" != 0 ] || fail 'Run as your own user, without sudo.'
python=${EFFORTLANE_PYTHON:-python3}
command -v "$python" >/dev/null 2>&1 || fail 'Python 3.11+ is required; install it before retrying.'
"$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || fail 'Python 3.11+ is required.'
command -v curl >/dev/null 2>&1 || fail 'curl is required.'
ref=${EFFORTLANE_REF:-main}
case "$ref" in ''|*[!a-zA-Z0-9._-]*) fail 'EFFORTLANE_REF must be a commit, tag, or simple branch name.';; esac
work=$(mktemp -d "${TMPDIR:-/tmp}/effortlane-install.XXXXXX")
trap 'rm -rf "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
printf '%s\n' "Effortlane: downloading source ($ref)..."
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  --connect-timeout 10 --max-time 120 --retry 2 \
  "https://codeload.github.com/itscloud0/effortlane/tar.gz/$ref" -o "$work/source.tar.gz" || fail 'Download failed; no installation changes made.'
"$python" - "$work" <<'PY'
import pathlib, sys, tarfile
work = pathlib.Path(sys.argv[1])
archive_path = work / 'source.tar.gz'
if archive_path.stat().st_size > 20_000_000:
    raise SystemExit('Effortlane install: archive exceeds size limit')
target = work / 'source'
target.mkdir()
with tarfile.open(archive_path, 'r:gz') as archive:
    members = archive.getmembers()
    if len(members) > 5000 or sum(x.size for x in members) > 100_000_000:
        raise SystemExit('Effortlane install: source exceeds extraction limit')
    for member in members:
        path = pathlib.Path(member.name)
        if (path.is_absolute() or '..' in path.parts or not (member.isdir() or member.isfile())
                or not (target / path).resolve().is_relative_to(target.resolve())):
            raise SystemExit('Effortlane install: unsafe archive member rejected')
    archive.extractall(target, members=members)
roots = list(target.iterdir())
if len(roots) != 1 or not roots[0].is_dir() or not (roots[0] / 'bootstrap.py').is_file():
    raise SystemExit('Effortlane install: expected source layout missing')
(work / 'source-root').write_text(str(roots[0]))
PY
source=$(cat "$work/source-root")
# A piped shell installer has no interactive stdin; give the hidden key prompt a TTY.
if [ -t 1 ] && [ -r /dev/tty ]; then
  "$python" "$source/bootstrap.py" "$@" < /dev/tty
else
  "$python" "$source/bootstrap.py" "$@"
fi
