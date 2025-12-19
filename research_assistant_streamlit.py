#!/usr/bin/env python3
"""
Web version of the research assistant using Streamlit.

This application wraps the core search, summarisation and library management
functions from `research_assistant.py` into a simple web interface.  It
persists your library between sessions using a JSON file and supports
interactive searching and adding of journal articles.  Once deployed
publicly (for example on Streamlit Community Cloud), the app can be
embedded into Notion by simply pasting the app’s URL into a Notion
page as an embed block.  According to Notion’s documentation, you can
insert an embed by clicking the plus icon, selecting **Embed**, and
pasting the content’s URL【287075832497258†L95-L104】.  Streamlit apps are
explicitly supported as an embeddable domain【287075832497258†L164-L166】.

To run locally:

```bash
pip install streamlit requests
streamlit run research_assistant_streamlit.py
```

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


# ---------------------------------------------------------------------------
# Configuration

CONTACT_EMAIL = "user@example.com"
SUMMARY_SENTENCES = 3
LIBRARY_FILE = "library.json"
CROSSREF_BASE_URL = "https://api.crossref.org/works"


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
        authors_list = []
        for author in item.get("author", []):
            given = author.get("given", "").strip()
            family = author.get("family", "").strip()
            name = " ".join(part for part in (given, family) if part)
            if name:
                authors_list.append(name)
        authors = ", ".join(authors_list) if authors_list else "Unknown"
        journal = "; ".join(item.get("container-title", [])) or ""
        year = None
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
        summary = None
        return cls(doi=doi, title=title, authors=authors, journal=journal,
                   year=year, abstract=abstract, summary=summary)


def summarise_text(text: str, sentences: int = SUMMARY_SENTENCES) -> str:
    raw_sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(raw_sentences) <= sentences:
        return text
    word_freq: Dict[str, int] = {}
    for sentence in raw_sentences:
        for word in re.findall(r"\b[a-zA-Z]{3,}\b", sentence.lower()):
            word_freq[word] = word_freq.get(word, 0) + 1
    scored_sentences = []
    for sentence in raw_sentences:
        score = sum(word_freq.get(word.lower(), 0) for word in re.findall(r"\b[a-zA-Z]{3,}\b", sentence))
        scored_sentences.append((score, sentence))
    top_sentences = sorted(scored_sentences, key=lambda x: x[0], reverse=True)[:sentences]
    top_sentences_sorted = sorted(top_sentences, key=lambda x: raw_sentences.index(x[1]))
    summary = " ".join(sentence.strip() for (_, sentence) in top_sentences_sorted)
    return summary


def search_crossref(query: str, rows: int = 10) -> List[Paper]:
    params = {
        "query": query,
        "rows": rows,
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created",
        "mailto": CONTACT_EMAIL,
    }
    headers = {
        "User-Agent": f"research-assistant-streamlit/1.0 (mailto:{CONTACT_EMAIL})"
    }
    try:
        response = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        items = data.get("message", {}).get("items", [])
        papers = [Paper.from_crossref(item) for item in items]
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
    except Exception as e:
        st.warning(f"ライブラリの読み込みに失敗しました: {e}")
        return []


def save_library(library: List[Paper]) -> None:
    try:
        with open(LIBRARY_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in library], f, ensure_ascii=False, indent=2)
    except Exception as e:
        st.error(f"ライブラリの保存に失敗しました: {e}")


def toggle_read(idx: int) -> None:
    """Callback to toggle read status of the paper at index idx in the session library."""
    library: List[Paper] = st.session_state.library
    library[idx].read = not library[idx].read
    save_library(library)


def add_to_library(paper: Paper) -> None:
    library: List[Paper] = st.session_state.library
    if not any(p.doi == paper.doi for p in library):
        # Ensure summary is generated before saving
        if paper.abstract and not paper.summary:
            paper.summary = summarise_text(paper.abstract)
        library.append(paper)
        save_library(library)
        st.session_state.library = library  # update session state
        st.success(f"'{paper.title}' をライブラリに追加しました。")
    else:
        st.warning("既にライブラリに存在します。")


def main() -> None:
    st.title("研究論文アシスタント")
    st.write("キーワードで査読付き論文を検索し、要約を閲覧・管理できます。")

    # Initialize session state for library and search results
    if "library" not in st.session_state:
        st.session_state.library = load_library()
    if "search_results" not in st.session_state:
        st.session_state.search_results: List[Paper] = []

    # Search form
    with st.form(key="search_form"):
        query = st.text_input("検索キーワード")
        rows = st.number_input("取得件数", min_value=1, max_value=50, value=5, step=1)
        submitted = st.form_submit_button("検索")
    if submitted and query:
        st.session_state.search_results = search_crossref(query, rows)

    # Display search results
    if st.session_state.search_results:
        st.subheader("検索結果")
        for i, paper in enumerate(st.session_state.search_results):
            with st.expander(f"{paper.title} ({paper.year if paper.year else 'n/a'})"):
                st.markdown(f"**著者:** {paper.authors}")
                st.markdown(f"**ジャーナル:** {paper.journal or '不明'}")
                st.markdown(f"**DOI:** {paper.doi}")
                if paper.abstract:
                    if not paper.summary:
                        paper.summary = summarise_text(paper.abstract)
                    st.markdown("**要約:**")
                    st.write(textwrap.fill(paper.summary, width=80))
                else:
                    st.markdown("**要約:** 抄録がありません。")
                if st.button("ライブラリに追加", key=f"add_{i}"):
                    add_to_library(paper)

    # Display library
    st.subheader("マイライブラリ")
    if st.session_state.library:
        for idx, paper in enumerate(st.session_state.library):
            col1, col2 = st.columns([0.85, 0.15])
            with col1:
                st.markdown(f"**{paper.title}** ({paper.year if paper.year else 'n/a'})")
                st.markdown(f"_ステータス:_ {'既読' if paper.read else '未読'}")
            with col2:
                st.checkbox("既読", value=paper.read, key=f"read_toggle_{idx}", on_change=toggle_read, args=(idx,))
            with st.expander("詳細を見る"):
                st.markdown(f"**著者:** {paper.authors}")
                st.markdown(f"**ジャーナル:** {paper.journal or '不明'}")
                st.markdown(f"**DOI:** {paper.doi}")
                if paper.summary:
                    st.markdown("**要約:**")
                    st.write(textwrap.fill(paper.summary, width=80))
                elif paper.abstract:
                    # generate summary on the fly if missing
                    summary = summarise_text(paper.abstract)
                    paper.summary = summary
                    st.markdown("**要約:**")
                    st.write(textwrap.fill(summary, width=80))
                    save_library(st.session_state.library)
                else:
                    st.markdown("**要約:** 抄録がありません。")
    else:
        st.info("ライブラリは空です。上のフォームから論文を検索して追加してください。")


if __name__ == "__main__":
    main()