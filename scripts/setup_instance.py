#!/usr/bin/env python3
"""Prepare a dedicated, empty Open WebUI v0.13.3 instance for the case study.

Standard library only. Does not install services, delete resources, update an
existing assistant, or generate evaluation answers. Files are placed under
knowledge/ in the knowledge base, matching a folder upload in the UI. --apply changes global
document settings on the target instance. --check-config is entirely offline.
"""

import argparse
import copy
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.13.3"
PROBES = {
    "README.md": ("Nextcloud sync Documents command", "nextcloud:/Documents"),
    "src/oikb/history.py": ("SyncHistory log sync_log schema", "sync_log"),
}
# Content types the browser sends for a folder upload in the Open WebUI UI.
CONTENT_TYPES = {".md": "text/markdown", ".py": "text/x-python-script"}


class SetupError(Exception):
    pass


class APIError(SetupError):
    def __init__(self, status, method, path):
        self.status = status
        super().__init__(
            f"HTTP {status}: {method} {path}. Check the API key, admin role, "
            "endpoint allowlist and local server logs."
        )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_configs(directory):
    try:
        configs = [json.loads((directory / name).read_text(encoding="utf-8")) for name in
                   ("assistant.json", "documents.json", "sources.json")]
        assistant, documents, sources = configs
        if not all(isinstance(item, dict) for item in configs):
            raise ValueError("Configuration roots must be JSON objects.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", assistant["id"]):
            raise ValueError("Invalid assistant ID.")
        if not assistant["params"]["system"].strip() or not assistant["base_model_id"]:
            raise ValueError("A system prompt and base model are required.")
        if assistant["knowledge_base_name"] != sources["knowledge_base_name"]:
            raise ValueError("The assistant and source knowledge-base names differ.")
        if not re.fullmatch(r"https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", sources["repository_url"]):
            raise ValueError("Expected a public GitHub repository URL without a trailing slash.")
        if not re.fullmatch(r"[0-9a-f]{40}", sources["commit"]):
            raise ValueError("Pin a full 40-character commit hash.")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", sources["upload_folder"]) or sources["upload_folder"] in (".", ".."):
            raise ValueError("upload_folder must be a single folder name.")
        if {item["path"] for item in sources["files"]} != set(PROBES) or len(sources["files"]) != 2:
            raise ValueError("This case-study helper expects README.md and src/oikb/history.py.")
        for item in sources["files"]:
            if PurePosixPath(item["path"]).name != item["upload_name"]:
                raise ValueError("Upload names must be the source basenames.")
            if PurePosixPath(item["path"]).suffix not in CONTENT_TYPES:
                raise ValueError(f"No content type known for {item['path']}.")
        if documents["embedding"]["RAG_EMBEDDING_ENGINE"] != "":
            raise ValueError("This helper supports the saved local/default embedding engine only.")
        for key in ("RAG_EMBEDDING_MODEL", "RAG_EMBEDDING_BATCH_SIZE"):
            if key not in documents["embedding"]:
                raise ValueError(f"Missing embedding setting: {key}")
        for key in ("TEXT_SPLITTER", "CHUNK_SIZE", "CHUNK_OVERLAP", "TOP_K"):
            if key not in documents["retrieval"]:
                raise ValueError(f"Missing retrieval setting: {key}")
        if not 0 <= documents["retrieval"]["CHUNK_OVERLAP"] < documents["retrieval"]["CHUNK_SIZE"]:
            raise ValueError("Chunk overlap must be nonnegative and smaller than chunk size.")
        # These exports intentionally contain no provider credential sections.
        for settings in documents.values():
            if not isinstance(settings, dict):
                raise ValueError("Document settings must be objects.")
            for key in settings:
                if any(word in key.lower() for word in ("api_key", "password", "secret", "config")):
                    raise ValueError(f"Unexpected credential/provider field: {key}")
        return configs
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise SetupError(f"Invalid configuration: {error}") from None


class Client:
    def __init__(self, base_url, api_key, timeout=180):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or
                parsed.hostname not in ("localhost", "127.0.0.1", "::1") or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path not in ("", "/")):
            raise SetupError("Use a loopback Open WebUI URL, e.g. http://localhost:3005.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, payload=None, raw=None, content_type=None, auth=True):
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body = raw
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if "application/json" not in response.headers.get("Content-Type", ""):
                    raise SetupError(f"Expected JSON from {path}, not a webpage.")
                result = json.load(response)
                if result is None:
                    raise SetupError(f"Empty result from {path}.")
                if isinstance(result, dict) and (result.get("status") is False or result.get("error")):
                    raise SetupError(f"Server reported an error for {path}; inspect its logs.")
                return result
        except HTTPError as error:
            # Raw error bodies can contain credentials or other configuration.
            raise APIError(error.code, method, path) from None
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise SetupError(f"Request failed for {path}: {type(error).__name__}") from None

    def upload(self, filename, content, content_type, metadata):
        # metadata.knowledge_id/directory_id makes the server link and index the file
        # into that knowledge-base folder, as the UI's folder upload does.
        boundary = "case-study-" + uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'
            f'{json.dumps(metadata)}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request(
            "POST", "/api/v1/files/?process=true&process_in_background=true",
            raw=body, content_type=f"multipart/form-data; boundary={boundary}",
        )


def assert_saved(expected, actual, label):
    different = [key for key, value in expected.items() if key not in actual or actual[key] != value]
    if different:
        raise SetupError(f"{label} read-back differs for: {', '.join(different)}")


def preflight(client, assistant):
    version = client.request("GET", "/api/version", auth=False).get("version", "")
    if version.lstrip("v") != EXPECTED_VERSION:
        raise SetupError(f"Expected Open WebUI {EXPECTED_VERSION}; found {version!r}.")
    model_path = "/api/v1/models/model?" + urlencode({"id": assistant["id"]})
    try:
        client.request("GET", model_path)
    except APIError as error:
        if error.status != 404:
            raise
    else:
        raise SetupError("The target assistant already exists. Use a fresh instance; it will not be overwritten.")
    # Reading admin-only configuration also verifies that the key belongs to an admin.
    retrieval = client.request("GET", "/api/v1/retrieval/config")
    embedding = client.request("GET", "/api/v1/retrieval/embedding")
    if client.request("GET", "/api/v1/files/count") != 0:
        raise SetupError("The instance already contains files. Use a dedicated empty instance.")
    if client.request("GET", "/api/v1/knowledge/?page=1").get("total") != 0:
        raise SetupError("The instance already contains knowledge bases. Use a dedicated empty instance.")
    available = client.request("GET", "/api/models").get("data", [])
    if assistant["base_model_id"] not in {item.get("id") for item in available}:
        raise SetupError(f"Base model {assistant['base_model_id']} is unavailable. Check Ollama and pull the model.")
    return retrieval, embedding


def fetch_sources(sources):
    repository = sources["repository_url"].removeprefix("https://github.com/")
    result = []
    # A separate, unauthenticated client is used for public source downloads.
    opener = build_opener(NoRedirect())
    for item in sources["files"]:
        url = f"https://raw.githubusercontent.com/{repository}/{sources['commit']}/{quote(item['path'])}"
        try:
            with opener.open(Request(url), timeout=60) as response:
                content = response.read(5 * 1024 * 1024 + 1)
            if not content or len(content) > 5 * 1024 * 1024:
                raise SetupError("Source file is empty or exceeds the 5 MiB limit.")
            content.decode("utf-8")
        except (URLError, OSError, ValueError) as error:
            raise SetupError(f"Could not download {item['path']}: {type(error).__name__}") from None
        result.append({**item, "content": content, "sha256": hashlib.sha256(content).hexdigest()})
    return result


def wait_processed(client, file_id, timeout, interval=2):
    deadline = time.monotonic() + timeout
    while True:
        result = client.request("GET", f"/api/v1/files/{quote(file_id, safe='')}/process/status")
        status = result.get("status")
        if status == "completed":
            return
        if status in ("failed", "error", "not_found"):
            raise SetupError(f"File {file_id} processing ended with status {status}.")
        if time.monotonic() >= deadline:
            raise SetupError(f"Timed out waiting for file {file_id}. No assistant was created.")
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


def has_evidence(result, file_id, marker):
    documents = result.get("documents") or []
    metadatas = result.get("metadatas") or []
    # Collection-query responses contain one nested result list per query.
    for docs, metadata in zip(documents, metadatas):
        if not isinstance(docs, list) or not isinstance(metadata, list):
            continue
        for text, info in zip(docs, metadata):
            if isinstance(text, str) and isinstance(info, dict):
                if info.get("file_id") == file_id and marker.lower() in text.lower():
                    return True
    return False


def folder_parts(sources, item):
    """knowledge/src/oikb/history.py -> ["knowledge", "src", "oikb"]."""
    return [sources["upload_folder"], *PurePosixPath(item["path"]).parent.parts]


def ensure_directory(client, kb_path, parts):
    """Return the ID of the nested knowledge-base folder, creating missing levels."""
    parent = None
    for name in parts:
        listing = client.request("GET", kb_path + "/files?" + urlencode({"page": 1, "directory_id": parent or ""}))
        existing = [d for d in listing.get("directories") or [] if d.get("name") == name and d.get("parent_id") == parent]
        if len(existing) > 1:
            raise SetupError(f"Duplicate knowledge-base folders named {name!r}.")
        parent = existing[0]["id"] if existing else client.request(
            "POST", kb_path + "/dirs/create", {"name": name, "parent_id": parent})["id"]
    return parent


def verify_layout(client, kb_path, expected):
    """expected: {file_id: directory_id}. The knowledge base must hold exactly these files there."""
    members = client.request("GET", kb_path + "/files?page=1")
    actual = {item.get("id"): item.get("directory_id") for item in members.get("items", [])}
    if members.get("total") != len(expected) or actual != expected:
        raise SetupError("The knowledge-base files or folders do not match the expected folder upload.")


def check_retrieval(client, knowledge_id, files, top_k, report, report_path):
    print("Checking that indexed passages can be retrieved...")
    for item in files:
        query, marker = PROBES[item["path"]]
        result = client.request("POST", "/api/v1/retrieval/query/collection", {
            "collection_names": [knowledge_id], "query": query, "k": top_k, "hybrid": False,
        })
        passed = has_evidence(result, item["id"], marker)
        report["retrieval_checks"].append({"path": item["path"], "query": query, "passed": passed})
        save_json(report_path, report)
        if not passed:
            raise SetupError(f"Retrieval did not return the expected passage from {item['path']}.")


def provision(client, configs, downloaded, report_path, processing_timeout=600):
    assistant, documents, sources = configs
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "instance_url": client.base_url,
        "source_commit": sources["commit"],
        "config_sha256": hashlib.sha256(json.dumps(configs, sort_keys=True).encode()).hexdigest(),
        "status": "started", "files": [], "retrieval_checks": [],
    }
    save_json(report_path, report)
    try:
        print("Checking version, admin access, empty knowledge store, and base model...")
        prior_retrieval, prior_embedding = preflight(client, assistant)
        report["previous_document_settings"] = {
            "retrieval": {k: prior_retrieval.get(k) for k in documents["retrieval"]},
            "embedding": {k: prior_embedding.get(k) for k in documents["embedding"]},
        }
        save_json(report_path, report)

        print("Applying the saved document and embedding settings...")
        client.request("POST", "/api/v1/retrieval/embedding/update", documents["embedding"])
        client.request("POST", "/api/v1/retrieval/config/update", documents["retrieval"])
        assert_saved(documents["embedding"], client.request("GET", "/api/v1/retrieval/embedding"), "Embedding settings")
        assert_saved(documents["retrieval"], client.request("GET", "/api/v1/retrieval/config"), "Document settings")

        knowledge = client.request("POST", "/api/v1/knowledge/create", {
            "name": sources["knowledge_base_name"],
            "description": sources["knowledge_base_description"],
            "access_grants": [],
        })
        knowledge_id = knowledge["id"]
        kb_path = "/api/v1/knowledge/" + quote(knowledge_id, safe="")
        report["knowledge_base_id"] = knowledge_id
        save_json(report_path, report)

        layout = {}
        for item in downloaded:
            folder = "/".join(folder_parts(sources, item))
            print(f"Uploading and processing {folder}/{item['upload_name']}...")
            directory_id = ensure_directory(client, kb_path, folder_parts(sources, item))
            uploaded = client.upload(
                item["upload_name"], item["content"], CONTENT_TYPES[PurePosixPath(item["path"]).suffix],
                {"knowledge_id": knowledge_id, "directory_id": directory_id},
            )
            file_id = uploaded["id"]
            record = {k: item[k] for k in ("path", "upload_name", "sha256")}
            record.update({"id": file_id, "folder": folder, "status": "uploaded"})
            report["files"].append(record)
            save_json(report_path, report)
            wait_processed(client, file_id, processing_timeout)
            file = client.request("GET", "/api/v1/files/" + quote(file_id, safe=""))
            if not (file.get("data") or {}).get("content", "").strip():
                raise SetupError(f"No extracted text for {item['path']}.")
            layout[file_id] = directory_id
            record["status"] = "indexed_in_knowledge_base"
            save_json(report_path, report)

        verify_layout(client, kb_path, layout)
        check_retrieval(client, knowledge_id, report["files"], documents["retrieval"]["TOP_K"], report, report_path)

        payload = {k: copy.deepcopy(assistant[k]) for k in
                   ("id", "name", "base_model_id", "params", "meta", "is_active")}
        payload["meta"]["knowledge"] = [{
            "id": knowledge_id, "name": sources["knowledge_base_name"],
            "type": "collection", "description": sources["knowledge_base_description"],
        }]
        payload["access_grants"] = []
        print("Creating the private assistant and verifying its saved configuration...")
        created = client.request("POST", "/api/v1/models/create", payload)
        report["assistant_id"] = created["id"]
        save_json(report_path, report)
        saved = client.request("GET", "/api/v1/models/model?" + urlencode({"id": assistant["id"]}))
        assert_saved({k: payload[k] for k in ("id", "name", "base_model_id", "params", "is_active")}, saved, "Assistant")
        assert_saved(payload["meta"], saved.get("meta") or {}, "Assistant metadata")
        if saved.get("access_grants"):
            raise SetupError("The assistant has unexpected sharing grants.")
        if client.request("GET", kb_path).get("access_grants"):
            raise SetupError("The knowledge base has unexpected sharing grants.")
        report["status"] = "ready_for_manual_questions"
        report["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        save_json(report_path, report)
        print(f"Ready: open {client.base_url} and select {assistant['name']} in a new chat.")
        print("No model answers were generated or graded. Run the three evaluation questions in the UI.")
    except (SetupError, KeyError, TypeError, OSError, KeyboardInterrupt) as error:
        report["status"] = "failed"
        report["error"] = str(error) if isinstance(error, SetupError) else type(error).__name__
        save_json(report_path, report)
        raise SetupError(
            f"Setup stopped: {report['error']}\nReport: {report_path}\n"
            "Any already-created resources and global setting changes are retained. "
            "No automatic deletion or rollback is performed. Review the report before retrying; "
            "this script intentionally refuses a nonempty target."
        ) from None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-config", action="store_true", help="Validate the three local JSON files; no network requests")
    mode.add_argument("--apply", action="store_true", help="Change global document settings and create resources on an empty instance")
    parser.add_argument("--base-url", default="http://localhost:3005", help="Loopback Open WebUI URL")
    parser.add_argument("--config-dir", type=Path, default=ROOT / "config")
    parser.add_argument("--processing-timeout", type=int, default=600, help="Seconds to wait per uploaded file")
    args = parser.parse_args()
    configs = read_configs(args.config_dir)
    if args.processing_timeout <= 0:
        raise SetupError("Processing timeout must be positive.")
    if args.check_config:
        print("Configuration valid: pinned sources, matching knowledge-base name, assistant prompt and document settings.")
        print("No API requests were made. Server compatibility and runtime behavior have not been tested.")
        return
    api_key = os.environ.get("OPENWEBUI_API_KEY") or getpass.getpass("Open WebUI admin API key (hidden): ")
    if not api_key.strip():
        raise SetupError("An admin API key is required.")
    client = Client(args.base_url, api_key.strip())
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    # Detect conflicts before downloading or changing anything on the instance.
    preflight(client, configs[0])
    print("Downloading the two pinned public source files...")
    downloaded = fetch_sources(configs[2])
    report_path = ROOT / ".local" / f"setup-{run_id}.json"
    provision(client, configs, downloaded, report_path, args.processing_timeout)
    print(f"Setup report: {report_path}")


if __name__ == "__main__":
    try:
        main()
    except SetupError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
