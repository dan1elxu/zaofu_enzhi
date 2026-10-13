from __future__ import annotations

import pytest

from zf.runtime.task_map import _verification_command_errors


@pytest.mark.parametrize("header,end", [
    ("node <<'JS'", "JS"),
    ('node <<"JS"', "JS"),
    ("node <<-'JS'", "\tJS"),
    ("node <<'中文'", "中文"),
])
def test_literal_heredoc_program_is_not_shell_prose_or_glob(header, end):
    command = (
        f"{header}\n"
        "const pattern = /^开始(?:游戏)?$/;\n"
        "// expected failure: regexp data is not shell prose\n"
        f"{end}\n"
    )
    assert _verification_command_errors(command) == []


def test_nested_literal_heredoc_program_is_preserved():
    command = """bash -s <<'SH'
set -eu
node <<'JS'
const pattern = /^开始(?:游戏)?$/;
JS
SH
"""
    assert _verification_command_errors(command) == []


def test_multiple_literal_heredocs_keep_checking_trailing_commands():
    command = "cat <<'A' <<'B'\n开始\nA\n游戏\nB\n"
    assert _verification_command_errors(command) == []
    assert _verification_command_errors(command + "false；这是说明\n")


@pytest.mark.parametrize("command", [
    "node <<'JS'\nconst pattern = /^开始$/;\nJS\n说明文字\n",
    "node <<'JS'; 说明文字\nconst pattern = /^开始$/;\nJS\n",
    "node <<'JS'\nconst pattern = /^开始$/;\n",
    "node <<'JS'\nconst pattern = /^开始$/;\n JS\n",
    "node <<'JS'; should fail\nconst pattern = /^开始$/;\nJS\n",
    "printf \"<<'JS'\"\n说明文字\nJS\n",
    "echo ok # <<'JS'\n说明文字\nJS\n",
    "false /这是说明/",
    "false /expected failure/",
    "cat <<'JS' \\\n说明文字\nconst pattern = /^开始$/;\nJS\n",
])
def test_heredoc_handling_does_not_hide_shell_prose(command):
    errors = _verification_command_errors(command)
    assert any("executable command only" in error for error in errors)


def test_heredoc_does_not_disable_shell_syntax_validation():
    command = "node <<'JS'\nconst pattern = /^开始$/;\nJS\n)\n"
    assert any("valid shell syntax" in e for e in _verification_command_errors(command))


def test_heredoc_does_not_hide_external_unquoted_path_globs():
    command = "node <<'JS'\nconst pattern = /^开始$/;\nJS\npytest tests/*.py\n"
    assert any("quote shell glob" in e for e in _verification_command_errors(command))
