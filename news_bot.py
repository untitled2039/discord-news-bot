"""
디스코드 데일리 뉴스봇 (Workflow 방식)

  [1단계 트리거]  GitHub Actions cron (매일 08:00 KST)  ─ 또는 로컬 cron / 작업 스케줄러
        │
  [2단계 수집]    구글 뉴스 RSS(분야별) + 네이버 뉴스 검색 API(선택)
        │         → 중복 제거 → 분야당 상위 N개
  [3단계 요약]    Gemini 또는 OpenAI 에 한 번에 전달
        │         → 기사별 3줄 요약 + 카테고리 분류 (JSON)
  [4단계 전송]    디스코드 웹훅으로 카테고리별 임베드 전송

외부 패키지 없이 파이썬 표준 라이브러리만 사용합니다 (Python 3.9+).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (DailyNewsBot/1.0)"

# ─────────────────────────────── 설정 ───────────────────────────────

def load_dotenv(path: str = ".env") -> None:
    """로컬 실행용 .env 로더 (이미 설정된 환경변수는 덮어쓰지 않음)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = re.split(r"\s+#", v, maxsplit=1)[0]   # 줄 끝 주석 제거
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()          # gemini | openai | none
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# 빈 문자열로 넘어와도 기본값을 쓰도록 `or` 사용 (GitHub Variables 미설정 시 "" 전달됨)
GEMINI_MODEL = os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
# 기본 모델이 과부하(503)일 때 차례로 시도할 모델. 비워두면 API에서 사용 가능한 Flash 모델을 자동 탐색
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",") if m.strip()]
SUMMARY_BATCH = int(os.getenv("SUMMARY_BATCH", "5"))   # LLM 1회 호출당 기사 수
# 추론(thinking) 강도: low(기본, 빠름) | default(모델 기본값). 요약엔 깊은 추론이 필요 없음
GEMINI_THINKING = (os.getenv("GEMINI_THINKING") or "low").lower()
LLM_TIME_BUDGET = int(os.getenv("LLM_TIME_BUDGET", "420"))   # 요약 단계 최대 소요 시간(초)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")
NAVER_QUERIES = [q.strip() for q in os.getenv("NAVER_QUERIES", "").split(",") if q.strip()]
PER_SECTION = int(os.getenv("ARTICLES_PER_SECTION", "3"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))
# 전송 기록: 최근 N일 안에 보낸 기사(같은 링크 또는 유사 제목)는 다시 보내지 않음
SENT_HISTORY_FILE = os.getenv("SENT_HISTORY_FILE") or "data/sent_history.json"
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS") or "3")
BRIEFING = (os.getenv("BRIEFING") or "on").lower() != "off"   # 오늘의 한 줄 브리핑 on/off
# 문장 임베딩 (중복 제거 + RAG 검색에 사용). 실패하면 자동으로 2-gram 방식만 사용
DEDUP_MODE = (os.getenv("DEDUP_MODE") or "embedding").lower()       # embedding | bigram
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL") or "gemini-embedding-001"
EMBED_DIM = int(os.getenv("EMBED_DIM") or "256")                     # 벡터 차원 (기록 파일 크기 절약)
EMBED_DUP_THRESHOLD = float(os.getenv("EMBED_DUP_THRESHOLD") or "0.88")   # 이 이상이면 같은 사건
RAG_THRESHOLD = float(os.getenv("RAG_THRESHOLD") or "0.72")              # 이 이상이면 관련 과거 기사
RAG_TOP_K = int(os.getenv("RAG_TOP_K") or "3")
EMBED_PER_SECTION = 15   # 분야별 상위 몇 개까지 임베딩할지 (API 호출량 제한)
# 요약 프롬프트 방식: fewshot(예시 2개 포함, 기본) | zeroshot
PROMPT_MODE = (os.getenv("PROMPT_MODE") or "fewshot").lower()

# 구글 뉴스 한국판 분야별 RSS (토픽 ID는 구글 뉴스 공식 섹션)
GN = "https://news.google.com/rss"
GN_PARAMS = "hl=ko&gl=KR&ceid=KR:ko"
SECTIONS: dict[str, str] = {
    "주요뉴스": f"{GN}?{GN_PARAMS}",
    "경제":     f"{GN}/headlines/section/topic/BUSINESS?{GN_PARAMS}",
    "IT/과학":  f"{GN}/headlines/section/topic/TECHNOLOGY?{GN_PARAMS}",
    "세계":     f"{GN}/headlines/section/topic/WORLD?{GN_PARAMS}",
    "사회":     f"{GN}/headlines/section/topic/NATION?{GN_PARAMS}",
}

CATEGORIES = ["정치", "경제", "사회", "IT/과학", "세계", "문화/생활", "스포츠"]
CATEGORY_STYLE = {   # 이모지, 임베드 색상
    "정치": ("🏛️", 0x5865F2), "경제": ("💰", 0x57F287), "사회": ("👥", 0xFEE75C),
    "IT/과학": ("💻", 0x3BA55D), "세계": ("🌏", 0xEB459E), "문화/생활": ("🎨", 0xF9A62B),
    "스포츠": ("⚽", 0xED4245), "기타": ("📰", 0x99AAB5),
}

# ─────────────────────────────── 공통 HTTP ───────────────────────────────

class Overloaded(RuntimeError):
    """재시도를 다 써도 429/5xx/시간 초과가 계속된 경우 (다른 모델로 넘길 신호)."""


def http(url: str, data: dict | None = None, headers: dict | None = None,
         retries: int = 3, timeout: int = 60, backoff: float = 2.0,
         max_timeouts: int | None = None) -> bytes:
    """429/5xx/네트워크 오류는 재시도. max_timeouts 를 주면 시간 초과는 그 횟수까지만 시도
    (응답이 느린 모델에 같은 요청을 반복해 봐야 시간만 낭비되므로)."""
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": UA, **(headers or {})}
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    last, timeouts = "", 0
    for attempt in range(1, retries + 1):
        wait = backoff ** attempt
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if not (e.code == 429 or e.code >= 500):   # 4xx 는 재시도해도 소용없음
                raise RuntimeError(f"HTTP {e.code}: {e.read()[:1000].decode(errors='ignore')}") from e
            wait = float(e.headers.get("Retry-After") or wait)
            last = f"HTTP {e.code}"
        except (urllib.error.URLError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            last = f"네트워크 오류 {reason}"
            if "timed out" in str(reason):
                timeouts += 1
                last = f"{timeout}s 안에 응답 없음"
                if max_timeouts is not None and timeouts >= max_timeouts:
                    raise Overloaded(f"{last} ({timeouts}회)") from e
                wait = 1
        if attempt < retries:
            print(f"  ↻ {last}, {wait:.0f}s 후 재시도 ({attempt}/{retries})")
            time.sleep(wait)
    # URL 쿼리(키 등)는 로그에 남기지 않음
    raise Overloaded(f"{last} — {retries}회 시도 실패 ({url.split('?')[0]})")

# ─────────────────────────────── 2단계: 수집 ───────────────────────────────

def clean(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def fetch_rss(section: str, url: str) -> list[dict]:
    root = ET.fromstring(http(url))
    now = datetime.now(timezone.utc)
    items = []
    for it in root.iter("item"):
        title = clean(it.findtext("title"))
        src_el = it.find("source")
        source = clean(src_el.text) if src_el is not None and src_el.text else ""
        # 구글 뉴스 제목은 "제목 - 언론사" 형태 → 언론사 분리
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        pub = it.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            dt = None
        if dt and now - dt > timedelta(hours=MAX_AGE_HOURS):
            continue
        items.append({
            "title": title, "link": (it.findtext("link") or "").strip(),
            "source": source, "section": section,
            "desc": clean(it.findtext("description"))[:300],
            "published": dt.isoformat() if dt else "",
        })
    return items


def fetch_naver(query: str) -> list[dict]:
    url = ("https://openapi.naver.com/v1/search/news.json?"
           + urllib.parse.urlencode({"query": query, "display": 10, "sort": "date"}))
    raw = http(url, headers={"X-Naver-Client-Id": NAVER_CLIENT_ID,
                             "X-Naver-Client-Secret": NAVER_CLIENT_SECRET})
    out = []
    for it in json.loads(raw).get("items", []):
        out.append({
            "title": clean(it["title"]),
            "link": it.get("originallink") or it["link"],
            "source": urllib.parse.urlparse(it.get("originallink") or it["link"]).netloc,
            "section": f"#{query}", "desc": clean(it.get("description"))[:300],
            "published": it.get("pubDate", ""),
        })
    return out


def norm(title: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", re.sub(r"\[[^\]]*\]", "", title)).lower()


def bigrams(t: str) -> set[str]:
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def is_dup(t: str, seen: list[str], threshold: float = 0.55) -> bool:
    """같은 사건을 다룬 다른 언론사 기사 제거 (글자 2-gram 자카드 유사도)."""
    bt = bigrams(t)
    for s in seen:
        bs = bigrams(s)
        if t == s or len(bt & bs) / len(bt | bs) >= threshold:
            return True
    return False


# ─────────────────────────────── 문장 임베딩 ───────────────────────────────
# 제목을 의미 벡터로 바꿔 코사인 유사도로 비교한다.
#  · 2-gram 은 글자가 겹쳐야 같다고 보지만("기준금리 동결" ≈ "기준금리 3.0% 동결"),
#    임베딩은 표현이 달라도 뜻이 같으면 가깝게 나온다("기준금리 동결" ≈ "금리 묶었다").

def _unit(v: list[float]) -> list[float]:
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))   # 정규화된 벡터끼리는 내적 = 코사인 유사도


_embed_model: str | None = None
_embed_dim_ok = True


def _embed_request(model: str, texts: list[str]) -> list[list[float]]:
    reqs = []
    for t in texts:
        r = {"model": f"models/{model}", "content": {"parts": [{"text": t}]},
             "taskType": "SEMANTIC_SIMILARITY"}
        if _embed_dim_ok:
            r["outputDimensionality"] = EMBED_DIM
        reqs.append(r)
    res = json.loads(http(f"{GEMINI_BASE}/models/{model}:batchEmbedContents", {"requests": reqs},
                          headers=GEMINI_HEADERS(), retries=3, timeout=30))
    return [_unit(e["values"]) for e in res["embeddings"]]


def _discover_embed_model() -> str | None:
    data = json.loads(http(f"{GEMINI_BASE}/models?pageSize=200", headers=GEMINI_HEADERS(),
                           retries=2, timeout=20))
    names = [m["name"].removeprefix("models/") for m in data.get("models", [])
             if "embedContent" in m.get("supportedGenerationMethods", [])]
    names.sort(key=lambda n: ("gemini" not in n, "exp" in n or "preview" in n, n))
    return names[0] if names else None


def embed(texts: list[str]) -> list[list[float]] | None:
    """여러 문장을 임베딩. 사용할 수 없으면 None (→ 2-gram 방식만 사용)."""
    global _embed_model, _embed_dim_ok
    if DEDUP_MODE != "embedding" or not GEMINI_API_KEY or not texts:
        return None
    model = _embed_model or GEMINI_EMBED_MODEL
    out: list[list[float]] = []
    try:
        for i in range(0, len(texts), 100):            # 요청당 최대 100개
            chunk = texts[i:i + 100]
            try:
                out += _embed_request(model, chunk)
            except RuntimeError as e:
                msg = str(e)
                if isinstance(e, Overloaded) or "HTTP 4" not in msg:
                    raise
                if _embed_dim_ok and "imensionality" in msg:      # 차원 지정 미지원 모델
                    _embed_dim_ok = False
                elif "not found" in msg.lower() or "no longer available" in msg:
                    new = _discover_embed_model()
                    if not new or new == model:
                        raise
                    print(f"  ⚠ 임베딩 모델 '{model}' 사용 불가 → '{new}'(으)로 전환")
                    model = new
                else:
                    raise
                out += _embed_request(model, chunk)
        _embed_model = model
        return out
    except Exception as e:
        print(f"  ✗ 임베딩 실패 → 2-gram 방식으로만 중복 제거: {e}")
        return None


def load_history() -> list[dict]:
    """최근 HISTORY_DAYS 일 안에 보낸 기사 목록. 파일이 없거나 깨져 있으면 빈 목록."""
    try:
        with open(SENT_HISTORY_FILE, encoding="utf-8") as f:
            items = json.load(f).get("items", [])
    except (OSError, ValueError, AttributeError):
        return []
    cutoff = (datetime.now(KST) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    return [it for it in items if isinstance(it, dict) and it.get("date", "") >= cutoff]


def save_history(history: list[dict], sent: list[dict]) -> None:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    items = history + [{
        "date": today, "title": a["title"], "link": a["link"],
        "category": a.get("category", ""),
        # LLM 요약이 성공한 기사만 요약을 저장 (RAG 검색 시 맥락으로 사용)
        "summary": a["summary"] if a.get("summarized") else [],
        "vec": [round(x, 4) for x in a["vec"]] if a.get("vec") else None,
    } for a in sent]
    os.makedirs(os.path.dirname(SENT_HISTORY_FILE) or ".", exist_ok=True)
    with open(SENT_HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": today, "items": items}, f, ensure_ascii=False, indent=1)
    print(f"  · 전송 기록 저장: {SENT_HISTORY_FILE} (최근 {HISTORY_DAYS}일, {len(items)}건)")


def collect(history: list[dict] | None = None) -> list[dict]:
    print("📥 [2단계] 뉴스 수집")
    pools: list[tuple[str, list[dict]]] = []
    for sec, url in SECTIONS.items():
        try:
            items = fetch_rss(sec, url)
            pools.append((sec, items))
            print(f"  · {sec}: {len(items)}건")
        except Exception as e:                       # 한 피드 실패해도 계속 진행
            print(f"  ✗ {sec} 실패: {e}")
    if NAVER_CLIENT_ID and NAVER_CLIENT_SECRET:
        for q in NAVER_QUERIES:
            try:
                items = fetch_naver(q)
                pools.append((f"#{q}", items))
                print(f"  · 네이버 '{q}': {len(items)}건")
            except Exception as e:
                print(f"  ✗ 네이버 '{q}' 실패: {e}")

    # 분야별 상위 후보를 한 번에 임베딩 (실패하면 vecs=None → 2-gram 만 사용)
    candidates = [a for _, items in pools for a in items[:EMBED_PER_SECTION]]
    vecs = embed([a["title"] for a in candidates])
    if vecs:
        for a, v in zip(candidates, vecs):
            a["vec"] = v
        print(f"  · 제목 {len(vecs)}건 임베딩 ({_embed_model}, {len(vecs[0])}차원)")

    def embed_dup(a: dict, others: list[dict]) -> tuple[float, dict] | None:
        """임베딩 코사인 유사도가 임계값 이상인 기사 중 가장 비슷한 것."""
        if not a.get("vec"):
            return None
        best = max(((cosine(a["vec"], o["vec"]), o) for o in others if o.get("vec")),
                   key=lambda x: x[0], default=None)
        return best if best and best[0] >= EMBED_DUP_THRESHOLD else None

    # 분야별 상위 N개 선택 + 전체 기준 중복 제거
    # + 최근 며칠 안에 이미 보낸 기사(같은 링크 또는 유사 제목)도 제외
    # 중복 판별 = 2-gram 자카드(글자 기준) OR 임베딩 코사인(의미 기준)
    history = history or []
    sent_links = {h.get("link") for h in history}
    sent_keys = [norm(h.get("title", "")) for h in history]
    seen: list[str] = []
    picked: list[dict] = []
    stats = {"bigram": 0, "embedding": 0, "already": 0}
    for _, items in pools:
        n = 0
        for a in items:
            key = norm(a["title"])
            if not key:
                continue
            if is_dup(key, seen):
                stats["bigram"] += 1
                continue
            if a["link"] in sent_links or is_dup(key, sent_keys):
                stats["already"] += 1
                continue
            hit = embed_dup(a, picked)
            if hit:
                stats["embedding"] += 1
                print(f"  ≈ 의미 중복({hit[0]:.2f}): '{a['title'][:30]}' ~ '{hit[1]['title'][:30]}'")
                continue
            hit = embed_dup(a, history)
            if hit:
                stats["already"] += 1
                print(f"  ≈ 이미 보낸 기사와 의미 중복({hit[0]:.2f}): '{a['title'][:30]}'")
                continue
            seen.append(key)
            picked.append(a)
            n += 1
            if n >= PER_SECTION:
                break
    print(f"  · 중복 제거: 2-gram {stats['bigram']}건"
          + (f", 임베딩 추가 {stats['embedding']}건" if vecs else " (임베딩 미사용)"))
    if history:
        print(f"  · 최근 {HISTORY_DAYS}일 내 이미 보낸 기사 {stats['already']}건 제외")
    print(f"  → 최종 {len(picked)}건 선정")
    return picked

# ─────────────────────────────── 3단계: LLM 요약 ───────────────────────────────

PROMPT = """너는 한국어 뉴스 에디터다. 아래 기사 목록의 각 기사에 대해:
1) 제목과 발췌문만을 근거로 핵심을 정확히 3줄로 요약한다. 각 줄은 40자 안팎의 완결된 문장(~했다/~이다 체).
   주어진 정보에 없는 사실·수치를 지어내지 말 것. 정보가 부족하면 제목 내용을 풀어쓴다.
2) 카테고리를 다음 중 하나로 분류한다: {cats}

반드시 아래 JSON 배열만 출력하라 (코드블록·설명 금지):
[{{"id": 0, "category": "경제", "summary": ["첫째 줄", "둘째 줄", "셋째 줄"]}}, ...]
{examples}
기사 목록:
{articles}"""

# Few-shot 예시 (In-Context Learning)
#  · 문체(~했다 체, 40자 안팎)와 "발췌문에 있는 내용만 쓰기"를 예시로 보여 준다.
#  · 헷갈리기 쉬운 분류 경계를 예시로 고정한다: 국회 입법 → 정치 / 부동산 시세 → 경제(사회 아님)
#  · 예시는 실제 뉴스가 아닌 가상의 기사이며, 출력에 섞이지 않도록 별도 표기한다.
FEW_SHOT_EXAMPLES = """
아래는 작성 예시다 (가상의 기사이며, 예시 내용을 출력에 포함하지 말 것).
예시 입력 A) 제목: 국회, 플랫폼 노동자 보호법 본회의 통과 | 언론사: 예시일보 | 발췌: 국회는 12일 본회의에서 배달·대리운전 등 플랫폼 노동자에게 산재보험을 의무 적용하는 법안을 재석 250명 중 231명 찬성으로 가결했다. 시행은 공포 후 1년 뒤다.
예시 출력 A) {"category": "정치", "summary": ["국회가 플랫폼 노동자 보호법을 본회의에서 가결했다.", "배달·대리운전 노동자도 산재보험을 의무 적용받는다.", "법은 공포 1년 뒤부터 시행된다."]}
예시 입력 B) 제목: 수도권 아파트 전셋값 12주 연속 상승 | 언론사: 예시경제 | 발췌: 한국부동산원에 따르면 이번 주 수도권 아파트 전셋값은 0.1% 올라 12주 연속 상승했다. 전세 매물 부족이 주된 원인으로 꼽힌다.
예시 출력 B) {"category": "경제", "summary": ["수도권 아파트 전셋값이 12주 연속 올랐다.", "이번 주 상승률은 0.1%로 집계됐다.", "전세 매물 부족이 주된 원인으로 꼽힌다."]}
"""


def build_prompt(articles: list[dict]) -> str:
    lines = [f'[{i}] 제목: {a["title"]} | 언론사: {a["source"]} | 발췌: {a["desc"]}'
             for i, a in enumerate(articles)]
    examples = FEW_SHOT_EXAMPLES if PROMPT_MODE == "fewshot" else ""
    return PROMPT.format(cats=", ".join(CATEGORIES), articles="\n".join(lines), examples=examples)


GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_HEADERS = lambda: {"x-goog-api-key": GEMINI_API_KEY}   # 키를 URL 대신 헤더로 (로그 노출 방지)


_thinking_supported = True


def _thinking_config(model: str) -> dict | None:
    """Gemini 3 이상은 thinkingLevel, 2.x 는 thinkingBudget 으로 추론을 줄임."""
    if GEMINI_THINKING == "default" or not _thinking_supported:
        return None
    if _version(model) >= 3:
        return {"thinkingLevel": "low"}
    return {"thinkingBudget": 0} if "lite" not in model else None


def _gemini_request(model: str, prompt: str) -> str:
    global _thinking_supported
    cfg = {"temperature": 0.2, "responseMimeType": "application/json"}
    tc = _thinking_config(model)
    if tc:
        cfg["thinkingConfig"] = tc
    t0 = time.monotonic()
    try:
        raw = http(f"{GEMINI_BASE}/models/{model}:generateContent",
                   {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg},
                   headers=GEMINI_HEADERS(), retries=4, timeout=90, backoff=2.5,   # 503 대기 2.5→6→16초
                   max_timeouts=2)                                                  # 시간 초과는 1회만 재시도
    except RuntimeError as e:
        if tc and not isinstance(e, Overloaded) and "HTTP 400" in str(e) and "thinking" in str(e).lower():
            print("  · 이 모델은 thinkingConfig 미지원 → 옵션 없이 재요청")
            _thinking_supported = False
            return _gemini_request(model, prompt)
        raise
    res = json.loads(raw)
    print(f"  · {model} 응답 {time.monotonic() - t0:.0f}s")
    parts = res["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def _version(name: str) -> float:
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def discover_fallbacks(current: str) -> list[str]:
    """API의 모델 목록에서 텍스트 생성이 되는 Flash 계열을 골라 최대 3개 반환 (lite 우선)."""
    if GEMINI_FALLBACK_MODELS:
        return [m for m in GEMINI_FALLBACK_MODELS if m != current]
    try:
        data = json.loads(http(f"{GEMINI_BASE}/models?pageSize=200",
                               headers=GEMINI_HEADERS(), retries=2, timeout=20))
    except Exception as e:
        print(f"  · 대체 모델 목록 조회 실패: {e}")
        return []
    skip = ("image", "tts", "audio", "live", "embedding", "vision", "exp", "thinking")
    names = []
    for m in data.get("models", []):
        name = m.get("name", "").removeprefix("models/")
        if ("flash" in name and name != current
                and "generateContent" in m.get("supportedGenerationMethods", [])
                and not any(s in name for s in skip)):
            names.append(name)
    # 정식(non-preview) 우선 → 가볍고 빠른 lite 우선 → 버전 높은 순
    names.sort(key=lambda n: ("preview" in n, "lite" not in n, -_version(n), n))
    return names[:3]


_fallback_cache: list[str] | None = None
_active_model: str | None = None
_deadline = float("inf")


def call_gemini(prompt: str) -> str:
    """① 지원 종료 모델 → 안내된 새 모델로 교체  ② 과부하(503/시간 초과) → 대체 모델로 전환."""
    global _fallback_cache, _active_model
    model = _active_model or GEMINI_MODEL.removeprefix("models/")
    tried: list[str] = []
    while True:
        tried.append(model)
        try:
            text = _gemini_request(model, prompt)
            _active_model = model            # 다음 묶음은 성공한 모델로 바로 시작
            return text
        except Overloaded as e:
            print(f"  ⚠ '{model}' 응답 없음/과부하: {e}")
            if _fallback_cache is None:
                _fallback_cache = discover_fallbacks(model)
            nxt = next((m for m in _fallback_cache if m not in tried), None)
            if not nxt or time.monotonic() > _deadline:
                raise
        except RuntimeError as e:
            msg = str(e)
            suggested = [m.rstrip(".") for m in re.findall(r"models/([\w.\-]+)", msg)
                         if m.rstrip(".") not in tried]
            if not (("no longer available" in msg or "not found" in msg.lower()) and suggested):
                raise
            nxt = suggested[0]
            print(f"  ⚠ '{model}' 지원 종료 (GEMINI_MODEL 설정을 '{nxt}'(으)로 바꿔 두세요)")
        print(f"  → '{nxt}'(으)로 전환")
        model = nxt


def call_openai(prompt: str) -> str:
    res = json.loads(http("https://api.openai.com/v1/chat/completions", {
        "model": OPENAI_MODEL, "temperature": 0.2,
        "messages": [{"role": "system", "content": "JSON만 출력하는 뉴스 요약기"},
                     {"role": "user", "content": prompt}],
    }, headers={"Authorization": f"Bearer {OPENAI_API_KEY}"}, timeout=120))
    return res["choices"][0]["message"]["content"]


def parse_json_array(text: str) -> list:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    return json.loads(text[start:end + 1])


def guess_category(a: dict) -> str:
    """LLM 실패 시 대체: 수집 분야를 그대로 카테고리로."""
    return a["section"] if a["section"] in CATEGORIES else "기타"


def summarize(articles: list[dict]) -> list[dict]:
    print(f"🧠 [3단계] LLM 요약 ({LLM_PROVIDER}, {'Few-shot' if PROMPT_MODE == 'fewshot' else 'Zero-shot'})")
    results: dict[int, dict] = {}
    if LLM_PROVIDER in ("gemini", "openai") and articles:
        if LLM_PROVIDER == "gemini":
            print(f"  · 사용 모델: {GEMINI_MODEL}")
        call = call_gemini if LLM_PROVIDER == "gemini" else call_openai
        # 작은 묶음으로 나눠 호출 → 응답이 빠르고, 한 묶음이 실패해도 나머지는 요약됨
        global _deadline
        _deadline = time.monotonic() + LLM_TIME_BUDGET
        n_batches = -(-len(articles) // SUMMARY_BATCH)
        for b in range(n_batches):
            if time.monotonic() > _deadline:
                print(f"  ✗ 시간 제한({LLM_TIME_BUDGET}s) 초과 → 남은 {n_batches - b}개 묶음은 발췌문으로 대체")
                break
            start = b * SUMMARY_BATCH
            chunk = articles[start:start + SUMMARY_BATCH]
            try:
                got = 0
                for r in parse_json_array(call(build_prompt(chunk))):
                    if isinstance(r, dict) and 0 <= int(r.get("id", -1)) < len(chunk):
                        results[start + int(r["id"])] = r
                        got += 1
                print(f"  · 묶음 {b + 1}/{n_batches}: {got}/{len(chunk)}건 요약")
            except Overloaded as e:
                print(f"  ✗ 묶음 {b + 1}/{n_batches} 실패: {e}")
                print(f"  ✗ 사용 가능한 모델이 모두 응답하지 않음 → 남은 기사는 발췌문으로 대체")
                break
            except Exception as e:
                print(f"  ✗ 묶음 {b + 1}/{n_batches} 실패, 원문 발췌로 대체: {e}")
        print(f"  → {len(results)}/{len(articles)}건 요약 완료"
              + (f" (최종 모델: {_active_model})" if _active_model else ""))

    for i, a in enumerate(articles):
        r = results.get(i, {})
        cat = r.get("category") if r.get("category") in CATEGORIES else guess_category(a)
        summary = [s for s in r.get("summary", []) if isinstance(s, str) and s.strip()][:3]
        if not summary:  # 폴백: 발췌문 사용
            summary = [a["desc"][:120] or a["title"]]
        a["category"], a["summary"] = cat, summary
        a["summarized"] = bool(r.get("summary"))   # LLM 요약 성공 여부 (브리핑 생성에 사용)
    return articles


BRIEFING_PROMPT = """너는 한국어 뉴스 에디터다. 아래는 오늘의 주요 뉴스 요약이다.
독자가 오늘 하루 뉴스의 흐름을 한눈에 파악할 수 있도록 가장 중요한 이슈 2~3개를 골라
2~3문장(전체 150자 안팎)으로 브리핑하라. 주어진 내용에 없는 사실·수치는 추가하지 말 것.

반드시 아래 JSON만 출력하라 (코드블록·설명 금지):
{{"briefing": "브리핑 문장"}}

오늘의 뉴스:
{articles}
{context}"""

# RAG: 전송 기록에서 검색한 관련 과거 기사를 브리핑 프롬프트에 덧붙이는 부분
RAG_CONTEXT = """
참고 자료 — 최근 며칠간 이미 전한 관련 뉴스:
{items}
브리핑의 중심은 오늘의 뉴스다. 참고 자료가 오늘 뉴스와 직접 이어지는 경우에만
"어제 ~에 이어"처럼 흐름을 짧게 연결하고, 참고 자료만의 내용을 따로 소개하지는 말 것."""


def retrieve_context(articles: list[dict], history: list[dict]) -> list[tuple[float, dict, dict]]:
    """RAG 검색 단계: 오늘 기사와 의미가 가까운 과거 기사를 전송 기록에서 찾는다.
    반환: [(유사도, 과거 기사, 오늘 기사)] 유사도 높은 순, 최대 RAG_TOP_K 건."""
    kb = [h for h in history if h.get("vec") and h.get("summary")]
    pairs = []
    for a in articles:
        if not (a.get("vec") and a.get("summarized")):
            continue
        for h in kb:
            score = cosine(a["vec"], h["vec"])
            if score >= RAG_THRESHOLD:
                pairs.append((score, h, a))
    pairs.sort(key=lambda x: -x[0])
    out, used = [], set()
    for score, h, a in pairs:
        if h["link"] not in used:
            used.add(h["link"])
            out.append((score, h, a))
        if len(out) >= RAG_TOP_K:
            break
    return out


def _day_label(date: str) -> str:
    today = datetime.now(KST).date()
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return date
    rel = {1: "어제", 2: "그저께"}.get((today - d).days, "")
    return f"{d.month}월 {d.day}일" + (f", {rel}" if rel else "")


def make_briefing(articles: list[dict], history: list[dict] | None = None) -> str:
    """오늘의 한 줄 브리핑. 요약된 기사가 없거나 실패·시간 초과면 빈 문자열(브리핑 생략)."""
    done = [a for a in articles if a.get("summarized")]
    if not BRIEFING or LLM_PROVIDER not in ("gemini", "openai") or not done:
        return ""
    if time.monotonic() > _deadline:
        print("  · 시간 제한 초과로 브리핑 생략")
        return ""
    lines = [f'- [{a["category"]}] {a["title"]}: {" ".join(a["summary"])}' for a in done]

    # RAG: 검색(Retrieve) → 프롬프트에 덧붙이기(Augment) → 생성(Generate)
    context = ""
    related = retrieve_context(done, history or [])
    if related:
        items = []
        for score, h, a in related:
            print(f"  · RAG 관련 과거 기사({score:.2f}): '{h['title'][:28]}' ↔ 오늘 '{a['title'][:28]}'")
            items.append(f'- ({_day_label(h["date"])}, {h.get("category", "")}) '
                         f'{h["title"]}: {" ".join(h["summary"])}')
        context = RAG_CONTEXT.format(items="\n".join(items))
    try:
        call = call_gemini if LLM_PROVIDER == "gemini" else call_openai
        text = call(BRIEFING_PROMPT.format(articles="\n".join(lines), context=context))
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        briefing = str(data.get("briefing", "")).strip()
        print(f"  · 오늘의 브리핑 생성 ({len(briefing)}자"
              + (f", 과거 기사 {len(related)}건 참고)" if related else ")"))
        return briefing[:600]
    except Exception as e:
        print(f"  ✗ 브리핑 생성 실패, 생략: {e}")
        return ""

# ─────────────────────────────── 4단계: 디스코드 전송 ───────────────────────────────

def build_embeds(articles: list[dict]) -> list[dict]:
    order = CATEGORIES + ["기타"]
    grouped: dict[str, list[dict]] = {}
    for a in articles:
        grouped.setdefault(a["category"], []).append(a)

    embeds = []
    for cat in sorted(grouped, key=lambda c: order.index(c) if c in order else 99):
        emoji, color = CATEGORY_STYLE.get(cat, CATEGORY_STYLE["기타"])
        blocks = []
        for a in grouped[cat]:
            title = a["title"].replace("[", "［").replace("]", "］")
            src = f" · {a['source']}" if a["source"] else ""
            body = "\n".join(f"> {s}" for s in a["summary"])
            blocks.append(f"**[{title}]({a['link']})**{src}\n{body}")
        desc = "\n\n".join(blocks)
        if len(desc) > 4000:  # 디스코드 임베드 설명 한도 4096자
            desc = desc[:3990] + "…"
        embeds.append({"title": f"{emoji} {cat}", "description": desc, "color": color})
    return embeds


def send_discord(articles: list[dict], briefing: str = "") -> bool:
    """디스코드로 전송했으면 True, 웹훅 미설정(콘솔 출력만)이면 False."""
    print("📤 [4단계] 디스코드 전송")
    today = datetime.now(KST)
    weekday = "월화수목금토일"[today.weekday()]
    header = f"## 🗞️ {today:%Y년 %m월 %d일} ({weekday}) 오늘의 주요 뉴스\n총 {len(articles)}건 · 3줄 요약"
    if briefing:
        header += f"\n\n**📌 오늘의 브리핑**\n> {briefing}"

    embeds = build_embeds(articles)
    # 메시지당 임베드 10개, 총 6000자 한도 → 나눠 보내기
    batches, cur, size = [], [], 0
    for e in embeds:
        esize = len(e["title"]) + len(e["description"])
        if cur and (len(cur) == 10 or size + esize > 5800):
            batches.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += esize
    if cur:
        batches.append(cur)

    if not DISCORD_WEBHOOK_URL:
        print("  (DISCORD_WEBHOOK_URL 미설정 → 콘솔 출력만)")
        print(header)
        for e in embeds:
            print(f"\n{e['title']}\n{e['description']}")
        return False

    for i, batch in enumerate(batches or [[]]):
        payload = {"username": "데일리 뉴스봇", "embeds": batch}
        if i == 0:
            payload["content"] = header
        http(DISCORD_WEBHOOK_URL, payload)
        time.sleep(1)
    print(f"  → {len(batches)}개 메시지 전송 완료")
    return True

# ─────────────────────────────── 실행 ───────────────────────────────

def main() -> int:
    print(f"⏰ [1단계] 실행 시작 {datetime.now(KST):%Y-%m-%d %H:%M} KST")
    history = load_history()
    articles = collect(history)
    if not articles:
        print("수집된 기사가 없습니다.")
        if DISCORD_WEBHOOK_URL:
            http(DISCORD_WEBHOOK_URL, {"content": "⚠️ 오늘은 새로 전할 뉴스를 수집하지 못했습니다."})
        return 1
    articles = summarize(articles)
    briefing = make_briefing(articles, history)
    if send_discord(articles, briefing):
        save_history(history, articles)   # 실제로 보낸 경우에만 기록 (로컬 테스트는 기록 안 함)
    print("✅ 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
