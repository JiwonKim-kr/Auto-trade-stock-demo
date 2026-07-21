"""논문 뉴스 수집기(§8) — NAVER API HUB(NCP) 뉴스 검색으로 (기사, 종목) 관측을 전향 수집.

플랫폼: developers.naver.com 검색 API 가 NCP NAVER API HUB 로 이관(2026) — 엔드포인트·
인증 헤더만 다르고 요청 파라미터·응답 형식은 동일하다. 현재 한시적 무료(유료 전환 시 사전 공지).
원문 복원 규칙(§8.2): title 의 검색 하이라이트 태그(<b> 등) 제거 + HTML 엔티티 unescape
— 그 결과가 원문이다(전처리는 분석 단계, 여기선 복원만).
매핑 규칙: 종목명 쿼리 + 제목·요약에 종목명 포함 검사(mapping_method="naver_query+name_match")
— 시황 기사("코스피 상승 마감")는 매핑 실패로 자동 제외된다.
시각: pubDate(RFC822, 분 단위, +0900)를 tz-aware 파싱 → UTC 저장. 시각 없는 관측은 버린다
(§8 "이 논문의 생명선"). DB 저장은 경계(repo.insert_news — (url,symbol) 최초 버전 고정).
"""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as tz
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx

logger = logging.getLogger("app.news")

SOURCE = "naver_api_hub"        # NCP NAVER API HUB(뉴스 검색) — 데이터 출처 기록(논문 provenance)
MAPPING_METHOD = "naver_query+name_match"
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(raw: str) -> str:
    """검색 하이라이트 태그 제거 + HTML 엔티티 복원 → 원문."""
    return html.unescape(_TAG_RE.sub("", raw or "")).strip()


def press_from_url(url: str) -> str:
    try:
        return urlparse(url).netloc or "unknown"
    except ValueError:
        return "unknown"


@dataclass(frozen=True)
class NewsTarget:
    symbol: str
    name: str


def load_targets(targets_path: str | Path, seed_path: str | Path) -> list[NewsTarget]:
    """news_targets.json(코드 배열, git 추적 — 유니버스 스냅샷 §8.4) + KRX 시드(코드→이름).

    이름을 해석 못 하는 코드는 경고 후 제외(쿼리를 만들 수 없다).
    """
    codes = json.loads(Path(targets_path).read_text(encoding="utf-8"))
    seed = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    names = {e["code"]: e["name"] for e in seed.get("symbols", [])}
    targets: list[NewsTarget] = []
    for code in codes:
        if code in names:
            targets.append(NewsTarget(symbol=code, name=names[code]))
        else:
            logger.warning("뉴스 타깃 %s — KRX 시드에 없음(이름 해석 불가) → 제외", code)
    return targets


def parse_item(item: dict, target: NewsTarget, now: datetime) -> dict | None:
    """네이버 API item 1건 → news 행 dict. 매핑 실패(종목명 미포함)·시각 결손이면 None."""
    title = clean_text(item.get("title", ""))
    desc = clean_text(item.get("description", ""))
    if target.name not in title and target.name not in desc:
        return None
    url = item.get("originallink") or item.get("link") or ""
    if not title or not url:
        return None
    try:
        published = parsedate_to_datetime(item["pubDate"])
    except (KeyError, TypeError, ValueError):
        return None
    if published.tzinfo is None:            # naive 는 신뢰 불가 — 분 단위 정렬에 못 쓴다
        return None
    return {
        "symbol": target.symbol,
        "headline": title,
        "press": press_from_url(url),
        "url": url,
        "published_at": published.astimezone(tz.utc),
        "collected_at": now,
        "source": SOURCE,
        "mapping_method": MAPPING_METHOD,
    }


class NaverNewsClient:
    """NAVER API HUB(NCP) 뉴스 검색. Application 인증정보의 Client ID/Secret 을 APIGW 헤더로 전송.

    엔드포인트·헤더만 developers.naver.com 과 다르고(파라미터·응답 동일), 응답 items 스키마도 같다.
    """

    BASE = "https://naverapihub.apigw.ntruss.com/search/v1/news"

    def __init__(self, client_id: str, client_secret: str,
                 client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._headers = {"X-NCP-APIGW-API-KEY-ID": client_id, "X-NCP-APIGW-API-KEY": client_secret}

    async def search(self, query: str, start: int = 1, display: int = 100) -> list[dict]:
        r = await self._client.get(self.BASE, headers=self._headers,
                                   params={"query": query, "display": display,
                                           "start": start, "sort": "date"})
        r.raise_for_status()
        return r.json().get("items", [])

    async def aclose(self) -> None:
        await self._client.aclose()


async def collect_target(client, repo, target: NewsTarget, now: datetime,
                         max_pages: int = 2, page_size: int = 100) -> tuple[int, int]:
    """한 종목 수집 — (매핑된 관측 수, 신규 삽입 수). 최신순이라 신규 0 인 페이지에서 중단."""
    mapped = inserted = 0
    for page in range(max_pages):
        items = await client.search(target.name, start=1 + page * page_size, display=page_size)
        rows = [row for it in items if (row := parse_item(it, target, now)) is not None]
        mapped += len(rows)
        new = await repo.insert_news(rows)
        inserted += new
        if not items or new == 0:
            break
    return mapped, inserted


async def collect_all(client, repo, targets: list[NewsTarget], now: datetime) -> dict:
    """전 타깃 수집 — 종목 하나의 실패가 전체를 죽이지 않는다(수집은 매 30분 재시도됨)."""
    mapped = inserted = errors = 0
    for target in targets:
        try:
            m, i = await collect_target(client, repo, target, now)
            mapped += m
            inserted += i
        except Exception:
            errors += 1
            logger.exception("뉴스 수집 실패: %s(%s)", target.name, target.symbol)
    return {"targets": len(targets), "mapped": mapped, "inserted": inserted, "errors": errors}
