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


def collect() -> list[dict]:
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

    # 분야별 상위 N개 선택 + 전체 기준 중복(유사 제목) 제거
    seen: list[str] = []
    picked: list[dict] = []
    for _, items in pools:
        n = 0
        for a in items:
            key = norm(a["title"])
            if not key or is_dup(key, seen):
                continue
            seen.append(key)
            picked.append(a)
            n += 1
            if n >= PER_SECTION:
                break
    print(f"  → 중복 제거 후 {len(picked)}건 선정")
    return picked

# ─────────────────────────────── 3단계: LLM 요약 ───────────────────────────────

PROMPT = """너는 한국어 뉴스 에디터다. 아래 기사 목록의 각 기사에 대해:
1) 제목과 발췌문만을 근거로 핵심을 정확히 3줄로 요약한다. 각 줄은 40자 안팎의 완결된 문장(~했다/~이다 체).
   주어진 정보에 없는 사실·수치를 지어내지 말 것. 정보가 부족하면 제목 내용을 풀어쓴다.
2) 카테고리를 다음 중 하나로 분류한다: {cats}

반드시 아래 JSON 배열만 출력하라 (코드블록·설명 금지):
[{{"id": 0, "category": "경제", "summary": ["첫째 줄", "둘째 줄", "셋째 줄"]}}, ...]

기사 목록:
{articles}"""


def build_prompt(articles: list[dict]) -> str:
    lines = [f'[{i}] 제목: {a["title"]} | 언론사: {a["source"]} | 발췌: {a["desc"]}'
             for i, a in enumerate(articles)]
    return PROMPT.format(cats=", ".join(CATEGORIES), articles="\n".join(lines))


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
    print(f"🧠 [3단계] LLM 요약 ({LLM_PROVIDER})")
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
    return articles

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


def send_discord(articles: list[dict]) -> None:
    print("📤 [4단계] 디스코드 전송")
    today = datetime.now(KST)
    weekday = "월화수목금토일"[today.weekday()]
    header = f"## 🗞️ {today:%Y년 %m월 %d일} ({weekday}) 오늘의 주요 뉴스\n총 {len(articles)}건 · 3줄 요약"

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
        return

    for i, batch in enumerate(batches or [[]]):
        payload = {"username": "데일리 뉴스봇", "embeds": batch}
        if i == 0:
            payload["content"] = header
        http(DISCORD_WEBHOOK_URL, payload)
        time.sleep(1)
    print(f"  → {len(batches)}개 메시지 전송 완료")

# ─────────────────────────────── 실행 ───────────────────────────────

def main() -> int:
    print(f"⏰ [1단계] 실행 시작 {datetime.now(KST):%Y-%m-%d %H:%M} KST")
    articles = collect()
    if not articles:
        print("수집된 기사가 없습니다.")
        if DISCORD_WEBHOOK_URL:
            http(DISCORD_WEBHOOK_URL, {"content": "⚠️ 오늘은 뉴스를 수집하지 못했습니다."})
        return 1
    send_discord(summarize(articles))
    print("✅ 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
