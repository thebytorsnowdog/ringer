#!/usr/bin/env python3
"""Validate a doc-swarm output file."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path


# Prefer the checked-out helper. A copied kit can ship the three helper
# modules alongside this script.
parents = Path(__file__).resolve().parents
if len(parents) > 3 and (checkout := parents[3]).joinpath("ringer_check_sandbox.py").is_file():
    sys.path.insert(0, str(checkout))

from ringer_check_sandbox import sandbox_command
from ringer_file_scope import descend, open_directory, open_regular
from ringer_reliability import run_bounded


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".next",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    ".venv",
    "venv",
}


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


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9_'-]+", text)


def heading_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def sections(markdown: str) -> dict[str, str]:
    result: dict[str, list[str]] = {}
    current = ""
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            current = heading_slug(match.group(2))
            result.setdefault(current, [])
            continue
        if current:
            result.setdefault(current, []).append(line)
    return {key: "\n".join(value).strip() for key, value in result.items()}


def extract_symbol_section(markdown: str, section_name: str) -> str:
    target = heading_slug(section_name)
    in_section = False
    collected: list[str] = []
    for line in markdown.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            slug = heading_slug(match.group(2))
            if in_section and slug != target:
                break
            in_section = slug == target
            continue
        if in_section:
            collected.append(line)
    return "\n".join(collected)


def code_spans(text: str) -> list[str]:
    spans = re.findall(r"`([^`\n]+)`", text)
    cleaned: list[str] = []
    for span in spans:
        item = span.strip()
        if item and not item.startswith(("http://", "https://")):
            cleaned.append(item)
    return sorted(set(cleaned))


def source_files(root_fd: int, prefix: tuple[str, ...] = ()):
    with os.scandir(root_fd) as entries:
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            parts = (*prefix, entry.name)
            if entry.is_dir(follow_symlinks=False):
                with descend(root_fd, (entry.name,)) as child:
                    yield from source_files(child, parts)
            elif entry.is_file(follow_symlinks=False):
                yield parts


def find_symbols(symbols: list[str], root: Path) -> set[str]:
    missing = {symbol: symbol.encode("utf-8") for symbol in symbols}
    with open_directory(root) as root_fd:
        for parts in source_files(root_fd):
            try:
                with open_regular(root_fd, parts) as (stream, info):
                    if info.st_size > 1_000_000:
                        continue
                    data = stream.read(1_000_001)
            except OSError:
                continue
            if b"\0" in data[:2048]:
                continue
            for symbol in tuple(missing):
                if missing[symbol] in data:
                    del missing[symbol]
            if not missing:
                break
    return set(missing)


def fenced_blocks(markdown: str, language: str) -> list[str]:
    pattern = re.compile(r"```([^\n`]*)\n(.*?)\n```", re.DOTALL)
    blocks: list[str] = []
    wanted = language.strip().lower()
    for match in pattern.finditer(markdown):
        info = match.group(1).strip().split()[0].lower() if match.group(1).strip() else ""
        if info == wanted:
            blocks.append(match.group(2))
    return blocks


def suffix_for(language: str) -> str:
    mapping = {
        "python": ".py",
        "py": ".py",
        "bash": ".sh",
        "sh": ".sh",
        "javascript": ".js",
        "js": ".js",
        "typescript": ".ts",
        "ts": ".ts",
    }
    return mapping.get(language.lower(), ".txt")


async def run_example(block: str, suffix: str, runner: str, cwd: Path, source_root: Path, index: int):
    with tempfile.TemporaryDirectory(prefix="doc-example-") as tmp, contextlib.ExitStack() as stack:
        scratch = Path(tmp).resolve()
        work = scratch / "work"
        work.mkdir()
        (scratch / "home").mkdir()
        # Relative imports and fixture reads still work. Their targets are in
        # the read-only source view and the example can create its own files.
        cwd_fd = stack.enter_context(open_directory(cwd))
        with os.scandir(cwd_fd) as entries:
            for entry in entries:
                if entry.name not in {".git", ".runtime"}:
                    (work / entry.name).symlink_to(cwd / entry.name)
        inputs = [(source_root, stack.enter_context(open_directory(source_root)))]
        if cwd != source_root and source_root not in cwd.parents:
            inputs.append((cwd, cwd_fd))
        with tempfile.NamedTemporaryFile(mode="w", prefix=f"example_{index}_", suffix=suffix,
                                         dir=work, encoding="utf-8", delete=False) as example:
            example.write(block + "\n")
        command = runner
        for placeholder, value in (("{file}", example.name), ("{example_cwd}", str(cwd))):
            command = command.replace(f"'{placeholder}'", placeholder).replace(f'"{placeholder}"', placeholder)
            command = command.replace(placeholder, shlex.quote(value))
        with sandbox_command(command, cwd=work, scratch=scratch, read_mounts=inputs) as (argv, fds, env, host_cwd):
            return await run_bounded(argv, host_cwd, 60, env=env, pass_fds=fds)


def run_examples(blocks: list[str], language: str, runner: str, cwd: Path, source_root: Path) -> bool:
    ok = True
    for index, block in enumerate(blocks, start=1):
        try:
            rc, timed_out, output = asyncio.run(
                run_example(block, suffix_for(language), runner, cwd, source_root, index)
            )
        except OSError as exc:
            print(f"FAIL: isolated example {index} could not start: {exc}")
            return False
        if timed_out or rc != 0:
            print(f"FAIL: runnable example {index} {'timed out' if timed_out else f'failed with exit {rc}'}")
            print(output)
            ok = False
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc-path", required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--required-sections", required=True)
    parser.add_argument("--min-words", required=True)
    parser.add_argument("--min-section-words", required=True)
    parser.add_argument("--symbol-section", required=True)
    parser.add_argument("--symbol-allowlist", default="NONE")
    parser.add_argument("--runnable-language", required=True)
    parser.add_argument("--example-runner", required=True)
    parser.add_argument("--example-cwd", required=True)
    args = parser.parse_args()

    doc_path = Path(args.doc_path).absolute()
    source_root = Path(args.source_root).absolute()
    example_cwd = Path(args.example_cwd).absolute()
    try:
        min_words = int(args.min_words)
        min_section_words = int(args.min_section_words)
    except ValueError:
        print("FAIL: MIN_WORDS and MIN_SECTION_WORDS must be integers")
        return 1

    try:
        with open_directory(doc_path.parent) as parent, open_regular(parent, (doc_path.name,)) as (stream, info):
            if info.st_size > 20 * 1024 * 1024:
                raise OSError("doc is larger than 20 MB")
            markdown = stream.read(info.st_size).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        print(f"FAIL: cannot read doc {doc_path}: {exc}")
        return 1

    all_words = words(markdown)
    if len(all_words) < min_words:
        print(f"FAIL: doc has {len(all_words)} words; minimum is {min_words}")
        return 1

    found_sections = sections(markdown)
    failures = False
    for section in split_list(args.required_sections):
        slug = heading_slug(section)
        body = found_sections.get(slug, "")
        if slug not in found_sections:
            print(f"FAIL: missing required section: {section}")
            failures = True
            continue
        count = len(words(body))
        if count < min_section_words:
            print(f"FAIL: section '{section}' has {count} words; minimum is {min_section_words}")
            failures = True

    symbol_body = extract_symbol_section(markdown, args.symbol_section)
    symbols = code_spans(symbol_body)
    allowlist = set(split_list(args.symbol_allowlist))
    symbols_to_check = [symbol for symbol in symbols if symbol not in allowlist]
    if not symbols_to_check:
        print(f"FAIL: no checkable code-span symbols found in section '{args.symbol_section}'")
        failures = True
    else:
        try:
            missing = find_symbols(symbols_to_check, source_root)
        except OSError as exc:
            print(f"FAIL: cannot scan source {source_root}: {exc}")
            return 1
        for symbol in sorted(missing):
            print(f"FAIL: documented symbol not found in source tree: {symbol}")
            failures = True

    examples = fenced_blocks(markdown, args.runnable_language)
    if not examples:
        print(f"FAIL: no runnable fenced examples labeled {args.runnable_language}")
        failures = True
    elif not run_examples(examples, args.runnable_language, args.example_runner, example_cwd, source_root):
        failures = True

    if failures:
        return 1
    print(f"OK: {doc_path} passed doc validation")
    print(f"OK: {len(symbols_to_check)} documented symbols found in source")
    print(f"OK: {len(examples)} runnable examples executed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

