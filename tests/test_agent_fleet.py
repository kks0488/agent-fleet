"""Exercise the CLI with a private tmux server and a fake Claude process."""

import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

import agent_fleet


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "agent_fleet.py"

# This program runs inside a real tmux pane. It only imitates the documented
# CLI and hook protocol; it does not contact any service or load credentials.
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import argparse
import json
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import time

p = argparse.ArgumentParser()
for key in ("session-id", "resume", "settings", "name", "model", "effort", "permission-mode"):
    p.add_argument("--" + key)
p.add_argument("brief", nargs="?", default="")
a = p.parse_args()
account = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
sid = a.resume or a.session_id
control = Path(os.environ["FLEET_FAKE_CONTROL"])
control.mkdir(parents=True, exist_ok=True)

def record(kind, **values):
    with (control / (kind + ".jsonl")).open("a") as f:
        f.write(json.dumps({"sid": sid, "account": str(account), "pid": os.getpid(), **values}) + "\n")

record("launches", resume=bool(a.resume), argv=sys.argv[1:])
if a.resume and (account / "fail-resume").exists():
    print("Resume failed in this test account", flush=True)
    sys.exit(12)
if not a.resume and (account / "fail-spawn").exists():
    print("Startup failed in this test account", flush=True)
    sys.exit(12)

transcript = account / "projects" / "demo" / (sid + ".jsonl")
if a.resume:
    if not transcript.exists():
        print("Conversation is missing", flush=True)
        sys.exit(13)
    mode = json.loads(transcript.read_text().splitlines()[0])["brief"]
else:
    mode = a.brief
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(json.dumps({"sessionId": sid, "brief": mode}) + "\n")

settings = json.loads(Path(a.settings).read_text()) if Path(a.settings).is_file() else json.loads(a.settings)

def hook(event, **values):
    payload = {"hook_event_name": event, "session_id": sid,
               "transcript_path": str(transcript), "cwd": os.getcwd(), **values}
    for group in settings.get("hooks", {}).get(event, []):
        matcher = group.get("matcher", "")
        match_value = values.get("notification_type", values.get("tool_name", values.get("source", "")))
        if matcher and matcher != "*" and not re.search(matcher, match_value):
            continue
        for entry in group.get("hooks", []):
            if entry.get("type") == "command":
                r = subprocess.run(entry["command"], shell=True,
                                   input=json.dumps(payload), text=True,
                                   capture_output=True)
                if r.returncode:
                    record("hook-errors", event=event, stderr=r.stderr, code=r.returncode)

print("Conversation: " + sid, flush=True)
trust = "FLEET_TRUST" in mode or (account / "show-trust").exists()
busy = "FLEET_BUSY" in mode and not a.resume
child = None
if "FLEET_STUBBORN" in mode:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if trust:
    print("Do you trust this folder? Enter to confirm", flush=True)
else:
    hook("SessionStart", source="resume" if a.resume else "startup")
    if "FLEET_CHILD" in mode:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(999)"])
        record("children", child_pid=child.pid)
    if busy:
        hook("UserPromptSubmit", prompt="test work")
        hook("PreToolUse", tool_name="Bash", tool_use_id="tool-demo")
        print("Working on a test task", flush=True)
    else:
        if "FLEET_ACTIVE_TOOL" in mode or "FLEET_DENIED_TOOL" in mode:
            hook("PreToolUse", tool_name="Bash", tool_use_id="tool-pending")
        if "FLEET_DENIED_TOOL" in mode:
            hook("PermissionRequest", tool_name="Bash", tool_use_id="tool-pending")
            # Permission was denied: neither PostToolUse nor PostToolUseFailure.
            hook("Stop", last_assistant_message="Permission denied")
        if "FLEET_ACTIVE_AGENT" in mode:
            hook("SubagentStart", agent_id="agent-demo", agent_type="general-purpose")
        # A promptless --resume need not emit idle_prompt: that notification
        # follows a completed response, not SessionStart.
        if not (a.resume and "FLEET_RESUME_NO_IDLE" in mode):
            hook("Notification", notification_type="idle_prompt", message="Waiting for input")
        print("> ", flush=True)

while True:
    if busy and (control / (sid + ".release")).exists():
        busy = False
        hook("PostToolUse", tool_name="Bash", tool_use_id="tool-demo")
        hook("Stop", last_assistant_message="Done")
        hook("Notification", notification_type="idle_prompt", message="Waiting for input")
        print("> ", flush=True)
    readable, _, _ = select.select([sys.stdin], [], [], 0.05)
    if not readable:
        continue
    line = sys.stdin.readline()
    if not line:
        break
    record("inputs", line=line.rstrip("\n"))
    if line.strip() == "/exit" and "FLEET_STUBBORN" not in mode:
        if child:
            child.terminate()
            child.wait()
        if not trust:
            hook("SessionEnd", reason="prompt_input_exit")
        break
'''


@unittest.skipUnless(shutil.which("tmux"), "tmux is not installed; integration tests skipped")
class FleetIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.work = self.base / "work folder"
        self.work.mkdir()
        self.account = self.base / "account-one"
        self.other = self.base / "account-two"
        self.account.mkdir()
        self.other.mkdir()
        self.control = self.base / "control"
        self.control.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        self.socket = "fleet-test-" + uuid.uuid4().hex[:12]
        self.env = dict(os.environ,
                        HOME=str(self.home),
                        PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                        AGENT_FLEET_DIR=str(self.base / "state"),
                        AGENT_FLEET_TMUX_SOCKET=self.socket,
                        AGENT_FLEET_START_TIMEOUT="3",
                        AGENT_FLEET_STOP_TIMEOUT="1",
                        FLEET_FAKE_CONTROL=str(self.control),
                        PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("CLAUDE_CONFIG_DIR", None)
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        subprocess.run(["tmux", "-L", self.socket, "kill-server"],
                       env=self.env, capture_output=True, timeout=5)

    def run_cli(self, *args, code=0, timeout=15):
        result = subprocess.run([sys.executable, str(CLI), *map(str, args)],
                                cwd=self.base, env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        self.assertEqual(result.returncode, code,
                         f"{args!r}\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return result

    def tmux(self, *args, check=True):
        result = subprocess.run(["tmux", "-L", self.socket, *map(str, args)],
                                env=self.env, capture_output=True, text=True,
                                timeout=5)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def brief(self, content="Write a short test note."):
        path = self.base / "brief file.md"
        path.write_text(content)
        return path

    def spawn(self, name="demo", content="Write a short test note.", **kwargs):
        result = self.run_cli("spawn", name, self.work, self.brief(content),
                              "--account", self.account, **kwargs)
        if result.returncode == 0 and "FLEET_BUSY" not in content:
            target = self.entry(name)["tmux"] + ":"
            self.wait_for(lambda: any(line.strip() == ">" for line in
                                     self.tmux("capture-pane", "-p", "-t", target).stdout.splitlines()))
        return result

    def ledger(self):
        return json.loads((self.base / "state" / "ledger.json").read_text())

    def entry(self, name="demo"):
        return self.ledger()["sessions"][name]

    def records(self, name):
        path = self.control / (name + ".jsonl")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("Timed out waiting for fake process state")

    def test_spawn_records_options_and_real_tmux(self):
        self.run_cli("spawn", "demo", self.work, self.brief(), "--account", self.account,
                     "--model", "sonnet", "--effort", "high", "--permission-mode", "plan")
        row = self.entry()
        self.assertEqual(row["account"], str(self.account))
        self.assertEqual(row["workdir"], str(self.work))
        self.assertTrue(row["session_id"])
        self.tmux("has-session", "-t", "=" + row["tmux"])
        launch = self.records("launches")[0]
        self.assertIn("--model", launch["argv"])
        self.assertIn("sonnet", launch["argv"])
        self.assertIn("--effort", launch["argv"])
        self.assertIn("high", launch["argv"])
        self.assertIn("plan", launch["argv"])
        self.assertFalse(self.records("hook-errors"))

    def test_spawn_uses_account_from_environment(self):
        self.env["CLAUDE_CONFIG_DIR"] = str(self.other)
        self.run_cli("spawn", "demo", self.work, self.brief())
        self.assertEqual(self.entry()["account"], str(self.other))
        self.assertEqual(self.records("launches")[0]["account"], str(self.other))

    def test_duplicate_name_refused(self):
        self.spawn()
        self.spawn(code=9)
        self.assertEqual(len(self.records("launches")), 1)

    def test_concurrent_spawn_has_only_one_winner(self):
        brief = self.brief()
        command = [sys.executable, str(CLI), "spawn", "demo", str(self.work),
                   str(brief), "--account", str(self.account)]
        first = subprocess.Popen(command, cwd=self.base, env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(command, cwd=self.base, env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            first_output = first.communicate(timeout=15)
            second_output = second.communicate(timeout=15)
        finally:
            for process in (first, second):
                if process.poll() is None:
                    process.kill()
                    process.communicate()
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 9],
                         f"{first_output!r} {second_output!r}")
        self.assertEqual(len(self.records("launches")), 1)

    def test_list_reports_live_and_missing(self):
        self.spawn()
        result = self.run_cli("list", "--json")
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "demo")
        self.assertTrue(rows[0]["alive"])
        self.assertIn(str(self.work), self.run_cli("list").stdout)
        self.tmux("kill-session", "-t", "=" + self.entry()["tmux"])
        rows = json.loads(self.run_cli("list", "--json").stdout)
        self.assertFalse(rows[0]["alive"])

    def test_dry_run_does_not_launch_or_write_state(self):
        self.run_cli("spawn", "demo", self.work, self.brief(), "--account", self.account,
                     "--dry-run")
        self.assertFalse((self.base / "state").exists())
        self.assertFalse(self.records("launches"))

    def test_keep_restarts_only_missing_session(self):
        config = self.base / "fleet.toml"
        config.write_text('[[jobs]]\nname = "watcher"\ncommand = "sleep 60"\n')
        result = self.run_cli("keep", "--config", config)
        self.assertIn("watcher", result.stdout)
        self.assertFalse(self.run_cli("keep", "--config", config).stdout.strip())
        row = self.entry("watcher")
        self.tmux("kill-session", "-t", "=" + row["tmux"])
        self.assertIn("watcher", self.run_cli("keep", "--config", config).stdout)
        self.tmux("has-session", "-t", "=" + self.entry("watcher")["tmux"])

    def test_switch_preserves_conversation_and_stops_previous_process(self):
        self.spawn()
        before = self.entry()
        result = self.run_cli("switch", "demo", self.other)
        after = self.entry()
        self.assertEqual(after["session_id"], before["session_id"])
        self.assertEqual(after["account"], str(self.other))
        launches = self.records("launches")
        self.assertEqual(len(launches), 2)
        self.assertTrue(launches[-1]["resume"])
        self.assertEqual(launches[0]["sid"], launches[1]["sid"])
        self.assertEqual(launches[1]["account"], str(self.other))
        self.assertIn("/exit", [record["line"] for record in self.records("inputs")])
        with self.assertRaises(ProcessLookupError):
            os.kill(launches[0]["pid"], 0)
        self.assertTrue(result.stdout.strip())

    def test_switch_refuses_busy_session_without_input(self):
        self.spawn(content="FLEET_BUSY")
        self.run_cli("switch", "demo", self.other, code=5)
        self.assertEqual(len(self.records("launches")), 1)
        self.assertFalse(self.records("inputs"))
        self.assertEqual(self.entry()["account"], str(self.account))

    def test_switch_verifies_resume_without_idle_notification(self):
        self.spawn(content="FLEET_RESUME_NO_IDLE")
        before = self.entry()
        self.run_cli("switch", "demo", self.other)
        after = self.entry()
        run = json.loads(Path(after["run_file"]).read_text())
        self.assertEqual(after["account"], str(self.other))
        self.assertEqual(after["session_id"], before["session_id"])
        self.assertEqual(run["last_event"], "SessionStart")
        self.assertEqual(run["state"], "busy")
        self.assertEqual(len(self.records("launches")), 2)

    def test_resume_failure_verifies_recovery_without_idle_notification(self):
        self.spawn(content="FLEET_RESUME_NO_IDLE")
        (self.other / "fail-resume").touch()
        self.run_cli("switch", "demo", self.other, code=8)
        entry = self.entry()
        run = json.loads(Path(entry["run_file"]).read_text())
        self.assertEqual(entry["account"], str(self.account))
        self.assertEqual(entry["phase"], "running")
        self.assertEqual(run["last_event"], "SessionStart")
        self.assertEqual(len(self.records("launches")), 3)

    def test_wait_allows_switch_after_work_finishes(self):
        self.spawn(content="FLEET_BUSY")
        release = self.control / (self.entry()["session_id"] + ".release")
        timer = threading.Timer(0.3, release.touch)
        timer.start()
        try:
            self.run_cli("switch", "demo", self.other, "--wait", "0.03")
        finally:
            timer.join()
        self.assertEqual(self.entry()["account"], str(self.other))

    def test_partial_input_is_not_overwritten_by_exit_command(self):
        self.spawn()
        self.tmux("send-keys", "-t", self.entry()["tmux"] + ":", "-l", "typed draft")
        self.wait_for(lambda: "typed draft" in
                      self.tmux("capture-pane", "-p", "-t", self.entry()["tmux"] + ":").stdout)
        self.run_cli("switch", "demo", self.other, code=5)
        self.assertFalse(self.records("inputs"))
        self.assertEqual(len(self.records("launches")), 1)

    def test_switch_refuses_reused_tmux_name_before_sending_exit(self):
        self.spawn()
        row = self.entry()
        self.tmux("rename-session", "-t", "=" + row["tmux"], "original-moved")
        inputs = self.control / "unrelated-inputs.txt"
        program = (
            "import pathlib, sys; print('> ', flush=True); "
            "pathlib.Path(sys.argv[1]).write_text(sys.stdin.readline())"
        )
        command = "exec " + shlex.join([sys.executable, "-u", "-c", program, str(inputs)])
        self.tmux("new-session", "-d", "-s", row["tmux"], command)
        self.wait_for(lambda: ">" in self.tmux("capture-pane", "-p", "-t", row["tmux"] + ":").stdout)
        self.run_cli("switch", "demo", self.other, code=5)
        self.assertFalse(inputs.exists())
        self.assertFalse(self.records("inputs"))
        self.assertEqual(len(self.records("launches")), 1)
        self.tmux("has-session", "-t", "=original-moved")
        self.tmux("has-session", "-t", "=" + row["tmux"])

    def test_switch_refuses_unknown_runner_before_sending_exit(self):
        self.spawn()
        path = Path(self.entry()["run_file"])
        run = json.loads(path.read_text())
        run.pop("runner_pid")
        path.write_text(json.dumps(run))
        self.run_cli("switch", "demo", self.other, code=5)
        self.assertFalse(self.records("inputs"))
        self.assertEqual(len(self.records("launches")), 1)

    def test_switch_refuses_background_child_even_with_idle_hook(self):
        self.spawn(content="FLEET_CHILD")
        self.run_cli("switch", "demo", self.other, code=5)
        self.assertEqual(len(self.records("launches")), 1)
        self.assertFalse(self.records("inputs"))

    def test_switch_clears_cancelled_tool_on_idle_notification(self):
        self.spawn(content="FLEET_ACTIVE_TOOL")
        self.run_cli("switch", "demo", self.other)
        self.assertEqual(self.entry()["account"], str(self.other))

    def test_switch_clears_denied_tool_after_response_ends(self):
        self.spawn(content="FLEET_DENIED_TOOL")
        self.run_cli("switch", "demo", self.other)
        self.assertEqual(self.entry()["account"], str(self.other))

    def test_switch_refuses_active_subagent_even_with_idle_notification(self):
        self.spawn(content="FLEET_ACTIVE_AGENT")
        self.run_cli("switch", "demo", self.other, code=5)
        self.assertFalse(self.records("inputs"))

    def test_protect_refuses_switch_and_unprotect_allows_it(self):
        self.spawn()
        self.run_cli("protect", "demo")
        self.run_cli("switch", "demo", self.other, code=3)
        self.assertFalse(self.records("inputs"))
        self.run_cli("unprotect", "demo")
        self.run_cli("switch", "demo", self.other)
        self.assertEqual(self.entry()["account"], str(self.other))

    def test_resume_failure_restores_original_account(self):
        self.spawn()
        (self.other / "fail-resume").touch()
        self.run_cli("switch", "demo", self.other, code=8)
        self.assertEqual(self.entry()["account"], str(self.account))
        launches = self.records("launches")
        self.assertEqual([r["account"] for r in launches],
                         [str(self.account), str(self.other), str(self.account)])
        self.assertEqual(len({r["sid"] for r in launches}), 1)
        self.tmux("has-session", "-t", "=" + self.entry()["tmux"])

    def test_conflicting_target_transcript_is_not_overwritten(self):
        self.spawn()
        target = self.other / "projects" / "demo" / (self.entry()["session_id"] + ".jsonl")
        target.parent.mkdir(parents=True)
        target.write_text("Different conversation data\n")
        self.run_cli("switch", "demo", self.other, code=8)
        self.assertEqual(target.read_text(), "Different conversation data\n")
        self.assertEqual(self.entry()["account"], str(self.account))

    def test_roundtrip_switch_preserves_new_conversation_history(self):
        self.spawn()
        sid = self.entry()["session_id"]
        first_transcript = self.account / "projects" / "demo" / (sid + ".jsonl")
        original = first_transcript.read_bytes()
        self.run_cli("switch", "demo", self.other)
        second_transcript = self.other / "projects" / "demo" / (sid + ".jsonl")
        extra = json.dumps({"type": "user", "message": "A later test turn."}) + "\n"
        with second_transcript.open("a") as handle:
            handle.write(extra)
        expected = second_transcript.read_bytes()
        self.run_cli("switch", "demo", self.account)
        self.assertEqual(self.entry()["session_id"], sid)
        self.assertEqual(self.entry()["account"], str(self.account))
        self.assertEqual(first_transcript.read_bytes(), expected)
        self.assertTrue(expected.startswith(original))
        backups = list(first_transcript.parent.glob(first_transcript.name + ".backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)

    def test_spawn_leaves_trust_prompt_unapproved(self):
        self.spawn(content="FLEET_TRUST", code=7)
        self.assertFalse(self.records("inputs"))
        self.assertEqual(len(self.records("launches")), 1)

    def test_spawn_returns_8_when_startup_verification_fails(self):
        (self.account / "fail-spawn").touch()
        self.spawn(code=8)
        self.assertEqual(self.entry()["phase"], "unverified")
        self.assertEqual(len(self.records("launches")), 1)

    def test_switch_trust_failure_does_not_approve_prompt(self):
        self.spawn()
        (self.other / "show-trust").touch()
        self.run_cli("switch", "demo", self.other, code=7)
        target_pid = next(r["pid"] for r in self.records("launches")
                          if r["account"] == str(self.other))
        self.assertFalse([r for r in self.records("inputs") if r["pid"] == target_pid])
        self.assertEqual(self.entry()["account"], str(self.account))

    def test_stubborn_old_process_prevents_second_launch(self):
        self.spawn(content="FLEET_STUBBORN")
        self.run_cli("switch", "demo", self.other, code=6)
        self.assertEqual(len(self.records("launches")), 1)
        self.assertEqual(self.entry()["account"], str(self.account))

    def test_missing_session_and_usage_have_distinct_codes(self):
        self.run_cli("switch", "missing", self.other, code=4)
        self.run_cli("switch", code=2)
        self.run_cli("switch", "missing", self.other, "--wait", "-1", code=2)

    def test_brief_and_paths_are_passed_without_shell_expansion(self):
        text = "Write literal $(touch should-not-exist), `echo text`, and 'quoted' words."
        self.spawn(content=text)
        launch = self.records("launches")[0]
        self.assertIn(text, launch["argv"])
        self.assertFalse((self.work / "should-not-exist").exists())


class StartupVerificationTests(unittest.TestCase):
    def test_resume_requires_matching_live_started_process_and_prompt(self):
        entry = {"session_id": "expected", "tmux": "fleet-test"}
        baseline = {"session_id": "expected", "started": True, "state": "busy", "pid": 123}
        cases = (
            ({"started": False}, True, "> \n"),
            ({"session_id": "other"}, True, "> \n"),
            ({"state": "mismatch"}, True, "> \n"),
            ({}, False, "> \n"),
            ({}, True, "Loading conversation...\n"),
        )
        for changes, live, text in cases:
            with self.subTest(changes=changes, live=live, text=text), \
                    mock.patch.object(agent_fleet, "run_state", return_value={**baseline, **changes}), \
                    mock.patch.object(agent_fleet, "exists", return_value=True), \
                    mock.patch.object(agent_fleet, "screen", return_value=text), \
                    mock.patch.object(agent_fleet, "alive", return_value=live), \
                    mock.patch.object(agent_fleet, "timeout", return_value=1), \
                    mock.patch.object(agent_fleet.time, "monotonic", side_effect=[0, 2]):
                with self.assertRaises(agent_fleet.FleetError) as failure:
                    agent_fleet.verify(entry, require_prompt=True)
                self.assertEqual(failure.exception.code, agent_fleet.REOPEN)


class DocumentationTests(unittest.TestCase):
    def test_exit_8_includes_spawn_verification_failure_in_both_readmes(self):
        for filename, verification, recovery in (
            ("README.md", "verification", "recovery"),
            ("README.ko.md", "최초 실행 확인 실패", "복구"),
        ):
            with self.subTest(filename=filename):
                line = (ROOT / filename).read_text().splitlines()[83]
                self.assertTrue(line.startswith("| `8` |"))
                self.assertIn("spawn", line)
                self.assertIn(verification, line)
                self.assertIn(recovery, line)


class HookStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-hook-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "run.json"
        agent_fleet.write_json(self.path, {
            "token": "test-token", "session_id": "test-session", "started": True,
            "state": "busy", "tools": [], "agents": [],
        })

    def event(self, kind, **values):
        payload = {"hook_event_name": kind, "session_id": "test-session", **values}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            self.assertEqual(agent_fleet.hook(str(self.path), "test-token"), 0)
        return agent_fleet.read_json(self.path)

    def test_stop_clears_denied_tool_without_completion_event(self):
        self.event("PreToolUse", tool_use_id="denied")
        self.event("PermissionRequest", tool_use_id="denied")
        run = self.event("Stop")
        self.assertEqual(run["tools"], [])
        self.assertEqual(run["state"], "busy")

    def test_idle_clears_cancelled_tool_but_permission_prompt_keeps_it(self):
        self.event("PreToolUse", tool_use_id="cancelled")
        run = self.event("Notification", notification_type="permission_prompt")
        self.assertEqual(run["tools"], ["cancelled"])
        self.assertEqual(run["state"], "busy")
        run = self.event("Notification", notification_type="idle_prompt")
        self.assertEqual(run["tools"], [])
        self.assertEqual(run["state"], "idle")

    def test_tool_cleanup_preserves_other_work(self):
        self.event("PreToolUse", tool_use_id="cancelled")
        self.event("SubagentStart", agent_id="still-running")
        run = self.event("Stop", background_tasks=["task"], session_crons=["cron"])
        self.assertEqual(run["tools"], [])
        run = self.event("Notification", notification_type="idle_prompt")
        self.assertEqual(run["agents"], ["still-running"])
        self.assertEqual(run["background_tasks"], ["task"])
        self.assertEqual(run["session_crons"], ["cron"])
        self.assertEqual(run["state"], "busy")


if __name__ == "__main__":
    unittest.main()
