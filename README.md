# PeakExit — 자동 매도 시스템

한국투자증권(KIS) API를 사용해 보유 종목을 실시간 모니터링하고
설정된 조건에 따라 자동으로 매도하는 Docker 기반 서비스입니다.

## 📐 매도 로직

### ① 손절 (Stop Loss)
수익률이 설정값(기본 -10%) 이하로 떨어지면 **즉시 시장가 매도**

### ② 트레일링 스탑 ("어깨에서 팔기")
```
매수 후 → 최고가 지속 추적 → 고점에서 N% 하락 시 매도
```
- **발동 조건**: 수익률이 N%(기본 +5%) 이상 오른 적 있어야 추적 시작
- **매도 조건**: 고점 대비 N%(기본 -5%) 하락
- 계속 오르는 동안은 절대 팔지 않음 → 고점을 지나 내려올 때만 매도

### ③ 목표가 즉시 매도 (Take Profit, 선택)
설정한 수익률 도달 시 즉시 전량 매도 (비활성화 가능)

---

## 🚀 빠른 시작

### 1. API 키 설정
```bash
cp .env.example .env
# .env 파일에 한투 API 키 입력
```

### 2. 한국투자증권 API 키 발급
1. [한국투자증권 개발자센터](https://apiportal.koreainvestment.com) 접속
2. 앱 등록 → App Key / App Secret 발급
3. 모의투자 먼저 테스트 권장 (`KIS_IS_MOCK=true`)

### 3. Docker 실행
```bash
docker compose up -d
```

### 4. 대시보드 접속
- **프론트엔드**: http://localhost:3000
- **API 문서**: http://localhost:8000/docs

---

## 🆕 최근 반영 내용

### 1) 대시보드 데이터 일관성
- 프론트는 `/api/stocks/dashboard` 단일 스냅샷 API로 요약/보유목록을 함께 조회합니다.
- `summary.holdings_count`와 실제 보유 행 수가 다르면 자동 재조회(최대 3회) 후, 불일치 시 이전 화면을 유지합니다.

### 2) 해외 잔고 누락 완화
- 해외 잔고는 짧은 간격으로 2회 조회 후 종목 키(`ticker+exchange`) 기준 병합하여 일시 누락을 줄였습니다.

### 3) 시드 기준 수익률 계산 방식
- 시드 기준은 `output2` 전체가 아니라 스칼라 총자산 기준으로 계산합니다.
- 국내 `output2`의 `nass_amt` 또는 `tot_evlu_amt` + 해외주식(원화환산)으로 총자산을 구성합니다.
- 예수금(`dnca_tot_amt`)은 요약에 함께 표시됩니다.

### 4) 표시 형식
- 대시보드 금액/수익률/비율은 소수점 없이 정수로 표시합니다.

### 5) 수동매도 장중 제한 (프론트)
- 수동매도 버튼 클릭 시 장중 여부를 먼저 검사합니다.
- 한국장/미국장을 구분하여 장외면 팝업 후 주문 요청을 중단합니다.

### 6) 텔레그램 명령
- `/summary`: 시드 기준 + 보유(매입원가) 기준 요약
- `/overview` (`/현황`): 요약 + 보유 현황 2개 메시지 연속 발송

---

## ⚙️ 설정값

| 설정 | 기본값 | 설명 |
|------|--------|------|
| `stop_loss_pct` | -10.0 | 손절 기준 (%) |
| `trailing_trigger_pct` | 5.0 | 트레일링 발동 최소 수익률 (%) |
| `trailing_drop_pct` | 5.0 | 고점 대비 매도 하락폭 (%) |
| `take_profit_pct` | null | 목표가 (null=비활성) |
| `seed_money` | 1,000,000 | 기준 시드머니 (원) |
| `check_interval_minutes` | 5 | 체크 주기 (분) |

설정은 대시보드 UI 또는 API로 실시간 변경 가능합니다.

---

## 📁 프로젝트 구조

```
peakexit/
├── backend/
│   ├── app/
│   │   ├── main.py                  # FastAPI 앱
│   │   ├── api/
│   │   │   ├── stocks.py            # 종목/매도 API
│   │   │   ├── portfolio.py         # 포트폴리오 API
│   │   │   └── settings.py          # 설정 API
│   │   ├── core/
│   │   │   ├── sell_engine.py       # 매도 로직 엔진 ⭐
│   │   │   ├── scheduler.py         # 주기적 체크 스케줄러
│   │   │   └── state.py             # 설정/로그 영속화
│   │   └── services/
│   │       └── kis_client.py        # 한투 API 클라이언트
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── index.html                   # 대시보드 UI
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## 🔌 주요 API 엔드포인트

| Method | Path | 설명 |
|--------|------|------|
| GET | `/api/stocks/dashboard` | 요약+보유를 한 번에 반환(권장) |
| GET | `/api/stocks/holdings` | 보유종목 + 매도신호 분석 |
| GET | `/api/stocks/portfolio-summary` | 시드머니 기준 수익 요약 |
| POST | `/api/stocks/run-check` | 즉시 매도 검사 실행 |
| POST | `/api/stocks/manual-sell/{ticker}` | 수동 즉시 매도 |
| GET | `/api/portfolio/sell-log` | 매도 이력 |
| GET | `/api/settings/` | 현재 설정 조회 |
| PATCH | `/api/settings/` | 설정 변경 |

---

## ⚠️ 주의사항

- **반드시 모의투자로 먼저 테스트**하세요 (`KIS_IS_MOCK=true`)
- 장 시간(09:00~15:30, 평일)에만 자동 매도가 실행됩니다
- 수동매도는 프론트에서 장중 여부를 검사하며, 장외에는 진행되지 않습니다 (한국장/미국장 구분)
- 실전 투자 전 손절/트레일링 수치를 본인 투자 성향에 맞게 조정하세요
- 본 시스템은 투자 손실에 대한 책임을 지지 않습니다


---

## 🏠 내부 네트워크 전용 설정

### 접속 방법
미니 PC의 내부 IP를 확인한 뒤, 같은 공유기에 연결된 기기에서 접속합니다.

```bash
# 미니 PC에서 내부 IP 확인
ip addr show | grep "inet 192"
# 또는
hostname -I
```

브라우저에서 `http://192.168.x.x:3000` 으로 접속

### 네트워크 구조
```
[같은 공유기의 기기]
       │
       ▼  192.168.x.x:3000
  [nginx] ── allow 192.168.0.0/16
       │       deny all (외부 차단)
       ├──▶ [frontend 컨테이너]
       └──▶ [backend 컨테이너]
                  │
                  └──▶ [한투 API / 텔레그램 API]
```

### IP 대역 수정
공유기 설정에 따라 `nginx/nginx.conf`의 allow 대역을 조정하세요.

```nginx
allow 192.168.1.0/24;   # 특정 서브넷만 허용할 경우
```

### 미니 PC 고정 IP 설정 (권장)
공유기 관리 페이지에서 미니 PC의 MAC 주소에 고정 IP를 할당하면
재부팅 후에도 항상 같은 주소로 접근 가능합니다.

### 부팅 시 자동 시작
```bash
# systemd 서비스로 등록
sudo systemctl enable docker
# 또는 docker compose 자체를 서비스로 등록
sudo nano /etc/systemd/system/peakexit.service
```

```ini
[Unit]
Description=PeakExit Docker Service
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=/path/to/peakexit
ExecStart=/usr/bin/docker compose up
ExecStop=/usr/bin/docker compose down
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable peakexit
sudo systemctl start peakexit
```

---

## 📦 GitHub 업로드 전 체크리스트

- `.env`, 실제 API 키/시크릿, 계좌번호 등 민감정보는 커밋하지 않습니다.
- 필요 시 `.env.example`만 최신 키 목록으로 유지합니다.
- `data/`(실행 로그/상태 파일) 같은 런타임 산출물은 `.gitignore`에 포함합니다.
- `README.md`의 접속 주소/포트/운영 방식이 실제 배포 설정과 일치하는지 확인합니다.
