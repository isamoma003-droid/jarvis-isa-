"""File tools. Everything resolves inside the workspace or is refused."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from .base import Tool, ToolContext, truncate

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "dist", "build", ".tox"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar",
                   ".whl", ".so", ".dylib", ".dll", ".exe", ".bin", ".ico", ".mp3",
                   ".mp4", ".wav", ".woff", ".woff2", ".ttf"}


def _walk(root: Path, max_entries: int = 5000):
    """Yield files under root, skipping noise directories."""
    count = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir())
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in SKIP_DIRS:
                    stack.append(entry)
            else:
                count += 1
                if count > max_entries:
                    return
                yield entry


def read_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.resolve(args["path"], must_exist=True)
    if path.is_dir():
        raise IsADirectoryError(f"{args['path']} is a directory - use list_dir")
    if path.suffix.lower() in BINARY_SUFFIXES:
        return f"{ctx.relative(path)} looks binary ({path.stat().st_size} bytes); not shown."
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line", 1)))
    limit = int(args.get("max_lines", 0)) or len(lines)
    window = lines[start - 1 : start - 1 + limit]
    numbered = "\n".join(f"{start + i:>6}\t{line}" for i, line in enumerate(window))
    header = f"{ctx.relative(path)} ({len(lines)} lines)"
    if start > 1 or len(window) < len(lines):
        header += f", showing {start}-{start + len(window) - 1}"
    return truncate(f"{header}\n{numbered}", ctx.config.max_tool_output, "file")


def write_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.resolve(args["path"])
    content = args["content"]
    existed = path.exists()
    verb = "overwrite" if existed else "create"
    ctx.approve(f"{verb} file", f"{ctx.relative(path)} ({len(content)} characters)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"{'Overwrote' if existed else 'Wrote'} {ctx.relative(path)} ({len(content)} characters)"


def edit_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.resolve(args["path"], must_exist=True)
    find, replace = args["find"], args["replace"]
    original = path.read_text(encoding="utf-8")
    occurrences = original.count(find)
    if occurrences == 0:
        raise ValueError(f"the text to find does not appear in {ctx.relative(path)}")
    if occurrences > 1 and not args.get("replace_all"):
        raise ValueError(
            f"the text to find appears {occurrences} times in {ctx.relative(path)}; "
            "include more context or set replace_all"
        )
    ctx.approve("edit file", f"{ctx.relative(path)} ({occurrences} replacement(s))")
    updated = original.replace(find, replace) if args.get("replace_all") \
        else original.replace(find, replace, 1)
    path.write_text(updated, encoding="utf-8")
    return f"Edited {ctx.relative(path)} ({occurrences if args.get('replace_all') else 1} replaced)"


def list_dir(ctx: ToolContext, args: dict[str, Any]) -> str:
    path = ctx.resolve(args.get("path", "."), must_exist=True)
    if not path.is_dir():
        return read_file(ctx, {"path": args.get("path", ".")})
    rows = []
    for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
        if entry.name in SKIP_DIRS:
            continue
        if entry.is_dir():
            rows.append(f"{entry.name}/")
        else:
            rows.append(f"{entry.name}  ({entry.stat().st_size} bytes)")
    body = "\n".join(rows) or "(empty)"
    return truncate(f"{ctx.relative(path)}:\n{body}", ctx.config.max_tool_output, "listing")


def find_files(ctx: ToolContext, args: dict[str, Any]) -> str:
    root = ctx.resolve(args.get("path", "."), must_exist=True)
    pattern = args["pattern"]
    hits = [
        ctx.relative(entry)
        for entry in _walk(root)
        if fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(ctx.relative(entry), pattern)
    ]
    if not hits:
        return f"No files matching {pattern!r} under {ctx.relative(root)}"
    body = "\n".join(sorted(hits)[:500])
    return truncate(f"{len(hits)} match(es):\n{body}", ctx.config.max_tool_output, "matches")


def search_text(ctx: ToolContext, args: dict[str, Any]) -> str:
    root = ctx.resolve(args.get("path", "."), must_exist=True)
    glob = args.get("glob", "*")
    try:
        needle = re.compile(args["pattern"], re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from exc

    found: list[str] = []
    files_hit = 0
    for entry in _walk(root):
        if entry.suffix.lower() in BINARY_SUFFIXES or not fnmatch.fnmatch(entry.name, glob):
            continue
        try:
            text = entry.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        matched = False
        for number, line in enumerate(text.splitlines(), start=1):
            if needle.search(line):
                matched = True
                found.append(f"{ctx.relative(entry)}:{number}: {line.strip()[:200]}")
                if len(found) >= 200:
                    break
        files_hit += matched
        if len(found) >= 200:
            break
    if not found:
        return f"No matches for {args['pattern']!r} under {ctx.relative(root)}"
    header = f"{len(found)} line(s) in {files_hit} file(s):"
    return truncate(header + "\n" + "\n".join(found), ctx.config.max_tool_output, "matches")


TOOLS = [
    Tool(
        name="read_file",
        description=(
            "Read a UTF-8 text file from the workspace. Returns numbered lines. "
            "Use start_line and max_lines to page through a long file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "start_line": {"type": "integer", "description": "First line to show (1-based)."},
                "max_lines": {"type": "integer", "description": "How many lines to show."},
            },
            "required": ["path"],
        },
        handler=read_file,
    ),
    Tool(
        name="write_file",
        description=(
            "Create a file or replace its entire contents. Parent directories are created. "
            "Prefer edit_file for a small change to an existing file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "description": "The complete new file contents."},
            },
            "required": ["path", "content"],
        },
        handler=write_file,
        dangerous=True,
    ),
    Tool(
        name="edit_file",
        description=(
            "Replace an exact string in a file. The text to find must be unique unless "
            "replace_all is set. Include surrounding context to disambiguate."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "find": {"type": "string", "description": "Exact text to replace."},
                "replace": {"type": "string", "description": "Replacement text."},
                "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
            },
            "required": ["path", "find", "replace"],
        },
        handler=edit_file,
        dangerous=True,
    ),
    Tool(
        name="list_dir",
        description="List the entries of a directory in the workspace.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Defaults to the workspace root."}
            },
        },
        handler=list_dir,
    ),
    Tool(
        name="find_files",
        description="Find files by glob pattern, e.g. '*.py' or 'src/**/test_*.py'.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string", "description": "Directory to search from."},
            },
            "required": ["pattern"],
        },
        handler=find_files,
    ),
    Tool(
        name="search_text",
        description="Search file contents with a regular expression and return matching lines.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "path": {"type": "string"},
                "glob": {"type": "string", "description": "Only search files matching this glob."},
            },
            "required": ["pattern"],
        },
        handler=search_text,
    ),
]
