# Sora Harness

[简体中文](README.md) | English

**Sora Harness** is a desktop Agent Harness built with Python and PySide6.

It provides shared infrastructure for multi-protocol model requests, streaming file and command tools, context caching and compaction, and multiple named subagents. Plugins and Skills build on these interfaces to support workflows such as multi-engine game translation and local hybrid retrieval. The design emphasizes clear engineering boundaries, control over local data, stable request prefixes, and bounded context growth in long conversations.

## Feature Overview

### 1. Prelude Conversations and Layered Prompt Presets

For tasks such as style-sensitive translation, code review, or fixed-format output, a few example exchanges can be more useful than a system prompt alone. Sora Harness supports **Prelude conversations** in both its data model and interface:

- **Visual editing on the welcome page**: Add and edit user/assistant example bubbles before starting a conversation. Editing only saves a local draft and does not make API requests. The Prelude is fixed in the conversation configuration when the first real message is sent.
- **Protected bubbles in the conversation view**: Prelude content appears at the beginning of the chat for reference. It does not create extra runtime turns, get duplicated in later steps, or become part of the history selected for compaction.
- **Layered prompts and switchable presets**: Prompts are assembled from `system` (identity, style, and format), `application` (environment, task strategy, file protection, and tool guidance; work mode only), `context` (user profile, preferences, project information, and task material), and `prelude`. Save multiple named presets. Global changes apply to new conversations by default; existing conversations can explicitly switch presets through the upper-right menu at a safe boundary.

### 2. Edit Responses, Rewind History, and Create Branches

An **append-only JSONL log + effective-history projection** allows conversations to be revised without discarding the original audit record:

- **Edit a model response in place**: Edit and save a turn's final public assistant response as plain text. The edited text replaces that response in effective history, truncates the later conversation suffix, and clears the native `ReplayState` tied to the old text. Future requests continue from the edited response. Original responses, reasoning, and actual token usage remain in the underlying log.
- **Rewind from a user message**: Before a conversation has been compacted, rewind from a user bubble, including the precise boundary of a mid-run steering message. Completed model responses and paired tool batches before that boundary are retained. The selected message and its images return to the composer for editing; historical tools are not rerun automatically.
- **Create an independent conversation branch**: After confirmation, branch from a user-message boundary in the same project. The title receives a `（分支）` suffix. The branch inherits effective history before the selected message, the model target at that point, and the applicable summary. It does not copy pending queues or running processes. After successful compaction, rewind is hidden throughout the conversation, while viewing, copying, and branching remain available. A branch from a pre-compaction message uses the effective history at that earlier boundary. Neither branching nor rewinding rolls back project files.
- **Regenerate an entire turn**: Return to the turn's starting point and run the model/tool loop again. If earlier tools changed files or ran commands, the new request receives a factual reminder so the model does not assume those effects were rolled back.

### 3. Plugin Architecture and Translation Suite

Sora Harness separates its general platform from domain-specific workflows. External extensions (`extensions/*/plugin.json` or local directory/ZIP packages) and built-in plugins share registration boundaries. `plugin_api.core` and `plugin_api.ui` are separate so CLI and background execution do not load Qt. Plugins can contribute actions, model tools, Skill directories (`skill_roots`), settings pages, and sidebar or standalone views through `PageHost`.

The current translation integration combines a **game translation plugin and a cross-host Game Translation Skill**:

- **Multi-engine detection and project preparation**: Translation Core supports adapted resource formats in Unity, Godot, Ren'Py, WOLF RPG Editor, RPG Maker, and related supported engines. It detects games, extracts text, and prepares a working project through `prepare`. External engine processes use process-tree management and configurable timeouts, with a default of 900 seconds.
- **Standard packages and Agent-written temporary adapters**: The Agent uses the background `translation` command entry or an authorized command tool to export packages, validate and apply UTF-8 JSON translation arrays, and build a separate game copy. The Python API exposes `make_package`, `apply_results`, and `build`; the CLI uses `package`, `apply`, and `build`. For an unsupported layout or extraction gap, an authorized Agent can create a self-contained `WORK/adapter.py` without adding game-specific branches to the general Core.
- **`/翻译` side-by-side reader**: Send `/翻译` from the composer to open a read-only view. The model can use `show_translation` to select a valid working project, update an open view, or provide a “View translation” entry without forcing a window open. The reader shows full Scene content, source on the left and saved translations on the right, with `Speaker  [dialogue]` formatting, aligned row heights, shared vertical scrolling, viewport-based layout batches, and cross-item text selection within a column.
- **Per-conversation state and safe refresh**: The reader displays project data saved through the Core; unsubmitted chat drafts do not become formal translations. Relevant tool results trigger an asynchronous reread while preserving the reading anchor. Each conversation's selected project and Scene are stored independently in `views.json` and restored when that conversation's translation view is reopened. Ordinary chat navigation does not forcibly rebind an independent window.

## Shared Platform Capabilities

| Area | Implementation |
| --- | --- |
| **Four native model protocols** | OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, and Google Gemini `generateContent`. Supports streaming display of model-provided reasoning, collapsible reasoning details, native signed/encrypted continuation blocks, effort controls, and continuation across models/providers. History is projected into the target protocol with paired tool results; native signatures remain subject to their original model/connection identity. |
| **Streaming file and command tools** | Core tools are scheduled as `read`, `exclusive`, or `control`. Consecutive independent read calls run in batches of up to four; registered writes and commands are serialized through a cross-process mutation lock. `read_text_file` reads 1–32 file ranges with a shared 20,000-character output budget and no total file-size cap. `search_text` supports `rg` and a Python scanner using 64 KiB chunks, with `files_with_matches`, `count`, and `summary` modes. `edit_text_file` requires a unique exact match and preserves the BOM and unaffected line endings. `run_command` uses Windows Job Objects to manage PowerShell/Git Bash process trees; its default `compact` output folds consecutive repeated lines. Full output is available through `read_tool_record`. |
| **Data protection** | File tools do not expose deletion. Creation and moves refuse to overwrite existing targets, and editing cannot turn a nonempty file into an empty or whitespace-only file. Use `create_directory` and conflict-safe, same-device `move_file` for authorized archival moves. Model keys saved through settings use Windows Credential Manager (`wincred`), with credential references in model configuration; compatible CLI connections can read a named environment variable. RAG Embedding keys and directly entered MCP authentication values may still be stored in local configuration, so the entire data directory must be excluded from publication. Commands and trusted Python plugins are not an operating-system sandbox. |
| **Steering, queues, and questions** | Add steering messages during a run; they are injected after the current model step and associated tool batch settle. FIFO queues start subsequent independent turns. Interrupted work can continue from saved results without rerunning completed calls. The nonblocking `request_user_input` tool docks a rounded question card above the composer, supporting options and free-text answers. |
| **Prefix caching and context compaction** | Keeps system rules, tool schemas, and the environment baseline stable; environment changes are appended persistently. Actual prefix-cache hits depend on the provider, cache retention, and request content—no fixed rate is guaranteed. Supports `/compact` and threshold-based compaction using the original prefix and current model. The post-compaction input target is approximately 10,000 tokens, retaining up to two recent complete turns within budget and aiming for a roughly 2,000-token summary. Images in covered history are removed from active context. Usage shows the current turn's actual cache-read ratio and the latest successful request's output tokens divided by request duration, including initial waiting and excluding tool execution and failed retries. Missing data is not estimated. After compaction, old usage is cleared until a normal request returns new usage. |
| **Multiple named subagents** | Configure profiles with independent connections, models, effort, prompts, timeouts, and step budgets. Background subagents run in separate processes with independent histories, sharing the parent workspace lease and mutation lock. Six tools manage them: `subagent`, `send_subagent`, `list_subagents`, `wait_subagents`, `get_subagent_result`, and `cancel_subagent`. Completion notifications can wake an idle parent with a bounded wakeup count. A `/子智能体` side panel displays task details. |
| **Skills, RAG, and MCP** | **Skills**: Discovers project `.agents/skills`, user directories, custom roots, and plugin contributions through background metadata scans. Select a Skill from `/` or let the model load it through `load_skill` / `read_skill_resource`.<br>**Local RAG plugin**: SQLite-backed inverted-index BM25 retrieval plus a separately configured Embedding service (OpenAI-compatible, Ollama, or Gemini), with RRF hybrid ranking, project/conversation scopes, deduplication, and version-aware source reads. TXT/Markdown import is supported; PDF/DOCX import requires a configured document-conversion MCP service. Semantic/hybrid queries also call the Embedding service and may incur usage charges.<br>**MCP 2.x**: Supports `stdio` and Streamable HTTP, reuses connections within a run, and stores oversized results for `read_tool_record` continuation. |
| **Request recording and diagnostics** | Enable “Record the next run's requests” on demand, or pass CLI `--record-requests`. At the HTTPX send boundary, records the transformed request body bytes in `body.json`, attempts in `attempts.jsonl`, and metadata in `manifest.json`. Authentication headers are excluded/redacted, and recording failures are kept separate from model retry behavior. |

## Repository Structure

```text
.
├── desktop/                         # Harness desktop source
│   ├── cli.py / gui.py / bootstrap.py  # CLI/GUI entry points and runtime assembly
│   ├── plugin_catalog.py            # Lazy plugin registration; separate core and UI entry points
│   ├── configuration/               # Model catalogs, adapters.json, and connection profiles
│   ├── credentials/                 # Windows credential access
│   ├── environment/                 # Python/Git/rg/Shell discovery and environment snapshots
│   ├── llm/                         # Model protocol layer
│   │   ├── protocols.py             # Protocol catalog and native continuation checks
│   │   ├── history_projection.py    # Cross-model history projection and signature checks
│   │   ├── usage.py / reasoning.py  # Token normalization and effort mapping
│   │   └── providers/               # Chat Completions / Responses / Messages / Gemini
│   ├── prompts/                     # default.json, summary.txt, and prefix assembly
│   ├── tools/                       # Contracts, registry, receipts, and built-in implementations
│   │   ├── filesystem/              # Streaming I/O, searches, safe edits, and moves
│   │   ├── command/                 # Shell configuration and Job Object process management
│   │   └── questions.py             # Nonblocking request_user_input
│   ├── skills/                      # Skill metadata, discovery, and read-only resource access
│   ├── runtime/
│   │   ├── agent/                   # AgentLoop, budgets, and checkpoints
│   │   ├── conversations/           # JSONL storage, revisions, branches, steering, and queues
│   │   ├── context/                 # Token estimation and prefix-reusing compaction
│   │   └── subagents/               # Subprocess Runner and status communication
│   ├── application/                 # Conversation services, presets, subagents, and task gates
│   ├── ui/
│   │   ├── shell/ & common/         # Theme, surfaces, interruptible animations, shared controls
│   │   ├── workspace/ & sidebar/    # PageHost, conversation navigation, and archives
│   │   ├── chat/                    # Virtualized messages, process details, composer, and / menu
│   │   └── settings/                # Models, prompts, Skills, MCP, subagents, and archives
│   └── plugins/                     # Translation, subagent, and model_trial integrations
├── game_translation/                # Translation Core: extraction, packages, validation, and builds
├── run.py                           # Translation Core CLI
├── process_support/                 # Shared process-tree support
├── skills/game-translation/          # Portable translation Skill and references
├── tools/                           # Optional engine helpers and guides; no downloaded binaries
├── desktop-extensions/
│   └── rag/                         # BM25/vector retrieval and auxiliary views
└── desktop-release/                 # PyInstaller and Inno Setup build entry points
```

## Quick Start

### 1. Prepare the Environment

The desktop application targets Windows; Windows 10 version 1809 or later and Python 3.11+ are recommended.

From the repository root:

```powershell
python -m pip install -r desktop/requirements.txt
```

A virtual environment is recommended. Main runtime dependencies are `PySide6` for the GUI, `httpx` for networking/SSE, `Pygments` for syntax highlighting, `PyYAML` for Skill metadata, and the official `mcp` 2.x SDK.

Optional Translation Core engine dependencies are separate from these desktop requirements. Follow the relevant guides under `tools/`; installing the desktop dependencies does not mean every game's resources can be extracted or rebuilt.

### 2. Start the Desktop GUI

```powershell
python -m desktop.gui
```

Source runs store data under `desktop/local/` by default. To use a separate data directory:

```powershell
python -m desktop.gui --data-dir "D:/HarnessData"
```

Installed builds default to `%LOCALAPPDATA%/DesktopHarness`.

On first launch:

1. Open **Settings → Models**, add an API connection, enter a full endpoint or base URL, discover or add models, and select a default. Model keys entered here are stored in Windows Credential Manager.
2. Start an ordinary chat in **Dialogue** mode or bind a local directory for **Work** mode. The welcome page also lets you edit Prelude example bubbles.
3. Type `/` to open the command menu. Selecting an item inserts it into the composer; sending executes it. Available entries include `/compact`, `/翻译`, `/子智能体`, `/资料库`, and discovered commands in the Skills group.

To install the separate RAG plugin for a source run:

```powershell
pwsh -NoProfile -File desktop-extensions/install-rag.ps1
```

Restart the application and configure the plugin on its settings page. This command deploys plugin code only; it does not import a corpus or enable Embedding.

### 3. Use the CLI

The CLI supports single-request trials, tool loops, and persistent conversations.

```powershell
# Save a model connection in the GUI first. my_project must be an existing directory.
# Adjust --settings-file if you use a different data directory.
# Create a persistent conversation; use --conversation <UUID> to continue with its original connection.
python -m desktop.cli --settings-file desktop/local/settings.json --mode conversation --workspace ./my_project --prompt "Inspect the directory structure and summarize the core modules"

# Record the complete API request bodies for this run.
# Default location: desktop/local/request-records/
python -m desktop.cli --settings-file desktop/local/settings.json --mode conversation --conversation <UUID> --prompt "Continue" --record-requests

# Compact an existing conversation without sending a new task.
python -m desktop.cli --settings-file desktop/local/settings.json --mode conversation --conversation <UUID> --compact
```

### 4. Build a Standalone Distribution and Installer

Run this from the repository root, or double-click `desktop-release/build.cmd`:

```powershell
pwsh -NoProfile -File desktop-release/build.ps1
```

The first build may need network access to prepare dependencies and the compiler. See the [build guide](desktop-release/README.md) for all options.

The build uses **PyInstaller** with two entry points—the GUI and an independent background Worker—and **Inno Setup** for the installer:

- Each build creates a separate artifact directory and a `DesktopHarness-Setup-0.1.0.exe` installer.
- Personal data under `local/`, including conversations, prompt presets, plugin data, and credential references, is not bundled.
- The installer does not overwrite user data. To reuse a specific configuration, select the same `--data-dir`.
- Source changes require a new build. Existing EXEs do not automatically load updated source files.

## Extensions and Usage Boundaries

See the [extension API guide](desktop/docs/EXTENSION_API.md), [minimal extension examples](desktop-extensions/examples/README.md), [translation Skill](skills/game-translation/SKILL.md), and [RAG guide](desktop-extensions/rag/README.md).

The repository publishes source, public default prompts, plugins, and build scripts. It does not include personal model connections, keys, conversations, request records, personal presets, corpus indexes, real games, or translation work directories. Complete request recordings can contain user text, tool results, and encoded images: removing authentication headers does not make the request body nonsensitive. Do not upload such records with the repository.

Engine support is limited to implemented resource formats and actual diagnostics. Proprietary resources, encryption, font requirements, and loading behavior can still require project-specific adapters. Protocol implementations, static source checks, and individual usage results do not establish compatibility with every provider or game.

## License

Project-owned source is released under the [MIT License](LICENSE). Retained third-party notices include the [RPGMTL license](game_translation/formats/RPGMTL-LICENSE.txt). PySide6/Qt, the MCP SDK, other runtime dependencies, and optional external engine tools retain their respective licenses. Binary distributions must also satisfy the licensing and distribution requirements of the dependencies actually bundled.

## Author

Project author: [S0ra233](https://github.com/S0ra233).
