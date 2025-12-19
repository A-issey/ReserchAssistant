#!/usr/bin/env python3
"""
Appleデザインを参考にしたUI/UXで、論文検索・ライブラリ管理・PDF閲覧を行うStreamlitアプリ。

このアプリの特長：
  * シンプルで洗練されたナビゲーションバーと明るい背景、余白を活かしたレイアウト。
  * Crossref APIで査読付き論文を検索し、要約付きで表示します。
  * ライブラリに保存した論文の既読管理と詳細表示。
  * PDFファイルのアップロードと要約生成に加えて、アップロードしたPDFをページ内で直接閲覧できます。

参考にしたAppleのデザイン原則：余白を多めに取り、タイポグラフィに気を配り、控えめなカラーアクセントを使用することで、コンテンツに集中しやすいUIを目指します。
"""

from __future__ import annotations

import os
import re
import json
import textwrap
import base64
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional

import streamlit as st  # type: ignore
import requests

try:
    from PyPDF2 import PdfReader  # type: ignore
except ImportError:
    PdfReader = None


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
        "User-Agent": f"research-assistant-apple/1.0 (mailto:{CONTACT_EMAIL})"
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


def embed_pdf(path: str) -> None:
    """PDFをページ内に埋め込んで表示する。"""
    try:
        with open(path, "rb") as f:
            base64_pdf = base64.b64encode(f.read()).decode("utf-8")
        pdf_display = f"<embed src='data:application/pdf;base64,{base64_pdf}' width='100%' height='600px' type='application/pdf'>"
        st.components.v1.html(pdf_display, height=600)
    except Exception as e:
        st.error(f"PDF表示に失敗しました: {e}")


def inject_css() -> None:
    """Apple風のスタイルを注入する。"""
    css = """
    <style>
    html, body, [data-testid="stApp"] {
        background-color: #f4f4f6;
        color: #1d1d1f;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    /* ナビゲーションバー */
    .navbar {
        background-color: #ffffff;
        border-bottom: 1px solid #e5e5ea;
        padding: 1rem 2rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 2rem;
    }
    .navbar a {
        color: #007aff;
        margin-left: 1.5rem;
        text-decoration: none;
        font-size: 0.9rem;
    }
    .navbar a:hover {
        text-decoration: underline;
    }
    .navbar .logo {
        font-weight: 600;
        font-size: 1.2rem;
    }
    /* セクションタイトル */
    .section-title {
        font-size: 1.5rem;
        font-weight: 600;
        margin: 1.5rem 0 0.5rem;
        color: #1d1d1f;
    }
    /* カード */
    .card {
        background-color: #ffffff;
        border-radius: 12px;
        padding: 1.25rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
    }
    .card-title {
        font-size: 1.2rem;
        font-weight: 600;
        margin-bottom: 0.4rem;
        color: #1d1d1f;
    }
    .card-meta {
        font-size: 0.85rem;
        color: #6e6e73;
        margin-bottom: 0.6rem;
    }
    .card-summary {
        font-size: 0.9rem;
        line-height: 1.5;
        color: #3c3c43;
        margin-bottom: 0.6rem;
    }
    .primary-btn {
        background-color: #007aff;
        color: #ffffff;
        padding: 0.4rem 0.8rem;
        border-radius: 8px;
        font-size: 0.9rem;
        border: none;
        cursor: pointer;
    }
    .primary-btn:hover {
        background-color: #005bb5;
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def render_navbar() -> None:
    navbar_html = """
    <div class="navbar">
        <div class="logo">研究アシスタント</div>
        <div class="nav-links">
            <a href="#search">検索</a>
            <a href="#library">ライブラリ</a>
            <a href="#pdf">PDF管理</a>
        </div>
    </div>
    """
    st.markdown(navbar_html, unsafe_allow_html=True)


def main() -> None:
    inject_css()
    render_navbar()

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
        rows = st.slider("取得件数", min_value=1, max_value=50, value=5)
        japanese_only = st.checkbox("日本語論文のみ", value=False)
        submitted = st.form_submit_button("検索")
    if submitted and query:
        st.session_state.search_results = search_crossref(query, rows, japanese_only=japanese_only)

    # 検索結果表示
    if st.session_state.get("search_results"):
        papers = st.session_state.search_results
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
                # 全文表示とダウンロード
                with st.expander("全文を表示"):
                    embed_pdf(pdfdoc.path)
                with open(pdfdoc.path, "rb") as f:
                    data = f.read()
                    st.download_button("PDFをダウンロード", data=data, file_name=pdfdoc.filename, mime="application/pdf", key=f"download_{idx}")
                st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.info("アップロードされたPDFはまだありません。")


if __name__ == "__main__":
    main()
