from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import stocks, portfolio, settings, telegram, logs
from app.core.scheduler import start_scheduler
from app.core.log_store import setup_file_logging
from app.services.telegram import get_notifier
from app.services.telegram_commands import start_command_handler

app = FastAPI(title="PeakExit - 자동 매도 시스템", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(stocks.router, prefix="/api/stocks", tags=["stocks"])
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(telegram.router, prefix="/api/telegram", tags=["telegram"])
app.include_router(logs.router, prefix="/api/logs", tags=["logs"])

@app.on_event("startup")
async def startup_event():
    setup_file_logging()
    start_scheduler()
    # 텔레그램 봇 명령어 핸들러 시작
    notifier = get_notifier()
    start_command_handler(notifier)

@app.get("/")
def root():
    return {"message": "PeakExit API is running"}

@app.get("/health")
def health():
    return {"status": "ok"}
