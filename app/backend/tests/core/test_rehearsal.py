"""The install rehearsal's own parts (app/scripts/local/rehearse.py and rehearsal/): the scenario files are well formed
and cover what the plan names, the leak scanner and the port choice do their one job, the fake Claude Code behaves like
the real one where the install depends on it, and two cheap scenarios run end to end. The full tiers run by
`dev.py rehearse` (and the fast tier inside `dev.py check`), not here."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

LOCAL = Path(__file__).resolve().parents[3] / "scripts" / "local"
spec = importlib.util.spec_from_file_location("rehearse_under_test", LOCAL / "rehearse.py")
rehearse = importlib.util.module_from_spec(spec)
sys.modules["rehearse_under_test"] = rehearse
spec.loader.exec_module(rehearse)

SCENARIOS = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((LOCAL / "rehearsal").glob("*.json"))]
FAST_PLAN = {2, 3, 4, 5, 9, 10, 16, 17, 18, 19, 21}
SLOW_PLAN = {1, 6, 8, 14, 20}


def test_every_scenario_names_its_tier_and_only_known_operations_and_checks():
    names = [spec["name"] for spec in SCENARIOS]
    assert len(names) == len(set(names))
    for scenario in SCENARIOS:
        assert scenario["tier"] in ("fast", "slow"), scenario["name"]
        assert scenario.get("steps") or scenario.get("interrupt_each_step"), scenario["name"]
        for step in scenario["steps"]:
            assert sum(key in step for key in ("run", "op", "check")) == 1, (scenario["name"], step)
            assert step.get("op", "file") in rehearse.OPS, (scenario["name"], step)
            assert step.get("check", "exists") in rehearse.CHECKS, (scenario["name"], step)
            assert set(step.get("run", {})) <= {"expect_rc", "args", "repo", "owner"}, (scenario["name"], step)


def test_the_fast_tier_holds_the_plans_fast_scenarios_and_the_slow_tier_the_rest():
    fast = {spec["plan"] for spec in SCENARIOS if spec["tier"] == "fast"}
    slow = {spec["plan"] for spec in SCENARIOS if spec["tier"] == "slow"}
    assert FAST_PLAN <= fast and SLOW_PLAN <= slow


def test_a_scenario_starts_the_default_fast_run_and_the_slow_ones_only_with_the_flag():
    assert {spec["tier"] for spec in rehearse.scenarios(False, [])} == {"fast"}
    assert {"slow", "fast"} == {spec["tier"] for spec in rehearse.scenarios(True, [])}
    assert [spec["name"] for spec in rehearse.scenarios(False, ["fresh-install"])] == ["fresh-install"]


@pytest.mark.parametrize("text", ["sk-ant-oat01-" + "A1b2C3d4" * 6, "x " + "0123456789abcdef" * 4 + " y"])
def test_the_leak_scanner_finds_a_token_shaped_string(text):
    assert rehearse.leaks(text, [])


def test_the_leak_scanner_finds_a_scenarios_own_secret_and_passes_clean_text():
    assert rehearse.leaks("the value is hunter2-hunter2", ["hunter2-hunter2"])
    assert rehearse.leaks("[connect] registered the doors, and they answer", ["hunter2-hunter2"]) == []
    assert "hunter2" not in " ".join(rehearse.leaks("the value is hunter2-hunter2", ["hunter2-hunter2"]))


def test_the_port_pair_is_never_one_of_the_owners_ports():
    for _ in range(300):
        page = rehearse.free_ports()
        assert {page, page + rehearse.ENGINE_OFFSET}.isdisjoint(rehearse.FORBIDDEN_PORTS)
        rehearse.release_ports(page)
    with pytest.raises(AssertionError):
        rehearse.guard_port("http://127.0.0.1:8430/health")
    with pytest.raises(AssertionError):
        rehearse.guard_port("http://127.0.0.1:8653")
    rehearse.guard_port("http://127.0.0.1:25000/health")


class Claude:
    """The fake Claude Code, run as a program with its own configuration folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def __call__(self, *words: str, broken: bool = False):
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.folder)}
        program = [sys.executable, str(LOCAL / "rehearsal" / "fake_claude.py")]
        command = [*program, *(["--broken"] if broken else []), *words]
        return subprocess.run(command, env=env, capture_output=True, check=False)


def test_the_fake_claude_keeps_doors_in_its_own_json_file_and_refuses_a_name_taken(tmp_path):
    claude = Claude(tmp_path)
    add = ["mcp", "add", "--transport", "http", "--scope", "user", "door", "http://127.0.0.1:1/x/", "--header",
           "Authorization: Bearer tok"]
    assert claude(*add).returncode == 0
    assert b"already exists" in claude(*add).stderr
    got = claude("mcp", "get", "door")
    assert got.returncode == 0 and b"URL: http://127.0.0.1:1/x/" in got.stdout and b"Bearer tok" in got.stdout
    assert claude("mcp", "remove", "door", "--scope", "user").returncode == 0
    assert claude("mcp", "get", "door").returncode == 1
    assert json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))["mcpServers"] == {}


def test_the_fake_claude_can_print_undefined_bytes_refuse_one_address_and_be_a_broken_copy(tmp_path):
    claude = Claude(tmp_path)
    (tmp_path / "fake-claude-behavior.json").write_text(json.dumps({"junk_bytes": True, "reject_url": "/refused"}))
    assert claude("mcp", "list").stdout.startswith(b"\x81\x8d")
    refused = claude("mcp", "add", "--transport", "http", "n", "http://h/refused", "--header", "Authorization: Bearer")
    assert refused.returncode == 1 and b"refused" in refused.stderr
    assert claude("--version", broken=True).returncode == 1


def test_two_cheap_scenarios_run_end_to_end(capsys):
    # Over a second: it builds a local git repository and runs the script twice (the reason, per docs/testing.md).
    assert rehearse.main(["concurrent-install", "utf16-settings-file"]) == 0
    out = capsys.readouterr().out
    assert "ok     concurrent-install" in out and "ok     utf16-settings-file" in out


def test_the_port_range_is_a_block_no_other_stage_draws_from():
    # The runner tests (app/scout) draw from 20000 to 39999, the stack-setup tests from 41000 up; the rehearsal's ports
    # once sat inside the first, and a runner test took one the stand-in app was about to bind.
    low, high = rehearse.PORT_RANGE
    assert low >= 40000 and high + rehearse.ENGINE_OFFSET < 41000


def test_a_port_pair_claimed_is_never_picked_again_until_it_is_released(monkeypatch):
    first = rehearse.free_ports()
    try:
        monkeypatch.setattr(rehearse.random, "randint", lambda low, high: first)
        with pytest.raises(RuntimeError):  # every pick lands on the claimed pair, though nothing is bound to it yet
            rehearse.free_ports()
        rehearse.release_ports(first)
        assert rehearse.free_ports() == first
    finally:
        rehearse.release_ports(first)


def test_a_port_pair_claimed_by_another_process_is_not_picked(monkeypatch):
    # Over a second: it starts a Python, the other process (a second `dev.py rehearse`) the claim has to hold against.
    program = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('r', {str(LOCAL / 'rehearse.py')!r})\n"
        "r = importlib.util.module_from_spec(spec); sys.modules['r'] = r; spec.loader.exec_module(r)\n"
        "print(r.free_ports(), flush=True); sys.stdin.readline()\n")
    holder = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        taken = int(holder.stdout.readline())
        monkeypatch.setattr(rehearse.random, "randint", lambda low, high: taken)
        with pytest.raises(RuntimeError):
            rehearse.free_ports()
    finally:
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.wait(timeout=30)
    monkeypatch.setattr(rehearse.random, "randint", lambda low, high: taken)
    try:
        assert rehearse.free_ports() == taken  # the claim ended with the process that held it
    finally:
        rehearse.release_ports(taken)
