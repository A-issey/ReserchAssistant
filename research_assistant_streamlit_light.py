#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import textwrap
from dataclasses import asdict, dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional

import requests
import streamlit as st  # type: ignore

try:
    from PyPDF2 import PdfReader  # type: ignore
except Exception:
    PdfReader = None


# -----------------------------
# Storage

APP_DATA_DIR = Path("app_data")
LIBRARY_JSON = APP_DATA_DIR / "library.json"
PDF_LIBRARY_JSON = APP_DATA_DIR / "pdf_library.json"
PDF_DIR = APP_DATA_DIR / "pdfs"

CROSSREF_BASE_URL = "https://api.crossref.org/works"


def ensure_dirs() -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default):
    try:
        if not path.exists():
            return default
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def sanitize_filename(name: str) -> str:
    name = name.strip().replace("\u0000", "")
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:180] if len(name) > 180 else name


# -----------------------------
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
    added_at: str = ""


@dataclass
class PDFDoc:
    doc_id: str
    filename: str
    path: str
    sha256: str
    pages: Optional[int]
    summary: Optional[str]
    added_at: str


# -----------------------------
# Summarization (simple extractive)


def summarise_text(text: str, sentences: int = 4) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    raw = re.split(r"(?<=[.!?。！？])\s+", text)
    raw = [s.strip() for s in raw if s.strip()]
    if len(raw) <= sentences:
        return " ".join(raw)

    freq: Dict[str, int] = {}
    tokens_re = re.compile(r"[\wぁ-んァ-ン一-龯]{2,}")
    for s in raw:
        for w in tokens_re.findall(s.lower()):
            freq[w] = freq.get(w, 0) + 1

    scored = []
    for idx, s in enumerate(raw):
        score = 0
        for w in tokens_re.findall(s.lower()):
            score += freq.get(w, 0)
        scored.append((score, idx, s))

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:sentences]
    top_sorted = sorted(top, key=lambda x: x[1])
    return " ".join(s for _, _, s in top_sorted)


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"&[a-zA-Z]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def extract_text_from_pdf_bytes(pdf_bytes: bytes, max_pages: int = 10) -> tuple[str, Optional[int]]:
    if PdfReader is None:
        return "", None
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        pages = min(total_pages, max_pages)
        parts: List[str] = []
        for i in range(pages):
            txt = reader.pages[i].extract_text() or ""
            if txt:
                parts.append(txt)
        return "\n".join(parts), total_pages
    except Exception:
        return "", None


def summarise_pdf_bytes(pdf_bytes: bytes) -> tuple[str, Optional[int]]:
    text, total_pages = extract_text_from_pdf_bytes(pdf_bytes)
    if PdfReader is None:
        return "PyPDF2 が未インストールのため、PDF要約を生成できません。", total_pages
    if not text.strip():
        return "PDFからテキストを抽出できませんでした（画像PDFの可能性があります）。", total_pages
    return summarise_text(text, sentences=5), total_pages


def has_japanese(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-龯]", text or ""))


# -----------------------------
# Crossref


def search_crossref(query: str, rows: int, mailto: str, japanese_only: bool) -> List[Paper]:
    params = {
        "query": query,
        "rows": rows,
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created",
        "mailto": mailto,
    }
    headers = {"User-Agent": f"research-assistant-streamlit (mailto:{mailto})"}

    r = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    msg = r.json().get("message", {})
    items = msg.get("items", [])

    papers: List[Paper] = []
    for it in items:
        title = "; ".join(it.get("title", [])).strip() or "(no title)"

        authors_list: List[str] = []
        for a in it.get("author", []) or []:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            name = " ".join([p for p in (given, family) if p]).strip()
            if name:
                authors_list.append(name)
        authors = ", ".join(authors_list) if authors_list else "Unknown"

        journal = "; ".join(it.get("container-title", [])).strip() or ""
        doi = (it.get("DOI") or "").strip()

        year: Optional[int] = None
        for key in ("published-print", "published-online", "created"):
            di = it.get(key) or {}
            try:
                year = di.get("date-parts", [[None]])[0][0]
            except Exception:
                year = None
            if year:
                break

        abstract = it.get("abstract")
        if abstract:
            abstract = strip_html(abstract)

        if japanese_only:
            check = f"{title} {abstract or ''}"
            if not has_japanese(check):
                continue

        summary = summarise_text(abstract, sentences=4) if abstract else None

        papers.append(
            Paper(
                doi=doi,
                title=title,
                authors=authors,
                journal=journal,
                year=year,
                abstract=abstract,
                summary=summary,
                read=False,
                added_at="",
            )
        )
    return papers


# -----------------------------
# Libraries


def load_library() -> List[Paper]:
    data = _load_json(LIBRARY_JSON, [])
    out: List[Paper] = []
    for d in data:
        try:
            out.append(Paper(**d))
        except Exception:
            continue
    return out


def save_library(items: List[Paper]) -> None:
    _save_json(LIBRARY_JSON, [asdict(p) for p in items])


def load_pdf_library() -> List[PDFDoc]:
    data = _load_json(PDF_LIBRARY_JSON, [])
    out: List[PDFDoc] = []
    for d in data:
        try:
            out.append(PDFDoc(**d))
        except Exception:
            continue
    return out


def save_pdf_library(items: List[PDFDoc]) -> None:
    _save_json(PDF_LIBRARY_JSON, [asdict(p) for p in items])


def upsert_paper(p: Paper) -> None:
    lib: List[Paper] = st.session_state.library
    if p.added_at == "":
        p.added_at = datetime.now().isoformat(timespec="seconds")
    if p.doi and any(x.doi == p.doi for x in lib):
        st.toast("すでにライブラリにあります", icon="ℹ️")
        return
    # fallback dedupe by title
    if any(x.title == p.title and (not p.doi) for x in lib):
        st.toast("すでにライブラリにあります", icon="ℹ️")
        return
    lib.append(p)
    save_library(lib)
    st.session_state.library = lib
    st.toast("ライブラリに追加しました", icon="✅")


def remove_paper(idx: int) -> None:
    lib: List[Paper] = st.session_state.library
    if 0 <= idx < len(lib):
        lib.pop(idx)
        save_library(lib)
        st.session_state.library = lib


def toggle_read(idx: int, value: bool) -> None:
    lib: List[Paper] = st.session_state.library
    if 0 <= idx < len(lib):
        lib[idx].read = value
        save_library(lib)
        st.session_state.library = lib


def add_pdf(file_name: str, pdf_bytes: bytes, summary: str, pages: Optional[int]) -> PDFDoc:
    ensure_dirs()
    safe_name = sanitize_filename(file_name)
    sha = hashlib.sha256(pdf_bytes).hexdigest()
    doc_id = sha[:12]

    # Deduplicate by sha
    existing: List[PDFDoc] = st.session_state.pdf_library
    for d in existing:
        if d.sha256 == sha:
            return d

    path = PDF_DIR / f"{doc_id}_{safe_name}"
    with path.open("wb") as f:
        f.write(pdf_bytes)

    doc = PDFDoc(
        doc_id=doc_id,
        filename=safe_name,
        path=str(path),
        sha256=sha,
        pages=pages,
        summary=summary,
        added_at=datetime.now().isoformat(timespec="seconds"),
    )
    existing.append(doc)
    save_pdf_library(existing)
    st.session_state.pdf_library = existing
    return doc


def remove_pdf(idx: int) -> None:
    pdfs: List[PDFDoc] = st.session_state.pdf_library
    if 0 <= idx < len(pdfs):
        path = Path(pdfs[idx].path)
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass
        pdfs.pop(idx)
        save_pdf_library(pdfs)
        st.session_state.pdf_library = pdfs


# -----------------------------
# PDF viewer


def pdf_iframe(pdf_bytes: bytes, height: int = 1100) -> None:
    """Render a scrollable, full PDF viewer in-page.

    Uses an <iframe> with a data: URL. For very large PDFs, the browser may struggle;
    in that case the download button is provided.
    """
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    html = f"""
    <div style="border:1px solid #e6e6ea; border-radius:14px; overflow:hidden; background:#fff;">
      <iframe
        src="data:application/pdf;base64,{b64}"
        style="width:100%; height:{height}px; border:0;"
      ></iframe>
    </div>
    """
    st.components.v1.html(html, height=height + 24)


# -----------------------------
# UI


def inject_css() -> None:
    css = """
    <style>
      :root {
        --bg: #ffffff;
        --page: #ffffff;
        --subtle: #f5f5f7;
        --text: #111111;
        --muted: #2f2f33;
        --muted2: #4b4b50;
        --border: #e6e6ea;
        --accent: #007aff;
      }

      html, body, [data-testid="stAppViewContainer"], [data-testid="stApp"] {
        background: var(--bg) !important;
        color: var(--text) !important;
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Segoe UI", Roboto, sans-serif !important;
        font-size: 17px !important;
        line-height: 1.65 !important;
      }

      /* kill dark-mode leftovers */
      [data-testid="stHeader"], header { background: var(--bg) !important; }

      /* constrain content width & add breathing room */
      .block-container {
        padding-top: 1.2rem;
        padding-bottom: 2.5rem;
        max-width: 1200px;
      }

      /* typography */
      h1, h2, h3, h4 { color: var(--text) !important; letter-spacing: -0.02em; }
      p, li, span, div { color: var(--text); }
      small, .muted { color: var(--muted2) !important; }

      /* inputs: force white backgrounds + readable text */
      input, textarea {
        background: #fff !important;
        color: var(--text) !important;
        border: 1px solid var(--border) !important;
        border-radius: 12px !important;
      }
      textarea { line-height: 1.6 !important; }

      /* Streamlit specific input wrappers */
      div[data-baseweb="input"] > div,
      div[data-baseweb="textarea"] > div {
        background: #fff !important;
        border: 1px solid var(--border) !important;
        border-radius: 12px !important;
      }

      /* buttons */
      button[kind="primary"] {
        background: var(--accent) !important;
        border: 1px solid var(--accent) !important;
        color: #fff !important;
        border-radius: 12px !important;
        padding: 0.55rem 0.9rem !important;
        font-weight: 600 !important;
      }
      button[kind="secondary"], button {
        border-radius: 12px !important;
      }

      /* card container */
      .ra-card {
        background: #fff;
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 1.0rem 1.1rem;
        box-shadow: 0 1px 2px rgba(0,0,0,0.04);
      }
      .ra-title { font-size: 1.1rem; font-weight: 700; margin-bottom: 0.25rem; }
      .ra-meta { color: var(--muted) !important; font-size: 0.95rem; margin-bottom: 0.6rem; }
      .ra-summary { color: var(--text) !important; font-size: 1.0rem; }
      .ra-chip {
        display:inline-block; padding: 2px 10px; border-radius: 999px;
        border: 1px solid var(--border); background: var(--subtle);
        font-size: 0.85rem; color: var(--muted);
      }

      /* sidebar */
      [data-testid="stSidebar"] {
        background: var(--subtle) !important;
        border-right: 1px solid var(--border);
      }
      [data-testid="stSidebar"] * { color: var(--text) !important; }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def topbar() -> None:
    st.markdown(
        """
        <div class="ra-card" style="background:#fff; border:1px solid #e6e6ea;">
          <div style="display:flex; align-items:flex-end; justify-content:space-between; gap:12px;">
            <div>
              <div style="font-size:1.55rem; font-weight:800; letter-spacing:-0.02em;">Research Assistant</div>
              <div class="muted" style="font-size:1.0rem;">論文検索・ライブラリ・PDF管理（読みやすさ最優先）</div>
            </div>
            <div class="ra-chip">Light UI</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def paper_label(p: Paper) -> str:
    status = "✅" if p.read else "⬜"
    year = str(p.year) if p.year else "n/a"
    t = p.title
    if len(t) > 60:
        t = t[:60] + "…"
    return f"{status} {t}  ({year})"


def pdf_label(d: PDFDoc) -> str:
    t = d.filename
    if len(t) > 55:
        t = t[:55] + "…"
    return f"📄 {t}"


def main() -> None:
    st.set_page_config(page_title="Research Assistant", layout="wide")
    ensure_dirs()
    inject_css()

    if "library" not in st.session_state:
        st.session_state.library = load_library()
    if "pdf_library" not in st.session_state:
        st.session_state.pdf_library = load_pdf_library()
    if "search_results" not in st.session_state:
        st.session_state.search_results = []

    # Sidebar settings
    with st.sidebar:
        st.markdown("### 設定")
        mailto = st.text_input("Crossref連絡用メール (mailto)", value=st.session_state.get("mailto", "your_email@example.com"))
        st.session_state.mailto = mailto
        st.divider()
        st.markdown("### PDFビュー")
        viewer_h = st.slider("PDF表示の高さ", min_value=800, max_value=1600, value=int(st.session_state.get("pdf_height", 1200)), step=50)
        st.session_state.pdf_height = viewer_h
        st.caption("※ 画像PDFは要約できないことがあります。")

    topbar()

    tabs = st.tabs(["🔎 検索", "📚 ライブラリ", "📄 PDF"])

    # ----------------- Search
    with tabs[0]:
        st.markdown("#### 論文を検索")
        with st.form("search_form", clear_on_submit=False):
            q = st.text_input("キーワード", placeholder="例: sound localization / 音響 空間" )
            c1, c2, c3 = st.columns([1.0, 1.0, 1.4])
            with c1:
                rows = st.slider("件数", 1, 25, 8)
            with c2:
                jp_only = st.checkbox("日本語が含まれるものだけ", value=True)
            with c3:
                st.caption("Crossrefは完全な日本語フィルタが無いので、タイトル/抄録に日本語が含まれるかで絞ります")
            run = st.form_submit_button("検索", use_container_width=True, type="primary")

        if run and q.strip():
            try:
                st.session_state.search_results = search_crossref(q.strip(), rows, st.session_state.mailto, jp_only)
            except Exception as e:
                st.error(f"検索に失敗しました: {e}")

        results: List[Paper] = st.session_state.search_results
        if results:
            st.markdown("---")
            st.markdown(f"#### 結果 ({len(results)})")
            for i, p in enumerate(results):
                st.markdown("<div class='ra-card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='ra-title'>{p.title}</div>", unsafe_allow_html=True)
                meta = f"{p.authors}"
                if p.journal:
                    meta += f" · {p.journal}"
                if p.year:
                    meta += f" · {p.year}"
                st.markdown(f"<div class='ra-meta'>{meta}</div>", unsafe_allow_html=True)
                if p.summary:
                    st.markdown(f"<div class='ra-summary'>{p.summary}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div class='ra-summary muted'>抄録がないため要約できません。</div>", unsafe_allow_html=True)

                b1, b2 = st.columns([1, 4])
                with b1:
                    if st.button("追加", key=f"add_{i}", type="primary"):
                        upsert_paper(p)
                with b2:
                    if p.abstract:
                        with st.expander("抄録（全文）"):
                            st.write(p.abstract)
                    if p.doi:
                        st.link_button("DOIを開く", f"https://doi.org/{p.doi}")
                st.markdown("</div>", unsafe_allow_html=True)

        else:
            st.info("検索すると結果がここに表示されます。")

    # ----------------- Library
    with tabs[1]:
        st.markdown("#### マイライブラリ")

        lib: List[Paper] = st.session_state.library
        if not lib:
            st.info("ライブラリは空です。検索タブから追加してください。")
        else:
            # filter
            filter_text = st.text_input("ライブラリ内検索", placeholder="タイトル/著者で絞り込み")
            filtered = []
            for p in lib:
                hay = f"{p.title} {p.authors} {p.journal}".lower()
                if not filter_text.strip() or filter_text.lower() in hay:
                    filtered.append(p)

            if not filtered:
                st.warning("該当するアイテムがありません")
            else:
                left, right = st.columns([1.05, 1.95], gap="large")
                with left:
                    st.markdown("<div class='ra-card'>", unsafe_allow_html=True)
                    idx = st.radio(
                        "一覧",
                        options=list(range(len(filtered))),
                        format_func=lambda i: paper_label(filtered[i]),
                        label_visibility="collapsed",
                    )
                    st.markdown("</div>", unsafe_allow_html=True)

                # map idx in filtered to idx in lib
                selected = filtered[idx]
                real_idx = lib.index(selected)

                with right:
                    st.markdown("<div class='ra-card'>", unsafe_allow_html=True)
                    st.markdown(f"<div class='ra-title'>{selected.title}</div>", unsafe_allow_html=True)
                    meta = f"{selected.authors}"
                    if selected.journal:
                        meta += f" · {selected.journal}"
                    if selected.year:
                        meta += f" · {selected.year}"
                    st.markdown(f"<div class='ra-meta'>{meta}</div>", unsafe_allow_html=True)

                    c1, c2, c3 = st.columns([1.1, 1.1, 2.0])
                    with c1:
                        new_val = st.checkbox("既読", value=selected.read, key=f"read_{real_idx}")
                        if new_val != selected.read:
                            toggle_read(real_idx, new_val)
                    with c2:
                        if st.button("削除", key=f"del_{real_idx}"):
                            remove_paper(real_idx)
                            st.rerun()
                    with c3:
                        if selected.doi:
                            st.link_button("DOIを開く", f"https://doi.org/{selected.doi}")

                    if selected.summary:
                        st.markdown("---")
                        st.markdown("**要約**")
                        st.write(selected.summary)
                    if selected.abstract:
                        st.markdown("---")
                        st.markdown("**抄録**")
                        st.write(selected.abstract)
                    st.markdown("</div>", unsafe_allow_html=True)

    # ----------------- PDFs
    with tabs[2]:
        st.markdown("#### PDF管理")

        uploaded = st.file_uploader("PDFをアップロード", type=["pdf"], accept_multiple_files=True)
        if uploaded:
            for f in uploaded:
                pdf_bytes = f.getvalue()
                with st.spinner(f"{f.name} を処理中..."):
                    summary, pages = summarise_pdf_bytes(pdf_bytes)
                    doc = add_pdf(f.name, pdf_bytes, summary, pages)
                st.toast(f"アップロード: {doc.filename}", icon="✅")

        pdfs: List[PDFDoc] = st.session_state.pdf_library
        if not pdfs:
            st.info("PDFはまだありません。アップロードするとここで読めます。")
        else:
            left, right = st.columns([1.05, 1.95], gap="large")
            with left:
                st.markdown("<div class='ra-card'>", unsafe_allow_html=True)
                pidx = st.radio(
                    "PDF一覧",
                    options=list(range(len(pdfs))),
                    format_func=lambda i: pdf_label(pdfs[i]),
                    label_visibility="collapsed",
                )
                st.markdown("</div>", unsafe_allow_html=True)

            doc = pdfs[pidx]
            path = Path(doc.path)
            try:
                pdf_bytes = path.read_bytes()
            except Exception:
                pdf_bytes = b""

            with right:
                st.markdown("<div class='ra-card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='ra-title'>{doc.filename}</div>", unsafe_allow_html=True)
                meta = f"追加: {doc.added_at}"
                if doc.pages:
                    meta += f" · {doc.pages} pages"
                st.markdown(f"<div class='ra-meta'>{meta}</div>", unsafe_allow_html=True)

                c1, c2, c3 = st.columns([1.2, 1.2, 2.2])
                with c1:
                    st.download_button("ダウンロード", data=pdf_bytes, file_name=doc.filename, mime="application/pdf", use_container_width=True)
                with c2:
                    if st.button("削除", key=f"pdf_del_{doc.doc_id}", use_container_width=True):
                        remove_pdf(pidx)
                        st.rerun()
                with c3:
                    st.caption("※ PDFが大きい場合、埋め込み表示が重いことがあります。その場合はダウンロード推奨。")

                if doc.summary:
                    st.markdown("---")
                    st.markdown("**要約**")
                    st.write(doc.summary)

                st.markdown("---")
                st.markdown("**PDF本文（ページ内で読めます）**")
                if pdf_bytes:
                    pdf_iframe(pdf_bytes, height=int(st.session_state.pdf_height))
                else:
                    st.error("PDFファイルを読み込めませんでした。")

                st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
