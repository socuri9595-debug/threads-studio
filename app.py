#!/usr/bin/env python3
"""Threads Studio — 로컬 웹앱.

트렌드 소재 → 페르소나로 글 생성(Claude) → 쿠팡 파트너스 링크/공정위 문구 → Threads 발행/예약.
모든 설정과 데이터는 ./data 에 파일로 저장됨 (내 컴퓨터 밖으로 안 나감).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlencode

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
KST = timezone(timedelta(hours=9))
THREADS_API = "https://graph.threads.net/v1.0"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
COUPANG_HOST = "https://api-gateway.coupang.com"

DISCLOSURE = "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."

app = FastAPI(title="Threads Studio")


# ───────────────────────── storage ─────────────────────────
def _path(name: str) -> Path:
    return DATA / f"{name}.json"


def load(name: str, default: Any) -> Any:
    p = _path(name)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save(name: str, obj: Any) -> None:
    _path(name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def now_kst() -> datetime:
    return datetime.now(KST)


DEFAULT_SETTINGS = {
    "anthropic_api_key": "",
    "anthropic_model": "claude-sonnet-5-5",
    "threads_token": "",
    "threads_username": "",
    "coupang_access_key": "",
    "coupang_secret_key": "",
    "coupang_subid": "",
    "naver_client_id": "",
    "naver_client_secret": "",
}

DEFAULT_PERSONAS = [
    {
        "id": "office-30s",
        "name": "출근하기 싫은 30대 직장인",
        "builtin": True,
        "prompt": "출근하기 싫어하는 30대 현실 직장인. 반말, 솔직하고 자조적, 과하게 밝지 않음. 월요병·퇴근 갈망·회의 현타·월급 스쳐감이 단골 소재. 친구한테 카톡 보내듯 2~3줄.",
    },
    {
        "id": "cat-lover",
        "name": "고양이 못 키우는 집사 지망생",
        "builtin": True,
        "prompt": "고양이를 키우고 싶지만 책임질 자신이 없어서 남의 고양이 사진만 모으는 1인 가구. 반말, 느긋하고 유머러스. 퇴근하고 집에 오면 아무도 없는 얘기, 길고양이 얘기.",
    },
    {
        "id": "honest-reviewer",
        "name": "호들갑 없는 찐 리뷰어",
        "builtin": True,
        "prompt": "물건을 과장 없이 담백하게 소개하는 사람. 반말. 장점 하나, 아쉬운 점 하나를 꼭 같이 말함. '진짜 물건이다' 같은 호들갑 금지. 숫자(용량·가격·개수)로 말함.",
    },
    {
        "id": "info-curator",
        "name": "정보 큐레이터",
        "builtin": True,
        "prompt": "검색하기 귀찮은 사람 대신 핵심만 정리해주는 사람. 반말 또는 해요체. 번호 매긴 3줄 요약을 좋아함. 출처가 불확실한 건 '~라고 함'으로 표시.",
    },
]

STYLE_RULES = """공통 규칙:
- 반드시 2~3줄. 한 줄 = 한 문장(또는 아주 짧은 두 문장). 줄 사이는 줄바꿈. 전체 200자 이내.
- 첫 줄이 스크롤을 멈추게 해야 한다. 사람의 상황·감정에서 시작한다.
- 이모지·해시태그·느낌표 남발 금지. 느낌표 최대 1개.
- 사람이 폰으로 툭 쓴 글처럼. 매끈한 광고 카피 금지. 설명체("~할 수 있습니다") 금지.
- 읽는 사람은 아무 배경지식이 없다. 설명 없이 알 수 없는 고유명사·내부 용어 금지.
- 사실이 아닌 체험을 지어내지 않는다. 사용자가 '직접 써봤다'고 명시하지 않은 상품은 써본 것처럼 말하지 않는다. "~라고 함", "~래", "~있더라" 같은 전해들은/발견한 톤으로 소개한다.
- 의학·건강 효능 단정 금지. 과장("인생템", "미쳤다", "역대급") 금지.
- 출력은 글 본문만. 따옴표·설명·번호 없이."""


# ───────────────────────── models ─────────────────────────
class Settings(BaseModel):
    anthropic_api_key: Optional[str] = None
    anthropic_model: Optional[str] = None
    threads_token: Optional[str] = None
    coupang_access_key: Optional[str] = None
    coupang_secret_key: Optional[str] = None
    coupang_subid: Optional[str] = None
    naver_client_id: Optional[str] = None
    naver_client_secret: Optional[str] = None


class Persona(BaseModel):
    id: Optional[str] = None
    name: str
    prompt: str


class Scrap(BaseModel):
    author: str = ""
    text: str
    likes: int = 0
    replies: int = 0
    reposts: int = 0
    url: str = ""


class Product(BaseModel):
    title: str = ""
    price: str = ""
    url: str = ""
    image: str = ""


class GenerateReq(BaseModel):
    keyword: str
    persona_id: str
    mode: str = "basic"  # basic | hook | persona
    count: int = 1
    product: Optional[Product] = None
    affiliate: bool = False
    experience: str = ""  # 직접 써본 경험 메모 (비어 있으면 소개형으로만 씀)
    reviews: str = ""  # 쿠팡 등에서 복사한 실제 구매 후기 (종합해서 '후기에 따르면' 톤으로 씀)
    extra: str = ""  # 추가 지시


class PublishReq(BaseModel):
    text: str
    image_url: str = ""
    reply_text: str = ""
    scheduled_at: Optional[str] = None  # ISO KST, None = 즉시
    tag: str = ""


class ReportReq(BaseModel):
    goal: str = "제휴 마케팅 링크 클릭 전환"


# ───────────────────────── helpers ─────────────────────────
def settings() -> dict:
    s = {**DEFAULT_SETTINGS, **load("settings", {})}
    return s


def mask(v: str) -> str:
    if not v:
        return ""
    return v[:4] + "•" * 6 + v[-4:] if len(v) > 10 else "•" * len(v)


def claude(system: str, user: str, max_tokens: int = 1200) -> str:
    s = settings()
    key = s["anthropic_api_key"]
    if not key:
        raise HTTPException(400, "설정에서 Claude API 키를 먼저 넣어주세요.")
    r = requests.post(
        ANTHROPIC_API,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": s["anthropic_model"], "max_tokens": max_tokens, "system": system,
              "messages": [{"role": "user", "content": user}]},
        timeout=90,
    )
    if r.status_code != 200:
        raise HTTPException(502, f"Claude API 오류 {r.status_code}: {r.text[:300]}")
    data = r.json()
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()


def threads(method: str, path: str, params: dict | None = None) -> dict:
    s = settings()
    tok = s["threads_token"]
    if not tok:
        raise HTTPException(400, "설정에서 Threads 토큰을 먼저 넣어주세요.")
    params = {**(params or {}), "access_token": tok}
    url = f"{THREADS_API}{path}"
    r = requests.get(url, params=params, timeout=60) if method == "GET" else requests.post(url, data=params, timeout=60)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text}
    if r.status_code != 200:
        msg = data.get("error", {}).get("message") if isinstance(data, dict) else None
        raise HTTPException(502, f"Threads API 오류 {r.status_code}: {msg or str(data)[:300]}")
    return data


def threads_publish(text: str, image_url: str = "", reply_to: str = "") -> str:
    params: dict = {"text": text}
    if image_url:
        params.update({"media_type": "IMAGE", "image_url": image_url})
    else:
        params["media_type"] = "TEXT"
    if reply_to:
        params["reply_to_id"] = reply_to
    cid = threads("POST", "/me/threads", params)["id"]
    for _ in range(20):
        st = threads("GET", f"/{cid}", {"fields": "status,error_message"})
        if st.get("status") == "FINISHED":
            break
        if st.get("status") == "ERROR":
            raise HTTPException(502, f"컨테이너 오류: {st.get('error_message')}")
        time.sleep(3)
    return threads("POST", "/me/threads_publish", {"creation_id": cid})["id"]


def coupang_headers(method: str, path_with_query: str) -> dict:
    s = settings()
    ak, sk = s["coupang_access_key"], s["coupang_secret_key"]
    if not (ak and sk):
        raise HTTPException(400, "쿠팡 파트너스 API 키가 없어요. 설정에 넣거나, 상품 정보를 직접 입력하세요.")
    path, _, query = path_with_query.partition("?")
    signed_date = datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")
    message = signed_date + method + path + query
    sig = hmac.new(sk.encode(), message.encode(), hashlib.sha256).hexdigest()
    auth = f"CEA algorithm=HmacSHA256, access-key={ak}, signed-date={signed_date}, signature={sig}"
    return {"Authorization": auth, "Content-Type": "application/json"}


# ───────────────────────── routes: basics ─────────────────────────
@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/settings")
def get_settings():
    s = settings()
    out = {k: (mask(v) if "key" in k or "token" in k or "secret" in k else v) for k, v in s.items()}
    out["has"] = {
        "anthropic": bool(s["anthropic_api_key"]),
        "threads": bool(s["threads_token"]),
        "coupang": bool(s["coupang_access_key"] and s["coupang_secret_key"]),
        "naver": bool(s["naver_client_id"] and s["naver_client_secret"]),
    }
    return out


@app.post("/api/settings")
def set_settings(body: Settings):
    s = settings()
    for k, v in body.model_dump(exclude_none=True).items():
        if v == "" or "•" in v:
            continue  # 빈 값/마스킹된 값은 유지
        s[k] = v.strip()
    save("settings", s)
    return {"ok": True}


@app.post("/api/settings/clear/{key}")
def clear_setting(key: str):
    s = settings()
    if key in s:
        s[key] = ""
        save("settings", s)
    return {"ok": True}


@app.get("/api/threads/me")
def threads_me():
    me = threads("GET", "/me", {"fields": "id,username,threads_profile_picture_url,threads_biography"})
    s = settings()
    s["threads_username"] = me.get("username", "")
    save("settings", s)
    return me


# ───────────────────────── personas ─────────────────────────
def personas_all() -> list[dict]:
    custom = load("personas", [])
    return DEFAULT_PERSONAS + custom


@app.get("/api/personas")
def get_personas():
    return personas_all()


@app.post("/api/personas")
def upsert_persona(p: Persona):
    custom = load("personas", [])
    if p.id and any(c["id"] == p.id for c in custom):
        for c in custom:
            if c["id"] == p.id:
                c["name"], c["prompt"] = p.name, p.prompt
    else:
        custom.append({"id": p.id or f"p-{uuid.uuid4().hex[:8]}", "name": p.name, "prompt": p.prompt, "builtin": False})
    save("personas", custom)
    return {"ok": True, "personas": personas_all()}


@app.delete("/api/personas/{pid}")
def delete_persona(pid: str):
    custom = [c for c in load("personas", []) if c["id"] != pid]
    save("personas", custom)
    return {"ok": True, "personas": personas_all()}


# ───────────────────────── trends ─────────────────────────
@app.get("/api/trends/google")
def trends_google():
    """Google Trends 한국 실시간 인기 검색어 (RSS, 키 불필요)."""
    try:
        r = requests.get("https://trends.google.co.kr/trending/rss?geo=KR", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"ht": "https://trends.google.com/trending/rss"}
        items = []
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            traffic = it.findtext("ht:approx_traffic", namespaces=ns) or ""
            news = it.find("ht:news_item", ns)
            headline = news.findtext("ht:news_item_title", namespaces=ns) if news is not None else ""
            items.append({"keyword": title, "traffic": traffic, "headline": headline or ""})
        return {"items": items[:30]}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"items": [], "error": f"트렌드를 불러오지 못했어요: {e}"}, status_code=200)


@app.get("/api/trends/naver")
def trends_naver(keywords: str):
    """네이버 데이터랩 검색어 트렌드 비교 (쉼표로 구분, 최대 5개). 키 필요."""
    s = settings()
    if not (s["naver_client_id"] and s["naver_client_secret"]):
        raise HTTPException(400, "네이버 데이터랩 API 키가 없어요.")
    kws = [k.strip() for k in keywords.split(",") if k.strip()][:5]
    end = now_kst().date()
    start = end - timedelta(days=30)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "week",
            "keywordGroups": [{"groupName": k, "keywords": [k]} for k in kws]}
    r = requests.post("https://openapi.naver.com/v1/datalab/search", json=body, timeout=20,
                      headers={"X-Naver-Client-Id": s["naver_client_id"], "X-Naver-Client-Secret": s["naver_client_secret"]})
    if r.status_code != 200:
        raise HTTPException(502, f"네이버 API 오류: {r.text[:200]}")
    return r.json()


# ───────────────────────── benchmark (scraps) ─────────────────────────
def reaction_score(sc: dict) -> int:
    return int(sc.get("likes", 0) * 1 + sc.get("replies", 0) * 3 + sc.get("reposts", 0) * 2)


@app.get("/api/scraps")
def get_scraps():
    items = load("scraps", [])
    for it in items:
        it["score"] = reaction_score(it)
    return sorted(items, key=lambda x: x["score"], reverse=True)


@app.post("/api/scraps")
def add_scrap(sc: Scrap):
    items = load("scraps", [])
    items.append({"id": uuid.uuid4().hex[:8], "created_at": now_kst().isoformat(), **sc.model_dump()})
    save("scraps", items)
    return {"ok": True}


@app.delete("/api/scraps/{sid}")
def del_scrap(sid: str):
    save("scraps", [i for i in load("scraps", []) if i["id"] != sid])
    return {"ok": True}


@app.get("/api/scraps/search")
def scrap_search(q: str):
    """Threads 키워드 검색 (토큰에 threads_keyword_search 권한 필요)."""
    data = threads("GET", "/keyword_search", {"q": q, "search_type": "TOP",
                                               "fields": "id,text,username,permalink,like_count,reply_count,repost_count"})
    return data


class ExtractReq(BaseModel):
    scrap_ids: list[str]
    name: str = ""


@app.post("/api/scraps/extract")
def extract_persona(req: ExtractReq):
    """선택한 스크랩에서 '무엇이 반응을 만드는지'를 뽑아 페르소나 프롬프트로 만든다. 문장 베끼기 금지."""
    items = [i for i in load("scraps", []) if i["id"] in req.scrap_ids]
    if not items:
        raise HTTPException(400, "선택된 글이 없어요.")
    corpus = "\n\n---\n\n".join(f"[좋아요 {i.get('likes',0)} 답글 {i.get('replies',0)} 리포스트 {i.get('reposts',0)}]\n{i['text']}" for i in items)
    system = "너는 SNS 글의 구조와 문체를 분석하는 에디터다. 분석 대상 글의 문장이나 표현을 그대로 재사용하는 지시는 절대 쓰지 않는다."
    user = f"""아래 Threads 글들을 분석해서, 같은 '효과'를 내되 전혀 다른 문장으로 쓰게 하는 페르소나 지시문을 만들어줘.

{corpus}

출력 형식(JSON만):
{{"name": "페르소나 이름(10자 이내)", "prompt": "지시문 300자 이내. 포함할 것: 화자 정체성, 말투(반말/해요체), 문장 길이와 줄 수, 첫 줄 여는 방식, 끝맺는 방식, 자주 쓰는 구조(질문/번호/대비), 피해야 할 것. 원문 문장 인용 금지."}}"""
    out = claude(system, user, 800)
    m = re.search(r"\{.*\}", out, re.S)
    try:
        obj = json.loads(m.group(0) if m else out)
    except json.JSONDecodeError:
        obj = {"name": req.name or "벤치마킹 페르소나", "prompt": out[:300]}
    if req.name:
        obj["name"] = req.name
    return obj


# ───────────────────────── coupang ─────────────────────────
@app.get("/api/coupang/search")
def coupang_search(keyword: str, limit: int = 10):
    path = f"/v2/providers/affiliate_open_api/apis/openapi/products/search?keyword={quote(keyword)}&limit={limit}"
    s = settings()
    if s["coupang_subid"]:
        path += f"&subId={quote(s['coupang_subid'])}"
    r = requests.get(COUPANG_HOST + path, headers=coupang_headers("GET", path), timeout=20)
    if r.status_code != 200:
        raise HTTPException(502, f"쿠팡 API 오류 {r.status_code}: {r.text[:200]}")
    data = r.json().get("data", {}).get("productData", [])
    return [{"title": d.get("productName"), "price": d.get("productPrice"), "url": d.get("productUrl"),
             "image": d.get("productImage"), "rocket": d.get("isRocket")} for d in data]


class DeeplinkReq(BaseModel):
    urls: list[str]


@app.post("/api/coupang/deeplink")
def coupang_deeplink(req: DeeplinkReq):
    path = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
    s = settings()
    body = {"coupangUrls": req.urls}
    if s["coupang_subid"]:
        body["subId"] = s["coupang_subid"]
    r = requests.post(COUPANG_HOST + path, headers=coupang_headers("POST", path), json=body, timeout=20)
    if r.status_code != 200:
        raise HTTPException(502, f"쿠팡 API 오류 {r.status_code}: {r.text[:200]}")
    return r.json().get("data", [])


# ───────────────────────── writer ─────────────────────────
MODE_HINTS = {
    "basic": "기본에 충실하게. 담백하고 공감되게.",
    "hook": "첫 줄을 더 강하게. 숫자나 반전, 질문으로 시작. 단, 낚시·과장은 금지.",
    "persona": "페르소나 말투를 최대한 살려서. 그 사람이 진짜 썼을 법하게.",
}


@app.post("/api/generate")
def generate(req: GenerateReq):
    persona = next((p for p in personas_all() if p["id"] == req.persona_id), None)
    if not persona:
        raise HTTPException(400, "페르소나를 찾을 수 없어요.")
    product_block = ""
    if req.product and (req.product.title or req.product.url):
        p = req.product
        product_block = f"\n소개할 상품: {p.title} / 가격: {p.price or '미상'}\n"
        if req.experience.strip():
            product_block += f"사용자가 직접 써본 경험 메모(이것만 사실로 쓸 수 있음, 없는 내용 추가 금지): {req.experience.strip()}\n"
        elif req.reviews.strip():
            product_block += ("아래는 다른 구매자들의 실제 후기다. 사용자는 이 상품을 직접 써보지 않았다. "
                              "후기를 종합해서 '후기 보니까 ~래', '~라는 평이 많더라', '~는 불만도 있음'처럼 반드시 후기임을 밝히는 톤으로 쓴다. "
                              "1인칭 체험('써봤는데', '쓰고 나서')으로 바꾸지 않는다. 장점 하나와 아쉬운 점 하나를 같이 말한다. 후기 숫자·개수를 지어내지 않는다.\n"
                              f"후기:\n{req.reviews.strip()[:3000]}\n")
        else:
            product_block += "사용자는 이 상품을 직접 써보지 않았다. 써본 것처럼 쓰지 말고, '이런 게 있더라/~라고 함' 식의 발견·소개 톤으로만 쓴다. 상품의 쓸모를 사람의 상황(언제 필요해지는지)과 연결해서 말한다.\n"
        product_block += "글 본문에 링크·가격 홍보 문구('지금 구매', '쟁여놔야')를 넣지 않는다. 링크는 별도로 붙는다.\n"
    system = f"너는 Threads 글을 대신 써주는 작가다.\n\n페르소나: {persona['prompt']}\n\n{STYLE_RULES}"
    user = f"""소재/키워드: {req.keyword}
모드: {MODE_HINTS.get(req.mode, MODE_HINTS['basic'])}
{product_block}{('추가 지시: ' + req.extra) if req.extra else ''}

{req.count}개의 서로 다른 글을 써줘. 각 글은 '===' 한 줄로 구분. 글 외의 설명은 쓰지 마."""
    out = claude(system, user, 1200)
    posts = [p.strip().strip('"') for p in re.split(r"\n=+\n", out) if p.strip()]
    results = []
    for t in posts[: req.count]:
        full = t
        reply = ""
        if req.affiliate and req.product and req.product.url:
            # 공정위 표시는 본문 '맨 앞'에 — 답글에만 두면 불충분
            full = f"{DISCLOSURE}\n\n{t}"
            reply = f"궁금한 사람 있을까 봐 링크 놓고 감 👇\n{req.product.url}"
        results.append({"text": full, "reply_text": reply, "chars": len(full), "lines": len([l for l in t.split('\n') if l.strip()])})
    return {"posts": results, "image_url": (req.product.image if req.product else "")}


# ───────────────────────── publish & queue ─────────────────────────
def _do_publish(item: dict) -> dict:
    pid = threads_publish(item["text"], item.get("image_url", ""))
    rid = ""
    if item.get("reply_text"):
        time.sleep(2)
        rid = threads_publish(item["reply_text"], reply_to=pid)
    return {"post_id": pid, "reply_id": rid}


@app.post("/api/publish")
def publish(req: PublishReq):
    queue = load("queue", [])
    item = {"id": uuid.uuid4().hex[:8], "created_at": now_kst().isoformat(), "tag": req.tag,
            "text": req.text, "image_url": req.image_url, "reply_text": req.reply_text,
            "scheduled_at": req.scheduled_at, "status": "scheduled" if req.scheduled_at else "publishing"}
    if req.scheduled_at:
        queue.append(item)
        save("queue", queue)
        return {"ok": True, "scheduled": True, "item": item}
    try:
        res = _do_publish(item)
        item.update(status="published", published_at=now_kst().isoformat(), **res)
    except HTTPException as e:
        item.update(status="failed", error=e.detail)
        queue.append(item)
        save("queue", queue)
        raise
    queue.append(item)
    save("queue", queue)
    return {"ok": True, "scheduled": False, "item": item}


@app.get("/api/queue")
def get_queue():
    return sorted(load("queue", []), key=lambda x: x.get("scheduled_at") or x["created_at"], reverse=True)


@app.delete("/api/queue/{qid}")
def del_queue(qid: str):
    save("queue", [i for i in load("queue", []) if i["id"] != qid])
    return {"ok": True}


def scheduler_loop():
    while True:
        try:
            queue = load("queue", [])
            changed = False
            for it in queue:
                if it.get("status") != "scheduled":
                    continue
                due = datetime.fromisoformat(it["scheduled_at"])
                if due.tzinfo is None:
                    due = due.replace(tzinfo=KST)
                if due <= now_kst():
                    try:
                        res = _do_publish(it)
                        it.update(status="published", published_at=now_kst().isoformat(), **res)
                    except Exception as e:  # noqa: BLE001
                        it.update(status="failed", error=str(getattr(e, "detail", e)))
                    changed = True
            if changed:
                save("queue", queue)
        except Exception as e:  # noqa: BLE001
            print("scheduler error:", e)
        time.sleep(30)


threading.Thread(target=scheduler_loop, daemon=True).start()


# ───────────────────────── my threads & report ─────────────────────────
@app.get("/api/threads/posts")
def my_posts(limit: int = 25):
    data = threads("GET", "/me/threads", {"fields": "id,text,timestamp,permalink,media_type", "limit": limit})
    posts = data.get("data", [])
    for p in posts:
        try:
            ins = threads("GET", f"/{p['id']}/insights", {"metric": "views,likes,replies,reposts,quotes"})
            for m in ins.get("data", []):
                vals = m.get("values") or []
                p[m["name"]] = (vals[0].get("value") if vals else m.get("total_value", {}).get("value")) or 0
        except HTTPException:
            p["insights_error"] = "권한 없음(threads_manage_insights)"
    return posts


@app.post("/api/threads/report")
def account_report(req: ReportReq):
    posts = my_posts(25)
    if not posts:
        raise HTTPException(400, "분석할 글이 없어요.")
    rows = "\n".join(
        f"- {p.get('timestamp','')[:10]} 조회 {p.get('views',0)} 좋아요 {p.get('likes',0)} 답글 {p.get('replies',0)} 리포스트 {p.get('reposts',0)} | {str(p.get('text',''))[:80]!r}"
        for p in posts)
    system = "너는 Threads 계정 성장 컨설턴트다. 숫자를 근거로, 실행 가능한 조언을 한국어로 쓴다. 과장과 뻔한 말은 뺀다."
    user = f"""계정 목표: {req.goal}

최근 글 {len(posts)}개:
{rows}

다음 구조로 써줘(마크다운):
1) 한 줄 진단
2) 숫자로 본 현재 상태 (잘 된 글 2개, 안 된 글 2개와 왜)
3) 다음 2주 실행 3가지 (각각: 무엇을, 왜, 어떻게 측정)
4) 계정 안전 체크 (조회수 급락·0조회 글·링크 비중 등 리스크 신호가 있으면 짚기)"""
    return {"report": claude(system, user, 1500)}


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8787"))
    print(f"\n  Threads Studio → http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
