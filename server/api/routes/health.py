"""Health and status endpoints."""

from fastapi import APIRouter

from server.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    return {"status": "healthy", "service": "nexow-server"}


@router.get("/status")
async def get_status():
    return {
        "service": "nexow-server",
        "version": "0.1.0",
        "environment": settings.environment,
    }
