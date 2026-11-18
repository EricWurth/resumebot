"""A stand-in for Claude Code in the install rehearsal: `--version`, `mcp add/get/remove/list`, `auth status` and
`setup-token`, kept in a JSON file in CLAUDE_CONFIG_DIR. It never reaches a network or the user's own configuration.

Behaviours (a JSON object in `<CLAUDE_CONFIG_DIR>/fake-claude-behavior.json`, written by a scenario):
  signed_in (default true)      `auth status` exit code
  junk_bytes                    bytes cp1252 leaves undefined are printed before every `mcp` answer
  reject_url                    an `mcp add` whose URL holds this text fails ("boom")
A first argument `--broken` makes this copy fail every call (a dangling copy earlier on the PATH).
"""

import json
import os
import sys
from pathlib import Path


def folder() -> Path:
    return Path(os.environ["CLAUDE_CONFIG_DIR"])


def load() -> dict:
    try:
        return json.loads((folder() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(config: dict) -> None:
    folder().mkdir(parents=True, exist_ok=True)
    (folder() / ".claude.json").write_text(json.dumps(config, indent=2), encoding="utf-8")


def behaviour() -> dict:
    try:
        return json.loads((folder() / "fake-claude-behavior.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def out(text: str, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    if behaviour().get("junk_bytes"):
        stream.flush()
        stream.buffer.write(b"\x81\x8d\x8f\x90\x9d ")
    stream.flush()
    stream.buffer.write((text + "\n").encode("utf-8"))
    stream.flush()


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--broken":
        out("this copy is broken", error=True)
        return 1
    if not argv or argv[0] == "--version":
        out("2.0.0 (Claude Code, rehearsal stand-in)")
        return 0
    if argv[0] == "auth" and argv[1:2] == ["status"]:
        return 0 if behaviour().get("signed_in", True) else 1
    if argv[0] == "setup-token":
        out("sk-ant-oat01-" + "R3h3arsal" * 10)
        return 0
    if argv[0] != "mcp":
        out("unknown command", error=True)
        return 1
    config = load()
    servers = config.setdefault("mcpServers", {})
    words = argv[1:]
    verb = words[0] if words else ""
    if verb == "list":
        out("\n".join(f"{name}: {entry['url']}" for name, entry in servers.items()) or "No MCP servers configured")
        return 0
    if verb == "get":
        entry = servers.get(words[1])
        if entry is None:
            out(f"No MCP server found with name: {words[1]}", error=True)
            return 1
        out(f"{words[1]}:\n  Scope: User config (available in all your projects)\n  Status: ✓ Connected\n"
            f"  Type: http\n  URL: {entry['url']}\n  Headers:\n    Authorization: {entry['authorization']}")
        return 0
    if verb == "remove":
        if words[1] not in servers:
            out(f"No MCP server found with name: {words[1]}", error=True)
            return 1
        del servers[words[1]]
        save(config)
        out(f"Removed MCP server {words[1]} from user config")
        return 0
    if verb == "add":
        positional, header, index = [], "", 0
        rest = words[1:]
        while index < len(rest):
            if rest[index] in ("--transport", "--scope"):
                index += 2
            elif rest[index] == "--header":
                header, index = rest[index + 1], index + 2
            else:
                positional.append(rest[index])
                index += 1
        name, url = positional[0], positional[1]
        if name in servers:
            out(f"MCP server {name} already exists in user config", error=True)
            return 1
        if behaviour().get("reject_url") and behaviour()["reject_url"] in url:
            out("boom: the add was refused", error=True)
            return 1
        servers[name] = {"url": url, "authorization": header.split(": ", 1)[-1]}
        save(config)
        out(f"Added HTTP MCP server {name} to user config")
        return 0
    out("unknown mcp command", error=True)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
