# tests/test_install_cron.py
"""C1 (final review): the daily cron line must be able to run the new pipeline.

cron runs with PATH=/usr/bin:/bin, so `gh` (Homebrew) is invisible to
outcomes.py / snapshot.py / team_upload unless the line exports a PATH that
includes it; and the actor login is resolved once at INSTALL time and passed
as `--actor`, so the usage-record pass never depends on `gh api user` at 09:00.

Never touches the real crontab: a fake `crontab` (and a fake `gh`) are put
first on PATH, and the fake crontab stores its table in a temp file.
"""
import os
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "install-cron.sh"


def _exe(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_bin(tmp_path, login="jason-m"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    table = tmp_path / "crontab.txt"
    # `crontab -l` prints the table (exit 1 when none, like the real one);
    # `crontab -` replaces it from stdin.
    _exe(bin_dir / "crontab",
         f'if [ "$1" = "-l" ]; then [ -f "{table}" ] || exit 1; cat "{table}"; '
         f'elif [ "$1" = "-" ]; then cat > "{table}"; else exit 2; fi\n')
    if login is not None:
        _exe(bin_dir / "gh", f'echo "{login}"\n')
    else:
        _exe(bin_dir / "gh", "exit 1\n")
    return bin_dir, table


def _install(bin_dir, env_extra=None):
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
    env.update(env_extra or {})
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True,
                          timeout=30)


def _entries(table):
    return [l for l in table.read_text().splitlines() if l.strip()]


def test_cron_line_exports_a_path_that_includes_homebrew_and_passes_actor(tmp_path):
    bin_dir, table = _fake_bin(tmp_path, login="jason-m")
    result = _install(bin_dir)
    assert result.returncode == 0, result.stderr
    lines = _entries(table)
    line = next(l for l in lines if l.startswith("0 9 * * *"))
    # PATH export comes first, before any step runs
    after_schedule = line[len("0 9 * * *"):].lstrip()
    assert after_schedule.startswith('export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH";')
    # the login is resolved at install time and passed to snapshot.py
    assert "snapshot.py --actor jason-m" in line


def test_cron_actor_can_be_given_explicitly(tmp_path):
    bin_dir, table = _fake_bin(tmp_path, login=None)  # gh can't resolve it
    result = _install(bin_dir, {"TOKEN_BURN_ACTOR": "someone"})
    assert result.returncode == 0, result.stderr
    line = next(l for l in _entries(table) if l.startswith("0 9 * * *"))
    assert "snapshot.py --actor someone" in line


def test_cron_without_a_resolvable_login_still_installs_and_warns(tmp_path):
    bin_dir, table = _fake_bin(tmp_path, login=None)
    result = _install(bin_dir)
    assert result.returncode == 0, result.stderr
    line = next(l for l in _entries(table) if l.startswith("0 9 * * *"))
    assert "--actor" not in line
    assert 'export PATH="/opt/homebrew/bin' in line  # runtime gh fallback can still work
    assert "WARNING" in result.stdout + result.stderr


def test_cron_install_is_idempotent_and_replaces_a_legacy_entry(tmp_path):
    bin_dir, table = _fake_bin(tmp_path, login="jason-m")
    legacy = (f"0 9 * * * cd '{REPO}' && ./sync-factory.sh >> '{REPO}/snapshot.log' 2>&1; "
              f"'/usr/bin/python3' snapshot.py >> '{REPO}/snapshot.log' 2>&1")
    table.write_text(f"MAILTO=\"\"\n5 * * * * /usr/bin/true\n# token-burn snapshot\n{legacy}\n")
    assert _install(bin_dir).returncode == 0
    assert _install(bin_dir).returncode == 0
    lines = _entries(table)
    assert lines.count("# token-burn snapshot") == 1
    ours = [l for l in lines if "snapshot.py" in l]
    assert len(ours) == 1 and "--actor jason-m" in ours[0]
    # unrelated entries survive
    assert "5 * * * * /usr/bin/true" in lines and 'MAILTO=""' in lines
