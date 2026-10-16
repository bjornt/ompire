"""Observe the real launcher's declaration and model Workshop tunnel namespaces.

This is a container-tool fake, not a grant-aware launcher. It knows only SDK
plug/slot declarations and the connect invocations Workshop actually receives.
The endpoint probe models a container-local route, not host TCP networking.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import yaml


def capture(root: Path) -> dict:
    definition = next(
        path for path in (root / "workshop.yaml", root / ".workshop.yaml") if path.is_file()
    )
    text = definition.read_text()
    document = yaml.safe_load(text)
    name = document.get("name", "workshop")
    declarations: dict[str, dict] = {"plugs": {}, "slots": {}}
    for sdk in document.get("sdks", []) or []:
        for kind, entries in declarations.items():
            for entry_name, attributes in (sdk.get(kind) or {}).items():
                entries[f"{name}/{sdk['name']}:{entry_name}"] = attributes
    additions = root / "workshop.my.yaml"
    return {
        "definition": text,
        "additions": additions.read_text() if additions.is_file() else None,
        **declarations,
    }


def connection(entry: dict, plug: str, slot: str) -> dict | None:
    declaration = entry["launch"]
    source = declaration["plugs"].get(plug)
    target = declaration["slots"].get(slot)
    if source is None or target is None:
        raise ValueError(f"undeclared plug/slot: {plug} -> {slot}")
    if source.get("interface") != target.get("interface"):
        raise ValueError(f"incompatible plug/slot interfaces: {plug} -> {slot}")
    if source.get("interface") != "tunnel":
        return None
    if not source.get("endpoint") or not target.get("endpoint"):
        raise ValueError(f"tunnel plug/slot requires endpoints: {plug} -> {slot}")
    return {
        "plug": plug,
        "slot": slot,
        "endpoint": source["endpoint"],
        "upstream": target["endpoint"],
    }


def probe(endpoint: str) -> dict:
    state = Path(os.environ["LOCAL_TEST_STATE"]) / "workshops.json"
    entries = json.loads(state.read_text())["workshops"] if state.is_file() else {}
    entry = entries.get(str(Path.cwd().resolve()))
    if entry is None:
        raise ValueError("workshop not launched; no container-local endpoints")
    # URLs and host:port observations address the same logical tunnel endpoint.
    target = urlsplit(endpoint).netloc if "://" in endpoint else endpoint
    for plug, slot in entry.get("connects", []):
        route = connection(entry, plug, slot)
        if route is not None and route["endpoint"] == target:
            return {"reachable": True, **route}
    raise ValueError(f"no connected tunnel at {target}")


def process_identity(pid: int) -> dict | None:
    """Linux start ticks plus boot identity distinguish a process from PID reuse."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


@contextmanager
def process_registry(root: Path):
    """Serialize exec admission with removal without racing Workshop's JSON writes."""
    state = Path(os.environ["LOCAL_TEST_STATE"])
    directory = state / "workshop-processes" / hashlib.sha256(str(root).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "processes.json"
        record = json.loads(path.read_text()) if path.exists() else {"children": [], "removed": None}
        yield record
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record))
        temporary.replace(path)


def execute(root: Path, container_id: str, argv: list[str]) -> None:
    """Register, then exec in place: no supervisor alters pipes, signals, or status."""
    root = root.resolve()
    with process_registry(root) as record:
        state = Path(os.environ["LOCAL_TEST_STATE"]) / "workshops.json"
        entry = json.loads(state.read_text())["workshops"].get(str(root))
        if entry is None or entry["id"] != container_id or record["removed"] == container_id:
            raise ValueError("workshop not launched; refusing exec after container removal")
        record["children"] = [
            child for child in record["children"]
            if process_identity(child["pid"]) == child["identity"]
        ]
        record["children"].append({
            "container_id": container_id,
            "pid": os.getpid(),
            "identity": process_identity(os.getpid()),
        })
    try:
        os.execvpe(argv[0], argv, os.environ)
    except FileNotFoundError as exc:
        print(f"workshop (fake): {exc}", file=sys.stderr)
        sys.exit(127)
    except PermissionError as exc:
        print(f"workshop (fake): {exc}", file=sys.stderr)
        sys.exit(126)


def remove_processes(root: Path, container_id: str) -> None:
    """Pin each exact recorded process before signalling; never signal a PID alone."""
    with process_registry(root.resolve()) as record:
        record["removed"] = container_id
        descriptors = []
        try:
            for child in record["children"]:
                if child["container_id"] != container_id:
                    continue
                try:
                    descriptor = os.pidfd_open(child["pid"])
                except ProcessLookupError:
                    continue
                # Revalidate after pidfd_open: a PID recycled between the read
                # and open must not let teardown kill a different host process.
                if process_identity(child["pid"]) != child["identity"]:
                    os.close(descriptor)
                    continue
                descriptors.append(descriptor)
            for descriptor in descriptors:
                try:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            # Removal is complete only after children close their pipes. pidfds
            # become readable on exit, including children reaped by the daemon.
            deadline = time.monotonic() + 5
            while descriptors:
                ready, _, _ = select.select(descriptors, [], [], max(0, deadline - time.monotonic()))
                if not ready:
                    raise ValueError("container exec children did not exit after removal")
                for descriptor in ready:
                    descriptors.remove(descriptor)
                    os.close(descriptor)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        record["children"] = [
            child for child in record["children"] if child["container_id"] != container_id
        ]


def process_selfcheck(workshop: str, root: Path) -> None:
    """Exercise real fake exec/remove, including PID reuse and isolation guards."""
    other = root.parent / "other-container"
    other.mkdir()
    (other / "workshop.yaml").write_text("name: other\nbase: ubuntu@24.04\n")
    subprocess.run([workshop, "launch", "-p", str(other)], check=True, capture_output=True, timeout=15)
    program = "import os,time; print(os.getpid(), flush=True); time.sleep(120)"
    children = []

    def start(project: Path | None):
        command = [sys.executable, "-c", program]
        if project is not None:
            command = [workshop, "exec", "-p", str(project), "--", *command]
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(child)
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 10)[0], "exec child did not become ready"
        assert child.stdout.readline().strip() == str(child.pid), "exec changed the child PID or failed"
        return child

    try:
        target = start(root)
        collateral = start(None)
        survivor = start(other)
        signalled = start(root)
        signalled.send_signal(signal.SIGTERM)
        assert signalled.wait(timeout=5) == -signal.SIGTERM, "exec changed signal/exit semantics"

        state = Path(os.environ["LOCAL_TEST_STATE"]) / "workshops.json"
        entry = json.loads(state.read_text())["workshops"][str(root)]
        # A stale PID whose identity no longer matches is deliberately the live
        # unrelated host process. A numeric-PID-only teardown would kill it.
        stale_identity = process_identity(collateral.pid)
        assert stale_identity is not None
        stale_identity["start_ticks"] = str(int(stale_identity["start_ticks"]) - 1)
        with process_registry(root) as record:
            record["children"].append({
                "container_id": entry["id"],
                "pid": collateral.pid,
                "identity": stale_identity,
            })
            # Correct process identity is insufficient without the matching
            # container: records from a different launch cannot widen teardown.
            record["children"].append({
                "container_id": "different-container-id",
                "pid": survivor.pid,
                "identity": process_identity(survivor.pid),
            })
        subprocess.run([workshop, "remove", "-p", str(root)], check=True, capture_output=True, timeout=15)
        assert target.wait(timeout=5) == -signal.SIGKILL, "remove left the container child alive"
        assert target.stdout is not None and target.stdout.read() == "", "remove left exec stdout open"
        assert collateral.poll() is None, "remove killed a reused PID belonging to an unrelated host process"
        assert survivor.poll() is None, "remove killed a different container's process"
        subprocess.run([workshop, "remove", "-p", str(other)], check=True, capture_output=True, timeout=15)
        assert survivor.wait(timeout=5) == -signal.SIGKILL, "second container did not own its child"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stdout is not None:
                child.stdout.close()
            if child.stderr is not None:
                child.stderr.close()


def main() -> None:
    command, *args = sys.argv[1:]
    if command == "capture" and len(args) == 1:
        result = capture(Path(args[0]))
    elif command == "connect" and len(args) == 3:
        result = connection(json.loads(args[0]), args[1], args[2])
    elif command == "probe" and len(args) == 1:
        result = probe(args[0])
    elif command == "execute" and len(args) >= 3:
        execute(Path(args[0]), args[1], args[2:])
        return
    elif command == "remove-processes" and len(args) == 2:
        remove_processes(Path(args[0]), args[1])
        return
    elif command == "process-selfcheck" and len(args) == 2:
        process_selfcheck(args[0], Path(args[1]))
        return
    else:
        raise ValueError("unsupported Workshop contract invocation")
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, StopIteration, yaml.YAMLError) as exc:
        print(f"workshop (fake): {exc}", file=sys.stderr)
        sys.exit(1)
