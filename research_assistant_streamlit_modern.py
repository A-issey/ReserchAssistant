#!/usr/bin/env python3
"""
映画サイト風デザインを参考にした研究論文アシスタント。

このアプリはCrossrefによる論文検索、ライブラリ管理、PDFアップロード＆要約機能を提供しつつ、
モダンで映画サイトのようなカードレイアウトとダークヘッダーナビゲーションを備えています。

参考サイト（`orange269152.studio.site/films`）の特徴：
  * ダークなヘッダーにホワイトのナビゲーションリンク
  * オフホワイトの背景に整然と並んだカード
  * カードは画像やテキストを縦長の枠に収め、余白を十分に設けている

このファイルを実行するには以下のライブラリが必要です。
  - streamlit
  - requests
  - PyPDF2 (PDF要約用、インストールしていない場合はPDF機能が無効化されます)

"""

from __future__ import annotations

import os
import re
import json
import textwrap
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional

import streamlit as st  # type: ignore
import requests

try:
    from PyPDF2 import PdfReader  # type: ignore
except ImportError:
    PdfReader = None  # PDF機能を有効にするにはPyPDF2が必要


# ---------------------------------------------------------------------------
# 設定

CONTACT_EMAIL = "user@example.com"
SUMMARY_SENTENCES = 3
LIBRARY_FILE = "library.json"
PDF_LIBRARY_FILE = "pdf_library.json"
CROSSREF_BASE_URL = "https://api.crossref.org/works"
PDF_UPLOAD_DIR = "pdf_uploads"


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

    @classmethod
    def from_crossref(cls, item: Dict) -> "Paper":
        doi = item.get("DOI", "")
        title = "; ".join(item.get("title", []))
        authors_list: List[str] = []
        for author in item.get("author", []):
            given = author.get("given", "").strip()
            family = author.get("family", "").strip()
            name = " ".join(part for part in (given, family) if part)
            if name:
                authors_list.append(name)
        authors = ", ".join(authors_list) if authors_list else "Unknown"
        journal = "; ".join(item.get("container-title", [])) or ""
        year: Optional[int] = None
        for key in ("published-print", "published-online", "created"):
            date_info = item.get(key)
            if date_info and isinstance(date_info.get("date-parts", []), list):
                try:
                    year = date_info["date-parts"][0][0]
                    break
                except (IndexError, TypeError):
                    continue
        abstract = item.get("abstract")
        if abstract:
            abstract_text = re.sub(r"<[^>]+>", "", abstract)
            abstract_text = re.sub(r"&[a-z]+;", "", abstract_text)
            abstract = abstract_text.strip()
        else:
            abstract = None
        return cls(doi=doi, title=title, authors=authors, journal=journal,
                   year=year, abstract=abstract, summary=None)


@dataclass
class PDFDoc:
    filename: str
    path: str
    summary: Optional[str]


def summarise_text(text: str, sentences: int = SUMMARY_SENTENCES) -> str:
    raw_sentences = re.split(r"(?<=[.!?。！？])\s+", text)
    if len(raw_sentences) <= sentences:
        return text
    word_freq: Dict[str, int] = {}
    for sentence in raw_sentences:
        for word in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", sentence.lower()):
            word_freq[word] = word_freq.get(word, 0) + 1
    scored_sentences = []
    for sentence in raw_sentences:
        score = sum(word_freq.get(word.lower(), 0) for word in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", sentence))
        scored_sentences.append((score, sentence))
    top_sentences = sorted(scored_sentences, key=lambda x: x[0], reverse=True)[:sentences]
    top_sentences_sorted = sorted(top_sentences, key=lambda x: raw_sentences.index(x[1]))
    return " ".join(sentence.strip() for (_, sentence) in top_sentences_sorted)


def summarise_pdf(path: str) -> str:
    if PdfReader is None:
        return "PyPDF2がインストールされていないため、PDFの要約を生成できません。"
    try:
        reader = PdfReader(path)
        text_parts: List[str] = []
        max_pages = min(len(reader.pages), 10)
        for i in range(max_pages):
            page = reader.pages[i]
            content = page.extract_text() or ""
            text_parts.append(content)
        full_text = "\n".join(text_parts)
        if not full_text.strip():
            return "このPDFからテキストを抽出できませんでした。"
        return summarise_text(full_text)
    except Exception as e:
        return f"PDFの読み込みに失敗しました: {e}"


def search_crossref(query: str, rows: int = 10, japanese_only: bool = False) -> List[Paper]:
    params = {
        "query": query,
        "rows": rows,
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created",
        "mailto": CONTACT_EMAIL,
    }
    headers = {
        "User-Agent": f"research-assistant-films/1.0 (mailto:{CONTACT_EMAIL})"
    }
    try:
        response = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        items = data.get("message", {}).get("items", [])
        papers: List[Paper] = []
        for item in items:
            paper = Paper.from_crossref(item)
            if japanese_only:
                text_to_check = (paper.title or "") + " " + (paper.abstract or "")
                if not re.search(r"[ぁ-んァ-ン一-龯]", text_to_check):
                    continue
            if paper.abstract and not paper.summary:
                paper.summary = summarise_text(paper.abstract)
            papers.append(paper)
        return papers
    except Exception as e:
        st.error(f"検索に失敗しました: {e}")
        return []


def load_library() -> List[Paper]:
    if not os.path.exists(LIBRARY_FILE):
        return []
    try:
        with open(LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Paper(**item) for item in data]
    except Exception:
        return []


def save_library(library: List[Paper]) -> None:
    with open(LIBRARY_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in library], f, ensure_ascii=False, indent=2)


def load_pdf_library() -> List[PDFDoc]:
    if not os.path.exists(PDF_LIBRARY_FILE):
        return []
    try:
        with open(PDF_LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [PDFDoc(**item) for item in data]
    except Exception:
        return []


def save_pdf_library(pdf_library: List[PDFDoc]) -> None:
    with open(PDF_LIBRARY_FILE, "w", encoding="utf-8") as f:
        json.dump([asdict(p) for p in pdf_library], f, ensure_ascii=False, indent=2)


def add_paper_to_library(paper: Paper) -> None:
    library: List[Paper] = st.session_state.library
    if not any(p.doi == paper.doi for p in library):
        library.append(paper)
        save_library(library)
        st.session_state.library = library
        st.success(f"'{paper.title}' をライブラリに追加しました。")
    else:
        st.warning("既にライブラリに存在します。")


def toggle_read_status(idx: int) -> None:
    library: List[Paper] = st.session_state.library
    library[idx].read = not library[idx].read
    save_library(library)
    st.session_state.library = library


def add_pdf_to_library(file, summary: str) -> None:
    os.makedirs(PDF_UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(PDF_UPLOAD_DIR, file.name)
    with open(file_path, "wb") as f:
        f.write(file.getbuffer())
    pdf_library: List[PDFDoc] = st.session_state.pdf_library
    pdf_library.append(PDFDoc(filename=file.name, path=file_path, summary=summary))
    save_pdf_library(pdf_library)
    st.session_state.pdf_library = pdf_library
    st.success(f"PDF '{file.name}' をアップロードしました。")


def inject_css() -> None:
    """ページスタイルとしてダークヘッダーとカードレイアウトのCSSを挿入します。"""
    css = """
    <style>
    /* 全体背景をオフホワイトに設定 */
    html, body, [data-testid="stApp"] {
        background-color: #f5f5f5;
        color: #333;
        font-family: "Helvetica Neue", Arial, sans-serif;
    }
    /* ヘッダー */
    .top-header {
        background-color: #111;
        color: white;
        padding: 1rem 2rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 2rem;
    }
    .nav-links a {
        color: #fff;
        margin-left: 1.5rem;
        text-decoration: none;
        font-size: 0.9rem;
        letter-spacing: 0.05rem;
    }
    .nav-links a:hover {
        text-decoration: underline;
    }
    /* カードスタイル */
    .card {
        background-color: #ffffff;
        border-radius: 8px;
        padding: 1rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.1);
    }
    .card-title {
        font-size: 1.1rem;
        font-weight: bold;
        margin-bottom: 0.25rem;
    }
    .card-meta {
        font-size: 0.8rem;
        color: #666;
        margin-bottom: 0.5rem;
    }
    .card-summary {
        font-size: 0.9rem;
        line-height: 1.4;
    }
    .section-title {
        font-size: 1.5rem;
        font-weight: bold;
        margin-bottom: 1rem;
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def render_header() -> None:
    """上部のヘッダーナビゲーションを描画します。"""
    header_html = """
    <div class="top-header">
        <div class="logo">研究アシスタント</div>
        <div class="nav-links">
            <a href="#search">検索</a>
            <a href="#library">ライブラリ</a>
            <a href="#pdf">PDF管理</a>
        </div>
    </div>
    """
    st.markdown(header_html, unsafe_allow_html=True)


def main() -> None:
    inject_css()
    render_header()

    # セッション状態の初期化
    if "library" not in st.session_state:
        st.session_state.library = load_library()
    if "pdf_library" not in st.session_state:
        st.session_state.pdf_library = load_pdf_library()

    # 検索セクション
    st.markdown("<div id='search'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>論文を検索</div>", unsafe_allow_html=True)
    with st.form(key="search_form"):
        query = st.text_input("検索キーワード")
        rows = st.slider("取得件数", min_value=1, max_value=50, value=5, key="rows_slider")
        japanese_only = st.checkbox("日本語論文のみ", value=False)
        submitted = st.form_submit_button("検索")
    if submitted and query:
        st.session_state.search_results = search_crossref(query, rows, japanese_only=japanese_only)

    # 検索結果表示
    if st.session_state.get("search_results"):
        papers = st.session_state.search_results
        # 2列レイアウト
        cols = st.columns(2)
        for idx, paper in enumerate(papers):
            with cols[idx % 2]:
                st.markdown("<div class='card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='card-title'>{paper.title}</div>", unsafe_allow_html=True)
                year_str = f"{paper.year}" if paper.year else "n/a"
                st.markdown(f"<div class='card-meta'>著者: {paper.authors}<br>ジャーナル: {paper.journal or '不明'}<br>年: {year_str}</div>", unsafe_allow_html=True)
                if paper.summary:
                    snippet = textwrap.shorten(paper.summary, width=120, placeholder="...")
                    st.markdown(f"<div class='card-summary'>{snippet}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div class='card-summary'>要約がありません。</div>", unsafe_allow_html=True)
                if st.button("ライブラリに追加", key=f"add_result_{idx}"):
                    add_paper_to_library(paper)
                st.markdown("</div>", unsafe_allow_html=True)

    # ライブラリセクション
    st.markdown("<div id='library'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>マイライブラリ</div>", unsafe_allow_html=True)
    if st.session_state.library:
        cols = st.columns(2)
        for idx, paper in enumerate(st.session_state.library):
            with cols[idx % 2]:
                st.markdown("<div class='card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='card-title'>{paper.title}</div>", unsafe_allow_html=True)
                year_str = f"{paper.year}" if paper.year else "n/a"
                status = "既読" if paper.read else "未読"
                st.markdown(f"<div class='card-meta'>ステータス: {status}<br>年: {year_str}</div>", unsafe_allow_html=True)
                if paper.summary:
                    st.markdown(f"<div class='card-summary'>{textwrap.fill(paper.summary, width=80)}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div class='card-summary'>要約がありません。</div>", unsafe_allow_html=True)
                # 既読トグル
                if st.checkbox("既読", value=paper.read, key=f"read_toggle_lib_{idx}"):
                    if not paper.read:
                        toggle_read_status(idx)
                else:
                    if paper.read:
                        toggle_read_status(idx)
                st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.info("ライブラリは空です。検索結果から論文を追加できます。")

    # PDF管理セクション
    st.markdown("<div id='pdf'></div>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>PDF管理</div>", unsafe_allow_html=True)
    uploaded_file = st.file_uploader("PDFをアップロード", type=["pdf"])
    if uploaded_file is not None:
        with st.spinner("PDFを処理中..."):
            summary = summarise_pdf(uploaded_file) if PdfReader is not None else ""
            add_pdf_to_library(uploaded_file, summary)

    if st.session_state.pdf_library:
        cols = st.columns(2)
        for idx, pdfdoc in enumerate(st.session_state.pdf_library):
            with cols[idx % 2]:
                st.markdown("<div class='card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='card-title'>{pdfdoc.filename}</div>", unsafe_allow_html=True)
                if pdfdoc.summary:
                    snippet = textwrap.shorten(pdfdoc.summary, width=120, placeholder="...")
                    st.markdown(f"<div class='card-summary'>{snippet}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div class='card-summary'>要約がありません。</div>", unsafe_allow_html=True)
                # ダウンロードボタン
                with open(pdfdoc.path, "rb") as f:
                    data = f.read()
                    st.download_button("PDFをダウンロード", data=data, file_name=pdfdoc.filename, mime="application/pdf", key=f"download_{idx}")
                st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.info("アップロードされたPDFはまだありません。")


if __name__ == "__main__":
    main()
