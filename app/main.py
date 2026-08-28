"""FastAPI application boundary for TraceOps."""

from fastapi import FastAPI

from app.schemas import HealthResponse

APP_VERSION = "0.1.0"


def create_app() -> FastAPI:
    """Create and configure the TraceOps API."""

    application = FastAPI(
        title="TraceOps",
        description="Evidence-backed AI production incident investigator",
        version=APP_VERSION,
    )

    @application.get(
        "/health",
        response_model=HealthResponse,
        tags=["system"],
        summary="Check service health",
    )
    async def health() -> HealthResponse:
        return HealthResponse(
            status="healthy",
            service="traceops",
            version=APP_VERSION,
        )

    return application


app = create_app()
