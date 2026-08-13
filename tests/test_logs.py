import logging

import pytest

from release import logs


@pytest.fixture(autouse=True)
def _reset():
    yield
    logging.getLogger(logs.ROOT).handlers.clear()


def record(level, message):
    return logging.LogRecord("release.test", level, __file__, 1, message, None, None)


@pytest.mark.parametrize(
    "level,expected",
    [
        (logging.DEBUG, "::debug::hello"),
        (logging.INFO, "hello"),
        (logging.WARNING, "::warning::hello"),
        (logging.ERROR, "::error::hello"),
        (logging.CRITICAL, "::error::hello"),
    ],
)
def test_actions_formatter_prefixes_by_level(level, expected):
    formatter = logs.ActionsFormatter("%(message)s")
    assert formatter.format(record(level, "hello")) == expected


def test_actions_formatter_escapes_newlines():
    # Workflow commands are single-line. An unescaped newline drops everything after
    # it out of the annotation, which is exactly what a multi-line preflight failure
    # would hit.
    formatter = logs.ActionsFormatter("%(message)s")
    out = formatter.format(record(logging.ERROR, "preflight failed:\n  - one\n  - two"))
    assert out == "::error::preflight failed:%0A  - one%0A  - two"
    assert "\n" not in out


def test_info_is_not_escaped_because_it_is_not_a_command():
    formatter = logs.ActionsFormatter("%(message)s")
    assert formatter.format(record(logging.INFO, "a\nb")) == "a\nb"


@pytest.mark.parametrize(
    "verbose,quiet,expected",
    [(False, False, logging.INFO), (True, False, logging.DEBUG), (False, True, logging.WARNING)],
)
def test_setup_levels(verbose, quiet, expected, monkeypatch):
    monkeypatch.delenv("RUNNER_DEBUG", raising=False)
    logs.setup(verbose=verbose, quiet=quiet)
    assert logging.getLogger(logs.ROOT).level == expected


def test_runner_debug_forces_verbose(monkeypatch):
    # Actions sets RUNNER_DEBUG=1 when someone re-runs with debug logging enabled,
    # which is precisely when they want the HTTP trace.
    monkeypatch.setenv("RUNNER_DEBUG", "1")
    logs.setup()
    assert logging.getLogger(logs.ROOT).level == logging.DEBUG


def test_setup_is_idempotent(monkeypatch):
    monkeypatch.delenv("RUNNER_DEBUG", raising=False)
    logs.setup()
    logs.setup()
    assert len(logging.getLogger(logs.ROOT).handlers) == 1


def test_logs_go_to_stderr_not_stdout(monkeypatch, capsys):
    monkeypatch.delenv("RUNNER_DEBUG", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    logs.setup()
    logging.getLogger("release.test").info("diagnostic")
    captured = capsys.readouterr()
    assert "diagnostic" in captured.err
    assert captured.out == ""


def test_group_emits_workflow_commands_under_actions(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    logs.setup()
    with logs.group("preflight"):
        pass
    err = capsys.readouterr().err
    assert "::group::preflight" in err
    assert "::endgroup::" in err


def test_group_closes_even_when_the_body_raises(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    logs.setup()
    with pytest.raises(ValueError), logs.group("preflight"):
        raise ValueError("boom")
    assert "::endgroup::" in capsys.readouterr().err


def test_group_is_a_plain_heading_outside_actions(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    logs.setup()
    with logs.group("preflight"):
        pass
    err = capsys.readouterr().err
    assert "preflight" in err
    assert "::group::" not in err
