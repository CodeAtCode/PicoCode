from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager
import os
import json
import uvicorn
from typing import Optional

from db import init_db, list_analyses, delete_analysis
from analyzer import analyze_local_path_background, search_semantic, call_coding_model
from config import CFG  # loads .env

DATABASE = CFG.get("database_path", "codebase.db")
MAX_FILE_SIZE = int(CFG.get("max_file_size", 200000))

# Controls how many characters of each snippet and total context we send to coding model
TOTAL_CONTEXT_LIMIT = 4000
_ANALYSES_CACHE = []

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(DATABASE)
    yield

app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    analyses = list_analyses(DATABASE)
    return templates.TemplateResponse("index.html", {"request": request, "analyses": analyses, "config": CFG})


@app.get("/analyses/status")
def analyses_status():
    global _ANALYSES_CACHE
    try:
        analyses = list_analyses(DATABASE)
        # If the DB returned a non-empty list, update cache and return it.
        if analyses:
            _ANALYSES_CACHE = analyses
            return JSONResponse(analyses)
        # If DB returned empty but we have a cached non-empty list, return cache
        if not analyses and _ANALYSES_CACHE:
            return JSONResponse(_ANALYSES_CACHE)
        # else return whatever (empty list) — first-run or truly empty
        return JSONResponse(analyses)
    except Exception as e:
        # On DB errors (e.g., locked) return last known cache to avoid empty responses spam.
        if _ANALYSES_CACHE:
            return JSONResponse(_ANALYSES_CACHE)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/analyses/{analysis_id}/delete")
def delete_analysis_endpoint(analysis_id: int):
    try:
        delete_analysis(DATABASE, analysis_id)
        return JSONResponse({"deleted": True})
    except Exception as e:
        return JSONResponse({"deleted": False, "error": str(e)}, status_code=500)


@app.post("/analyze")
def analyze(background_tasks: BackgroundTasks):
    local_path = CFG.get("local_path")
    if not local_path or not os.path.exists(local_path):
        raise HTTPException(status_code=400, detail="Configured LOCAL_PATH does not exist")
    venv_path = CFG.get("venv_path")
    background_tasks.add_task(analyze_local_path_background, local_path, DATABASE, venv_path, MAX_FILE_SIZE, CFG)
    return RedirectResponse(url="/", status_code=303)


@app.post("/code")
def code_endpoint(request: Request):
    payload = None
    try:
        payload = request.json()
    except Exception:
        try:
            payload = json.loads(request.body().decode("utf-8"))
        except Exception:
            payload = None

    if not payload or "prompt" not in payload:
        return JSONResponse({"error": "prompt required"}, status_code=400)

    prompt = payload["prompt"]
    explicit_context = payload.get("context", "") or ""
    use_rag = bool(payload.get("use_rag", True))
    analysis_id = payload.get("analysis_id")
    try:
        top_k = int(payload.get("top_k", 5))
    except Exception:
        top_k = 5

    used_context = []
    combined_context = explicit_context or ""

    # If RAG requested and an analysis_id provided, perform semantic search and build context
    if use_rag and analysis_id:
        try:
            retrieved = search_semantic(prompt, DATABASE, analysis_id=int(analysis_id), top_k=top_k)
            # Build context WITHOUT including snippets: only include file references and scores
            context_parts = []
            total_len = len(combined_context)
            for r in retrieved:
                part = f"File: {r.get('path')} (score: {r.get('score', 0):.4f})\n"
                if total_len + len(part) > TOTAL_CONTEXT_LIMIT:
                    break
                context_parts.append(part)
                total_len += len(part)
                used_context.append({"path": r.get("path"), "score": r.get("score")})
            if context_parts:
                retrieved_text = "\n".join(context_parts)
                if combined_context:
                    combined_context = combined_context + "\n\nRetrieved:\n" + retrieved_text
                else:
                    combined_context = "Retrieved:\n" + retrieved_text
        except Exception:
            used_context = []

    # Call the coding model with prompt and combined_context
    try:
        resp = call_coding_model(prompt, combined_context)
    except Exception as e:
        return JSONResponse({"error": f"coding model call failed: {e}"}, status_code=500)

    return JSONResponse({"response": resp, "used_context": used_context})


@app.post("/analyses/{analysis_id}/delete")
def delete_analysis_endpoint(analysis_id: int):
    try:
        delete_analysis(DATABASE, analysis_id)
        return JSONResponse({"deleted": True})
    except Exception as e:
        return JSONResponse({"deleted": False, "error": str(e)}, status_code=500)


if __name__ == "__main__":
    uvicorn.run("main:app", host=CFG.get("uvicorn_host", "127.0.0.1"), port=int(CFG.get("uvicorn_port", 8000)), reload=True)
