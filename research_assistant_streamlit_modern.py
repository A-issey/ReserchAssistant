#!/usr/bin/env python3
"""
Streamlitベースの研究論文・PDF管理アプリ（モダンデザイン対応）

このアプリは以下の機能を備えています。

* キーワード検索でCrossrefから査読付き論文を取得し、抄録の要約を生成します。
* 日本語論文のみを対象に検索するオプションを備えています（Crossrefの言語フィルタを利用）。
* 取得した論文をライブラリに追加し、既読／未読を管理できます。
* PDFファイルをアップロードしてクラウド（ローカルストレージ）に保存し、本文から要約を自動生成します。
* ガラスモーフィズム風のグラデーション背景とカードUIを用いたモダンなデザインを採用しています。

このファイルをStreamlitで実行すると、ブラウザ上でGUIアプリとして動作します。Notionへの埋め込みや
Streamlit Community Cloudへのデプロイも可能です。

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
from PyPDF2 import PdfReader  # type: ignore


# ---------------------------------------------------------------------------
# 設定

# Crossrefへの問い合わせ時に含めるメールアドレス（任意）。
CONTACT_EMAIL = "user@example.com"

# 要約文に含める最大文数。
SUMMARY_SENTENCES = 3

# 論文ライブラリを保存するJSONファイル名。
LIBRARY_FILE = "library.json"

# PDFライブラリを保存するJSONファイル名。
PDF_LIBRARY_FILE = "pdf_library.json"

# Crossref APIのエンドポイント。
CROSSREF_BASE_URL = "https://api.crossref.org/works"

# PDF保存用ディレクトリ
PDF_UPLOAD_DIR = "pdf_uploads"


@dataclass
class Paper:
    """Crossrefから取得した論文のメタデータと要約を保持するデータクラス。"""
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
        """Crossref APIのレスポンスからPaperインスタンスを生成します。"""
        doi = item.get("DOI", "")
        title = "; ".join(item.get("title", []))
        # 著者名を「姓 名」形式で連結
        authors_list: List[str] = []
        for author in item.get("author", []):
            given = author.get("given", "").strip()
            family = author.get("family", "").strip()
            name = " ".join(part for part in (given, family) if part)
            if name:
                authors_list.append(name)
        authors = ", ".join(authors_list) if authors_list else "Unknown"
        journal = "; ".join(item.get("container-title", [])) or ""
        # 発行年をprint/online/作成日の順に取得
        year: Optional[int] = None
        for key in ("published-print", "published-online", "created"):
            date_info = item.get(key)
            if date_info and isinstance(date_info.get("date-parts", []), list):
                try:
                    year = date_info["date-parts"][0][0]
                    break
                except (IndexError, TypeError):
                    continue
        # 抄録（JATSタグを除去）
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
    """アップロードされたPDFファイルを管理するデータクラス。"""
    filename: str
    path: str
    summary: Optional[str]


def summarise_text(text: str, sentences: int = SUMMARY_SENTENCES) -> str:
    """単純な頻度ベースの抽出的要約を生成します。"""
    raw_sentences = re.split(r"(?<=[.!?。！？])\s+", text)
    if len(raw_sentences) <= sentences:
        return text
    word_freq: Dict[str, int] = {}
    for sentence in raw_sentences:
        # 英単語と日本語のひらがな・カタカナ・漢字を含める
        for word in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", sentence.lower()):
            word_freq[word] = word_freq.get(word, 0) + 1
    scored_sentences = []
    for sentence in raw_sentences:
        score = sum(word_freq.get(word.lower(), 0) for word in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", sentence))
        scored_sentences.append((score, sentence))
    top_sentences = sorted(scored_sentences, key=lambda x: x[0], reverse=True)[:sentences]
    top_sentences_sorted = sorted(top_sentences, key=lambda x: raw_sentences.index(x[1]))
    summary = " ".join(sentence.strip() for (_, sentence) in top_sentences_sorted)
    return summary


def summarise_pdf(path: str) -> str:
    """PDFファイルからテキストを抽出し要約を生成します。"""
    try:
        reader = PdfReader(path)
        text_parts: List[str] = []
        # ページ数が多すぎる場合に備え、先頭数ページのみを読み込む
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
    """Crossrefから検索語に一致する査読付き論文を取得します。

    日本語のみ検索する場合はCrossrefの言語フィルタを使わずに結果を取得し、タイトルや抄録に
    日本語文字が含まれるものだけをフィルタします。CrossrefのAPIは `language:ja` フィルタを
    サポートしていないため、これにより400エラーが回避されます。
    """
    params = {
        "query": query,
        "rows": rows,
        # 常にジャーナル記事のみ取得
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created",
        "mailto": CONTACT_EMAIL,
    }
    headers = {
        "User-Agent": f"research-assistant-modern/1.0 (mailto:{CONTACT_EMAIL})"
    }
    try:
        response = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        items = data.get("message", {}).get("items", [])
        papers: List[Paper] = []
        for item in items:
            paper = Paper.from_crossref(item)
            # 日本語のみを要求する場合、日本語の文字がタイトルまたは抄録に含まれるか判定
            if japanese_only:
                text_to_check = (paper.title or "") + " " + (paper.abstract or "")
                # 日本語文字の範囲に一致するか
                if not re.search(r"[ぁ-んァ-ン一-龯]", text_to_check):
                    continue
            # 要約を事前に生成
            if paper.abstract and not paper.summary:
                paper.summary = summarise_text(paper.abstract)
            papers.append(paper)
        return papers
    except Exception as e:
        st.error(f"検索に失敗しました: {e}")
        return []


def load_library() -> List[Paper]:
    """ローカルストレージから論文ライブラリを読み込みます。"""
    if not os.path.exists(LIBRARY_FILE):
        return []
    try:
        with open(LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [Paper(**item) for item in data]
    except Exception as e:
        st.warning(f"ライブラリの読み込みに失敗しました: {e}")
        return []


def save_library(library: List[Paper]) -> None:
    """論文ライブラリをディスクに保存します。"""
    try:
        with open(LIBRARY_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in library], f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.error(f"ライブラリの保存に失敗しました: {e}")


def load_pdf_library() -> List[PDFDoc]:
    """保存されたPDFドキュメントのライブラリを読み込みます。"""
    if not os.path.exists(PDF_LIBRARY_FILE):
        return []
    try:
        with open(PDF_LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [PDFDoc(**item) for item in data]
    except Exception as e:
        st.warning(f"PDFライブラリの読み込みに失敗しました: {e}")
        return []


def save_pdf_library(library: List[PDFDoc]) -> None:
    """PDFドキュメントのライブラリを保存します。"""
    try:
        with open(PDF_LIBRARY_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in library], f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.error(f"PDFライブラリの保存に失敗しました: {e}")


def add_paper_to_library(paper: Paper) -> None:
    """指定されたPaperをライブラリに追加します。"""
    library: List[Paper] = st.session_state.library
    if not any(p.doi == paper.doi for p in library):
        library.append(paper)
        save_library(library)
        st.session_state.library = library
        st.success(f"'{paper.title}' をライブラリに追加しました。")
    else:
        st.warning("既にライブラリに存在します。")


def toggle_read_status(idx: int) -> None:
    """ライブラリ内の指定された論文の既読状態を切り替えます。"""
    library: List[Paper] = st.session_state.library
    library[idx].read = not library[idx].read
    save_library(library)
    # 状態更新
    st.session_state.library = library


def add_pdf_to_library(file, summary: str) -> None:
    """アップロードされたPDFファイルを保存し、ライブラリに登録します。"""
    # 保存ディレクトリが無ければ作成
    os.makedirs(PDF_UPLOAD_DIR, exist_ok=True)
    file_path = os.path.join(PDF_UPLOAD_DIR, file.name)
    # ファイルを書き出し
    with open(file_path, "wb") as f:
        f.write(file.getbuffer())
    pdf_library: List[PDFDoc] = st.session_state.pdf_library
    pdf_library.append(PDFDoc(filename=file.name, path=file_path, summary=summary))
    save_pdf_library(pdf_library)
    st.session_state.pdf_library = pdf_library
    st.success(f"PDF '{file.name}' をアップロードしました。")


def inject_css() -> None:
    """ページ全体にカスタムCSSを注入してモダンなデザインを適用します。"""
    css = """
    <style>
    /* 全体の背景にグラデーションを適用 */
    html, body, [data-testid="stApp"] {
        background: linear-gradient(135deg, #eef2f3 0%, #8e9eab 100%);
        height: 100%;
        margin: 0;
        padding: 0;
    }
    /* ガラスモーフィズム風カード */
    .glass-card {
        background: rgba(255, 255, 255, 0.25);
        border-radius: 16px;
        box-shadow: 0 4px 30px rgba(0, 0, 0, 0.1);
        backdrop-filter: blur(10px);
        -webkit-backdrop-filter: blur(10px);
        border: 1px solid rgba(255, 255, 255, 0.3);
        padding: 1rem;
        margin-bottom: 1rem;
    }
    </style>
    """
    st.markdown(css, unsafe_allow_html=True)


def main() -> None:
    """アプリのメインエントリーポイント。"""
    inject_css()
    st.title("研究アシスタント（モダン版）")
    st.write("日本語論文検索・ライブラリ管理・PDF要約をサポートします。")

    # セッション状態の初期化
    if "library" not in st.session_state:
        st.session_state.library = load_library()
    if "pdf_library" not in st.session_state:
        st.session_state.pdf_library = load_pdf_library()

    # 検索セクション
    with st.expander("論文検索", expanded=True):
        query = st.text_input("検索キーワード")
        rows = st.slider("取得件数", min_value=1, max_value=50, value=5)
        japanese_only = st.checkbox("日本語論文のみ検索する", value=False)
        if st.button("検索する"):
            if query:
                papers = search_crossref(query, rows, japanese_only=japanese_only)
                st.session_state.search_results = papers
            else:
                st.warning("検索キーワードを入力してください。")

    # 検索結果の表示
    if st.session_state.get("search_results"):
        st.subheader("検索結果")
        for i, paper in enumerate(st.session_state.search_results):
            with st.container():
                # カードデザイン適用
                st.markdown(f"<div class='glass-card'>", unsafe_allow_html=True)
                st.markdown(f"**{paper.title}** ({paper.year if paper.year else 'n/a'})")
                st.markdown(f"著者: {paper.authors}")
                st.markdown(f"ジャーナル: {paper.journal or '不明'}")
                st.markdown(f"DOI: {paper.doi}")
                if paper.summary:
                    with st.expander("要約を表示"):
                        st.write(textwrap.fill(paper.summary, width=80))
                else:
                    st.markdown("要約: 抄録がありません。")
                if st.button("ライブラリに追加", key=f"add_paper_{i}"):
                    add_paper_to_library(paper)
                st.markdown("</div>", unsafe_allow_html=True)

    # ライブラリ表示
    with st.expander("マイライブラリ (論文)", expanded=True):
        if st.session_state.library:
            for idx, paper in enumerate(st.session_state.library):
                st.markdown(f"<div class='glass-card'>", unsafe_allow_html=True)
                col1, col2 = st.columns([0.85, 0.15])
                with col1:
                    st.markdown(f"**{paper.title}** ({paper.year if paper.year else 'n/a'})")
                    st.markdown(f"ステータス: {'既読' if paper.read else '未読'}")
                with col2:
                    st.checkbox("既読", value=paper.read, key=f"read_toggle_{idx}", on_change=toggle_read_status, args=(idx,))
                with st.expander("詳細・要約"):
                    st.markdown(f"著者: {paper.authors}")
                    st.markdown(f"ジャーナル: {paper.journal or '不明'}")
                    st.markdown(f"DOI: {paper.doi}")
                    if paper.summary:
                        st.write(textwrap.fill(paper.summary, width=80))
                    elif paper.abstract:
                        summary = summarise_text(paper.abstract)
                        paper.summary = summary
                        save_library(st.session_state.library)
                        st.write(textwrap.fill(summary, width=80))
                    else:
                        st.write("抄録がありません。")
                st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.info("ライブラリは空です。上の検索セクションから論文を追加できます。")

    # PDFアップロードとライブラリ表示
    with st.expander("PDFアップロード・管理", expanded=True):
        uploaded_file = st.file_uploader("PDFをアップロード", type=["pdf"])
        if uploaded_file is not None:
            with st.spinner("PDFを処理中..."):
                summary = summarise_pdf(uploaded_file)
                add_pdf_to_library(uploaded_file, summary)
        # PDFライブラリの表示
        if st.session_state.pdf_library:
            for idx, pdfdoc in enumerate(st.session_state.pdf_library):
                st.markdown(f"<div class='glass-card'>", unsafe_allow_html=True)
                st.markdown(f"**{pdfdoc.filename}**")
                with st.expander("要約を表示"):
                    if pdfdoc.summary:
                        st.write(textwrap.fill(pdfdoc.summary, width=80))
                    else:
                        st.write("要約がありません。")
                # ダウンロードリンク提供
                with open(pdfdoc.path, "rb") as f:
                    data = f.read()
                    st.download_button(label="PDFをダウンロード", data=data, file_name=pdfdoc.filename, mime="application/pdf")
                st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.info("アップロードされたPDFはまだありません。")


if __name__ == "__main__":
    main()
