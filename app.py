from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
SETTINGS_FILE = APP_DIR / "koboldcode_settings.json"
DEFAULT_SETTINGS = {
    "base_url": "http://127.0.0.1:5001",
    "api_key": "",
    "workspace": str((APP_DIR / "workspace").resolve()),
    "model": "",
    "max_tokens": 4096,
    "temperature": 0.2,
    "require_shell_approval": True,
    "max_tool_rounds": 12,
}
(APP_DIR / "workspace").mkdir(exist_ok=True)

def load_settings() -> dict[str, Any]:
    data = DEFAULT_SETTINGS.copy()
    try:
        if SETTINGS_FILE.exists():
            data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    data["base_url"] = str(data.get("base_url", DEFAULT_SETTINGS["base_url"])).rstrip("/")
    data["workspace"] = str(Path(data.get("workspace") or DEFAULT_SETTINGS["workspace"]).expanduser().resolve())
    return data

def save_settings(data: dict[str, Any]) -> dict[str, Any]:
    current = load_settings()
    allowed = set(DEFAULT_SETTINGS)
    current.update({k: v for k, v in data.items() if k in allowed})
    current["base_url"] = str(current["base_url"]).strip().rstrip("/")
    if not current["base_url"].startswith(("http://", "https://")):
        raise HTTPException(400, "Base URL must begin with http:// or https://")
    current["workspace"] = str(Path(current["workspace"]).expanduser().resolve())
    Path(current["workspace"]).mkdir(parents=True, exist_ok=True)
    current["max_tokens"] = max(128, min(int(current["max_tokens"]), 32768))
    current["temperature"] = max(0.0, min(float(current["temperature"]), 2.0))
    current["max_tool_rounds"] = max(1, min(int(current["max_tool_rounds"]), 30))
    SETTINGS_FILE.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current

def safe_root() -> Path:
    settings = load_settings()
    root = Path(settings["workspace"]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root

def safe_path(rel_path: str) -> Path:
    root = safe_root()
    candidate = (root / (rel_path or ".")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError("Path escapes the selected workspace.")
    return candidate

def api_headers(settings: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = str(settings.get("api_key", "")).strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers

app = FastAPI(title="KoboldCode", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")

class SettingsInput(BaseModel):
    base_url: str = "http://127.0.0.1:5001"
    api_key: str = ""
    workspace: str = ""
    model: str = ""
    max_tokens: int = 4096
    temperature: float = 0.2
    require_shell_approval: bool = True
    max_tool_rounds: int = 12

class ChatInput(BaseModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    user_message: str

class ApprovalInput(BaseModel):
    approved: bool

pending_approvals: dict[str, asyncio.Future] = {}

@app.get("/")
async def index():
    return FileResponse(APP_DIR / "static" / "index.html")

@app.get("/api/settings")
async def get_settings():
    s = load_settings()
    return {**s, "api_key": "••••••••" if s.get("api_key") else ""}

@app.post("/api/settings")
async def put_settings(inp: SettingsInput):
    values = inp.model_dump()
    if values.get("api_key") == "••••••••":
        values["api_key"] = load_settings().get("api_key", "")
    return {**save_settings(values), "api_key": "••••••••" if values.get("api_key") else ""}

@app.get("/api/workspace")
async def workspace_info():
    root = safe_root()
    return {"workspace": str(root), "entries": _list_files(".", max_entries=300)}

@app.get("/api/diagnostics")
async def diagnostics():
    s = load_settings()
    base = s["base_url"]
    out: dict[str, Any] = {"base_url": base, "reachable": False, "checks": [], "model": None}
    timeout = httpx.Timeout(8.0, connect=3.0)
    async with httpx.AsyncClient(timeout=timeout, headers=api_headers(s)) as client:
        checks = [
            ("version", "/api/extra/version"),
            ("context", "/api/extra/true_max_context_length"),
            ("performance", "/api/extra/perf"),
            ("props", "/props"),
            ("models", "/v1/models"),
        ]
        for name, path in checks:
            try:
                r = await client.get(base + path)
                item: dict[str, Any] = {"name": name, "path": path, "status": r.status_code}
                if r.is_success:
                    try:
                        payload = r.json()
                    except Exception:
                        payload = r.text[:2000]
                    item["data"] = payload
                    out["reachable"] = True
                    if name == "models":
                        out["model"] = payload
                else:
                    item["error"] = r.text[:500]
                out["checks"].append(item)
            except Exception as e:
                out["checks"].append({"name": name, "path": path, "error": str(e)})
    return out

def _list_files(rel: str, max_entries: int = 200) -> list[dict[str, Any]]:
    base = safe_path(rel)
    if not base.exists() or not base.is_dir():
        return []
    items = []
    try:
        for p in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                resolved = p.resolve()
                resolved.relative_to(safe_root())
            except Exception:
                continue
            items.append({
                "name": p.name,
                "path": str(p.relative_to(safe_root())).replace("\\", "/"),
                "is_dir": p.is_dir(),
                "size": p.stat().st_size if p.is_file() else None,
            })
            if len(items) >= max_entries:
                break
    except PermissionError:
        return items
    return items

@app.get("/api/files")
async def list_files(path: str = "."):
    try:
        p = safe_path(path)
        if p.is_file():
            return {"path": path, "is_dir": False, "content": p.read_text(encoding="utf-8", errors="replace")[:500_000]}
        return {"path": path, "is_dir": True, "entries": _list_files(path)}
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/approve/{approval_id}")
async def approve(approval_id: str, inp: ApprovalInput):
    fut = pending_approvals.get(approval_id)
    if fut is None or fut.done():
        raise HTTPException(404, "Approval request no longer exists.")
    fut.set_result(bool(inp.approved))
    return {"ok": True}

@app.post("/api/chat")
async def chat(inp: ChatInput):
    s = load_settings()
    if not inp.user_message.strip():
        raise HTTPException(400, "Message cannot be empty.")
    return StreamingResponse(
        agent_stream(inp, s),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

TOOLS = [
    {"type":"function","function":{"name":"list_files","description":"List files and directories under the selected workspace. Use a relative path; '.' means workspace root.","parameters":{"type":"object","properties":{"path":{"type":"string","default":"."}},"required":[]}}},
    {"type":"function","function":{"name":"read_file","description":"Read a UTF-8 text file inside the workspace.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
    {"type":"function","function":{"name":"write_file","description":"Create or overwrite a UTF-8 text file inside the workspace. Existing file content is replaced.","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
    {"type":"function","function":{"name":"edit_file","description":"Replace an exact unique text snippet in a file. old_text must occur exactly once.","parameters":{"type":"object","properties":{"path":{"type":"string"},"old_text":{"type":"string"},"new_text":{"type":"string"}},"required":["path","old_text","new_text"]}}},
    {"type":"function","function":{"name":"search_files","description":"Search text files in the workspace for a literal or regex pattern.","parameters":{"type":"object","properties":{"pattern":{"type":"string"},"path":{"type":"string","default":"."},"regex":{"type":"boolean","default":False}},"required":["pattern"]}}},
    {"type":"function","function":{"name":"run_command","description":"Run a shell command with the workspace as current directory. Commands may modify files or the system.","parameters":{"type":"object","properties":{"command":{"type":"string"},"timeout":{"type":"integer","default":30}},"required":["command"]}}},
    {"type":"function","function":{"name":"git_status","description":"Return git status for the workspace.","parameters":{"type":"object","properties":{},"required":[]}}},
    {"type":"function","function":{"name":"git_diff","description":"Return git diff for the workspace, including untracked files only if git itself reports them (usually it does not).","parameters":{"type":"object","properties":{},"required":[]}}},
]

SYSTEM_PROMPT = """You are KoboldCode, a coding agent working in the user's selected workspace.
Use tools to inspect the project before making assumptions. Make focused changes and verify them with tests or relevant commands when practical.
Workspace-relative paths only. Never claim a change or command succeeded unless the tool result confirms it.
For edits, prefer edit_file for narrow changes and write_file for new or complete files. Explain the final result concisely, listing changed files and verification performed.
You may ask the user for clarification in normal text when required. Do not output hidden chain-of-thought; provide concise conclusions and summaries.
The run_command tool may require explicit user approval. Do not attempt to bypass approval by encoding commands in another tool.
"""

async def agent_stream(inp: ChatInput, settings: dict[str, Any]) -> AsyncIterator[str]:
    base = settings["base_url"]
    headers = api_headers(settings)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    # Keep only valid prior message objects; caller controls its current in-memory transcript.
    for m in inp.messages[-40:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool") and "content" in m:
            messages.append({k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id", "name") if k in m})
    messages.append({"role": "user", "content": inp.user_message})
    yield sse("status", {"text": "Connecting to KoboldCpp"})
    rounds = int(settings.get("max_tool_rounds", 12))
    repeated_failures: dict[str, int] = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0), headers=headers) as client:
            for turn in range(rounds):
                body = {
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "stream": True,
                    "temperature": float(settings.get("temperature", 0.2)),
                    "max_tokens": int(settings.get("max_tokens", 4096)),
                }
                model = str(settings.get("model", "")).strip()
                if model:
                    body["model"] = model
                yield sse("status", {"text": f"Thinking · turn {turn + 1}"})
                try:
                    async with client.stream("POST", base + "/v1/chat/completions", json=body) as response:
                        if response.status_code >= 400:
                            detail = (await response.aread()).decode("utf-8", "replace")[:3000]
                            yield sse("error", {"message": f"KoboldCpp returned HTTP {response.status_code}: {detail}"})
                            return
                        content_parts: list[str] = []
                        tool_acc: dict[int, dict[str, Any]] = {}
                        finish_reason = None
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            choices = chunk.get("choices") or []
                            if not choices:
                                if "error" in chunk:
                                    yield sse("error", {"message": str(chunk["error"])})
                                    return
                                continue
                            choice = choices[0]
                            finish_reason = choice.get("finish_reason") or finish_reason
                            delta = choice.get("delta") or {}
                            text_piece = delta.get("content")
                            if isinstance(text_piece, str) and text_piece:
                                content_parts.append(text_piece)
                                yield sse("token", {"text": text_piece})
                            calls = delta.get("tool_calls") or []
                            for tc in calls:
                                idx = tc.get("index", 0)
                                acc = tool_acc.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if tc.get("id"):
                                    acc["id"] = tc["id"]
                                if tc.get("type"):
                                    acc["type"] = tc["type"]
                                fn = tc.get("function") or {}
                                if fn.get("name"):
                                    acc["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    acc["function"]["arguments"] += fn["arguments"]
                        content = "".join(content_parts)
                        calls = [tool_acc[i] for i in sorted(tool_acc)]
                        # Some OpenAI-compatible servers omit tool-call IDs. Normalize them
                        # before appending the assistant message so every tool result matches.
                        for call in calls:
                            if not call.get("id"):
                                call["id"] = f"call_{uuid.uuid4().hex}"
                except httpx.RequestError as e:
                    yield sse("error", {"message": f"Could not connect to KoboldCpp at {base}: {e}"})
                    return

                assistant_message: dict[str, Any] = {"role": "assistant", "content": content or None}
                if calls:
                    assistant_message["tool_calls"] = calls
                messages.append(assistant_message)

                if not calls:
                    if finish_reason == "tool_calls":
                        yield sse("error", {"message": "KoboldCpp indicated a tool call, but no complete tool call was received. Check the model chat template and KoboldCpp tool-calling settings."})
                        return
                    yield sse("done", {"message": content, "messages": messages[-40:]})
                    return

                for call in calls:
                    call_id = call["id"]
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "").strip()
                    raw_args = fn.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except Exception:
                        result = {"error": "Tool arguments were not valid JSON.", "arguments": str(raw_args)[:1000]}
                        messages.append({"role": "tool", "tool_call_id": call_id, "name": name or "unknown", "content": json.dumps(result)})
                        yield sse("tool_result", {"name": name, "result": result})
                        continue

                    if name == "run_command" and settings.get("require_shell_approval", True):
                        approval_id = f"ap_{int(time.time()*1000)}_{len(pending_approvals)}"
                        fut = asyncio.get_running_loop().create_future()
                        pending_approvals[approval_id] = fut
                        yield sse("approval_required", {"id": approval_id, "tool": name, "args": args, "message": "Allow this shell command to run?"})
                        try:
                            approved = await asyncio.wait_for(fut, timeout=300)
                        except asyncio.TimeoutError:
                            approved = False
                        finally:
                            # Also clean up when a browser disconnect cancels the stream.
                            pending_approvals.pop(approval_id, None)
                        if not approved:
                            result = {"error": "User denied or approval timed out. Do not retry the same command unless the user asks."}
                            messages.append({"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)})
                            yield sse("tool_result", {"name": name, "result": result})
                            continue
                    yield sse("tool_start", {"name": name, "args": args})
                    try:
                        result = await asyncio.to_thread(execute_tool, name, args)
                    except Exception as e:
                        result = {"error": f"{type(e).__name__}: {e}"}
                    messages.append({"role": "tool", "tool_call_id": call_id, "name": name or "unknown", "content": _serialize_tool_result(result)})
                    yield sse("tool_result", {"name": name, "result": result})
                    if _is_repeated_tool_failure(name, args, result, repeated_failures):
                        message = (
                            f"Stopped after the same {name} call failed repeatedly. "
                            f"The tool error was: {result['error']} Try a different approach "
                            "instead of repeating the same operation."
                        )
                        yield sse("error", {"message": message})
                        yield sse("done", {"message": message, "messages": messages[-40:]})
                        return
            yield sse("done", {"message": "Stopped after reaching the configured tool-call limit.", "messages": messages[-40:]})
    except Exception as e:
        yield sse("error", {"message": f"{type(e).__name__}: {e}"})

def _is_repeated_tool_failure(
    name: str,
    args: Any,
    result: Any,
    seen: dict[str, int],
    threshold: int = 2,
) -> bool:
    """Detect the same failing tool call repeating within one agent task."""
    if not isinstance(result, dict) or not result.get("error"):
        return False
    fingerprint = json.dumps(
        {"name": name, "args": args},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    seen[fingerprint] = seen.get(fingerprint, 0) + 1
    return seen[fingerprint] >= threshold


def _serialize_tool_result(result: dict[str, Any], limit: int = 20000) -> str:
    """Keep tool messages valid JSON even when the result is too large for the model context."""
    serialized = json.dumps(result, ensure_ascii=False)
    if len(serialized) <= limit:
        return serialized
    preview_limit = max(0, limit - 100)
    return json.dumps({
        "truncated": True,
        "original_characters": len(serialized),
        "preview": serialized[:preview_limit],
    }, ensure_ascii=False)


def execute_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return {"error": "Arguments must be an object."}
    if name == "list_files":
        return {"path": args.get("path", "."), "entries": _list_files(str(args.get("path", ".")), 300)}
    if name == "read_file":
        p = safe_path(str(args["path"]))
        if not p.is_file():
            return {"error": "File does not exist or is not a file."}
        if p.stat().st_size > 1_000_000:
            return {"error": "File exceeds the 1 MB read limit."}
        return {"path": str(p.relative_to(safe_root())).replace("\\", "/"), "content": p.read_text(encoding="utf-8", errors="replace")}
    if name == "write_file":
        p = safe_path(str(args["path"]))
        content = str(args["content"])
        if len(content.encode("utf-8")) > 1_000_000:
            return {"error": "File exceeds the 1 MB write limit."}
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(p.relative_to(safe_root())).replace("\\", "/"), "bytes": len(content.encode("utf-8")), "overwrote_existing_file": existed}
    if name == "edit_file":
        p = safe_path(str(args["path"]))
        if not p.is_file():
            return {"error": "File does not exist."}
        old, new = str(args["old_text"]), str(args["new_text"])
        content = p.read_text(encoding="utf-8")
        count = content.count(old)
        if count != 1:
            return {"error": f"Expected old_text to occur exactly once, found {count}. No change made."}
        updated = content.replace(old, new, 1)
        p.write_text(updated, encoding="utf-8")
        return {"ok": True, "path": str(p.relative_to(safe_root())).replace("\\", "/")}
    if name == "search_files":
        pattern = str(args["pattern"])
        base = safe_path(str(args.get("path", ".")))
        rx = re.compile(pattern if args.get("regex") else re.escape(pattern), re.IGNORECASE)
        matches = []
        candidates = [base] if base.is_file() else base.rglob("*")
        for p in candidates:
            if not p.is_file() or p.stat().st_size > 500_000 or len(matches) >= 100:
                continue
            try:
                p.resolve().relative_to(safe_root())
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except (ValueError, OSError):
                continue
            for n, line in enumerate(lines, 1):
                if rx.search(line):
                    matches.append({"path": str(p.relative_to(safe_root())).replace("\\", "/"), "line": n, "text": line[:500]})
                    if len(matches) >= 100:
                        break
        return {"matches": matches, "truncated": len(matches) >= 100}
    if name == "run_command":
        command = str(args["command"])
        timeout = max(1, min(int(args.get("timeout", 30)), 120))
        completed = subprocess.run(command, shell=True, cwd=str(safe_root()), capture_output=True, text=True, timeout=timeout, errors="replace")
        return {"returncode": completed.returncode, "stdout": completed.stdout[-12000:], "stderr": completed.stderr[-12000:], "cwd": str(safe_root())}
    if name in ("git_status", "git_diff"):
        cmd = ["git", "status", "--short", "--branch"] if name == "git_status" else ["git", "diff", "--"]
        completed = subprocess.run(cmd, cwd=str(safe_root()), capture_output=True, text=True, timeout=20, errors="replace")
        return {"returncode": completed.returncode, "stdout": completed.stdout[-20000:], "stderr": completed.stderr[-4000:]}
    return {"error": f"Unknown tool: {name}"}
