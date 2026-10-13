"""Mechanical safety checks for executable Task Contract commands.

Command data boundaries: docs/impl/verification-command-heredoc.md.
"""

from __future__ import annotations

import re
import shlex


_INLINE_EVAL_RE = re.compile(
    r"(?<![\w./-])(?:node|python(?:3(?:\.\d+)*)?)\s+"
    r"(?:-e|-c|--eval(?:=|\s+))"
)
_SHELL_LITERAL_OR_HEREDOC_RE = re.compile(
    r"(?P<heredoc>(?<![<\\])<<(?P<tabs>-)?[ \t]*"
    r"(?P<delimiter>'[^'\n]*'|\"(?:\\.|[^\"\\\n])*\")"
    r"(?=$|[\s;&|()<>]))"
    r"|(?P<literal>'[^']*'|\"(?:\\.|[^\"\\])*\"|\\[^\n])"
    r"|(?P<comment>(?<!\S)#[^\n]*)"
)


def without_quoted_heredoc_bodies(command: str) -> str:
    """Mask literal stdin programs before checking outer shell arguments.

    A quoted here-document is data to the shell, so JavaScript regexes and
    other embedded code are not shell prose or unquoted path globs. Only
    complete, wholly quoted delimiters are handled here; the original command
    still goes through shell syntax validation and remains the executed value.
    """

    parts: list[str] = []
    kept = scan = 0
    while match := _SHELL_LITERAL_OR_HEREDOC_RE.search(command, scan):
        scan = match.end()
        if match.group("heredoc") is None:
            continue
        line_end = command.find("\n", scan)
        while line_end != -1:
            header = command[:line_end]
            if (len(header) - len(header.rstrip("\\"))) % 2 == 0:
                break
            line_end = command.find("\n", line_end + 1)
        if line_end == -1:
            break
        headers = [match, *(
            item for item in _SHELL_LITERAL_OR_HEREDOC_RE.finditer(
                command, scan, line_end
            ) if item.group("heredoc") is not None
        )]
        body_end = line_end + 1
        for header in headers:
            delimiter = shlex.split(header.group("delimiter"))[0]
            end = _heredoc_end(
                command, body_end, delimiter,
                strip_tabs=header.group("tabs") is not None,
            )
            if end is None:
                # Do not hide malformed input or any subsequent shell prose.
                return "".join(parts) + command[kept:]
            body_end = end
        parts.append(command[kept:line_end + 1])
        kept = scan = body_end
    return "".join(parts) + command[kept:]


def _heredoc_end(
    command: str, start: int, delimiter: str, *, strip_tabs: bool,
) -> int | None:
    offset = start
    for line in command[start:].splitlines(keepends=True):
        offset += len(line)
        candidate = line.removesuffix("\n")
        if strip_tabs:
            candidate = candidate.lstrip("\t")
        if candidate == delimiter:
            return offset
    return None


def escape_sensitive_inline_eval_errors(command: str) -> list[str]:
    """Reject inline programs whose backslashes cross interpreter layers.

    A raw ``\\`` after ``node -e``/``python -c`` is interpreted once by the
    shell and again by the embedded language. The command can remain valid
    shell syntax while changing the program. Script or test files preserve the
    command identity consumed by candidate verification.
    """

    match = _INLINE_EVAL_RE.search(command)
    if match is None or "\\\\" not in command[match.end():]:
        return []
    return [
        "must not embed escape-sensitive code in node -e/python -c; "
        "move the assertion into a repository test/script file or use a "
        "command without nested backslash escaping"
    ]


__all__ = ["escape_sensitive_inline_eval_errors", "without_quoted_heredoc_bodies"]
