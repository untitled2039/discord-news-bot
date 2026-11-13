# 🗞️ 디스코드 데일리 뉴스봇

> **Workflow / AI Agent 개발 과제** 제출물
> 매일 아침 8시, 그날의 주요 뉴스를 **3줄 요약 + 카테고리 분류**해서 디스코드 채널로 보내주는 봇

---

### 과제 수행 내용과 이 프로젝트의 대응

| 과제 항목 | 이 프로젝트에서 수행한 내용 | 참고 섹션 |
|---|---|---|
| 1. 주제 선정 | 매일 뉴스를 일일이 찾아보는 번거로움 해결 → 데일리 뉴스 요약봇 | [1. 주제 선정](#1-주제-선정) |
| 2. 시스템 설계 | Workflow 방식 선택, 4단계 처리 흐름 설계 | [2. 시스템 설계](#2-시스템-설계) |
| 3. 기술 활용 | 텍스트 전처리·토큰화, 문장 임베딩, LLM, Few-shot 프롬프트, RAG, 워크플로 설계 | [3. 사용 기술](#3-사용-기술) |
| 4. 구현 및 테스트 | `news_bot.py` 구현, 모의 테스트 및 실제 실행 결과 확인 | [5. 실행 결과](#5-실행-결과) |

### 제출물 체크리스트

- [x] 프로젝트 소스코드 — `news_bot.py`, `.github/workflows/daily-news.yml`
- [x] README.md (프로젝트 소개, 사용 기술, 실행 방법) — 이 문서
- [x] 시스템 처리 흐름도 — [`docs/flowchart.png`](docs/flowchart.png)
- [ ] 실행 결과 (스크린샷 또는 시연 영상) — `docs/screenshot.png` *(실행 후 추가)*
- [x] API Key·비밀번호 미포함 — `.env`는 `.gitignore`로 제외, 키는 GitHub Secrets로 관리

---

## 1. 주제 선정

**문제**: 매일 여러 포털·언론사를 돌아다니며 뉴스를 확인하는 것이 번거롭고, 제목만 보고는 핵심 내용을 파악하기 어렵다.

**해결**: 정해진 시간에 주요 뉴스를 자동으로 모아 AI가 3줄로 요약하고, 평소 자주 쓰는 디스코드로 보내준다.

**선정 이유**
- **실용성**: 일상에서 매일 겪는 불편을 직접 해결하는 서비스
- **구현 현실성**: 데이터 수집 → 요약 → 전송까지 처리 흐름이 명확해, 프로젝트 기간 안에 안정적으로 동작하는 결과물을 만들 수 있음

---

## 2. 시스템 설계

### Workflow vs Agent 비교

| 구분 | **Workflow (채택)** | AI Agent |
|---|---|---|
| 동작 방식 | 수집 → 요약 → 전송 순서가 코드로 고정 | AI가 검색·선별 과정을 스스로 판단 |
| 장점 | 매일 정해진 시간에 오차 없이 같은 품질로 동작 | 특정 키워드 심층 검색, 중복 기사 필터링을 자율 수행 |
| 적합한 경우 | 지정된 언론사·RSS의 톱뉴스를 매일 아침 받아볼 때 | 사용자 관심사에 맞춰 AI가 직접 웹을 검색하고 큐레이션할 때 |

**Workflow를 선택한 이유**: 이 서비스의 핵심은 "매일 아침 빠짐없이, 일정한 품질로" 받아보는 것입니다. 따라서 흐름 제어는 전부 코드가 담당하고, LLM은 3단계의 **요약·분류 도구**로만 사용했습니다. Agent 방식의 장점인 중복 기사 필터링은 제목 유사도(2-gram + 문장 임베딩)로 구현해 Workflow 안에 포함했습니다.

### 시스템 처리 흐름도

![시스템 처리 흐름도](docs/flowchart.png)

```mermaid
flowchart TD
    A["⏰ 1단계 트리거<br/>GitHub Actions cron · 매일 08:00 KST"] --> B
    H[("🗂️ 전송 기록 (지식 베이스)<br/>제목 · 요약 · 임베딩 벡터")]
    subgraph B["📥 2단계 뉴스 수집"]
        B1["구글 뉴스 RSS<br/>주요·경제·IT/과학·세계·사회"] --> EM
        B2["네이버 뉴스 검색 API<br/>(선택, 관심 키워드)"] --> EM
        EM["🔢 제목 문장 임베딩<br/>Gemini Embedding"] --> B3
        B3["중복 제거: 2-gram + 임베딩 유사도<br/>이미 보낸 기사 제외 · 분야당 3건"]
    end
    H -. 최근 3일 보낸 기사 .-> B3
    B3 --> C["🧠 3단계 LLM 요약<br/>Few-shot · 3줄 요약 + 카테고리 분류"]
    C -->|성공| G["📌 오늘의 브리핑 생성 (RAG)<br/>관련 과거 기사 검색 → 맥락 추가 → 생성"]
    C -->|실패| E["RSS 발췌문으로 대체<br/>(브리핑 생략)"]
    H -. 관련 과거 기사 검색 .-> G
    G --> D["📤 4단계 결과 전송<br/>디스코드 웹훅 · 브리핑 + 카테고리별 임베드"]
    E --> D
    D -. 보낸 기사 저장 · 커밋 .-> H
```

### 단계별 처리 내용

| 단계 | 내용 | 구현 위치 |
|---|---|---|
| 1. 트리거 | GitHub Actions cron이 매일 23:00 UTC(= 08:00 KST)에 실행 | `.github/workflows/daily-news.yml` |
| 2. 수집 | 구글 뉴스 RSS 5개 분야 + 네이버 API(선택) → 24시간 이내 기사 → **제목 임베딩** → 중복 제거(2-gram + 임베딩) → **최근 3일 안에 이미 보낸 기사 제외** → 분야당 3건 | `collect()`, `embed()`, `load_history()` |
| 3. 요약 | 선정 기사를 5건씩 **Few-shot 프롬프트**로 LLM에 전달 → 기사별 3줄 요약 + 7개 카테고리 분류(JSON) → **관련 과거 기사를 검색(RAG)해 "오늘의 브리핑" 생성** | `summarize()`, `retrieve_context()`, `make_briefing()` |
| 4. 전송 | 브리핑을 머리말로, 카테고리별 색상 임베드로 디스코드 웹훅 전송 (한도 초과 시 자동 분할) → **보낸 기사를 전송 기록에 저장** | `send_discord()`, `save_history()` |

### 주요 기능

**📌 오늘의 한 줄 브리핑**
기사별 3줄 요약이 끝나면, 그 요약들을 LLM에 한 번 더 전달해 오늘 뉴스의 흐름을 2~3문장으로 정리합니다. 메시지 맨 위에 표시되어 기사를 다 읽지 않아도 하루 이슈를 파악할 수 있습니다. 요약이 하나도 성공하지 못했거나 시간 제한을 넘기면 브리핑만 생략하고 나머지는 정상 전송합니다.

**🗂️ 이미 보낸 기사 다시 안 보내기**
큰 사건은 여러 날 연속 헤드라인에 오르기 때문에, 하루 단위 중복 제거만으로는 같은 기사를 며칠씩 받게 됩니다. 보낸 기사의 제목·링크를 `data/sent_history.json`에 남기고, 다음 실행 때 **같은 링크이거나 제목이 유사한 기사**는 제외합니다. "금리 동결" → "금리 동결 이후 환율 급등"처럼 새로운 전개가 담긴 기사는 제목이 충분히 달라 그대로 전송됩니다.
GitHub Actions는 실행할 때마다 새 환경에서 시작하므로, 실행 후 기록 파일을 저장소에 자동 커밋해 다음 실행으로 상태를 이어갑니다. 기록은 최근 3일 치만 유지됩니다.

**🔢 문장 임베딩으로 중복 기사 판별**
기존 2-gram 방식은 글자가 겹쳐야 같은 기사로 봅니다. 그래서 `한국은행 기준금리 3.0% 동결`과 `한은, 금리 묶었다`처럼 **뜻은 같고 표현이 다른 기사**를 놓칩니다. 제목을 Gemini 임베딩 모델로 256차원 벡터로 바꾸고, **코사인 유사도 0.88 이상**이면 같은 사건으로 판단합니다. 2-gram은 빠르고 확실한 글자 중복을, 임베딩은 의미 중복을 맡아 둘 중 하나라도 걸리면 제외합니다. 임베딩 API를 쓸 수 없으면 자동으로 2-gram만 사용합니다. 실행 로그에 `≈ 의미 중복(0.91): ...`처럼 유사도와 함께 기록되어 기준값을 조정할 수 있습니다.

**🎯 Few-shot 프롬프트**
요약 프롬프트에 가상의 예시 기사 2개와 모범 출력을 넣었습니다(In-Context Learning). 예시로 ① `~했다` 체, 40자 안팎의 문체, ② 발췌문에 있는 내용만 쓰기, ③ 헷갈리기 쉬운 분류 경계(국회 입법 → 정치, 부동산 시세 → 사회가 아닌 경제)를 보여 줍니다. `PROMPT_MODE=zeroshot`으로 바꾸면 예시 없이 실행되어 두 방식을 비교할 수 있습니다.

**🔗 전송 기록을 활용한 RAG 브리핑**
전송 기록에 제목·링크뿐 아니라 **요약과 임베딩 벡터**도 저장해, 최근 3일 치 뉴스를 작은 지식 베이스로 씁니다. 브리핑을 만들 때 오늘 기사와 **코사인 유사도 0.72 이상**인 과거 기사를 최대 3건 검색(Retrieve)해 프롬프트에 참고 자료로 덧붙이고(Augment), LLM이 "어제 금리 동결에 이어 오늘은 환율이 급등했다"처럼 흐름을 이어 브리핑을 생성(Generate)합니다. 같은 사건(0.88 이상)은 2단계에서 이미 제외되므로, 검색되는 것은 "관련은 있지만 새로운 전개"인 기사입니다.

### 안정성 장치
- 피드 하나가 실패해도 나머지로 계속 진행
- 429/5xx 응답은 자동 재시도 (지수 백오프)
- 기사를 5건씩 나눠 요약 요청 → 응답이 빠르고, 한 묶음이 실패해도 나머지는 요약됨
- Gemini 3 계열의 추론(thinking) 강도를 `low`로 낮춰 응답 시간 단축 (요약에는 깊은 추론이 불필요)
- 503(과부하)은 간격을 늘려 재시도, 시간 초과는 1회만 재시도 후 가볍고 빠른 lite 모델부터 다른 Flash 모델로 자동 전환
- 지원 종료된 모델이면 오류 메시지가 안내하는 새 모델로 자동 교체
- 요약 단계에 시간 제한(기본 7분)을 두고, 모든 모델이 실패하면 RSS 발췌문으로 대체해서라도 전송
- API 키는 URL이 아닌 요청 헤더로 보내 로그에 노출되지 않음
- 디스코드 한도(메시지당 임베드 10개, 6000자)에 맞춰 메시지 자동 분할

---

## 3. 사용 기술

| 분류 | 기술 | 용도 |
|---|---|---|
| 언어 | Python 3.9+ (표준 라이브러리만 사용) | 전체 로직 |
| 스케줄러 | GitHub Actions `schedule` (cron) | 1단계 트리거 |
| 데이터 수집 | 구글 뉴스 RSS, 네이버 뉴스 검색 API | 2단계 수집 |
| 텍스트 처리 | XML 파싱, HTML 태그·말머리 제거, 문자 2-gram 자카드 유사도 | 전처리, 글자 기준 중복 제거 |
| 문장 임베딩 | Gemini Embedding API (`gemini-embedding-001`, 256차원), 코사인 유사도 | 의미 기준 중복 제거, RAG 검색 |
| LLM | Google Gemini API (`gemini-3.8-flash`) / OpenAI API (`gpt-4o-mini`) | 3단계 요약·분류 |
| 프롬프트 엔지니어링 | 역할 지정, Few-shot 예시, 출력 형식(JSON) 강제, 환각 방지 지시 | 3단계 요약·브리핑 품질 |
| RAG | 전송 기록(요약 + 벡터) 검색 → 브리핑 프롬프트에 맥락 추가 | 이전 보도와 이어지는 브리핑 |
| 상태 저장 | JSON 파일 + GitHub Actions 자동 커밋 | 재전송 방지, RAG 지식 베이스 |
| 메신저 연동 | Discord Webhook (Embed) | 4단계 전송 |
| 보안 | GitHub Secrets, `.env` + `.gitignore` | API 키 보호 |

### 수업 내용 적용

| 수업 내용 | 프로젝트 적용 | 구현 위치 |
|---|---|---|
| NLP 응용 분야 | 텍스트 요약(기사별 3줄), 텍스트 분류(7개 카테고리), 다중 문서 요약(오늘의 브리핑) | `summarize()`, `make_briefing()` |
| 텍스트 전처리와 토큰화 | HTML 태그·특수문자 정리, `[속보]` 등 말머리 제거, 문자 2-gram 토큰화로 한국어 조사 변화에 대응 | `clean()`, `norm()`, `bigrams()` |
| 워드·문장 임베딩 | 기사 제목을 문장 임베딩 벡터로 변환, 코사인 유사도로 의미 중복 판별 | `embed()`, `cosine()`, `collect()` |
| Transformer와 LLM 구조 | 추론(thinking) 강도 조절, `temperature` 0.2, JSON 출력 모드, 모델 과부하 시 자동 전환 | `_gemini_request()`, `call_gemini()` |
| 프롬프트 엔지니어링과 In-Context Learning | 역할 지정, **Few-shot** 예시(문체·분류 경계), 출력 형식 제한, 환각 방지 지시. Zero-shot과 전환 가능 | `PROMPT`, `FEW_SHOT_EXAMPLES` |
| 질의응답 시스템과 RAG | 전송 기록을 지식 베이스로 삼아 관련 과거 기사를 벡터 검색해 브리핑 생성에 활용 | `retrieve_context()`, `make_briefing()` |
| 워크플로우와 에이전트 비교 | Workflow 방식 채택. 흐름은 코드가 고정하고 LLM은 요약·분류 도구로 사용 | 전체 구조 |
| AI 에이전트 시스템 설계 (모델, Tool) | 작업 성격에 맞는 모델 역할 분리: 생성 모델(요약·브리핑)과 임베딩 모델(유사도·검색) | `call_gemini()`, `embed()` |

**검토했으나 적용하지 않은 것: CoT · ToT**
뉴스 3줄 요약은 여러 단계를 거쳐 추론해야 하는 문제가 아니어서, 단계별 추론(CoT)이나 여러 경로를 탐색하는 추론(ToT)을 써도 품질 이득이 적습니다. 오히려 응답 시간과 비용이 늘어납니다. 실제로 기본 추론 설정에서 응답 시간 초과가 발생해, 반대로 추론 강도를 낮춰(`thinkingLevel: low`) 문제를 해결했습니다.

---

## 4. 실행 방법

### 4-1. 키 준비
1. **디스코드 웹훅**: 채널 설정 → 연동 → 웹후크 → 새 웹후크 → *웹후크 URL 복사*
2. **Gemini API 키**: https://aistudio.google.com/apikey (무료 티어로 충분)
3. *(선택)* **네이버 API**: https://developers.naver.com → 애플리케이션 등록 → 검색 API

### 4-2. 로컬 실행
```bash
git clone <이 저장소 URL>
cd news-bot
cp .env.example .env     # .env 파일에 키 입력
python news_bot.py
```
`DISCORD_WEBHOOK_URL`을 비워두면 디스코드 대신 콘솔에 결과만 출력합니다.

### 4-3. 매일 자동 실행 (GitHub Actions, 무료)
1. 저장소 **Settings → Secrets and variables → Actions**
   - **Secrets**: `DISCORD_WEBHOOK_URL`, `GEMINI_API_KEY` (선택: `OPENAI_API_KEY`, `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`)
   - **Variables** (선택): `LLM_PROVIDER`(`gemini`/`openai`), `GEMINI_MODEL`(기본 `gemini-3.8-flash`), `NAVER_QUERIES`(예: `반도체,인공지능`), `PROMPT_MODE`(`fewshot`/`zeroshot`), `DEDUP_MODE`(`embedding`/`bigram`)
   - 전송 기록 커밋을 위해 워크플로에 `contents: write` 권한이 설정되어 있습니다. 실행 후 `Save sent history` 단계가 권한 오류로 실패하면 **Settings → Actions → General → Workflow permissions**를 *Read and write permissions*로 바꿔 주세요.

> 💡 Gemini 모델은 주기적으로 지원이 종료됩니다. 로그에 `model ... is no longer available` 오류가 보이면 코드를 고칠 필요 없이 **Variables에 `GEMINI_MODEL`을 오류 메시지가 안내하는 새 모델명으로 등록**하면 됩니다.
2. **Actions 탭 → Daily News Bot → Run workflow**로 즉시 테스트
3. 이후 매일 08시 무렵 자동 실행 (GitHub 사정상 수 분~십여 분 늦을 수 있음)

> 💡 공개 저장소는 60일간 커밋이 없으면 예약 실행이 자동 중지됩니다. 알림 메일이 오면 Actions 탭에서 다시 활성화하세요.

**대안 스케줄러**: 항상 켜진 PC나 라즈베리파이가 있다면 cron으로도 실행할 수 있습니다.
```
0 8 * * * cd /path/to/news-bot && /usr/bin/python3 news_bot.py >> bot.log 2>&1
```

### 4-4. 커스터마이즈

| 하고 싶은 것 | 수정 위치 |
|---|---|
| 수집 분야/언론사 RSS 변경 | `news_bot.py`의 `SECTIONS` (예: 연합뉴스 `https://www.yna.co.kr/rss/news.xml`) |
| 기사 수 조절 | `ARTICLES_PER_SECTION` 환경변수 |
| Gemini 대체 모델 지정 | `GEMINI_FALLBACK_MODELS` (쉼표 구분, 비우면 자동 탐색) |
| 요약 묶음 크기 / 시간 제한 | `SUMMARY_BATCH` / `LLM_TIME_BUDGET` |
| Gemini 추론 강도 | `GEMINI_THINKING` (`low` 기본 / `default`) |
| 오늘의 브리핑 끄기 | `BRIEFING=off` |
| 재전송 방지 기간 | `HISTORY_DAYS` (기본 3일) |
| Zero-shot / Few-shot 전환 | `PROMPT_MODE` (`fewshot` 기본 / `zeroshot`) |
| 임베딩 사용 여부 | `DEDUP_MODE` (`embedding` 기본 / `bigram`) |
| 의미 중복 · RAG 유사도 기준 | `EMBED_DUP_THRESHOLD` (기본 0.88) / `RAG_THRESHOLD` (기본 0.72), `RAG_TOP_K` (기본 3) |
| 전송 시간 변경 | `daily-news.yml`의 cron (UTC 기준, KST −9시간) |
| 요약 스타일 | `news_bot.py`의 `PROMPT` |
| 카테고리 | `CATEGORIES`, `CATEGORY_STYLE` |

---

## 5. 실행 결과

### 디스코드 수신 화면

<!-- 실제 실행 후 디스코드 화면을 캡처해 docs/screenshot.png 로 저장하세요 -->
![디스코드 실행 결과](docs/screenshot.png)

### 실행 로그 
⏰ [1단계] 실행 시작 2026-11-12 19:51 KST
📥 [2단계] 뉴스 수집
<<<<<<< HEAD
  · 주요뉴스: 38건
  · 경제: 35건
  · IT/과학: 34건
  · 세계: 36건
  · 사회: 37건
  · 제목 75건 임베딩 (gemini-embedding-001, 256차원)
  ≈ 의미 중복(0.91): '한은, 금리 묶었다… 3연속' ~ '한국은행 기준금리 3.0% 동결'
  · 중복 제거: 2-gram 6건, 임베딩 추가 3건
  · 최근 3일 내 이미 보낸 기사 4건 제외
  → 최종 15건 선정
🧠 [3단계] LLM 요약 (gemini, Few-shot)
  → 15/15건 요약 완료
  · RAG 관련 과거 기사(0.78): '한국은행 기준금리 3.0% 동결' ↔ 오늘 '한국은행 금리 동결 이후 환율 급등'
  · 오늘의 브리핑 생성 (142자, 과거 기사 1건 참고)
📤 [4단계] 디스코드 전송
  → 1개 메시지 전송 완료
  · 전송 기록 저장: data/sent_history.json (최근 3일, 45건)
=======
  · 주요뉴스: 32건
  · 경제: 51건
  · IT/과학: 18건
  · 세계: 29건
  · 사회: 51건
  → 중복 제거 후 15건 선정
🧠 [3단계] LLM 요약 (gemini)
  · 사용 모델: gemini-3.8-flash
  ↻ HTTP 503, 2s 후 재시도 (1/4)
  ↻ HTTP 503, 6s 후 재시도 (2/4)
  ↻ HTTP 429, 16s 후 재시도 (3/4)
  ⚠ 'gemini-3.8-flash' 응답 없음/과부하: HTTP 503 — 4회 시도 실패 (https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent)
  → 'gemini-3.5-flash-lite'(으)로 전환
  · gemini-3.5-flash-lite 응답 2s
  · 묶음 1/3: 5/5건 요약
  · gemini-3.5-flash-lite 응답 3s
  · 묶음 2/3: 5/5건 요약
  · gemini-3.5-flash-lite 응답 2s
  · 묶음 3/3: 5/5건 요약
  → 15/15건 요약 완료 (최종 모델: gemini-3.5-flash-lite)
📤 [4단계] 디스코드 전송
  → 2개 메시지 전송 완료
>>>>>>> 9bcb5292f25e260891a9ed62304570a591377351
✅ 완료

*(건수는 실행 시점에 따라 달라집니다.)*

### 테스트 내용
| 테스트 | 방법 | 결과 |
|---|---|---|
| 전체 흐름(수집→요약→전송) | 네트워크 응답을 모의(mock) 데이터로 대체해 실행 | ✅ 정상 동작 |
| 24시간 지난 기사 제외 | 오래된 날짜의 기사 포함 RSS 입력 | ✅ 제외됨 |
| 중복 기사 제거 | `[속보] 한국은행 기준금리 3.0% 동결` vs `한국은행, 기준금리 3.0%로 동결` | ✅ 중복으로 판정 |
| LLM 실패 대비 | API 오류(403) 강제 발생 | ✅ 발췌문으로 대체 전송 |
| 모델 지원 종료 대응 | `models/... is no longer available` 404 응답 모의 | ✅ 안내된 새 모델로 자동 교체 |
| 모델 과부하 대응 | 기본 모델에 503 응답 지속 | ✅ 대체 Flash 모델로 전환 후 12/12건 요약 |
| 응답 지연 대응 | 기본 모델 시간 초과 + 2순위 모델 503 | ✅ lite 모델로 전환 후 15/15건 요약 |
| 전체 모델 장애 | 모든 모델 503 | ✅ 시간 내 중단, 발췌문으로 대체 전송 |
| 오늘의 브리핑 | 요약 완료 후 브리핑 생성 | ✅ 메시지 머리말에 표시 |
| 브리핑 실패 대비 | 모든 모델 503 | ✅ 브리핑만 생략, 나머지 전송 |
| 재전송 방지 | 2일 연속 실행: 전날 기사 재보도(`[속보] 한국은행, 기준금리 3.0%로 동결`) + 같은 기사 + 새 기사 2건 | ✅ 전날 기사 2건 제외, 새 기사 2건만 전송 |
| 새 전개 기사 유지 | 전날 "기준금리 동결" 전송 후 "금리 동결 이후 환율 급등" | ✅ 다른 기사로 판단해 전송 |
| 로컬 테스트 시 기록 보호 | 웹훅 없이 콘솔 출력만 실행 | ✅ 전송 기록 변경 없음 |
| 임베딩 의미 중복 | `한국은행 기준금리 3.0% 동결` vs `한은, 금리 묶었다… 3연속` (2-gram으로는 미검출) | ✅ 임베딩으로 중복 판정 |
| 이미 보낸 기사와 의미 중복 | 전날 `한국은행 기준금리 3.0% 동결` 전송 후 `한은 기준금리 묶어… 동결 결정` | ✅ 재전송 제외 |
| 임베딩 실패 대비 | 임베딩 API 503 | ✅ 2-gram 방식만으로 정상 동작 |
| Few-shot / Zero-shot 전환 | `PROMPT_MODE` 변경 후 프롬프트 확인 | ✅ 예시 포함 여부 전환 |
| RAG 검색 | 전날 "기준금리 동결" 기록 후 오늘 "금리 동결 이후 환율 급등" | ✅ 관련 과거 기사 1건 검색, 브리핑 프롬프트에 맥락 추가 |
| 실제 운영 | GitHub Actions 수동 실행 | *(스크린샷 첨부)* |

---

## 6. 파일 구성
```
news-bot/
├── news_bot.py                       # 1~4단계 전체 로직
├── .env.example                      # 환경변수 템플릿 (실제 키 없음)
├── .gitignore                        # .env 등 민감 파일 제외
├── .github/workflows/daily-news.yml  # 매일 08:00 KST 스케줄 + 전송 기록 커밋
├── data/
│   └── sent_history.json             # 최근 3일간 보낸 기사 (제목·요약·임베딩, RAG 지식 베이스)
├── docs/
│   ├── flowchart.png                 # 시스템 처리 흐름도
│   ├── flowchart.mmd                 # 흐름도 원본 (Mermaid)
│   └── screenshot.png                # 실행 결과 화면 (추가 예정)
└── README.md
```

---

## 7. 보안 유의사항
- API Key, 웹훅 URL 등 민감한 정보는 **저장소에 업로드하지 않습니다.**
- 로컬에서는 `.env` 파일(`.gitignore`로 제외), GitHub Actions에서는 **Secrets**로만 관리합니다.
- 디스코드 웹훅 URL이 유출되면 누구나 채널에 메시지를 보낼 수 있으므로, 유출 시 즉시 웹훅을 삭제하고 새로 발급하세요.

---

## 8. 참고 자료 및 출처

| 항목 | 출처 | 활용 내용 |
|---|---|---|
| 구글 뉴스 RSS | https://news.google.com/rss | 분야별 헤드라인 수집 |
| 네이버 뉴스 검색 API | https://developers.naver.com/docs/serviceapi/search/news/news.md | 키워드 뉴스 수집 (선택) |
| Gemini API | https://ai.google.dev/gemini-api/docs | 기사 요약·분류, 브리핑 |
| Gemini Embedding API | https://ai.google.dev/gemini-api/docs/embeddings | 제목 임베딩 (중복 제거, RAG 검색) |
| OpenAI API | https://platform.openai.com/docs | 기사 요약·분류 (대체) |
| Discord Webhook | https://discord.com/developers/docs/resources/webhook | 결과 전송 |
| GitHub Actions | https://docs.github.com/actions | 매일 자동 실행 |
| **AI 도구** | **Claude (Anthropic)** | 코드 초안 작성, 테스트 코드 작성, README 구성 보조 |

- 요약은 RSS의 **제목과 발췌문**만 근거로 생성합니다. 기사 본문 전체를 수집하지 않아 언론사 저작권 문제를 피하며, 원문은 각 제목 링크로 확인합니다.
- 별도의 외부 오픈소스 라이브러리는 사용하지 않았습니다 (Python 표준 라이브러리만 사용).
