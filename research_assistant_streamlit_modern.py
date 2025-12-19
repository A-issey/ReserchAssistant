#!/usr/bin/env python3
"""research_assistant_streamlit_v2.py

Readable-first UI/UX (Apple-ish) + robust in-page PDF reading.

What this fixes vs previous versions:
  - Low contrast / small type: bumps base font sizes, darker text colors, better spacing.
  - Streamlit default dark input: forces light input fields & clearer focus styles.
  - PDF not rendering / black box: replaces <embed> with a pdf.js-based viewer inside an iframe.
  - PDFs duplicated on every rerun: de-dupe by SHA256 hash.
  - PDF summary bug: saves PDF first then runs PyPDF2 on the saved file.

Dependencies:
  - streamlit
  - requests
  - PyPDF2
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


# -----------------------------
# App config

st.set_page_config(
    page_title="研究アシスタント",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)


CONTACT_EMAIL = os.getenv("CROSSREF_MAILTO", "user@example.com")
CROSSREF_BASE_URL = "https://api.crossref.org/works"

DATA_DIR = os.getenv("DATA_DIR", ".")
LIBRARY_FILE = os.path.join(DATA_DIR, "library.json")
PDF_LIBRARY_FILE = os.path.join(DATA_DIR, "pdf_library.json")
PDF_UPLOAD_DIR = os.path.join(DATA_DIR, "pdf_uploads")

SUMMARY_SENTENCES = 3


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

    @classmethod
    def from_crossref(cls, item: Dict) -> "Paper":
        doi = item.get("DOI", "")
        title = "; ".join(item.get("title", [])).strip() or "(no title)"

        authors_list: List[str] = []
        for a in item.get("author", []) or []:
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            name = " ".join(p for p in (given, family) if p)
            if name:
                authors_list.append(name)
        authors = ", ".join(authors_list) if authors_list else "Unknown"

        journal = "; ".join(item.get("container-title", [])).strip() or ""

        year: Optional[int] = None
        for key in ("published-print", "published-online", "created"):
            date_info = item.get(key) or {}
            parts = date_info.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                try:
                    year = int(parts[0][0])
                    break
                except Exception:
                    pass

        abstract = item.get("abstract")
        if abstract:
            # strip tags/entities
            abstract = re.sub(r"<[^>]+>", "", abstract)
            abstract = re.sub(r"&[a-z]+;", "", abstract)
            abstract = abstract.strip() or None

        return cls(
            doi=doi,
            title=title,
            authors=authors,
            journal=journal,
            year=year,
            abstract=abstract,
            summary=None,
            read=False,
        )


@dataclass
class PDFDoc:
    id: str  # sha256
    filename: str
    path: str
    summary: Optional[str]


# -----------------------------
# Utilities


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_library() -> List[Paper]:
    data = _read_json(LIBRARY_FILE, [])
    out: List[Paper] = []
    for item in data:
        try:
            out.append(Paper(**item))
        except Exception:
            continue
    return out


def save_library(library: List[Paper]) -> None:
    _write_json(LIBRARY_FILE, [asdict(p) for p in library])


def load_pdf_library() -> List[PDFDoc]:
    data = _read_json(PDF_LIBRARY_FILE, [])
    out: List[PDFDoc] = []
    for item in data:
        try:
            out.append(PDFDoc(**item))
        except Exception:
            continue
    return out


def save_pdf_library(pdf_library: List[PDFDoc]) -> None:
    _write_json(PDF_LIBRARY_FILE, [asdict(p) for p in pdf_library])


def summarise_text(text: str, sentences: int = SUMMARY_SENTENCES) -> str:
    sents = re.split(r"(?<=[.!?。！？])\s+", text.strip())
    sents = [s.strip() for s in sents if s.strip()]
    if len(sents) <= sentences:
        return " ".join(sents)

    freq: Dict[str, int] = {}
    for s in sents:
        for w in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", s.lower()):
            freq[w] = freq.get(w, 0) + 1

    scored = []
    for s in sents:
        score = 0
        for w in re.findall(r"[\wぁ-んァ-ン一-龯]{2,}", s.lower()):
            score += freq.get(w, 0)
        scored.append((score, s))

    top = sorted(scored, key=lambda x: x[0], reverse=True)[:sentences]
    # keep original order
    top_s = sorted(top, key=lambda x: sents.index(x[1]))
    return " ".join(s for _, s in top_s)


def summarise_pdf_file(path: str) -> str:
    if PdfReader is None:
        return "PyPDF2が未インストールのためPDF要約は無効です。requirements.txtに PyPDF2 を追加してください。"
    try:
        reader = PdfReader(path)
        text_parts: List[str] = []
        max_pages = min(len(reader.pages), 12)
        for i in range(max_pages):
            txt = reader.pages[i].extract_text() or ""
            if txt.strip():
                text_parts.append(txt)
        full = "\n".join(text_parts).strip()
        if not full:
            return "このPDFからテキストを抽出できませんでした（画像PDFの可能性があります）。"
        return summarise_text(full)
    except Exception as e:
        return f"PDFの読み込みに失敗しました: {e}"


def is_japanese_text(s: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-龯]", s or ""))


def search_crossref(query: str, rows: int, japanese_only: bool) -> List[Paper]:
    params = {
        "query": query,
        "rows": rows,
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created",
        "mailto": CONTACT_EMAIL,
    }
    headers = {"User-Agent": f"research-assistant/2.0 (mailto:{CONTACT_EMAIL})"}
    try:
        r = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
        r.raise_for_status()
        items = (r.json() or {}).get("message", {}).get("items", [])
        out: List[Paper] = []
        for it in items:
            p = Paper.from_crossref(it)
            if japanese_only:
                blob = f"{p.title} {p.abstract or ''}"
                if not is_japanese_text(blob):
                    continue
            if p.abstract:
                p.summary = summarise_text(p.abstract)
            out.append(p)
        return out
    except Exception as e:
        st.error(f"検索に失敗しました: {e}")
        return []


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


# -----------------------------
# PDF viewer (pdf.js)


def pdfjs_viewer(pdf_bytes: bytes, height: int = 900) -> None:
    """Render the entire PDF using pdf.js inside a Streamlit HTML component.

    This avoids browser PDF plugin issues (black box / not rendering).
    """
    b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    # pdf.js via CDN (works on Streamlit Cloud). If your environment blocks CDN,
    # you can self-host pdf.js and replace the URLs.
    html = f"""
<div style="width:100%; height:{height}px; border-radius:16px; overflow:hidden; border:1px solid rgba(0,0,0,0.12); background:#fff;">
  <iframe
    style="width:100%; height:100%; border:0;"
    sandbox="allow-scripts allow-same-origin"
    srcdoc="
<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <meta name='viewport' content='width=device-width, initial-scale=1'/>
  <style>
    body{{ margin:0; background:#fff; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }}
    .top{{ position:sticky; top:0; background:rgba(255,255,255,0.92); backdrop-filter: blur(10px); border-bottom:1px solid rgba(0,0,0,0.08); padding:10px 12px; display:flex; gap:10px; align-items:center; }}
    .btn{{ padding:8px 10px; border-radius:10px; border:1px solid rgba(0,0,0,0.12); background:#fff; cursor:pointer; font-size:14px; }}
    .btn:active{{ transform: translateY(1px); }}
    .meta{{ color:#1d1d1f; font-size:14px; margin-left:auto; }}
    #viewer{{ padding:18px; }}
    canvas{{ display:block; margin:0 auto 18px auto; box-shadow:0 8px 24px rgba(0,0,0,0.08); border-radius:12px; }}
  </style>
  <script src='https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.6.82/pdf.min.js'></script>
</head>
<body>
  <div class='top'>
    <button class='btn' id='zoomOut'>−</button>
    <button class='btn' id='zoomIn'>＋</button>
    <button class='btn' id='fit'>幅に合わせる</button>
    <div class='meta' id='status'>読み込み中…</div>
  </div>
  <div id='viewer'></div>

  <script>
    const b64 = "{b64}";
    const pdfData = Uint8Array.from(atob(b64), c => c.charCodeAt(0));

    const viewer = document.getElementById('viewer');
    const status = document.getElementById('status');
    let pdfDoc = null;
    let scale = 1.25;
    let fitWidth = false;

    function clearViewer() {{ viewer.innerHTML = ''; }}

    async function renderAll() {{
      if (!pdfDoc) return;
      clearViewer();
      status.textContent = `全 ${pdfDoc.numPages} ページ`;
      const maxW = Math.min(1100, document.documentElement.clientWidth - 36);
      for (let p = 1; p <= pdfDoc.numPages; p++) {{
        const page = await pdfDoc.getPage(p);
        const vp0 = page.getViewport({{ scale: 1 }});
        const s = fitWidth ? (maxW / vp0.width) : scale;
        const viewport = page.getViewport({{ scale: s }});
        const canvas = document.createElement('canvas');
        const ctx = canvas.getContext('2d');
        canvas.width = Math.floor(viewport.width);
        canvas.height = Math.floor(viewport.height);
        viewer.appendChild(canvas);
        await page.render({{ canvasContext: ctx, viewport }}).promise;
      }}
    }}

    document.getElementById('zoomIn').onclick = () => {{ fitWidth=false; scale = Math.min(scale + 0.15, 3.0); renderAll(); }};
    document.getElementById('zoomOut').onclick = () => {{ fitWidth=false; scale = Math.max(scale - 0.15, 0.6); renderAll(); }};
    document.getElementById('fit').onclick = () => {{ fitWidth=true; renderAll(); }};

    pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/4.6.82/pdf.worker.min.js';

    (async () => {{
      try {{
        pdfDoc = await pdfjsLib.getDocument({{ data: pdfData }}).promise;
        await renderAll();
      }} catch (e) {{
        status.textContent = '表示に失敗しました';
        viewer.innerHTML = `<div style='padding:18px;color:#b00020;'>PDFレンダリングに失敗: ${{e}}</div>`;
      }}
    }})();
  </script>
</body>
</html>
    "></iframe>
</div>
"""
    st.components.v1.html(html, height=height + 12, scrolling=False)


# -----------------------------
# UI (Readable-first)


def inject_css() -> None:
    css = """
<style>
:root{
  --bg:#f5f5f7;
  --card:#ffffff;
  --text:#111111;
  --muted:#3c3c43;
  --muted2:#6e6e73;
  --border:rgba(0,0,0,0.10);
  --shadow:0 10px 30px rgba(0,0,0,0.06);
  --radius:18px;
  --blue:#007aff;
}

html, body, [data-testid="stApp"]{
  background:var(--bg) !important;
  color:var(--text) !important;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  font-size:17px !important;
  line-height:1.65 !important;
}

/* content width */
section.main > div{
  max-width: 1080px;
  padding-top: 0.5rem;
}

/* hide Streamlit default header spacing a bit */
header[data-testid="stHeader"]{ background: transparent; }

/* App header */
.appbar{
  position: sticky;
  top: 0;
  z-index: 999;
  background: rgba(245,245,247,0.85);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
  padding: 14px 6px 10px 6px;
  margin-bottom: 18px;
}
.appbar-inner{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
}
.brand{
  font-weight: 700;
  font-size: 20px;
  letter-spacing: -0.02em;
}
.subtitle{
  color: var(--muted2);
  font-size: 14px;
  margin-top: -2px;
}

.pill{
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:8px 12px;
  border:1px solid var(--border);
  border-radius:999px;
  background: rgba(255,255,255,0.75);
  box-shadow: 0 6px 18px rgba(0,0,0,0.04);
  color: var(--muted);
  font-size: 14px;
}

/* Inputs */
input, textarea{
  background: #fff !important;
  color: #111 !important;
}
div[data-baseweb="input"] > div{
  background:#fff !important;
  border:1px solid var(--border) !important;
  border-radius: 14px !important;
  box-shadow:none !important;
}
div[data-baseweb="input"] input{
  font-size: 17px !important;
  padding: 14px 14px !important;
}

/* Slider */
div[data-testid="stSlider"]{ padding-top: 6px; }
div[data-testid="stSlider"] *{ font-size: 15px !important; }

/* Checkbox */
div[data-testid="stCheckbox"] label{ font-size: 16px !important; color: var(--text) !important; }

/* Buttons */
button[kind="primary"]{
  background: var(--blue) !important;
  border: 1px solid rgba(0,0,0,0.06) !important;
  border-radius: 14px !important;
  padding: 10px 14px !important;
  font-weight: 600 !important;
}
button[kind="secondary"]{
  border-radius: 14px !important;
  padding: 10px 14px !important;
  font-weight: 600 !important;
}

/* File uploader */
div[data-testid="stFileUploader"] section{
  background:#fff !important;
  border:1px dashed rgba(0,0,0,0.18) !important;
  border-radius: var(--radius) !important;
  padding: 16px !important;
}

/* Cards */
.card{
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 18px;
  box-shadow: var(--shadow);
}
.card + .card{ margin-top: 14px; }
.title{
  font-weight: 700;
  font-size: 18px;
  letter-spacing: -0.01em;
  margin-bottom: 6px;
}
.meta{
  color: var(--muted);
  font-size: 15px;
  margin-bottom: 10px;
}
.summary{
  color: var(--text);
  font-size: 16px;
  line-height: 1.7;
}
.muted{
  color: var(--muted2);
  font-size: 14px;
}

/* Tabs */
button[data-baseweb="tab"]{ font-size: 16px !important; }

</style>
"""
    st.markdown(css, unsafe_allow_html=True)


def app_header() -> None:
    st.markdown(
        """
<div class="appbar">
  <div class="appbar-inner">
    <div>
      <div class="brand">研究アシスタント</div>
      <div class="subtitle">論文検索 / ライブラリ / PDF管理</div>
    </div>
    <div class="pill">Tip: PDFは「幅に合わせる」で読みやすい</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def card_start() -> None:
    st.markdown('<div class="card">', unsafe_allow_html=True)


def card_end() -> None:
    st.markdown('</div>', unsafe_allow_html=True)


def render_paper_card(p: Paper, actions: bool, key_prefix: str) -> None:
    card_start()
    st.markdown(f"<div class='title'>{p.title}</div>", unsafe_allow_html=True)
    meta = f"{p.authors}"
    if p.journal:
        meta += f" · {p.journal}"
    if p.year:
        meta += f" · {p.year}"
    if p.doi:
        meta += f" · DOI: {p.doi}"
    st.markdown(f"<div class='meta'>{meta}</div>", unsafe_allow_html=True)

    summ = p.summary or (summarise_text(p.abstract) if p.abstract else None)
    if summ:
        st.markdown(f"<div class='summary'>{textwrap.shorten(summ, 260, placeholder='…')}</div>", unsafe_allow_html=True)
    else:
        st.markdown("<div class='muted'>要約がありません（抄録なし）。</div>", unsafe_allow_html=True)

    if actions:
        cols = st.columns([1, 1, 2])
        with cols[0]:
            if st.button("ライブラリに追加", type="primary", key=f"{key_prefix}_add"):
                add_paper_to_library(p)
        with cols[1]:
            if p.doi:
                st.link_button("DOIを開く", f"https://doi.org/{p.doi}")
    card_end()


def add_paper_to_library(paper: Paper) -> None:
    library: List[Paper] = st.session_state.library
    if paper.doi and any(x.doi == paper.doi for x in library):
        st.toast("すでにライブラリにあります", icon="ℹ️")
        return
    library.append(paper)
    save_library(library)
    st.session_state.library = library
    st.toast("ライブラリに追加しました", icon="✅")


def delete_paper_by_index(idx: int) -> None:
    library: List[Paper] = st.session_state.library
    if 0 <= idx < len(library):
        library.pop(idx)
        save_library(library)
        st.session_state.library = library


def add_pdf(uploaded) -> None:
    os.makedirs(PDF_UPLOAD_DIR, exist_ok=True)
    data: bytes = uploaded.getbuffer().tobytes()
    pid = sha256_bytes(data)

    pdf_library: List[PDFDoc] = st.session_state.pdf_library
    if any(d.id == pid for d in pdf_library):
        st.toast("同じPDFが既に保存されています（重複を防止）", icon="ℹ️")
        return

    safe_name = re.sub(r"[^0-9A-Za-zぁ-んァ-ン一-龯._\- ]+", "_", uploaded.name)
    out_path = os.path.join(PDF_UPLOAD_DIR, f"{pid[:12]}_{safe_name}")
    with open(out_path, "wb") as f:
        f.write(data)

    summary = summarise_pdf_file(out_path)
    doc = PDFDoc(id=pid, filename=uploaded.name, path=out_path, summary=summary)
    pdf_library.append(doc)
    save_pdf_library(pdf_library)
    st.session_state.pdf_library = pdf_library
    st.toast("PDFを保存しました", icon="✅")


def delete_pdf_by_id(doc_id: str) -> None:
    pdf_library: List[PDFDoc] = st.session_state.pdf_library
    keep: List[PDFDoc] = []
    for d in pdf_library:
        if d.id == doc_id:
            try:
                if os.path.exists(d.path):
                    os.remove(d.path)
            except Exception:
                pass
        else:
            keep.append(d)
    save_pdf_library(keep)
    st.session_state.pdf_library = keep


# -----------------------------
# Main


def main() -> None:
    inject_css()
    app_header()

    if "library" not in st.session_state:
        st.session_state.library = load_library()
    if "pdf_library" not in st.session_state:
        st.session_state.pdf_library = load_pdf_library()
    if "search_results" not in st.session_state:
        st.session_state.search_results = []

    tab_search, tab_library, tab_pdf = st.tabs(["🔎 検索", "📚 ライブラリ", "📄 PDF"])

    # ---- Search
    with tab_search:
        st.markdown("### 論文検索")
        with st.form("search_form", clear_on_submit=False):
            query = st.text_input("検索キーワード", placeholder="例：sound localization / 音像制御 / UE5 film education")
            rows = st.slider("取得件数", 1, 50, 10)
            japanese_only = st.checkbox("日本語論文のみ（タイトル/抄録に日本語が含まれるもの）")
            ok = st.form_submit_button("検索", type="primary")

        if ok and query.strip():
            with st.spinner("検索中..."):
                st.session_state.search_results = search_crossref(query.strip(), rows, japanese_only)

        results: List[Paper] = st.session_state.search_results
        if results:
            st.markdown(f"#### 検索結果（{len(results)}件）")
            for i, p in enumerate(results):
                render_paper_card(p, actions=True, key_prefix=f"sr_{i}")
        else:
            st.info("検索キーワードを入力して『検索』を押してください。")

    # ---- Library
    with tab_library:
        st.markdown("### マイライブラリ")
        library: List[Paper] = st.session_state.library
        if not library:
            st.info("まだ空です。『検索』タブから論文を追加できます。")
        else:
            # Appleっぽい「左：リスト / 右：詳細」
            left, right = st.columns([1, 2], gap="large")
            with left:
                st.markdown("**論文一覧**")
                options = [f"{'✅' if p.read else '⬜'} {p.title[:60]}" for p in library]
                idx = st.radio("", list(range(len(options))), format_func=lambda i: options[i], label_visibility="collapsed")
            with right:
                p = library[idx]
                card_start()
                st.markdown(f"<div class='title'>{p.title}</div>", unsafe_allow_html=True)
                meta = f"{p.authors}"
                if p.journal:
                    meta += f" · {p.journal}"
                if p.year:
                    meta += f" · {p.year}"
                st.markdown(f"<div class='meta'>{meta}</div>", unsafe_allow_html=True)
                st.checkbox("既読", value=p.read, key=f"lib_read_{idx}")
                # sync read status
                if st.session_state.get(f"lib_read_{idx}") != p.read:
                    p.read = st.session_state.get(f"lib_read_{idx}")
                    save_library(library)
                st.markdown("---")
                if p.summary:
                    st.markdown(f"<div class='summary'>{p.summary}</div>", unsafe_allow_html=True)
                elif p.abstract:
                    st.markdown(f"<div class='summary'>{summarise_text(p.abstract)}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div class='muted'>抄録がありません。</div>", unsafe_allow_html=True)

                btns = st.columns([1, 1, 2])
                with btns[0]:
                    if p.doi:
                        st.link_button("DOIを開く", f"https://doi.org/{p.doi}")
                with btns[1]:
                    if st.button("削除", key=f"lib_del_{idx}"):
                        delete_paper_by_index(idx)
                        st.rerun()
                card_end()

    # ---- PDF
    with tab_pdf:
        st.markdown("### PDF管理")
        st.markdown("<div class='muted'>※ Streamlit Cloudでは保存先はアプリのストレージ（再デプロイで消える場合あり）。本格的なクラウド永続化はS3/Supabase等が必要です。</div>", unsafe_allow_html=True)

        uploaded = st.file_uploader("PDFをアップロード", type=["pdf"], accept_multiple_files=False)
        if uploaded is not None:
            with st.spinner("PDFを保存・要約しています..."):
                add_pdf(uploaded)

        pdfs: List[PDFDoc] = st.session_state.pdf_library
        if not pdfs:
            st.info("まだPDFがありません。上からアップロードしてください。")
        else:
            left, right = st.columns([1, 2], gap="large")
            with left:
                st.markdown("**PDF一覧**")
                labels = [d.filename for d in pdfs]
                sel = st.radio("", list(range(len(labels))), format_func=lambda i: labels[i], label_visibility="collapsed")
            doc = pdfs[sel]

            with right:
                card_start()
                st.markdown(f"<div class='title'>{doc.filename}</div>", unsafe_allow_html=True)
                if doc.summary:
                    st.markdown(f"<div class='summary'>{doc.summary}</div>", unsafe_allow_html=True)
                else:
                    st.markdown("<div class='muted'>要約がありません。</div>", unsafe_allow_html=True)

                # action row
                b1, b2, b3 = st.columns([1, 1, 2])
                pdf_bytes = b""
                try:
                    with open(doc.path, "rb") as f:
                        pdf_bytes = f.read()
                except Exception:
                    pass

                with b1:
                    if pdf_bytes:
                        st.download_button("ダウンロード", data=pdf_bytes, file_name=doc.filename, mime="application/pdf")
                with b2:
                    if st.button("削除", key=f"pdf_del_{doc.id}"):
                        delete_pdf_by_id(doc.id)
                        st.rerun()
                card_end()

                st.markdown("#### PDF全文")
                if pdf_bytes:
                    pdfjs_viewer(pdf_bytes, height=900)
                else:
                    st.error("PDFファイルを読み込めませんでした。")


if __name__ == "__main__":
    main()
