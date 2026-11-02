#!/usr/bin/env python3
"""Manage named Claude Code sessions in tmux using only the standard library."""

import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timezone


USAGE, PROTECTED, MISSING, BUSY = 2, 3, 4, 5
OLD_PROCESS, TRUST, REOPEN, CONFLICT = 6, 7, 8, 9
SCRIPT = Path(__file__).resolve()
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
    "PostToolUse", "PostToolUseFailure", "PermissionRequest", "Notification",
    "SubagentStart", "SubagentStop", "Stop", "StopFailure",
)


class FleetError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def state_dir():
    return Path(os.environ.get("AGENT_FLEET_DIR", "~/.local/state/agent-fleet")).expanduser().resolve()


def timeout(name, default):
    try:
        value = float(os.environ.get(name, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except ValueError:
        raise FleetError(USAGE, f"{name} must be a positive number")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def lock(path, blocking=True):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "a", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise FleetError(BUSY, "another operation holds this conversation lock")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def ledger():
    root = state_dir()
    with lock(root / "ledger.lock"):
        path = root / "ledger.json"
        data = read_json(path, {"version": 1, "sessions": {}})
        if data.get("version") != 1 or not isinstance(data.get("sessions"), dict):
            raise FleetError(CONFLICT, "unsupported ledger format")
        yield data


def save_ledger(data):
    data["updated_at"] = stamp()
    write_json(state_dir() / "ledger.json", data)


def tmux(args, entry=None, check=True):
    command = ["tmux"]
    socket_name = (entry or {}).get("socket", os.environ.get("AGENT_FLEET_TMUX_SOCKET"))
    if socket_name:
        command.extend(["-L", socket_name])
    result = subprocess.run(command + list(args), capture_output=True, text=True)
    if check and result.returncode:
        raise FleetError(CONFLICT, "tmux failed: " + result.stderr.strip())
    return result


def exists(entry):
    return tmux(["has-session", "-t", "=" + entry["tmux"]], entry, False).returncode == 0


def pane(entry):
    result = tmux(["list-panes", "-s", "-t", "=" + entry["tmux"], "-F", "#{pane_id}"], entry, False)
    panes = result.stdout.splitlines()
    if result.returncode or len(panes) != 1:
        raise FleetError(BUSY, "expected one managed tmux pane; leave its layout unchanged")
    return panes[0]


def screen(entry):
    return tmux(["capture-pane", "-p", "-t", pane(entry)], entry).stdout


def trust_screen(text):
    return bool(re.search(
        r"trust (?:this|the) (?:folder|directory|workspace)|"
        r"(?:trust|safety).{0,45}(?:folder|directory)|"
        r"(?:folder|directory).{0,45}(?:trust|safety)", text, re.I,
    ))


def empty_prompt(text):
    lines = text.rstrip().splitlines()
    # Claude's empty prompt is a standalone chevron; reject partially typed text.
    prompts = [line for line in lines[-15:] if re.match(r"\s*[❯>]", line)]
    return bool(prompts and re.fullmatch(r"\s*[❯>]\s*", prompts[-1]))


def process_table():
    result = subprocess.run(["ps", "-e", "-o", "pid=,ppid=,stat="], capture_output=True, text=True)
    if result.returncode:
        raise FleetError(BUSY, "cannot inspect running processes")
    table = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and not parts[2].startswith("Z"):
            table[int(parts[0])] = int(parts[1])
    return table


def descendants(pid, table=None):
    table = process_table() if table is None else table
    found, pending = set(), [pid]
    while pending:
        parent = pending.pop()
        for child, owner in table.items():
            if owner == parent and child not in found:
                found.add(child)
                pending.append(child)
    return found


def identity(pid):
    if not pid:
        return ""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True)
    return result.stdout.strip() if not result.returncode else ""


def alive(pid, expected=None):
    if not pid:
        return False
    table = process_table()
    return pid in table and (not expected or identity(pid) == expected)


def run_state(entry):
    return read_json(Path(entry.get("run_file", "")), {}) or {}


def unsafe_orphan(entry):
    if entry.get("kind") != "claude":
        return False
    state = run_state(entry)
    return alive(state.get("pid"), state.get("identity"))


def validate_name(value):
    if not NAME.fullmatch(value):
        raise FleetError(USAGE, "name must start with a letter and use letters, digits, '_' or '-' (up to 64)")
    return value


def directory(value):
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise FleetError(USAGE, f"directory does not exist: {path}")
    return str(path)


def account(value=None):
    return directory(value or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def set_phase(entry, phase, **extra):
    entry.update(phase=phase, updated_at=stamp(), **extra)


def prepare(entry, prompt=None, resume=False):
    root = state_dir()
    token = uuid.uuid4().hex
    run_file = root / "runs" / (token + ".json")
    settings_file = root / "runs" / (token + ".settings.json")
    command = shlex.join([sys.executable, str(SCRIPT), "_hook", str(run_file), token])
    hooks = {event: [{"matcher": "", "hooks": [{"type": "command", "command": command, "timeout": 5}]}] for event in EVENTS}
    write_json(settings_file, {"hooks": hooks})
    argv = ["claude", "--resume" if resume else "--session-id", entry["session_id"],
            "--name", entry["name"], "--settings", str(settings_file),
            "--permission-mode", entry["permission_mode"]]
    for key in ("model", "effort"):
        if entry.get(key):
            argv.extend(["--" + key, entry[key]])
    if prompt is not None:
        argv.append(prompt)
    run = {"token": token, "state": "starting", "session_id": entry["session_id"],
           "account": entry["account"], "workdir": entry["workdir"], "argv": argv,
           "tools": [], "agents": [], "updated_at": stamp()}
    write_json(run_file, run)
    entry["run_file"], entry["launch_token"] = str(run_file), token
    return run_file


def launch(entry, prompt=None, resume=False):
    run_file = prepare(entry, prompt, resume)
    command = "exec " + shlex.join([sys.executable, str(SCRIPT), "_run", str(run_file)])
    # An explicit runner environment overrides tmux server environment from older clients.
    environment = os.environ.copy()
    environment["CLAUDE_CONFIG_DIR"] = entry["account"]
    environment["AGENT_FLEET_DIR"] = str(state_dir())
    # tmux may keep a different PATH; the runner restores the caller's selected PATH.
    with lock(Path(str(run_file) + ".lock")):
        run = read_json(run_file)
        run["environment"] = {k: environment[k] for k in ("PATH", "CLAUDE_CONFIG_DIR", "AGENT_FLEET_DIR") if k in environment}
        write_json(run_file, run)
    tmux(["new-session", "-d", "-s", entry["tmux"], "-c", entry["workdir"], command], entry)


def verify(entry, require_prompt=False):
    end = time.monotonic() + timeout("AGENT_FLEET_START_TIMEOUT", 75)
    while True:
        state = run_state(entry)
        if state.get("state") in ("exited", "error", "mismatch"):
            raise FleetError(REOPEN, f"Claude did not open the expected conversation ({state.get('state')})")
        if exists(entry):
            text = screen(entry)
            if trust_screen(text):
                raise FleetError(TRUST, f"folder trust needs attention; attach to {entry['tmux']}; no keys were sent")
            if state.get("started") and state.get("session_id") == entry["session_id"] and alive(state.get("pid"), state.get("identity")):
                # A promptless resume may never send idle_prompt. Startup is
                # distinct from proving an existing session safe to stop.
                if not require_prompt or empty_prompt(text):
                    return
        if time.monotonic() >= end:
            raise FleetError(REOPEN, "startup could not be verified; inspect the tmux pane")
        time.sleep(0.1)


def idle(entry, state=None, text=None):
    state = run_state(entry) if state is None else state
    if state.get("state") != "idle" or not state.get("started"):
        return False
    if state.get("tools") or state.get("agents") or state.get("background_tasks") or state.get("session_crons"):
        return False
    if not alive(state.get("pid"), state.get("identity")):
        return False
    if descendants(state["pid"]):
        return False
    text = screen(entry) if text is None else text
    if trust_screen(text):
        raise FleetError(TRUST, "folder trust is awaiting a decision; no keys were sent")
    if not empty_prompt(text):
        return False
    target = pane(entry)
    cursor = tmux(["display-message", "-p", "-t", target, "#{cursor_y}"], entry).stdout.strip()
    viewport = tmux(["capture-pane", "-p", "-t", target], entry).stdout.splitlines()
    if not cursor.isdigit() or int(cursor) >= len(viewport):
        return False
    row = viewport[int(cursor)].strip()
    if row not in ("", ">", "❯"):
        return False
    attached = tmux(["display-message", "-p", "-t", pane(entry), "#{session_attached}"], entry).stdout.strip()
    return attached == "0"


def wait_idle(entry, minutes):
    end = time.monotonic() + minutes * 60
    while True:
        if not exists(entry):
            raise FleetError(MISSING, "tmux session is missing")
        text = screen(entry)
        if trust_screen(text):
            raise FleetError(TRUST, "folder trust is awaiting a decision; no keys were sent")
        if idle(entry, text=text):
            # Two observations reject a transient prompt or a hook still exiting.
            time.sleep(0.15)
            if idle(entry):
                return
        if time.monotonic() >= end:
            raise FleetError(BUSY, "session is busy, attached, or has no verified empty input prompt; left unchanged")
        time.sleep(min(0.2, max(0, end - time.monotonic())))


def wait_gone(entry, known):
    end = time.monotonic() + timeout("AGENT_FLEET_STOP_TIMEOUT", 20)
    while True:
        table = process_table()
        present = any(pid in table and identity(pid) == start for pid, start in known.items())
        if not present and not exists(entry):
            return
        if time.monotonic() >= end:
            raise FleetError(OLD_PROCESS, "previous process or pane remains; no replacement was opened")
        time.sleep(0.1)


def process_family(entry):
    state = run_state(entry)
    pid = state.get("pid")
    if not alive(pid, state.get("identity")):
        return {}
    return {p: identity(p) for p in ({pid} | descendants(pid))}


def stop_idle(entry):
    if not idle(entry):
        raise FleetError(BUSY, "session changed while checking; left unchanged")
    known = process_family(entry)
    if not known:
        raise FleetError(MISSING, "managed Claude process is missing")
    target = pane(entry)
    state = run_state(entry)
    runner = state.get("runner_pid")
    pane_pid = tmux(["display-message", "-p", "-t", target, "#{pane_pid}"], entry).stdout.strip()
    if not runner or pane_pid != str(runner) or process_table().get(state.get("pid")) != runner:
        raise FleetError(BUSY, "tmux pane no longer belongs to the managed runner; no keys were sent")
    tmux(["send-keys", "-t", target, "-l", "/exit"], entry)
    tmux(["send-keys", "-t", target, "Enter"], entry)
    wait_gone(entry, known)


def stop_failed_launch(entry):
    state = run_state(entry)
    if not exists(entry) and not alive(state.get("pid"), state.get("identity")):
        return
    known = process_family(entry)
    if state.get("pid") and descendants(state["pid"]):
        raise FleetError(OLD_PROCESS, "failed replacement has child processes; inspect it before recovery")
    if state.get("tools") or state.get("agents") or state.get("background_tasks") or state.get("session_crons") or state.get("started") and state.get("state") not in ("idle", "ending", "exited"):
        raise FleetError(OLD_PROCESS, "failed replacement has active work; inspect it before recovery")
    # A replacement is started without a prompt. TERM does not approve a trust dialog.
    pid = state.get("pid")
    if pid and alive(pid, state.get("identity")):
        os.kill(pid, signal.SIGTERM)
    elif exists(entry):
        raise FleetError(OLD_PROCESS, "replacement process identity is unknown; no recovery was started")
    wait_gone(entry, known)


def copy_conversation(source, target_account):
    state = run_state(source)
    transcript = Path(state.get("transcript_path", "")).resolve()
    source_root = Path(source["account"]) / "projects"
    try:
        relative = transcript.relative_to(source_root.resolve())
    except ValueError:
        raise FleetError(REOPEN, "transcript is outside this account's projects directory")
    if transcript.name != source["session_id"] + ".jsonl" or not transcript.is_file():
        raise FleetError(REOPEN, "conversation transcript could not be verified")
    target_root = (Path(target_account) / "projects").resolve()
    destination = target_root / relative
    if not destination.resolve().is_relative_to(target_root):
        raise FleetError(REOPEN, "target conversation path is unsafe")
    source_bytes = transcript.read_bytes()
    if destination.exists():
        target_bytes = destination.read_bytes()
        if source_bytes != target_bytes:
            if not target_bytes or not source_bytes.startswith(target_bytes) or not target_bytes.endswith(b"\n"):
                raise FleetError(REOPEN, "target account already has a divergent transcript; no file was replaced")
            # Preserve the old account's shorter history before updating an exact ancestor.
            backup = destination.with_name(destination.name + ".backup-" + uuid.uuid4().hex)
            with backup.open("xb") as handle:
                os.chmod(backup, 0o600)
                handle.write(target_bytes)
            fd, temporary = tempfile.mkstemp(prefix=".conversation-", dir=destination.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(source_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with destination.open("xb") as dst:
            os.chmod(destination, 0o600)
            dst.write(source_bytes)
            dst.flush()
            os.fsync(dst.fileno())
    # Only transcript and conversation-specific sidecars, never account settings or credentials.
    sidecar = transcript.with_suffix("")
    if sidecar.is_symlink():
        raise FleetError(REOPEN, "conversation sidecar is a symlink")
    if sidecar.is_dir():
        target_sidecar = destination.with_suffix("")
        for root, directories, files in os.walk(sidecar, followlinks=False):
            if any((Path(root) / d).is_symlink() for d in directories):
                raise FleetError(REOPEN, "conversation sidecar contains a symlink")
            for filename in files:
                src = Path(root) / filename
                if src.is_symlink():
                    raise FleetError(REOPEN, "conversation sidecar contains a symlink")
                dst = target_sidecar / src.relative_to(sidecar)
                if not dst.resolve().is_relative_to(target_root):
                    raise FleetError(REOPEN, "target sidecar path is unsafe")
                if dst.exists():
                    if src.read_bytes() != dst.read_bytes():
                        raise FleetError(REOPEN, "target sidecar differs; no file was replaced")
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with src.open("rb") as reader, dst.open("xb") as writer:
                        os.chmod(dst, 0o600)
                        shutil.copyfileobj(reader, writer)
    return str(destination)


def spawn(args):
    validate_name(args.name)
    cwd = directory(args.workdir)
    cfg = account(args.account)
    brief = Path(args.brief).expanduser().resolve()
    if not brief.is_file():
        raise FleetError(USAGE, "brief file does not exist")
    prompt = brief.read_text(encoding="utf-8")
    if not prompt.strip():
        raise FleetError(USAGE, "brief file is empty")
    entry = {"name": args.name, "tmux": "fleet-" + args.name, "socket": os.environ.get("AGENT_FLEET_TMUX_SOCKET"),
             "account": cfg, "workdir": cwd, "created_at": stamp(), "updated_at": stamp(),
             "model": args.model, "effort": args.effort, "permission_mode": args.permission_mode,
             "kind": "claude", "session_id": str(uuid.uuid4()), "protected": False, "phase": "starting"}
    if args.dry_run:
        print(json.dumps({"dry_run": True, "name": args.name, "tmux": entry["tmux"], "account": cfg,
                          "workdir": cwd, "brief": str(brief), "model": args.model, "effort": args.effort,
                          "permission_mode": args.permission_mode}, indent=2))
        return
    if not shutil.which("claude"):
        raise FleetError(USAGE, "claude command is not on PATH")
    with ledger() as data:
        previous = data["sessions"].get(args.name)
        if previous and (exists(previous) or unsafe_orphan(previous) or previous.get("phase") in ("switching", "recovery-needed")):
            raise FleetError(CONFLICT, "name is already active or requires recovery")
        if exists(entry):
            raise FleetError(CONFLICT, "tmux session with this name already exists")
        if previous:
            entry["protected"] = previous.get("protected", False)
        data["sessions"][args.name] = entry
        try:
            launch(entry, prompt)
            save_ledger(data)
            verify(entry)
            set_phase(entry, "running")
        except FleetError as error:
            set_phase(entry, "trust" if error.code == TRUST else "unverified", error=error.message)
            save_ledger(data)
            raise
        save_ledger(data)
    print(f"spawned {args.name}: tmux={entry['tmux']} account={cfg}")


def list_sessions(args):
    with ledger() as data:
        entries = []
        for entry in data["sessions"].values():
            row = dict(entry)
            row["alive"] = exists(entry)
            row["tmux_state"] = "alive" if row["alive"] else "missing"
            entries.append(row)
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
    elif not entries:
        print("No sessions.")
    else:
        print("NAME\tTMUX\tSTATE\tACCOUNT\tWORKDIR\tPROTECTED")
        for row in entries:
            print("\t".join(str(row[key]) for key in ("name", "tmux", "tmux_state", "account", "workdir", "protected")))


def protect(args, enabled):
    with ledger() as data:
        entry = data["sessions"].get(args.name)
        if not entry:
            raise FleetError(MISSING, "session is not in the ledger")
        entry.update(protected=enabled, updated_at=stamp())
        save_ledger(data)
    print(f"{args.name}: protected={str(enabled).lower()}")


def switch(args):
    target_account = account(args.account_dir)
    if not math.isfinite(args.wait) or args.wait < 0:
        raise FleetError(USAGE, "--wait must be a nonnegative number of minutes")
    with ledger() as data:
        original = dict(data["sessions"].get(args.name) or {})
    if not original:
        raise FleetError(MISSING, "session is not in the ledger")
    if original.get("protected"):
        raise FleetError(PROTECTED, "protected session was left unchanged")
    if original.get("kind") != "claude":
        raise FleetError(MISSING, "session is not a managed Claude conversation")
    sid = original["session_id"]
    with lock(state_dir() / "locks" / (sid + ".lock"), blocking=False):
        with ledger() as data:
            entry = data["sessions"].get(args.name)
            if not entry or entry.get("session_id") != sid:
                raise FleetError(CONFLICT, "ledger changed; retry the command")
            if entry.get("protected"):
                raise FleetError(PROTECTED, "protected session was left unchanged")
            if not exists(entry):
                raise FleetError(MISSING, "tmux session is missing")
            if entry.get("account") == target_account:
                print(f"{args.name}: already using this account")
                return
            wait_idle(entry, args.wait)
            source = dict(entry)
            set_phase(entry, "switching", switch_from=source["account"], switch_to=target_account)
            save_ledger(data)
            try:
                stop_idle(source)
            except FleetError:
                set_phase(entry, "recovery-needed")
                save_ledger(data)
                raise
            failure = None
            target_started = False
            try:
                copy_conversation(source, target_account)
                entry["account"] = target_account
                target_started = True
                launch(entry, resume=True)
                save_ledger(data)
                verify(entry, require_prompt=True)
            except (FleetError, OSError) as error:
                failure = error if isinstance(error, FleetError) else FleetError(REOPEN, str(error))
            if failure is None:
                set_phase(entry, "running")
                entry.pop("error", None)
                save_ledger(data)
                print(f"switched {args.name}: same conversation, account={target_account}")
                return
            if target_started:
                try:
                    stop_failed_launch(entry)
                except FleetError as error:
                    set_phase(entry, "recovery-needed", error=error.message)
                    save_ledger(data)
                    raise
            # Reopen only after the replacement process is confirmed gone.
            entry.update(account=source["account"])
            try:
                launch(entry, resume=True)
                save_ledger(data)
                verify(entry, require_prompt=True)
                set_phase(entry, "running", error=failure.message)
                save_ledger(data)
                print(f"restored {args.name}: original account", file=sys.stderr)
            except (FleetError, OSError) as recovery:
                set_phase(entry, "recovery-needed", error=f"replacement failed: {failure}; recovery failed: {recovery}")
                save_ledger(data)
                raise FleetError(REOPEN, "replacement and recovery could not be verified; inspect tmux and ledger")
            raise FleetError(failure.code if failure.code == TRUST else REOPEN, failure.message)


def keep(args):
    config = Path(args.config).expanduser().resolve()
    with config.open("rb") as handle:
        settings = tomllib.load(handle)
    jobs = settings.get("jobs", [])
    if not isinstance(jobs, list):
        raise FleetError(USAGE, "config must contain [[jobs]] tables")
    prepared, names = [], set()
    for job in jobs:
        name = validate_name(job.get("name", ""))
        if name in names:
            raise FleetError(USAGE, "duplicate job name in config")
        names.add(name)
        cmd = job.get("command")
        if not (isinstance(cmd, str) and cmd.strip()) and not (isinstance(cmd, list) and cmd and all(isinstance(v, str) and v for v in cmd)):
            raise FleetError(USAGE, "job command must be a nonempty string or argument array")
        workdir = Path(job.get("workdir", str(config.parent))).expanduser()
        if not workdir.is_absolute():
            workdir = config.parent / workdir
        prepared.append((name, cmd, directory(workdir)))
    with ledger() as data:
        for name, command, cwd in prepared:
            old = data["sessions"].get(name)
            if old and old.get("kind") != "keep":
                raise FleetError(CONFLICT, "keep name belongs to a managed Claude conversation")
            entry = old or {"name": name, "tmux": "fleet-" + name, "socket": os.environ.get("AGENT_FLEET_TMUX_SOCKET"),
                            "account": "-", "workdir": cwd, "kind": "keep", "created_at": stamp(), "protected": False}
            if exists(entry):
                continue
            argv = ["/bin/sh", "-c", command] if isinstance(command, str) else command
            tmux(["new-session", "-d", "-s", entry["tmux"], "-c", cwd, "exec " + shlex.join(argv)], entry)
            entry.update(workdir=cwd, command=command, phase="running", updated_at=stamp())
            data["sessions"][name] = entry
            save_ledger(data)
            message = f"{stamp()} restarted {name} tmux={entry['tmux']}"
            with open(state_dir() / "keep.log", "a", encoding="utf-8") as handle:
                handle.write(message + "\n")
            print(message)


def run_child(path):
    run_file = Path(path)
    run = read_json(run_file)
    if not run:
        return USAGE
    env = os.environ.copy()
    env.update(run.get("environment", {}))
    try:
        child = subprocess.Popen(run["argv"], cwd=run["workdir"], env=env)
    except OSError as error:
        with lock(Path(str(run_file) + ".lock")):
            current = read_json(run_file)
            current.update(state="error", error=str(error), updated_at=stamp())
            write_json(run_file, current)
        return REOPEN
    with lock(Path(str(run_file) + ".lock")):
        current = read_json(run_file)
        current.update(pid=child.pid, identity=identity(child.pid), runner_pid=os.getpid())
        write_json(run_file, current)
    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    result = child.wait()
    with lock(Path(str(run_file) + ".lock")):
        current = read_json(run_file)
        current.update(state="exited", exit_code=result, updated_at=stamp())
        write_json(run_file, current)
    return result if result >= 0 else 128 - result


def hook(path, token):
    event = json.load(sys.stdin)
    run_file = Path(path)
    with lock(Path(str(run_file) + ".lock")):
        run = read_json(run_file)
        if not run or run.get("token") != token:
            return 0
        if event.get("session_id") != run.get("session_id"):
            run.update(state="mismatch", updated_at=stamp())
            write_json(run_file, run)
            return 0
        kind = event.get("hook_event_name", "")
        if run.get("state") == "mismatch" and kind != "SessionStart":
            return 0
        run["updated_at"] = stamp()
        run["last_event"] = kind
        if kind == "SessionStart" and event.get("transcript_path"):
            run["transcript_path"] = event["transcript_path"]
        tools, agents = set(run.get("tools", [])), set(run.get("agents", []))
        if kind == "SessionStart":
            run.update(started=True, state="busy")
        elif kind == "SessionEnd":
            run["state"] = "ending"
        elif kind == "PreToolUse":
            tools.add(event.get("tool_use_id", "unknown"))
            run["state"] = "busy"
        elif kind in ("PostToolUse", "PostToolUseFailure"):
            tools.discard(event.get("tool_use_id", "unknown"))
            run["state"] = "busy"
        elif kind == "SubagentStart":
            agents.add(event.get("agent_id", "unknown"))
            run["state"] = "busy"
        elif kind == "SubagentStop":
            agents.discard(event.get("agent_id", "unknown"))
            run["state"] = "busy"
        elif kind == "Notification":
            if event.get("notification_type") == "idle_prompt":
                # Denied or cancelled calls need not have a completion hook.
                tools.clear()
            run["state"] = "idle" if event.get("notification_type") == "idle_prompt" and not tools and not agents else "busy"
        elif kind in ("Stop", "StopFailure"):
            tools.clear()
            run["state"] = "busy"
            run["background_tasks"] = event.get("background_tasks", [])
            run["session_crons"] = event.get("session_crons", [])
        else:
            run["state"] = "busy"
        run["tools"], run["agents"] = sorted(tools), sorted(agents)
        write_json(run_file, run)
    return 0


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    add = commands.add_parser("spawn", help="start a named Claude conversation")
    add.add_argument("name")
    add.add_argument("workdir")
    add.add_argument("brief")
    add.add_argument("--account")
    add.add_argument("--model")
    add.add_argument("--effort")
    add.add_argument("--permission-mode", default="default", choices=("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions", "manual"))
    add.add_argument("--dry-run", action="store_true")
    show = commands.add_parser("list", help="list ledger entries and live tmux state")
    show.add_argument("--json", action="store_true")
    move = commands.add_parser("switch", help="resume an idle conversation under another account directory")
    move.add_argument("name")
    move.add_argument("account_dir")
    move.add_argument("--wait", type=float, default=0, metavar="MIN")
    for verb in ("protect", "unprotect"):
        item = commands.add_parser(verb)
        item.add_argument("name")
    keeper = commands.add_parser("keep", help="restart missing long-running jobs")
    keeper.add_argument("--config", default="fleet.toml")
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv and argv[0] == "_run" and len(argv) == 2:
            return run_child(argv[1])
        if argv and argv[0] == "_hook" and len(argv) == 3:
            return hook(argv[1], argv[2])
        args = parser().parse_args(argv)
        if args.command == "spawn":
            spawn(args)
        elif args.command == "list":
            list_sessions(args)
        elif args.command == "switch":
            switch(args)
        elif args.command in ("protect", "unprotect"):
            protect(args, args.command == "protect")
        elif args.command == "keep":
            keep(args)
        return 0
    except FleetError as error:
        print(f"agent-fleet: {error.message}", file=sys.stderr)
        return error.code
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"agent-fleet: {error}", file=sys.stderr)
        return USAGE


if __name__ == "__main__":
    sys.exit(main())
