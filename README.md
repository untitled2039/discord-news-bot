# 🗞️ 디스코드 데일리 뉴스봇

매일 아침 8시, 그날의 주요 뉴스를 **3줄 요약 + 카테고리 분류**해서 디스코드 채널로 보내주는 봇입니다.
뉴스를 일일이 찾아보는 번거로움을 없애는 것이 목표입니다.

## 설계 방식: Workflow

| 구분 | **Workflow (채택)** | Agent |
|---|---|---|
| 동작 | 수집 → 요약 → 전송 순서가 코드로 고정 | AI가 검색·선별을 스스로 판단 |
| 장점 | 매일 같은 시간, 같은 품질로 오차 없이 동작 | 키워드 심층 검색, 중복 필터링을 자율 수행 |
| 적합한 경우 | 지정 언론사·RSS 톱뉴스를 매일 받아볼 때 | 개인 관심사에 맞춘 큐레이션 |

이 프로젝트는 "매일 정해진 시간에 안정적으로 동작"이 핵심이므로 Workflow 방식을 택했습니다.
LLM은 판단 주체가 아니라 **3단계의 요약·분류 도구**로만 쓰이고, 흐름 제어는 전부 코드가 담당합니다.
Agent 방식의 장점 중 하나인 중복 기사 필터링은 규칙 기반(제목 유사도)으로 구현해 Workflow 안에 넣었습니다.

## 처리 흐름

```
[1단계 트리거] GitHub Actions cron ─ 매일 23:00 UTC = 08:00 KST
      ↓
[2단계 수집]  구글 뉴스 RSS 5개 분야(주요·경제·IT/과학·세계·사회)
             + 네이버 뉴스 검색 API (선택, 관심 키워드)
             → 24시간 이내 기사만 → 유사 제목 중복 제거 → 분야당 3건
      ↓
[3단계 요약]  Gemini(기본) 또는 OpenAI에 한 번의 호출로 전달
             → 기사별 3줄 요약 + 7개 카테고리 분류 (JSON 응답)
      ↓
[4단계 전송]  디스코드 웹훅 → 카테고리별 색상 임베드
```

**안정성 장치**
- 피드 하나가 실패해도 나머지로 계속 진행
- 429/5xx 응답은 자동 재시도 (지수 백오프)
- LLM 호출이 실패하면 RSS 발췌문으로 대체해서라도 전송
- 디스코드 한도(메시지당 임베드 10개, 6000자)에 맞춰 자동 분할

## 설치 · 실행

외부 패키지가 필요 없습니다 (Python 3.9+ 표준 라이브러리만 사용).

### 1. 키 준비
1. **디스코드 웹훅**: 채널 설정 → 연동 → 웹후크 → 새 웹후크 → *웹후크 URL 복사*
2. **Gemini API 키**: https://aistudio.google.com/apikey (무료 티어로 충분)
3. *(선택)* 네이버 API: https://developers.naver.com → 애플리케이션 등록 → 검색 API

### 2. 로컬 테스트
```bash
cp .env.example .env     # 값 채우기
python news_bot.py
```
`DISCORD_WEBHOOK_URL`을 비워두면 디스코드 대신 콘솔에 결과만 출력합니다.

### 3. 매일 자동 실행 (GitHub Actions, 무료)
1. 이 폴더를 GitHub 저장소로 push
2. 저장소 **Settings → Secrets and variables → Actions**
   - Secrets: `DISCORD_WEBHOOK_URL`, `GEMINI_API_KEY` (선택: `OPENAI_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`)
   - Variables (선택): `LLM_PROVIDER`(`gemini`/`openai`), `NAVER_QUERIES`(예: `반도체,인공지능`)
3. **Actions 탭 → Daily News Bot → Run workflow** 로 바로 한 번 테스트
4. 이후 매일 08시 경 자동 실행 (GitHub 사정상 수 분~십여 분 늦을 수 있음)

> 💡 공개 저장소는 60일간 커밋이 없으면 예약 실행이 자동 중지됩니다. 메일 알림이 오면 Actions 탭에서 다시 켜주세요.

**대안 스케줄러**: 항상 켜진 PC나 라즈베리파이가 있다면 cron으로도 됩니다.
```
0 8 * * * cd /path/to/news-bot && /usr/bin/python3 news_bot.py >> bot.log 2>&1
```

## 커스터마이즈

| 하고 싶은 것 | 수정 위치 |
|---|---|
| 수집 분야/언론사 RSS 변경 | `news_bot.py`의 `SECTIONS` (예: 연합뉴스 `https://www.yna.co.kr/rss/news.xml`) |
| 기사 수 조절 | `ARTICLES_PER_SECTION` 환경변수 |
| 전송 시간 변경 | `daily-news.yml`의 cron (UTC 기준, KST −9시간) |
| 요약 스타일 | `news_bot.py`의 `PROMPT` |
| 카테고리 | `CATEGORIES`, `CATEGORY_STYLE` |

## 파일 구성
```
news-bot/
├── news_bot.py                     # 1~4단계 전체 로직
├── .env.example                    # 환경변수 템플릿
├── .github/workflows/daily-news.yml  # 매일 08:00 KST 스케줄
└── README.md
```

## 참고
- 요약은 RSS의 제목·발췌문을 근거로 생성합니다. 기사 본문 전체를 긁어오지 않아 언론사 저작권·차단 문제를 피하며, 원문은 각 제목 링크로 확인합니다.
- `.env`와 API 키는 절대 커밋하지 마세요 (`.gitignore` 포함).
