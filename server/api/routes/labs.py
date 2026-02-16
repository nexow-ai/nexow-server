"""Strategy Labs API endpoints — conversational strategy building."""

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from server.ai.lab_architect import LabRequest, process_lab_message

router = APIRouter(prefix="/api/labs", tags=["labs"])


@router.post("/message")
async def lab_message(request: LabRequest):
    """Process a conversational message and stream back the AI response."""
    return StreamingResponse(
        process_lab_message(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
