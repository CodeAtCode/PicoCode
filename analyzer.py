import os
import json
import traceback
import subprocess
import asyncio
import concurrent.futures
from pathlib import Path
from typing import Optional, Dict, Any

from db import create_analysis, store_file, store_embedding, update_analysis_status, update_analysis_counts
from external_api import get_embedding_for_text, call_coding_api

# language detection by extension
EXT_LANG = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
}

EMBEDDING_CONCURRENCY = 4  # tunable: how many concurrent embedding requests
_THREADPOOL_WORKERS = max(16, EMBEDDING_CONCURRENCY + 8)
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=_THREADPOOL_WORKERS)


def detect_language(path: str):
    if "LICENSE.md" in path:
        return "text"
    if "__editable__" in path:
        return "text"
    if "_virtualenv.py" in path:
        return "text"
    ext = Path(path).suffix.lower()
    return EXT_LANG.get(ext, "text")


def _find_python_executable_in_venv(venv_path: str):
    p1 = os.path.join(venv_path, "bin", "python")
    p2 = os.path.join(venv_path, "Scripts", "python.exe")
    if os.path.exists(p1) and os.access(p1, os.X_OK):
        return p1
    if os.path.exists(p2) and os.access(p2, os.X_OK):
        return p2
    if os.path.exists(venv_path) and os.access(venv_path, os.X_OK):
        return venv_path
    return None


def parse_venv_dependencies_with_pip(venv_path: str):
    if not venv_path:
        return {}
    venv_python = _find_python_executable_in_venv(venv_path)
    if not venv_python:
        return {}
    try:
        cmd = [venv_python, "-m", "pip", "list", "--format=json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        data = json.loads(proc.stdout)
        return {item.get("name"): item.get("version") for item in data if "name" in item}
    except Exception:
        return {}


def parse_pyproject_dependencies(local_path: str):
    pyproject = os.path.join(local_path, "pyproject.toml")
    if not os.path.exists(pyproject):
        return {}
    try:
        try:
            import tomllib as _toml  # Python 3.11+
        except Exception:
            import tomli as _toml  # fallback
        with open(pyproject, "rb") as fh:
            data = _toml.load(fh)
        deps = {}
        project = data.get("project", {})
        if isinstance(project, dict):
            deps_list = project.get("dependencies") or []
            if isinstance(deps_list, list):
                for dep in deps_list:
                    deps[dep] = ""
        tool = data.get("tool", {})
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            for key in ("dependencies", "dev-dependencies"):
                deps_section = poetry.get(key, {})
                if isinstance(deps_section, dict):
                    for k, v in deps_section.items():
                        deps[k] = v if not isinstance(v, dict) else ""
        tool_uv = data.get("tool", {}).get("uv")
        if tool_uv is not None:
            deps["__tool_uv_config_present__"] = True
        return deps
    except Exception:
        return {}


def find_uv_managed_venv(local_path: str):
    candidates = [
        ".venv",
        ".uv/venv",
        ".uvenv",
        "venv",
        "env",
        ".venv/venv",
    ]
    for c in candidates:
        p = os.path.join(local_path, c)
        if os.path.isdir(p):
            py = _find_python_executable_in_venv(p)
            if py:
                return p
    return None


def detect_uv_usage(local_path: str, venv_path: str = None):
    details = {}
    py_deps = parse_pyproject_dependencies(local_path)
    if py_deps:
        details["pyproject"] = py_deps
        if py_deps.get("__tool_uv_config_present__") or any(
            (k.lower() == "uv" or (isinstance(k, str) and k.lower().split()[0] == "uv")) for k in py_deps.keys()
        ):
            return {"uv_detected": True, "source": "pyproject", "details": details, "resolved_venv": None}
    if venv_path and os.path.exists(venv_path):
        pips = parse_venv_dependencies_with_pip(venv_path)
        details["pip"] = pips
        if any(name.lower() == "uv" for name in pips.keys()):
            return {"uv_detected": True, "source": "pip", "details": details, "resolved_venv": venv_path}
    found = find_uv_managed_venv(local_path)
    if found:
        pips = parse_venv_dependencies_with_pip(found)
        details["pip_discovered"] = pips
        if any(name.lower() == "uv" for name in pips.keys()):
            return {"uv_detected": True, "source": "venv_search", "details": details, "resolved_venv": found}
        return {"uv_detected": False, "source": "venv_search_no_uv", "details": details, "resolved_venv": found}
    return {"uv_detected": False, "source": "none", "details": details, "resolved_venv": None}


# Async helpers ---------------------------------------------------------------

async def _run_in_executor(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: func(*args, **kwargs))


async def async_get_embedding(text: str, model: Optional[str] = None):
    # Wrap the (possibly blocking) get_embedding_for_text in a threadpool so the event loop isn't blocked.
    return await _run_in_executor(get_embedding_for_text, text, model)


# Main async processing for a single file
async def _process_file(
    semaphore: asyncio.Semaphore,
    database_path: str,
    analysis_id: int,
    full_path: str,
    rel_path: str,
    cfg: Optional[Dict[str, Any]],
):
    try:
        # read file content in threadpool
        try:
            content = await _run_in_executor(lambda p: open(p, "r", encoding="utf-8", errors="ignore").read(), full_path)
        except Exception:
            return {"stored": False, "embedded": False}

        if not content:
            return {"stored": False, "embedded": False}

        lang = detect_language(rel_path)
        if lang == "text":
            # ignore files whose extensions are not explicitly mapped in EXT_LANG
            return {"stored": False, "embedded": False}

        # store file (store_file is sync, run in executor)
        fid = await _run_in_executor(store_file, database_path, analysis_id, rel_path, content, lang)

        # embedding
        embedding_model = None
        if isinstance(cfg, dict):
            embedding_model = cfg.get("embedding_model")

        # limit concurrency for embedding provider
        await semaphore.acquire()
        try:
            emb = await async_get_embedding(content, model=embedding_model)
        finally:
            semaphore.release()

        if emb:
            await _run_in_executor(store_embedding, database_path, fid, emb)
            return {"stored": True, "embedded": True}
        else:
            # store a small error record if embedding provider returns None/empty
            err_msg = "Embedding API returned no vector for file."
            try:
                await _run_in_executor(
                    store_file,
                    database_path,
                    analysis_id,
                    f"errors/{rel_path}.error.txt",
                    err_msg,
                    "error",
                )
            except Exception:
                pass
            return {"stored": True, "embedded": False}
    except Exception as e:
        tb = traceback.format_exc()
        try:
            error_payload = {"file": rel_path, "error": str(e), "traceback": tb[:2000]}
            await _run_in_executor(
                store_file,
                database_path,
                analysis_id,
                f"errors/{rel_path}.error.txt",
                json.dumps(error_payload, indent=2),
                "error",
            )
            print(error_payload)
        except Exception:
            pass
        return {"stored": False, "embedded": False}


async def analyze_local_path(
    local_path: str,
    database_path: str,
    venv_path: Optional[str] = None,
    max_file_size: int = 200000,
    cfg: Optional[dict] = None,
):
    """
    Async implementation of the analysis pipeline. Use the synchronous wrapper
    analyze_local_path_background(...) below if you want to keep a blocking API.
    """
    aid = None
    semaphore = asyncio.Semaphore(EMBEDDING_CONCURRENCY)
    try:
        name = os.path.basename(os.path.abspath(local_path)) or local_path
        aid = await _run_in_executor(create_analysis, database_path, name, local_path, "running")

        file_count = 0
        emb_count = 0
        tasks = []

        for root, dirs, files in os.walk(local_path):
            for fname in files:
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, local_path)
                try:
                    size = os.path.getsize(full)
                    if size > max_file_size:
                        continue
                except Exception:
                    continue
                # schedule processing but don't block the loop
                tasks.append(_process_file(semaphore, database_path, aid, full, rel, cfg))

        # execute tasks with bounded concurrency handled inside _process_file
        # gather results while letting exceptions be handled per-task
        for chunk_start in range(0, len(tasks), 256):
            chunk = tasks[chunk_start : chunk_start + 256]
            results = await asyncio.gather(*chunk, return_exceptions=False)
            for r in results:
                if isinstance(r, dict):
                    if r.get("stored"):
                        file_count += 1
                    if r.get("embedded"):
                        emb_count += 1

        # detect uv usage and deps (run in executor because it may use subprocess / file IO)
        uv_info = await _run_in_executor(detect_uv_usage, local_path, venv_path)
        try:
            await _run_in_executor(
                store_file,
                database_path,
                aid,
                "uv_detected.json",
                json.dumps(uv_info, indent=2),
                "meta",
            )
        except Exception:
            pass

        resolved_venv = uv_info.get("resolved_venv") or venv_path
        if resolved_venv and os.path.exists(resolved_venv):
            deps = await _run_in_executor(parse_venv_dependencies_with_pip, resolved_venv)
            try:
                await _run_in_executor(
                    store_file,
                    database_path,
                    aid,
                    "venv_dependencies.json",
                    json.dumps(deps, indent=2),
                    "deps",
                )
            except Exception:
                pass
            if uv_info.get("uv_detected") and uv_info.get("resolved_venv") and uv_info.get("resolved_venv") != venv_path:
                note = {"note": "using discovered venv because uv detected", "discovered_venv": uv_info.get("resolved_venv")}
                try:
                    await _run_in_executor(
                        store_file,
                        database_path,
                        aid,
                        "uv_resolution_note.json",
                        json.dumps(note, indent=2),
                        "meta",
                    )
                except Exception:
                    pass
        else:
            deps = await _run_in_executor(parse_pyproject_dependencies, local_path)
            if deps:
                try:
                    await _run_in_executor(
                        store_file,
                        database_path,
                        aid,
                        "pyproject_deps.txt",
                        json.dumps(deps, indent=2),
                        "deps",
                    )
                except Exception:
                    pass

        # update final counts/status
        await _run_in_executor(update_analysis_counts, database_path, aid, file_count, emb_count)
        await _run_in_executor(update_analysis_status, database_path, aid, "completed")
    except Exception:
        try:
            if aid:
                await _run_in_executor(update_analysis_status, database_path, aid, "failed")
        except Exception:
            pass
        traceback.print_exc()


def analyze_local_path_background(local_path: str, database_path: str, venv_path: Optional[str] = None, max_file_size: int = 200000, cfg: Optional[dict] = None):
    """
    Backwards-compatible blocking wrapper for the async analyze_local_path.
    Call this from synchronous code; it will run the async pipeline and return when finished.
    """
    asyncio.run(analyze_local_path(local_path, database_path, venv_path=venv_path, max_file_size=max_file_size, cfg=cfg))


# Simple synchronous helpers preserved for compatibility --------------------------------

def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def norm(a):
    return sum(x * x for x in a) ** 0.5


def cosine(a, b):
    na = norm(a)
    nb = norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return dot(a, b) / (na * nb)


def search_semantic(query: str, database_path: str, analysis_id: int, top_k: int = 5):
    # keep this sync for compatibility; it's small and infrequent compared to the bulk embedding work
    q_emb = get_embedding_for_text(query)
    if not q_emb:
        return []
    conn = __import__("sqlite3").connect(database_path)
    cur = conn.execute(
        "SELECT files.id, files.path, embeddings.vector FROM files JOIN embeddings ON files.id = embeddings.file_id WHERE files.analysis_id = ?",
        (analysis_id,),
    )
    rows = cur.fetchall()
    scored = []
    for fid, path, vector_json in rows:
        try:
            vec = json.loads(vector_json)
            score = cosine(q_emb, vec)
            scored.append({"file_id": fid, "path": path, "score": score})
        except Exception:
            continue
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def call_coding_model(prompt: str, context: str = ""):
    combined = f"Context:\n{context}\n\nPrompt:\n{prompt}" if context else prompt
    return call_coding_api(combined)
