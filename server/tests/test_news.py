"""논문 뉴스 수집(§8) — 원문 복원·매핑·시각 파싱·(url,symbol) 중복·t+1 정렬·라우트."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.db.repo import Repository
from app.db.session import init_db, make_engine, make_sessionmaker
from app.main import create_app
from app.news.alignment import KST, next_trading_day, return_window
from app.news.collector import (
    NewsTarget,
    clean_text,
    collect_target,
    load_targets,
    parse_item,
)

NOW = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
T = NewsTarget(symbol="005930", name="삼성전자")
KEY = {"X-API-Key": "dev-local-key"}


async def make_repo(tmp_path) -> Repository:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path}/n.db")
    await init_db(engine)
    return Repository(make_sessionmaker(engine))


def item(title="<b>삼성전자</b> 2분기 &quot;깜짝&quot; 실적", url="https://news.example/a1",
         pub="Fri, 10 Jul 2026 09:12:00 +0900", desc=""):
    return {"title": title, "originallink": url, "link": "https://n.naver/x",
            "description": desc, "pubDate": pub}


# ── 원문 복원·매핑·시각 ────────────────────────────────────────────────────────
def test_clean_text_restores_original():
    assert clean_text("<b>삼성전자</b> &quot;실적&quot; 발표") == '삼성전자 "실적" 발표'


def test_parse_item_full_row():
    row = parse_item(item(), T, NOW)
    assert row["headline"] == '삼성전자 2분기 "깜짝" 실적'
    assert row["symbol"] == "005930" and row["press"] == "news.example"
    assert row["published_at"] == datetime(2026, 7, 10, 0, 12, tzinfo=timezone.utc)  # +0900→UTC
    assert row["collected_at"] == NOW
    assert row["source"] == "naver_api_hub" and row["mapping_method"] == "naver_query+name_match"


def test_parse_item_rejects_unmapped_and_timeless():
    # 시황 기사(종목명 미포함) → 매핑 실패로 제외(§8.2)
    assert parse_item(item(title="코스피 상승 마감"), T, NOW) is None
    # 보도 시각 결손 → 제외("이 논문의 생명선")
    bad = item()
    del bad["pubDate"]
    assert parse_item(bad, T, NOW) is None


def test_load_targets_resolves_names_from_seed(tmp_path):
    (tmp_path / "targets.json").write_text('["005930", "999999"]', encoding="utf-8")
    (tmp_path / "seed.json").write_text(json.dumps(
        {"symbols": [{"code": "005930", "name": "삼성전자", "market": "KOSPI"}]},
        ensure_ascii=False), encoding="utf-8")
    targets = load_targets(tmp_path / "targets.json", tmp_path / "seed.json")
    assert targets == [NewsTarget(symbol="005930", name="삼성전자")]   # 미해석 코드는 제외


# ── (url, symbol) 관측 단위 — 최초 버전 고정 ──────────────────────────────────
async def test_insert_news_dedup_and_multi_symbol(tmp_path):
    repo = await make_repo(tmp_path)
    row = parse_item(item(), T, NOW)
    assert await repo.insert_news([row, dict(row)]) == 1          # 같은 배치 내 중복도 1건
    edited = dict(row, headline="수정된 제목")
    assert await repo.insert_news([edited]) == 0                  # 수정 기사 재수집 → 최초 버전 고정
    other = dict(row, symbol="000660")                            # 같은 기사·다른 종목 = 별도 관측
    assert await repo.insert_news([other]) == 1
    assert await repo.count_news() == 2 and await repo.count_news("005930") == 1


async def test_labels_and_model_outputs_roundtrip(tmp_path):
    repo = await make_repo(tmp_path)
    await repo.insert_news([parse_item(item(), T, NOW)])
    assert await repo.add_news_label(1, "positive", "v1") == 1
    assert await repo.add_news_label(1, "neutral", "v2") == 2     # 재라벨은 append(자기일치도)
    out_id = await repo.add_news_model_output(
        1, model="claude-haiku-4-5", prompt_version="p1",
        raw_output='{"label":"positive"}', parsed_label="positive")
    assert out_id == 1


# ── 수집 루프 — 신규 0 페이지에서 중단 ────────────────────────────────────────
class FakeNaver:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    async def search(self, query, start=1, display=100):
        self.calls += 1
        idx = (start - 1) // display
        return self.pages[idx] if idx < len(self.pages) else []


async def test_collect_target_stops_when_page_all_known(tmp_path):
    repo = await make_repo(tmp_path)
    page = [item(url="https://news.example/a1"), item(url="https://news.example/a2")]
    client = FakeNaver(pages=[page, page])                        # 2페이지가 같은 내용(전부 기존)
    mapped, inserted = await collect_target(client, repo, T, NOW, max_pages=3, page_size=100)
    assert (mapped, inserted) == (4, 2)
    assert client.calls == 2                                      # 2페이지째 신규 0 → 3페이지 안 감


# ── t+1 정렬(§8.3) ───────────────────────────────────────────────────────────
HOLIDAYS = frozenset({"2026-07-14"})   # 화요일을 공휴일로 가정


def test_return_window_intraday():
    w = return_window(datetime(2026, 7, 10, 9, 12, tzinfo=KST), frozenset())   # 금 장중
    assert (w.entry_date.isoformat(), w.entry_field) == ("2026-07-10", "close")
    assert (w.exit_date.isoformat(), w.exit_field) == ("2026-07-13", "close")  # t+1 = 월


def test_return_window_after_close_and_weekend():
    w = return_window(datetime(2026, 7, 10, 18, 0, tzinfo=KST), frozenset())   # 금 마감 후
    assert (w.entry_date.isoformat(), w.entry_field) == ("2026-07-13", "open")  # 월 시가
    assert w.exit_date.isoformat() == "2026-07-14"                              # 화 시가
    w2 = return_window(datetime(2026, 7, 11, 12, 0, tzinfo=KST), frozenset())  # 토
    assert w2.entry_date.isoformat() == "2026-07-13"


def test_return_window_skips_holiday():
    assert next_trading_day(datetime(2026, 7, 13, tzinfo=KST).date(), HOLIDAYS).isoformat() \
        == "2026-07-15"                                                        # 화(휴장) 건너뜀
    w = return_window(datetime(2026, 7, 13, 16, 0, tzinfo=KST), HOLIDAYS)      # 월 마감 후
    assert w.entry_date.isoformat() == "2026-07-15"                            # 수 시가


# ── 라우트 — 미설정 시 안전 스킵 ──────────────────────────────────────────────
def test_news_collect_route_skips_without_config():
    app = create_app()
    with TestClient(app) as c:
        r = c.post("/internal/news/collect", headers=KEY)
        assert r.status_code == 200 and "skipped" in r.json()
