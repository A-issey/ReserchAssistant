#!/usr/bin/env python3
"""
Appleのデザインガイドラインをより忠実に意識した論文検索アプリ。

このアプリは次の機能を持ちます：
  * Crossref API を用いた査読付き論文の検索と要約表示（日本語判定による絞り込みに対応）
  * 取得した論文のライブラリ保存および既読管理
  * PDF ファイルのアップロード、要約生成、ページ内閲覧

UI/UX の改善点：
  * 背景は淡いグレー (#f5f5f7) で統一し、カードや入力欄は純白で区切りを付けました。
  * ナビバーは Apple サイトのように白ベースでボーダーのみとし、ブルーアクセント (#007aff) をリンクやボタンに使用しています。
  * 検索フォームの入力欄やスライダーも白背景にカスタマイズし、控えめな影と丸みを与えています。
  * PDF 埋め込みは高さを 800px に拡張し、スクロール可能な iframe で全文閲覧を可能にしました。

注：Crossref API 利用時は `select` パラメータで必要なフィールドのみに絞り込み、取得件数は `rows` で制御することが推奨されています【199494085728094†L340-L375】。また、問い合わせには `mailto` パラメータと適切な User‑Agent を含める必要があります【891973487454928†L327-L341】。
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
    """非常に単純な抽出的要約。
    文章を文ごとに分割し、単語の頻度に基づいてスコアリングし、上位数文を抽出します。"""
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
    """アップロードしたPDFの最初の数ページからテキストを抽出して要約します。"""
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
    """Crossref API から査読付き論文を検索し、Paper オブジェクトのリストを返します。
    日本語論文のみを希望する場合は、タイトルや要約に日本語文字が含まれているものだけを残します。"""
    params = {
        "query": query,
        "rows": rows,
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created",
        "mailto": CONTACT_EMAIL,
    }
    headers = {
        "User-Agent": f"research-assistant-refined/1.0 (mailto:{CONTACT_EMAIL})"
    }
    try:
        response = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        items = data.get("message", {}).get("items", [])
        papers: List[Paper] = []
        for item in items:
            paper = Paper.from_crossref(item)
            # 日本語判定：タイトルと抄録にひらがな・カタカナ・漢字のいずれかを含む
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


def embed_pdf(path: str, height: int = 800) -> None:
    """PDF を Base64 エンコードしてページ内に埋め込む。高さを指定可能。"""
    try:
        with open(path, "rb") as f:
            base64_pdf = base64.b64encode(f.read()).decode("utf-8")
        # iframe を使用してスクロール可能にする
        pdf_display = f"<iframe src='data:application/pdf;base64,{base64_pdf}' width='100%' height='{height}px' style='border:none;'></iframe>"
        st.components.v1.html(pdf_display, height=height + 20)
    except Exception as e:
        st.error(f"PDF表示に失敗しました: {e}")


def inject_css() -> None:
    """アプリ全体に適用するカスタムCSSを挿入する。"""
    css = """
    <style>
    html, body, [data-testid="stApp"] {
        background-color: #f5f5f7;
        color: #1d1d1f;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    /* ナビバー */
    .navbar {
        background-color: #ffffff;
        border-bottom: 1px solid #e5e5ea;
        padding: 1rem 2rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        position: sticky;
        top: 0;
        z-index: 100;
    }
    .navbar .logo {
        font-weight: 600;
        font-size: 1.2rem;
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
    /* 入力欄・セレクト・スライダー */
    input[type="text"], textarea {
        background-color: #ffffff !important;
        border: 1px solid #d1d1d6 !important;
        border-radius: 10px !important;
        padding: 0.5rem 0.75rem !important;
        color: #1d1d1f !important;
        font-size: 1rem !important;
    }
    input[type="text"]:focus {
        border-color: #007aff !important;
        box-shadow: 0 0 0 2px rgba(0,122,255,0.3) !important;
    }
    .stSlider > div[data-testid="stWidget"] {
        padding: 0.5rem 0 !important;
    }
    /* カードデザイン */
    .card {
        background-color: #ffffff;
        border-radius: 14px;
        padding: 1.25rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 2px 6px rgba(0, 0, 0, 0.06);
    }
    .card-title {
        font-size: 1.2rem;
        font-weight: 600;
        margin-bottom: 0.5rem;
        color: #1d1d1f;
    }
    .card-meta {
        font-size: 0.85rem;
        color: #636366;
        margin-bottom: 0.6rem;
    }
    .card-summary {
        font-size: 0.92rem;
        line-height: 1.5;
        color: #3c3c43;
        margin-bottom: 0.6rem;
    }
    .primary-btn {
        background-color: #007aff;
        color: #ffffff;
        padding: 0.4rem 0.9rem;
        border-radius: 8px;
        font-size: 0.9rem;
        border: none;
        cursor: pointer;
    }
    .primary-btn:hover {
        background-color: #005bb5;
    }
    /* ファイルアップローダー */
    div[data-testid="stFileUploadDropzone"] {
        background-color: #ffffff;
        border: 2px dashed #c7c7cc;
        border-radius: 12px;
        padding: 1rem;
        color: #636366;
    }
    /* チェックボックス */
    label.css-1q8dd3e span {
        font-size: 0.9rem;
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
        query = st.text_input("検索キーワード", key="search_query")
        rows = st.slider("取得件数", min_value=1, max_value=50, value=5, key="search_rows")
        japanese_only = st.checkbox("日本語論文のみ", value=False, key="japan_only")
        submitted = st.form_submit_button("検索", help="Crossref API を使って論文を検索します")
    if submitted and query:
        st.session_state.search_results = search_crossref(query, rows, japanese_only=japanese_only)

    # 検索結果表示
    if st.session_state.get("search_results"):
        papers = st.session_state.search_results
        # 余白を確保するため列の間にスペースを設ける
        cols = st.columns(2, gap="large")
        for idx, paper in enumerate(papers):
            with cols[idx % 2]:
                st.markdown("<div class='card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='card-title'>{paper.title}</div>", unsafe_allow_html=True)
                year_str = f"{paper.year}" if paper.year else "n/a"
                st.markdown(
                    f"<div class='card-meta'>著者: {paper.authors}<br>ジャーナル: {paper.journal or '不明'}<br>年: {year_str}</div>",
                    unsafe_allow_html=True,
                )
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
        cols = st.columns(2, gap="large")
        for idx, paper in enumerate(st.session_state.library):
            with cols[idx % 2]:
                st.markdown("<div class='card'>", unsafe_allow_html=True)
                st.markdown(f"<div class='card-title'>{paper.title}</div>", unsafe_allow_html=True)
                year_str = f"{paper.year}" if paper.year else "n/a"
                status = "既読" if paper.read else "未読"
                st.markdown(
                    f"<div class='card-meta'>ステータス: {status}<br>年: {year_str}</div>",
                    unsafe_allow_html=True,
                )
                if paper.summary:
                    st.markdown(
                        f"<div class='card-summary'>{textwrap.fill(paper.summary, width=80)}</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown("<div class='card-summary'>要約がありません。</div>", unsafe_allow_html=True)
                # 既読トグル
                # チェックボックスは横幅がカラム幅に自動調整されるため、簡潔に表示される
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
    uploaded_file = st.file_uploader("PDFをアップロード", type=["pdf"], key="pdf_upload")
    if uploaded_file is not None:
        with st.spinner("PDFを処理中..."):
            summary = summarise_pdf(uploaded_file) if PdfReader is not None else ""
            add_pdf_to_library(uploaded_file, summary)

    if st.session_state.pdf_library:
        cols = st.columns(2, gap="large")
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
                with st.expander("PDF全文を表示"):
                    embed_pdf(pdfdoc.path, height=800)
                with open(pdfdoc.path, "rb") as f:
                    data = f.read()
                    st.download_button(
                        "PDFをダウンロード",
                        data=data,
                        file_name=pdfdoc.filename,
                        mime="application/pdf",
                        key=f"download_{idx}",
                    )
                st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.info("アップロードされたPDFはまだありません。")


if __name__ == "__main__":
    main()
