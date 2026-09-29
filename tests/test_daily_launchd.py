# tests/test_daily_launchd.py
"""The daily chain runs from a LaunchAgent (keychain access), not cron.

Never touches the real launchd or crontab: fake `launchctl`, `crontab`, `gh`,
`plutil` and python are first on PATH, and HOME is a temp dir.
"""
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _exe(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _env(tmp_path, bin_dir, extra=None):
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home")}
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update(extra or {})
    return env


def _fakes(tmp_path, login="jason-m"):
    b = tmp_path / "bin"
    b.mkdir()
    table = tmp_path / "crontab.txt"
    calls = tmp_path / "launchctl.log"
    _exe(b / "crontab", f'if [ "$1" = "-l" ]; then [ -f "{table}" ] || exit 1; cat "{table}"; '
                        f'elif [ "$1" = "-" ]; then cat > "{table}"; fi\n')
    _exe(b / "launchctl", f'echo "$@" >> "{calls}"; [ "$1" = "print" ] && exit 1; exit 0\n')
    _exe(b / "plutil", "exit 0\n")
    _exe(b / "gh", f'echo "{login}"\n' if login else "exit 1\n")
    return b, table, calls


def test_install_writes_a_0900_agent_with_actor_and_removes_the_cron_entry(tmp_path):
    b, table, calls = _fakes(tmp_path)
    table.write_text(f"5 * * * * /usr/bin/true\n# token-burn snapshot\n"
                     f"0 9 * * * cd '{REPO}' && ./sync-factory.sh; python3 snapshot.py\n")
    r = subprocess.run(["bash", str(REPO / "install-daily-launchd.sh")],
                       env=_env(tmp_path, b), capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    plist = (tmp_path / "home/Library/LaunchAgents/com.token-burn.daily.plist").read_text()
    assert "<key>Hour</key>\n      <integer>9</integer>" in plist
    assert "run-daily.sh" in plist and "<string>--actor</string>" in plist
    assert "<string>jason-m</string>" in plist
    assert "bootstrap" in calls.read_text()
    lines = [l for l in table.read_text().splitlines() if l.strip()]
    assert lines == ["5 * * * * /usr/bin/true"]          # only unrelated entries remain


def test_run_daily_runs_every_step_even_when_one_fails(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    log = tmp_path / "steps.log"
    # fake python: record the script and args; fail the outcome fetch
    _exe(b / "fakepy", f'echo "$@" >> "{log}"; [ "$1" = "outcomes.py" ] && exit 1; exit 0\n')
    work = tmp_path / "repo"
    work.mkdir()
    (work / "run-daily.sh").write_text((REPO / "run-daily.sh").read_text())
    _exe(work / "sync-factory.sh", f'echo sync >> "{log}"\n')
    r = subprocess.run(["bash", str(work / "run-daily.sh"), "--actor", "jason-m"],
                       env=_env(tmp_path, b, {"TOKEN_BURN_PYTHON": str(b / "fakepy")}),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == ["sync", "outcomes.py", "snapshot.py --actor jason-m"]
    assert "outcome fetch step failed" in r.stdout
