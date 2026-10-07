"""REST API cho chatbot hỗ trợ đơn hàng, kèm giao diện chat tĩnh. Chạy bằng `python src/main.py`."""

import re
import threading
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

load_dotenv()  # nạp .env sớm để chạy được cả khi khởi động bằng `uvicorn api:app`

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import agent_trace as trace
import tools

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_MESSAGE_LENGTH = 1000
MAX_SESSIONS = 200  # vượt quá thì bỏ phiên cũ nhất, để bộ nhớ không phình mãi


@dataclass
class Session:
    agent: agent.SupportAgent
    lock: threading.Lock = field(default_factory=threading.Lock)  # mỗi phiên chỉ xử lý một lượt tại một thời điểm


SESSIONS: "OrderedDict[str, Session]" = OrderedDict()
_sessions_lock = threading.Lock()


# --- schema ---

class MessageIn(BaseModel):
    message: str


class ApprovalIn(BaseModel):
    approved: bool


class ToolEventOut(BaseModel):
    tool: str
    args: dict[str, Any]
    level: str
    reason: str
    outcome: str


class ApprovalOut(BaseModel):
    tool: str
    args: dict[str, Any]
    reason: str


class ChatOut(BaseModel):
    status: Literal["reply", "pending_approval"]
    reply: str | None = None  # toàn văn câu trả lời của agent
    body: str | None = None  # reply bỏ 3 dòng cuối (Tóm tắt / Đề xuất / Trạng thái)
    summary: str | None = None
    suggestion: str | None = None
    case_status: str | None = None
    approval: ApprovalOut | None = None
    tool_events: list[ToolEventOut] = []


# --- xử lý câu trả lời ---

_FIELD_RE = re.compile(r"^[\s*\-•]*(Tóm tắt|Đề xuất|Trạng thái)\s*\**\s*:\s*\**\s*(.*?)\s*\**\s*$")
_FIELD_KEYS = {"Tóm tắt": "summary", "Đề xuất": "suggestion", "Trạng thái": "case_status"}


def split_reply(text: str) -> dict[str, str | None]:
    """Tách 3 dòng cuối do system prompt yêu cầu. Model sai định dạng thì body là toàn văn."""
    lines = text.rstrip().splitlines()
    fields: dict[str, str | None] = {"summary": None, "suggestion": None, "case_status": None}
    end = len(lines)
    while end > 0:
        line = lines[end - 1]
        match = _FIELD_RE.match(line)
        if match:
            fields[_FIELD_KEYS[match.group(1)]] = match.group(2)
        elif line.strip():
            break
        end -= 1
    return {"body": "\n".join(lines[:end]).strip(), **fields}


def to_response(result: agent.TurnResult) -> ChatOut:
    out: dict[str, Any] = {
        "status": result.status,
        "tool_events": [asdict(e) for e in result.tool_events],
        "approval": asdict(result.approval) if result.approval else None,
    }
    if result.reply is not None:
        out["reply"] = result.reply
        out.update(split_reply(result.reply))
    return ChatOut(**out)


def validate_message(text: str) -> str | None:
    """Trả về thông báo lỗi nếu tin nhắn không hợp lệ, ngược lại None."""
    if not text.strip():
        return "Tin nhắn trống, vui lòng nhập nội dung."
    if len(text) > MAX_MESSAGE_LENGTH:
        return f"Tin nhắn quá dài ({len(text)} ký tự, tối đa {MAX_MESSAGE_LENGTH})."
    return None


# --- app ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    tools.load_orders()
    app.state.client = agent.make_client()  # thiếu GEMINI_API_KEY thì lỗi ngay lúc khởi động
    trace.event(f"SERVER SẴN SÀNG: model={agent.model_name()}, {len(tools.ORDERS)} đơn hàng mẫu")
    yield


app = FastAPI(title="Order Support Chatbot", lifespan=lifespan)


def _get_session(session_id: str) -> Session:
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Phiên không tồn tại hoặc đã hết hạn. Hãy tạo phiên mới.")
    return session


def _run_turn(session_id: str, run) -> ChatOut:
    """Chạy một lượt trên phiên, từ chối nếu phiên đang bận (không chạy song song trên cùng lịch sử)."""
    session = _get_session(session_id)
    if not session.lock.acquire(blocking=False):
        session.agent.trace.note("YÊU CẦU BỊ TỪ CHỐI (409)", "phiên đang xử lý lượt trước")
        raise HTTPException(409, "Phiên đang xử lý tin nhắn trước, vui lòng đợi.")
    try:
        return to_response(run(session.agent))
    except agent.ApprovalStateError as e:
        session.agent.trace.note("YÊU CẦU BỊ TỪ CHỐI (409)", str(e))
        raise HTTPException(409, f"Không thực hiện được: {e}.")
    finally:
        session.lock.release()


# Các endpoint dùng `def` (không phải `async def`) vì gọi Gemini là blocking; FastAPI sẽ chạy chúng trong threadpool.

@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "model": agent.model_name()}


@app.post("/api/sessions", status_code=201)
def create_session() -> dict:
    session_id = uuid.uuid4().hex
    with _sessions_lock:
        SESSIONS[session_id] = Session(agent.SupportAgent(app.state.client, session_id))
        while len(SESSIONS) > MAX_SESSIONS:
            SESSIONS.popitem(last=False)
    return {"session_id": session_id}


@app.post("/api/sessions/{session_id}/messages", response_model=ChatOut)
def send_message(session_id: str, body: MessageIn) -> ChatOut:
    error = validate_message(body.message)
    if error:
        trace.event(f"[{session_id[:8]}] TIN NHẮN BỊ TỪ CHỐI (400), không gọi LLM", error)
        raise HTTPException(400, error)
    return _run_turn(session_id, lambda a: a.chat(body.message))


@app.post("/api/sessions/{session_id}/approval", response_model=ChatOut)
def resolve_approval(session_id: str, body: ApprovalIn) -> ChatOut:
    return _run_turn(session_id, lambda a: a.resolve_approval(body.approved))


@app.post("/api/demo/reset-orders")
def reset_orders() -> dict:
    """Nạp lại dữ liệu đơn mẫu (huỷ đơn / hoàn tiền trước đó bị xoá), để demo lại từ đầu."""
    tools.load_orders()
    return {"status": "ok", "orders": len(tools.ORDERS)}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
