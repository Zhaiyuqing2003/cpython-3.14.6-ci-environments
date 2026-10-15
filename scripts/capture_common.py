"""Helpers shared by the macOS/iOS and Windows capture scripts.

Runs on the runner's own Python (3.9 or newer), so it uses only the
standard library and avoids newer syntax.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys

# Environment variables whose names look like credentials are never written
# out: the artifacts of a public repository are public.
_SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_KEY$|^KEY", re.I)


def filtered_env(env=None) -> dict:
    env = os.environ if env is None else env
    return {k: v for k, v in sorted(env.items()) if not _SECRET_NAME.search(k)}


def run(argv, *, env=None, cwd=None, timeout=600, stdin=None) -> dict:
    """Run a command and return its argv, exit status and output."""
    try:
        proc = subprocess.run(
            argv,
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL if stdin is None else stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": list(map(str, argv)), "error": repr(exc)}
    return {
        "argv": list(map(str, argv)),
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace"),
    }


def runner_facts() -> dict:
    names = [
        "ImageOS",
        "ImageVersion",
        "RUNNER_OS",
        "RUNNER_ARCH",
        "RUNNER_NAME",
        "RUNNER_ENVIRONMENT",
        "GITHUB_REPOSITORY",
        "GITHUB_RUN_ID",
        "GITHUB_RUN_ATTEMPT",
        "GITHUB_JOB",
        "GITHUB_WORKFLOW",
        "GITHUB_SHA",
        "GITHUB_SERVER_URL",
    ]
    facts = {name: os.environ.get(name) for name in names}
    if facts["GITHUB_SERVER_URL"] and facts["GITHUB_RUN_ID"]:
        facts["run_url"] = "{}/{}/actions/runs/{}".format(
            facts["GITHUB_SERVER_URL"], facts["GITHUB_REPOSITORY"], facts["GITHUB_RUN_ID"]
        )
    facts["captured_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    facts["capture_python"] = {"executable": sys.executable, "version": sys.version}
    return facts


def write_json(path, data) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=False)
        f.write("\n")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def record_step(args) -> None:
    """Append one command's argv, working directory and environment."""
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    entry = {
        "name": args.name,
        "argv": argv,
        "cwd": os.getcwd(),
        "env": filtered_env(),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def combine(args) -> None:
    """Merge the parts written by the other subcommands into one JSON file."""
    out = {"schema": 1}
    out["mirrors"] = json.loads(args.mirrors) if args.mirrors else None
    out["cpython"] = {"repository": "python/cpython", "ref": args.cpython_ref}
    if args.cpython_dir:
        head = run(["git", "-C", args.cpython_dir, "rev-parse", "HEAD"])
        out["cpython"]["commit"] = head.get("stdout", "").strip() or None
    out["runner"] = runner_facts()
    for name in sorted(os.listdir(args.parts)):
        if name.endswith(".json"):
            out[name[: -len(".json")]] = read_json(os.path.join(args.parts, name))
    steps = []
    if args.steps and os.path.exists(args.steps):
        with open(args.steps, encoding="utf-8") as f:
            steps = [json.loads(line) for line in f if line.strip()]
    out["steps"] = steps
    outcomes = {}
    for item in args.outcome or []:
        key, _, value = item.partition("=")
        outcomes[key] = value
    out["step_outcomes"] = outcomes
    out["build_succeeded"] = bool(outcomes) and all(v == "success" for v in outcomes.values())
    write_json(args.out, out)
    print("wrote", args.out, "build_succeeded =", out["build_succeeded"])


def add_common_subcommands(sub) -> None:
    p = sub.add_parser("step", help="record a command line and its environment")
    p.add_argument("--out", required=True)
    p.add_argument("name")
    p.add_argument("argv", nargs="+")
    p.set_defaults(func=record_step)

    p = sub.add_parser("combine", help="merge the captured parts into one file")
    p.add_argument("--parts", required=True)
    p.add_argument("--steps")
    p.add_argument("--out", required=True)
    p.add_argument("--mirrors", help="JSON: the CPython CI job this job mirrors")
    p.add_argument("--cpython-ref", default="v3.14.6")
    p.add_argument("--cpython-dir")
    p.add_argument("--outcome", action="append", help="STEP=success|failure|...")
    p.set_defaults(func=combine)
