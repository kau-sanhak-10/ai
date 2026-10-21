from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """배포·Docker 헬스체크용. 백엔드 /actuator/health처럼 ApiResponse 형식을 쓰지 않는다."""
    return {"status": "UP"}
