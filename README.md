# CHAPTERS Case Study — Humdaan Syed

**Chapters Case Study Assistant** is an Open WebUI `v0.13.3` assistant using Ollama `qwen3:4b`. It answers questions from a pinned snapshot of `README.md` and `src/oikb/history.py` in the public [open-webui/oikb](https://github.com/open-webui/oikb) repository.

| Deliverable | Contents |
| --- | --- |
| [questions_results.pdf](questions_results.pdf) | Two answerable questions and one unanswerable question, each with expected evidence, the observed answer and a verdict |
| [Operational Design.pdf](<Operational Design.pdf>) | Freshness, read-only permissions, failed-refresh detection, OpCo separation, limitations and next improvement |
| [config/](config) and [scripts/setup_instance.py](scripts/setup_instance.py) | Pinned assistant, document and source configuration, plus the automated setup script |
| This README | How to reproduce the instance |

**What was and was not tested**

- **Tested:** the PoC was built manually in Open WebUI on macOS, and the answers in `questions_results.pdf` come from that instance. The setup script was then run on a fresh instance, which matched the PoC in document settings, assistant configuration, knowledge-base layout, file contents and embeddings (41 chunks).
- **Not reliable yet:** answers vary between runs. When the questions were asked again on the instance built by the script, it wrongly reported missing evidence for Q1 initially. The model searched file names and grepped for `SyncHistory.log()`, which never appears in the source, instead of reading the file. Q1 failed twice and passed on the third attempt; see the limitation in `Operational Design.pdf`.
- **Not tested:** Windows setup, and the operational controls (scheduled refresh, alerting, OpCo groups), which are proposals only.
- **Scope:** only `README.md` and `src/oikb/history.py` at commit `29b0e45` are indexed, not the full repository.

**Platform status:** This PoC was set up on **macOS only**, using Colima and native Ollama. [Open WebUI also supports Windows and Linux](https://docs.openwebui.com/getting-started/quick-start/). Windows instructions are provided below but have not been verified for this submission.

## Prerequisites

| Platform | Container runtime | Terminal |
| --- | --- | --- |
| macOS | Colima with Docker CLI, or Docker Desktop | Terminal |
| Windows | Docker Desktop configured for Linux containers | PowerShell |

Install native Ollama. Python 3.9+ is required for the setup script; no extra Python packages are needed. Initial image, model, embedding, and source downloads require internet access.

## 1. Start Ollama and Open WebUI

Start your container runtime: `colima start` on macOS with Colima, or launch Docker Desktop. Start the Ollama application. Alternatively, run `ollama serve` in a separate terminal if it is not already running.

Download the model on either platform:

```shell
ollama pull qwen3:4b
```

Create a new Open WebUI instance on port `3005`. The port is chosen to avoid an Open WebUI instance you may already run on `3000`; if `3005` is taken, pick another unused port and use it everywhere below. This single-line command works in macOS Terminal and Windows PowerShell:

```shell
docker run -d --name oikb-case-study-humdaan --restart unless-stopped -p 127.0.0.1:3005:8080 -e OLLAMA_BASE_URL=http://host.docker.internal:11434 -v oikb-case-study-humdaan-data:/app/backend/data ghcr.io/open-webui/open-webui:v0.13.3
```

Open [http://localhost:3005](http://localhost:3005) and create the local administrator account. Under **Admin Settings → Connections**, confirm the Ollama URL is `http://host.docker.internal:11434` and that `qwen3:4b` is available.

If Open WebUI cannot reach Ollama, check the host networking and [Ollama listening-address settings](https://docs.ollama.com/faq#how-do-i-configure-ollama-server). On macOS, the Ollama application's environment is configured through `launchctl`; on Windows, set user environment variables and restart Ollama. For a connection requiring `OLLAMA_HOST=0.0.0.0:11434`, restrict access with the host firewall because that address listens beyond localhost.

Choose **2A** for automated setup or **2B** for the manual fallback.

## 2A. Script setup

Use a dedicated empty instance. The script changes global document settings and refuses an existing target assistant or a nonempty knowledge/file store.

1. Under **Admin Settings → Authentication**, enable API keys and save.
2. If API endpoint restrictions are enabled, allow:

   ```text
   /api/models,/api/v1/models,/api/v1/knowledge,/api/v1/files,/api/v1/retrieval
   ```

3. Under personal **Settings → Account**, create or copy your admin API key. Do not commit it to Git.
4. From this repository's root folder, run the commands for your platform.

**macOS:**

```bash
python3 scripts/setup_instance.py --check-config
python3 scripts/setup_instance.py --apply --base-url http://localhost:3005
```

**Windows PowerShell:**

```powershell
py -3 scripts/setup_instance.py --check-config
py -3 scripts/setup_instance.py --apply --base-url http://localhost:3005
```

If the Windows `py` launcher is unavailable, use `python` after confirming it runs Python 3.9+.

Enter the API key at the hidden prompt, or set it in the `OPENWEBUI_API_KEY` environment variable. The script reads:

- [config/assistant.json](config/assistant.json): model, system prompt, context size, and tools.
- [config/documents.json](config/documents.json): embedding, chunking, and retrieval settings.
- [config/sources.json](config/sources.json): repository, pinned commit, selected paths, and upload folder.

It downloads the two source files and creates a private knowledge base. It uploads the files into a `knowledge/` folder, keeping the repository paths (`knowledge/README.md`, `knowledge/src/oikb/history.py`), the same as a folder upload in the UI. It then checks retrieval and creates the assistant using the new knowledge-base ID. If it fails, inspect `.local/`: partial resources and settings are kept, and nothing is rolled back or overwritten automatically. Inspect them before retrying or finishing the setup manually.

**Script status:** Verified on macOS against a fresh Open WebUI `v0.13.3` instance with Ollama `qwen3:4b`. Both files were indexed in the folder layout, both retrieval checks passed, and the assistant was created and read back. Not yet run on Windows.

## 2B. Manual fallback — same browser steps on macOS and Windows

This route does not require API keys or the setup script.

### Download the source files

Save these raw files under their original filenames. Both links point to commit `29b0e453812c1b973cfad45420140809db4a98f5`:

- [README.md](https://raw.githubusercontent.com/open-webui/oikb/29b0e453812c1b973cfad45420140809db4a98f5/README.md)
- [history.py](https://raw.githubusercontent.com/open-webui/oikb/29b0e453812c1b973cfad45420140809db4a98f5/src/oikb/history.py)

Use the upstream README, **not this submission README**. The original code path is `src/oikb/history.py`. Save raw text rather than a GitHub HTML page.

### Configure processing and upload

Under **Admin Settings → Documents**, set these values before uploading:

| Setting | Value |
| --- | --- |
| Content extraction | Default |
| Embedding engine | Default / SentenceTransformers |
| Embedding model | `sentence-transformers/all-MiniLM-L6-v2` |
| Text splitter | Token (Transformers) |
| Tokenizer model | `sentence-transformers/all-MiniLM-L6-v2` |
| Chunk size / overlap / minimum size | `200` / `40` / `0` |
| Top K | `10` |
| Hybrid search and Markdown header splitting | Off |
| Bypass embedding/retrieval and full-context mode | Off |

Match any additional settings with [config/documents.json](config/documents.json), including the saved RAG template. Then:

1. Under **Workspace → Knowledge**, create a private knowledge base named `oikb-case-study`.
2. Copy the description from [config/sources.json](config/sources.json), recording the original file paths and commit.
3. Put the downloaded files in a local folder named `knowledge`, keeping their repository paths (`knowledge/README.md`, `knowledge/src/oikb/history.py`), and upload that folder to the knowledge base.
4. Wait for processing to finish and check that both files contain readable text. If you change embedding or chunking settings afterward, reprocess the files.

### Create the assistant

Under **Workspace → Models**, create **Chapters Case Study Assistant** with ID `chapters-case-study-assistant`:

- Base model: `qwen3:4b`; context size: `8192`; function calling: **Native**.
- Attach `oikb-case-study` and keep the assistant private.
- Enable **Citations**, **Status Updates**, and **Built-in Tools → Knowledge**.
- Disable other built-in tool categories, memory, web search, image generation, code interpreter, terminal, vision, and chat file uploads.
- Copy the complete `params.system` value from [config/assistant.json](config/assistant.json) into the system prompt, using actual line breaks rather than literal JSON `\n` escapes. Leave other sampling parameters without explicit overrides.

To display the prompt as plain text on macOS:

```bash
python3 -c "import json; print(json.load(open('config/assistant.json', encoding='utf-8'))['params']['system'])"
```

On Windows, use the same command with `py -3` instead of `python3`. Save the assistant and reload the interface.

## 3. Use and restart

Open [Open WebUI](http://localhost:3005), start a new chat, and select **Chapters Case Study Assistant**. Ask questions in the browser.

Stop or restart the container without deleting its saved data:

```shell
docker stop oikb-case-study-humdaan
docker start oikb-case-study-humdaan
```

Keep Ollama and the container runtime running. There is no need to rerun setup after a normal restart.
