#!/usr/bin/env python3
"""Validate a migration-swarm patch export."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def split_list(value: str) -> list[str]:
    raw = value.strip()
    if not raw or raw.upper() == "NONE":
        return []
    parts: list[str] = []
    for chunk in raw.replace("\n", ";").replace(",", ";").split(";"):
        item = chunk.strip().strip("'\"")
        if item:
            parts.append(item)
    return parts


def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def normalize(path: str) -> str:
    return path.strip().strip("/").replace("\\", "/")


def is_runtime_export_path(path: str) -> bool:
    """True for the Claude engine `.runtime/` tree, which must not be exported."""
    return ".runtime" in normalize(path).split("/")


def is_owned(path: str, owned: list[str]) -> bool:
    rel = normalize(path)
    for entry in owned:
        item = normalize(entry)
        if not item:
            continue
        if item.endswith("/"):
            if rel.startswith(item):
                return True
        elif rel == item or rel.startswith(f"{item}/"):
            return True
    return False


def staged_paths() -> list[str]:
    proc = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMRTD"])
    if proc.returncode != 0:
        print("FAIL: could not read staged paths from git diff --cached")
        print(proc.stderr.strip())
        sys.exit(1)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def ignored_paths_under(owned: list[str]) -> list[str]:
    ignored: set[str] = set()
    for entry in owned:
        target = normalize(entry)
        if not target:
            continue
        proc = run_git(["status", "--ignored=matching", "--porcelain", "--", target])
        if proc.returncode != 0:
            print(f"FAIL: could not inspect ignored paths under {target}")
            print(proc.stderr.strip())
            sys.exit(1)
        for line in proc.stdout.splitlines():
            if line.startswith("!! "):
                ignored.add(normalize(line[3:]))
    return sorted(ignored)


def copy_ignored(paths: list[str], allowed: list[str], export_root: Path) -> list[str]:
    copied: list[str] = []
    export_root.mkdir(parents=True, exist_ok=True)
    for rel in paths:
        if allowed and not is_owned(rel, allowed):
            continue
        src = Path(rel)
        if not src.exists():
            print(f"FAIL: ignored path disappeared before copy: {rel}")
            sys.exit(1)
        dst = export_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        if not dst.exists():
            print(f"FAIL: attempted to copy ignored path but copy is missing: {dst}")
            sys.exit(1)
        copied.append(rel)
    return copied


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-key", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--export-dir", required=True)
    parser.add_argument("--owned-files", required=True)
    parser.add_argument("--ignored-exports", default="NONE")
    args = parser.parse_args()

    patch = Path(args.patch)
    if not patch.exists() or patch.stat().st_size == 0:
        print(f"FAIL: exported patch is missing or empty: {patch}")
        return 1

    owned = split_list(args.owned_files)
    if not owned:
        print("FAIL: owned file list is empty; the check cannot prove patch scope")
        return 1

    changed = staged_paths()
    if not changed:
        print("FAIL: no staged paths found after git add -A; patch should not be empty")
        return 1

    runtime_staged = [path for path in changed if is_runtime_export_path(path)]
    if runtime_staged:
        print("FAIL: refusing to export Claude engine .runtime/ paths:")
        for path in runtime_staged:
            print(f"  - {path}")
        return 1

    outside = [path for path in changed if not is_owned(path, owned)]
    if outside:
        print("FAIL: patch touches files outside this task's ownership:")
        for path in outside:
            print(f"  - {path}")
        print("Owned paths:")
        for path in owned:
            print(f"  - {path}")
        return 1

    ignored = [path for path in ignored_paths_under(owned) if not is_runtime_export_path(path)]
    ignored_mode = args.ignored_exports.strip()
    ignored_export_root = Path(args.export_dir) / f"{args.task_key}-gitignored"
    if ignored and ignored_mode.upper() == "NONE":
        print("FAIL: ignored paths exist under owned paths and would not be in the patch:")
        for path in ignored:
            print(f"  - {path}")
        print("Set GITIGNORED_EXPORTS to AUTO or an explicit semicolon-separated path list.")
        return 1

    copied: list[str] = []
    if ignored:
        allowed = [] if ignored_mode.upper() == "AUTO" else split_list(ignored_mode)
        copied = copy_ignored(ignored, allowed, ignored_export_root)
        missing = [path for path in ignored if path not in copied]
        if missing:
            print("FAIL: these ignored paths were detected but not copied:")
            for path in missing:
                print(f"  - {path}")
            return 1

    print(f"OK: exported non-empty patch {patch}")
    print(f"OK: staged paths are within owned scope ({len(changed)} paths)")
    if copied:
        print(f"OK: copied ignored outputs to {ignored_export_root} ({len(copied)} paths)")
    else:
        print("OK: no ignored owned outputs needed copying")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

