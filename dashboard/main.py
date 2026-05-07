import json
import os
import secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import DASHBOARD_PASS, DASHBOARD_USER, LOG_FILE, MEMORY_FILE

app = FastAPI(title="Trading Agent Dashboard", docs_url=None, redoc_url=None)
security = HTTPBasic()

BASE_DIR = Path(__file__).parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

IST = timezone(timedelta(hours=5, minutes=30))


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(credentials.username.encode(), DASHBOARD_USER.encode())
    ok_pass = secrets.compare_digest(credentials.password.encode(), DASHBOARD_PASS.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def load_memory() -> dict:
    try:
        with open(MEMORY_FILE) as f:
            return json.load(f)
    except Exception:
        return {"trades": [], "lessons": [], "stats": {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0,
            "total_pnl": 0.0, "best_trade_pnl": 0.0, "worst_trade_pnl": 0.0,
        }}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/summary")
async def summary(_=Depends(require_auth)):
    data = load_memory()
    stats = data["stats"]
    total = stats.get("total_trades", 0)
    wins = stats.get("winning_trades", 0)
    losses = stats.get("losing_trades", 0)
    pnl = stats.get("total_pnl", 0.0)
    win_rate = round(wins / total * 100, 1) if total > 0 else 0.0

    open_trades = [t for t in data["trades"] if t.get("status") == "open"]
    today_ist = datetime.now(IST).date().isoformat()
    today_trades = [
        t for t in data["trades"]
        if t.get("status") == "closed" and (t.get("exit_time", "") or "").startswith(today_ist)
    ]
    today_pnl = sum(t.get("pnl", 0) for t in today_trades)

    return {
        "total_pnl": pnl,
        "today_pnl": round(today_pnl, 2),
        "today_trades": len(today_trades),
        "open_positions": len(open_trades),
        "total_trades": total,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": win_rate,
        "best_trade": stats.get("best_trade_pnl", 0.0),
        "worst_trade": stats.get("worst_trade_pnl", 0.0),
        "lessons_count": len(data.get("lessons", [])),
    }


@app.get("/api/positions")
async def positions(_=Depends(require_auth)):
    data = load_memory()
    open_trades = [t for t in data["trades"] if t.get("status") == "open"]
    result = []
    for t in open_trades:
        entry_time = t.get("entry_time", "")
        result.append({
            "trade_id": t.get("trade_id"),
            "symbol": t.get("symbol"),
            "action": t.get("action"),
            "quantity": t.get("quantity"),
            "entry_price": t.get("entry_price"),
            "stop_loss": t.get("stop_loss"),
            "target_1": t.get("target_1"),
            "setup_type": t.get("setup_type"),
            "confidence": t.get("confidence"),
            "time_horizon": t.get("time_horizon"),
            "entry_time": entry_time,
        })
    return result


@app.get("/api/trades")
async def trades(
    date: Optional[str] = Query(None),
    symbol: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(100, le=500),
    _=Depends(require_auth),
):
    data = load_memory()
    closed = [t for t in data["trades"] if t.get("status") == "closed"]

    if date:
        closed = [t for t in closed if (t.get("exit_time") or "").startswith(date)]
    if symbol:
        closed = [t for t in closed if t.get("symbol", "").upper() == symbol.upper()]
    if action:
        closed = [t for t in closed if t.get("action", "").upper() == action.upper()]

    closed.sort(key=lambda x: x.get("exit_time") or "", reverse=True)
    return closed[:limit]


@app.get("/api/lessons")
async def lessons(limit: int = Query(20, le=100), _=Depends(require_auth)):
    data = load_memory()
    all_lessons = data.get("lessons", [])
    return list(reversed(all_lessons[-limit:]))


@app.get("/api/logs")
async def logs(lines: int = Query(80, le=300), _=Depends(require_auth)):
    try:
        async with aiofiles.open(LOG_FILE, "r") as f:
            content = await f.read()
        log_lines = content.strip().split("\n")
        return {"lines": log_lines[-lines:]}
    except Exception as e:
        return {"lines": [f"Could not read log file: {e}"]}


@app.get("/api/health")
async def health(_=Depends(require_auth)):
    mem_ok = Path(MEMORY_FILE).exists()
    log_ok = Path(LOG_FILE).exists()
    return {
        "status": "ok" if mem_ok else "degraded",
        "memory_file": mem_ok,
        "log_file": log_ok,
        "timestamp": datetime.now(IST).isoformat(),
    }
