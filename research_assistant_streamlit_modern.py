#!/usr/bin/env python3
"""research_assistant_streamlit_v2.py

可読性最優先（高コントラスト）で作り直したStreamlit版 研究アシスタント。

目的
 - 白背景 + 白文字の事故を起こさない（強制的に黒系文字 + 太めの行間）
 - 論文：Crossrefで査読付き（journal-article）を検索し、要約して保存
 - PDF：アップロードしたPDFを「要約」だけでなく、ページ内で全文閲覧（iframe）

注意
 - Streamlit Cloudの無料枠は永続ストレージではありません（再デプロイ等で消えることがあります）。
   クラウド永続化が必要なら、S3等の外部ストレージ連携が別途必要です。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import textwrap
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

import requests
import streamlit as st

try:
    from PyPDF2 import PdfReader  # type: ignore
except Exception:
    PdfReader = None


# ---------------------------
# Settings

CROSSREF_BASE_URL = "https://api.crossref.org/works"
CONTACT_EMAIL = "user@example.com"  # あなたのメールに変更推奨

LIBRARY_FILE = "library.json"
PDF_LIBRARY_FILE = "pdf_library.json"
PDF_UPLOAD_DIR = "pdf_uploads"


# ---------------------------
# Models


@dataclass
class Paper:
    doi: str
    title: str
    authors: str
    journal: str
    year: Optional[int]
    abstract: Optional[str]
    summary: Optional[str]
    read: bool = False


@dataclass
class PDFDoc:
    id: str  # sha256
    filename: str
    path: str
    summary: Optional[str]


# ---------------------------
# Utils


JA_RE = re.compile(r"[ぁ-んァ-ン一-龯]")


def safe_strip_html(text: str) -> str:
    # CrossrefのabstractはJATS XMLのことがある
    t = re.sub(r"<[^>]+>", " ", text)
    t = re.sub(r"&[a-zA-Z]+;", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def summarise_text(text: str, sentences: int = 3) -> str:
    sents = re.split(r"(?<=[.!?。！？])\s+", (text or "").strip())
    sents = [s.strip() for s in sents if s.strip()]
    if len(sents) <= sentences:
        return " ".join(sents)

    # 超シンプルな頻度ベース
    freq: Dict[str, int] = {}
    for s in sents:
        for w in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", s.lower()):
            freq[w] = freq.get(w, 0) + 1

    scored = []
    for i, s in enumerate(sents):
        score = sum(freq.get(w.lower(), 0) for w in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", s))
        scored.append((score, i, s))
    top = sorted(scored, key=lambda x: x[0], reverse=True)[:sentences]
    top = sorted(top, key=lambda x: x[1])
    return " ".join(s for _, __, s in top)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_library() -> List[Paper]:
    data = load_json(LIBRARY_FILE, [])
    out: List[Paper] = []
    for item in data:
        try:
            out.append(Paper(**item))
        except Exception:
            continue
    return out


def save_library(items: List[Paper]) -> None:
    save_json(LIBRARY_FILE, [asdict(x) for x in items])


def load_pdf_library() -> List[PDFDoc]:
    data = load_json(PDF_LIBRARY_FILE, [])
    out: List[PDFDoc] = []
    for item in data:
        try:
            out.append(PDFDoc(**item))
        except Exception:
            continue
    return out


def save_pdf_library(items: List[PDFDoc]) -> None:
    save_json(PDF_LIBRARY_FILE, [asdict(x) for x in items])


def crossref_search(query: str, rows: int = 10, japanese_only: bool = False) -> List[Paper]:
    params = {
        "query": query,
        "rows": rows,
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created",
        "mailto": CONTACT_EMAIL,
    }
    headers = {"User-Agent": f"research-assistant/2.0 (mailto:{CONTACT_EMAIL})"}

    r = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    items = r.json().get("message", {}).get("items", [])

    results: List[Paper] = []
    for it in items:
        doi = it.get("DOI", "") or ""
        title = "; ".join(it.get("title", []) or [])
        journal = "; ".join(it.get("container-title", []) or [])
        authors_list = []
        for a in it.get("author", []) or []:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            nm = " ".join([x for x in [given, family] if x])
            if nm:
                authors_list.append(nm)
        authors = ", ".join(authors_list) if authors_list else "Unknown"

        year: Optional[int] = None
        for key in ("published-print", "published-online", "created"):
            di = it.get(key)
            try:
                year = di["date-parts"][0][0]
                break
            except Exception:
                pass

        abstract = it.get("abstract")
        if abstract:
            abstract = safe_strip_html(abstract)

        paper = Paper(
            doi=doi,
            title=title or "(no title)",
            authors=authors,
            journal=journal,
            year=year,
            abstract=abstract,
            summary=summarise_text(abstract, 3) if abstract else None,
            read=False,
        )

        if japanese_only:
            chk = f"{paper.title} {paper.abstract or ''}"
            if not JA_RE.search(chk):
                continue

        results.append(paper)

    return results


def summarise_pdf_bytes(pdf_bytes: bytes) -> str:
    if PdfReader is None:
        return "PyPDF2が未導入のため要約できません（requirements.txtにPyPDF2を追加してください）。"
    try:
        from io import BytesIO

        reader = PdfReader(BytesIO(pdf_bytes))
        text_parts: List[str] = []
        max_pages = min(len(reader.pages), 12)
        for i in range(max_pages):
            t = reader.pages[i].extract_text() or ""
            if t.strip():
                text_parts.append(t)
        full = "\n".join(text_parts).strip()
        if not full:
            return "PDFからテキストを抽出できませんでした（画像PDFの可能性があります）。"
        return summarise_text(full, 4)
    except Exception as e:
        return f"PDF要約に失敗しました: {e}"


def pdf_iframe_viewer(pdf_bytes: bytes, height_px: int = 900) -> None:
    """data: URLでPDFをiframe表示。高さを大きめにして『全体表示されない』を回避。"""
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    html = f"""
    <div style="border:1px solid rgba(0,0,0,0.08); border-radius:16px; overflow:hidden; background:#fff;">
      <iframe
        src="data:application/pdf;base64,{b64}#toolbar=1&navpanes=0&scrollbar=1"
        style="width:100%; height:{height_px}px; border:0;"
      ></iframe>
    </div>
    """
    st.components.v1.html(html, height=height_px + 30)


# ---------------------------
# UI


def inject_css(font_px: int = 18) -> None:
    # とにかく白文字事故を潰す：色を強制
    css = f"""
    <style>
      :root {{
        --bg: #f5f5f7;
        --card: #ffffff;
        --text: #111111;
        --muted: #3a3a3c;
        --muted2: #5a5a5f;
        --border: rgba(0,0,0,0.10);
        --accent: #007aff;
      }}

      html, body, [data-testid="stApp"] {{
        background: var(--bg) !important;
        color: var(--text) !important;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif !important;
        font-size: {font_px}px !important;
        line-height: 1.65 !important;
      }}

      /* Streamlitが勝手に色を薄くする箇所を強制上書き */
      p, span, div, label, li, a {{
        color: var(--text);
      }}
      small {{ color: var(--muted2) !important; }}
      [data-testid="stMarkdownContainer"] * {{ color: var(--text) !important; }}

      /* 入力UI（黒背景になるのを潰す） */
      input, textarea {{
        background: #fff !important;
        color: var(--text) !important;
        border: 1px solid var(--border) !important;
        border-radius: 12px !important;
      }}
      textarea {{ line-height: 1.7 !important; }}

      /* ボタン */
      .stButton > button {{
        background: var(--accent) !important;
        color: #fff !important;
        border: none !important;
        border-radius: 999px !important;
        padding: 10px 16px !important;
        font-weight: 600 !important;
      }}
      .stButton > button * {{
        color: #fff !important;
      }}
      .stButton > button:hover {{ filter: brightness(0.95); }}

      /* カード */
      .ra-card {{
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 20px;
        padding: 18px 18px;
        box-shadow: 0 6px 18px rgba(0,0,0,0.06);
        margin-bottom: 14px;
      }}
      .ra-title {{ font-size: 1.1em; font-weight: 700; margin-bottom: 8px; color: var(--text) !important; }}
      .ra-meta {{ color: var(--muted) !important; font-size: 0.95em; margin-bottom: 10px; }}
      .ra-summary {{ color: var(--text) !important; font-size: 1.0em; }}

      /* タブ */
      button[role="tab"] {{
        font-size: 1.0em !important;
        padding: 10px 14px !important;
      }}

      /* 見出し */
      h1, h2, h3 {{ color: var(--text) !important; }}

      /* リンク */
      a {{ color: var(--accent) !important; }}

      /* expanderの本文 */
      details, summary {{ color: var(--text) !important; }}
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def card_open(title: str, meta: str = ""):
    st.markdown("<div class='ra-card'>", unsafe_allow_html=True)
    st.markdown(f"<div class='ra-title'>{title}</div>", unsafe_allow_html=True)
    if meta:
        st.markdown(f"<div class='ra-meta'>{meta}</div>", unsafe_allow_html=True)


def card_close():
    st.markdown("</div>", unsafe_allow_html=True)


def main() -> None:
    st.set_page_config(page_title="Research Assistant", layout="wide")

    font_px = st.session_state.get("font_px", 18)
    inject_css(font_px=font_px)

    if "library" not in st.session_state:
        st.session_state.library = load_library()
    if "pdf_library" not in st.session_state:
        st.session_state.pdf_library = load_pdf_library()

    st.title("Research Assistant")
    st.caption("可読性最優先（高コントラスト）。論文検索 / ライブラリ / PDF全文閲覧")

    tabs = st.tabs(["検索", "ライブラリ", "PDF", "設定"])

    # ---------------- Search
    with tabs[0]:
        st.subheader("論文検索")
        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            query = st.text_input("キーワード", placeholder="例: sound, film education, Unreal Engine" )
        with c2:
            rows = st.slider("件数", 1, 30, 8)
        with c3:
            japanese_only = st.checkbox("日本語っぽい論文だけ", value=False)

        if st.button("検索する", use_container_width=False) and query.strip():
            try:
                st.session_state.search_results = crossref_search(query.strip(), rows=rows, japanese_only=japanese_only)
            except Exception as e:
                st.error(f"検索に失敗しました: {e}")

        results: List[Paper] = st.session_state.get("search_results", [])
        if results:
            st.markdown("---")
            st.subheader("結果")
            cols = st.columns(2)
            for i, p in enumerate(results):
                with cols[i % 2]:
                    year = p.year if p.year else "n/a"
                    meta = f"{p.authors}<br>{p.journal or 'Unknown'} / {year}"
                    card_open(p.title, meta)
                    if p.summary:
                        st.markdown(f"<div class='ra-summary'>{textwrap.shorten(p.summary, 260, placeholder='…')}</div>", unsafe_allow_html=True)
                    else:
                        st.markdown("<div class='ra-summary'>要約なし</div>", unsafe_allow_html=True)

                    if st.button("ライブラリに保存", key=f"save_{i}"):
                        lib: List[Paper] = st.session_state.library
                        if p.doi and any(x.doi == p.doi for x in lib):
                            st.warning("すでに保存済み")
                        else:
                            lib.append(p)
                            save_library(lib)
                            st.session_state.library = lib
                            st.success("保存しました")
                    card_close()

    # ---------------- Library
    with tabs[1]:
        st.subheader("マイライブラリ")
        lib: List[Paper] = st.session_state.library
        if not lib:
            st.info("まだ空です。検索タブから保存してください。")
        else:
            # 左リスト / 右詳細（見やすさ重視）
            left, right = st.columns([1, 2])
            with left:
                options = [f"{'✅' if p.read else '⬜'} {p.title[:60]}" for p in lib]
                idx = st.selectbox("論文", list(range(len(options))), format_func=lambda i: options[i])
                if st.button("既読/未読を切替", use_container_width=True):
                    lib[idx].read = not lib[idx].read
                    save_library(lib)
                    st.session_state.library = lib
                if st.button("この論文を削除", use_container_width=True):
                    lib.pop(idx)
                    save_library(lib)
                    st.session_state.library = lib
                    st.rerun()
            with right:
                p = lib[idx]
                year = p.year if p.year else "n/a"
                meta = f"著者: {p.authors}<br>ジャーナル: {p.journal or 'Unknown'}<br>年: {year}<br>DOI: {p.doi or 'n/a'}"
                card_open(p.title, meta)
                if p.summary:
                    st.markdown(f"<div class='ra-summary'>{p.summary}</div>", unsafe_allow_html=True)
                if p.abstract:
                    with st.expander("抄録を表示"):
                        st.write(p.abstract)
                card_close()

    # ---------------- PDFs
    with tabs[2]:
        st.subheader("PDF管理")
        st.caption("アップロードしたPDFは『要約』だけでなくページ内で全文閲覧できます。")

        up = st.file_uploader("PDFをアップロード", type=["pdf"], accept_multiple_files=False)
        if up is not None:
            pdf_bytes = up.getvalue()
            doc_id = sha256_bytes(pdf_bytes)

            os.makedirs(PDF_UPLOAD_DIR, exist_ok=True)
            path = os.path.join(PDF_UPLOAD_DIR, f"{doc_id}_{up.name}")

            pdfs: List[PDFDoc] = st.session_state.pdf_library
            if any(d.id == doc_id for d in pdfs):
                st.warning("同じPDF（内容一致）がすでにあります")
            else:
                with open(path, "wb") as f:
                    f.write(pdf_bytes)
                with st.spinner("要約生成中…"):
                    summary = summarise_pdf_bytes(pdf_bytes)
                pdfs.append(PDFDoc(id=doc_id, filename=up.name, path=path, summary=summary))
                save_pdf_library(pdfs)
                st.session_state.pdf_library = pdfs
                st.success("アップロードしました")

        pdfs: List[PDFDoc] = st.session_state.pdf_library
        if not pdfs:
            st.info("まだPDFがありません")
        else:
            left, right = st.columns([1, 2])
            with left:
                names = [d.filename for d in pdfs]
                pidx = st.selectbox("PDF", list(range(len(names))), format_func=lambda i: names[i])
                if st.button("このPDFを削除", use_container_width=True):
                    try:
                        os.remove(pdfs[pidx].path)
                    except Exception:
                        pass
                    pdfs.pop(pidx)
                    save_pdf_library(pdfs)
                    st.session_state.pdf_library = pdfs
                    st.rerun()
            with right:
                d = pdfs[pidx]
                card_open(d.filename, f"ID: {d.id[:12]}…")
                if d.summary:
                    st.markdown(f"<div class='ra-summary'>{d.summary}</div>", unsafe_allow_html=True)

                # PDF全文（ページ内）
                with open(d.path, "rb") as f:
                    pdf_bytes = f.read()

                st.markdown("#### PDF全文")
                pdf_iframe_viewer(pdf_bytes, height_px=900)

                st.download_button(
                    "PDFをダウンロード",
                    data=pdf_bytes,
                    file_name=d.filename,
                    mime="application/pdf",
                    use_container_width=True,
                )
                card_close()

    # ---------------- Settings
    with tabs[3]:
        st.subheader("表示設定")
        st.caption("文字が小さい/薄いと感じたら、まずここを上げる")
        new_font = st.slider("ベースフォント(px)", 16, 22, int(font_px))
        if new_font != font_px:
            st.session_state.font_px = int(new_font)
            st.rerun()


if __name__ == "__main__":
    main()
