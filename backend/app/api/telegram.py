from fastapi import APIRouter
from app.services.telegram import get_notifier
from app.core.scheduler import send_daily_report

router = APIRouter()


@router.post("/test")
def test_notification():
    """텔레그램 연결 테스트"""
    notifier = get_notifier()
    ok = notifier.notify_system("✅ 텔레그램 연결 테스트 성공!\nPeakExit과 정상 연결되었습니다.")
    return {"success": ok, "enabled": notifier.enabled}


@router.post("/report/now")
def send_report_now(report_type: str = "close"):
    """즉시 수익률 리포트 발송 (open | close)"""
    send_daily_report(report_type)
    return {"message": f"리포트 발송 완료 ({report_type})"}
