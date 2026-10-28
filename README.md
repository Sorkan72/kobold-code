# KoboldCode

KoboldCode is a local-first coding-agent starter designed for KoboldCpp, including 1.123.1. It provides a browser IDE shell, a tool-calling agent loop, workspace file operations, shell execution with approval, and backend diagnostics.

## Requirements

- Python 3.10+
- KoboldCpp running locally (default `http://127.0.0.1:5001`)
- A chat/instruct model with tool-call support. For KoboldCpp, check the model's chat template and consider enabling `--jinja` and `--jinja_tools` (`--jinjatools`).

## Run

Windows: double-click `run_windows.bat`, then open `http://127.0.0.1:8787`.

macOS/Linux:
```sh
chmod +x run_unix.sh
./run_unix.sh
```
Then open `http://127.0.0.1:8787`.

## First use

1. Open Settings and set the KoboldCpp base URL (default `http://127.0.0.1:5001`) and optional API password.
2. Select a workspace directory. KoboldCode confines its file tools to that directory.
3. Use Diagnostics to inspect the connected backend and model capabilities.
4. Give the agent a coding task. Shell commands require approval by default; writes/edits are recorded in the activity panel.

## Included in this MVP

- OpenAI-compatible `GET /v1/models` and `POST /v1/chat/completions` integration.
- Tool-call loop with `read_file`, `write_file`, `edit_file`, `list_files`, `search_files`, `run_command`, `git_status`, and `git_diff`.
- Streaming assistant text to the browser using server-sent events from KoboldCode.
- Shell command approval, project-root path confinement, bounded tool output, and file-size limits.
- KoboldCpp diagnostics via `/api/extra/version`, `/api/extra/true_max_context_length`, `/api/extra/perf`, `/props`, and `/v1/models`.
- Local conversation state for the current browser session.

## Important limits

This is a working starter, not yet a feature-complete Claude Code/Codex clone. It does not yet include durable multi-project history, robust patch-based edits, Git commit/revert UI, image/audio workflows, MCP client management, background tasks, or sandboxed shell execution. Shell approval is not a security sandbox: approved commands run as your user. Only use trusted models and trusted workspaces.

KoboldCpp's API compatibility can vary by build and chat template. Diagnostics reports what the server exposes; tool calling must be verified with the specific model/template you use. API docs on your running instance are usually available at `http://127.0.0.1:5001/api`.

## Configuration

The app binds to `127.0.0.1:8787` by default. It stores settings in `koboldcode_settings.json` in the app directory. Do not expose the app port to your LAN without adding authentication and reviewing the security model.
