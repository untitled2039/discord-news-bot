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
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "gemini").lower()          # gemini | openai | none
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
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

def http(url: str, data: dict | None = None, headers: dict | None = None,
         retries: int = 3, timeout: int = 60) -> bytes:
    body = json.dumps(data).encode() if data is not None else None
    h = {"User-Agent": UA, **(headers or {})}
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # 429/5xx 만 재시도
            if e.code == 429 or e.code >= 500:
                wait = float(e.headers.get("Retry-After") or 2 ** attempt)
                print(f"  ↻ HTTP {e.code}, {wait:.0f}s 후 재시도 ({attempt}/{retries})")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:300].decode(errors='ignore')}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == retries:
                raise
            print(f"  ↻ 네트워크 오류 {e}, 재시도 ({attempt}/{retries})")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"요청 실패: {url}")

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


def call_gemini(prompt: str) -> str:
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    res = json.loads(http(url, {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }, timeout=120))
    return res["candidates"][0]["content"]["parts"][0]["text"]


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
        try:
            raw = call_gemini(build_prompt(articles)) if LLM_PROVIDER == "gemini" \
                else call_openai(build_prompt(articles))
            for r in parse_json_array(raw):
                if isinstance(r, dict) and 0 <= int(r.get("id", -1)) < len(articles):
                    results[int(r["id"])] = r
            print(f"  → {len(results)}/{len(articles)}건 요약 완료")
        except Exception as e:
            print(f"  ✗ LLM 요약 실패, 원문 발췌로 대체: {e}")

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
