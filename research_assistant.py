#!/usr/bin/env python3
"""
A simple command‑line tool to search, summarise and manage peer‑reviewed
research articles.

Overview
========

This script uses the Crossref REST API to search for scholarly works.  It
retrieves basic metadata (title, authors, DOI, abstract) and generates an
extractive summary of each abstract.  Users can store articles in a
persistent library, mark them as read, and list or update their saved
items.  Saved items are written to a JSON file in the same directory as
the script.

The design emphasises transparency and openness: it only queries
open/anonymous APIs and does not require an API key.  To conform to
Crossref’s best‑practice recommendations it sets an explicit User‑Agent
header and includes a `mailto` parameter in all requests.  If you plan to
use this tool regularly we recommend editing the `CONTACT_EMAIL` constant
below to reflect your own email address so Crossref can contact you if
there are any issues with your usage.

Usage
-----

Run the script from the command line:

```
python research_assistant.py
```

You will see a menu with options to search for papers, list your saved
papers, mark a paper as read or unread, or exit.  When searching, enter
a descriptive query (e.g., "deep reinforcement learning") and the
number of results you want to retrieve.  After the results are
displayed you can choose which items to save to your library.  When
listing papers the script shows which ones you’ve read and provides
short summaries of their abstracts.

Limitations
-----------

* Not all Crossref records include abstracts.  In those cases the tool
  will notify you that no abstract is available.
* The summary algorithm is a simple frequency‑based extractive
  summariser.  It captures key sentences but may not always produce
  coherent or concise summaries.  You can adjust the number of
  sentences retained by editing the `SUMMARY_SENTENCES` constant.
* Crossref returns a mixture of peer‑reviewed and non peer‑reviewed
  works.  To approximate peer‑reviewed content this tool filters for
  journal articles by requesting the `type=journal-article` filter.
* For simplicity this is a terminal application.  You could extend it
  into a graphical or web application by reusing the `search_crossref`
  and `summarise_text` functions.

"""

import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Optional

import requests

# ---------------------------------------------------------------------------
# Configuration constants

# Replace this email with your own to comply with Crossref’s best practices.
CONTACT_EMAIL = "user@example.com"

# Number of sentences to include in the summary.
SUMMARY_SENTENCES = 3

# Path to the JSON file that stores the user’s library.
LIBRARY_FILE = "library.json"

# Crossref API base URL.
CROSSREF_BASE_URL = "https://api.crossref.org/works"

# ---------------------------------------------------------------------------

def debug(msg: str) -> None:
    """Print a debug message to stderr (can be toggled off globally)."""
    # Uncomment the next line to enable debug output
    # print(f"DEBUG: {msg}", file=sys.stderr)


@dataclass
class Paper:
    """A simple data container for scholarly articles."""
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
        """Create a Paper instance from a Crossref API item."""
        doi = item.get("DOI", "")
        title = "; ".join(item.get("title", []))
        # Build a comma‑separated list of authors (first + last name).
        authors_list = []
        for author in item.get("author", []):
            given = author.get("given", "").strip()
            family = author.get("family", "").strip()
            name = " ".join(part for part in (given, family) if part)
            if name:
                authors_list.append(name)
        authors = ", ".join(authors_list) if authors_list else "Unknown"
        journal = "; ".join(item.get("container-title", [])) or ""
        # Choose the print or online publication date.
        year = None
        for key in ("published-print", "published-online", "created"):
            date_info = item.get(key)
            if date_info and isinstance(date_info.get("date-parts", []), list):
                try:
                    year = date_info["date-parts"][0][0]
                    break
                except (IndexError, TypeError):
                    continue
        # Normalise abstract if present (Crossref returns JATS XML tags).
        abstract = item.get("abstract")
        if abstract:
            # Remove HTML/XML tags and decode HTML entities.
            abstract_text = re.sub(r"<[^>]+>", "", abstract)
            abstract_text = re.sub(r"&[a-z]+;", "", abstract_text)
            abstract = abstract_text.strip()
        else:
            abstract = None
        summary = None
        return cls(doi=doi, title=title, authors=authors, journal=journal,
                   year=year, abstract=abstract, summary=summary)


def load_library() -> List[Paper]:
    """Load the user’s saved papers from the JSON file."""
    if not os.path.exists(LIBRARY_FILE):
        return []
    try:
        with open(LIBRARY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        library = [Paper(**item) for item in data]
        return library
    except Exception as e:
        print(f"Failed to load library: {e}")
        return []


def save_library(library: List[Paper]) -> None:
    """Save the user’s library to disk."""
    try:
        with open(LIBRARY_FILE, "w", encoding="utf-8") as f:
            json.dump([asdict(paper) for paper in library], f, ensure_ascii=False, indent=2)
        debug("Library saved successfully")
    except Exception as e:
        print(f"Error saving library: {e}")


def summarise_text(text: str, sentences: int = SUMMARY_SENTENCES) -> str:
    """Produce a simple extractive summary using word frequencies.

    Args:
        text: The input text to summarise.
        sentences: Maximum number of sentences to return.

    Returns:
        A summary string.
    """
    # Split text into sentences.  We use a simple regex which may not
    # handle abbreviations perfectly but is sufficient for short abstracts.
    raw_sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(raw_sentences) <= sentences:
        return text
    # Compute word frequencies (lower‑cased, alphabetical words only).
    word_freq: Dict[str, int] = {}
    for sentence in raw_sentences:
        for word in re.findall(r"\b[a-zA-Z]{3,}\b", sentence.lower()):
            word_freq[word] = word_freq.get(word, 0) + 1
    # Score each sentence by summing its word frequencies.
    scored_sentences = []
    for sentence in raw_sentences:
        score = sum(word_freq.get(word.lower(), 0) for word in re.findall(r"\b[a-zA-Z]{3,}\b", sentence))
        scored_sentences.append((score, sentence))
    # Sort sentences by score and choose top N.
    top_sentences = sorted(scored_sentences, key=lambda x: x[0], reverse=True)[:sentences]
    # Restore original order.
    top_sentences_sorted = sorted(top_sentences, key=lambda x: raw_sentences.index(x[1]))
    summary = " ".join(sentence.strip() for (_, sentence) in top_sentences_sorted)
    return summary


def search_crossref(query: str, rows: int = 10) -> List[Paper]:
    """Search Crossref for journal articles matching the query.

    Args:
        query: The search term.
        rows: Number of results to return (max 1000 per Crossref docs).

    Returns:
        A list of Paper objects containing metadata and abstracts when available.
    """
    params = {
        "query": query,
        "rows": rows,
        "filter": "type:journal-article",
        "select": "DOI,title,author,container-title,abstract,published-print,published-online,created"
    }
    # Include a mailto parameter as recommended by Crossref.
    params["mailto"] = CONTACT_EMAIL
    headers = {
        "User-Agent": f"research-assistant/1.0 (mailto:{CONTACT_EMAIL})"
    }
    try:
        response = requests.get(CROSSREF_BASE_URL, params=params, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        items = data.get("message", {}).get("items", [])
        papers = [Paper.from_crossref(item) for item in items]
        return papers
    except Exception as e:
        print(f"Failed to search Crossref: {e}")
        return []


def display_paper(paper: Paper, index: Optional[int] = None) -> None:
    """Print a paper’s metadata and summary to the console."""
    prefix = f"[{index}] " if index is not None else ""
    year_str = f" ({paper.year})" if paper.year else ""
    print(f"{prefix}{paper.title}{year_str}")
    print(f"   Authors : {paper.authors}")
    print(f"   Journal : {paper.journal or 'Unknown'}")
    print(f"   DOI     : {paper.doi}")
    if paper.abstract:
        if not paper.summary:
            paper.summary = summarise_text(paper.abstract)
        print("   Summary :")
        wrapped = textwrap.fill(paper.summary, width=80, subsequent_indent=" " * 11)
        print(f"{wrapped}")
    else:
        print("   Summary : No abstract available.")
    print(f"   Status  : {'Read' if paper.read else 'Unread'}\n")


def choose_indices(prompt: str, max_index: int) -> List[int]:
    """Ask the user to choose one or more indices from a list."""
    while True:
        selection = input(prompt).strip()
        if not selection:
            return []
        try:
            indices = [int(i) for i in re.split(r"[\s,]+", selection) if i]
            if all(0 <= i < max_index for i in indices):
                return indices
        except ValueError:
            pass
        print(f"Please enter valid numbers separated by spaces (0–{max_index - 1}).")


def search_and_save(library: List[Paper]) -> None:
    """Interactively search for new papers and add selected ones to the library."""
    query = input("Enter search terms: ").strip()
    if not query:
        print("No query entered.")
        return
    try:
        rows_input = input("How many results would you like to retrieve (default 5)? ").strip()
        rows = int(rows_input) if rows_input else 5
    except ValueError:
        print("Invalid number. Using 5 results.")
        rows = 5
    print(f"Searching for '{query}'...\n")
    results = search_crossref(query, rows=rows)
    if not results:
        print("No results found or there was an error with the search.")
        return
    for idx, paper in enumerate(results):
        display_paper(paper, index=idx)
    indices = choose_indices("Enter the indices of papers to save (separated by spaces), or press Enter to skip: ", len(results))
    added = 0
    for i in indices:
        paper = results[i]
        # Avoid duplicates by DOI.
        if not any(p.doi == paper.doi for p in library):
            # Generate summary now to store it.
            if paper.abstract and not paper.summary:
                paper.summary = summarise_text(paper.abstract)
            library.append(paper)
            added += 1
        else:
            print(f"Paper '{paper.title}' is already in your library.")
    if added > 0:
        save_library(library)
        print(f"Added {added} papers to your library.")
    else:
        print("No new papers were added.")


def list_library(library: List[Paper]) -> None:
    """Display all papers in the user’s library."""
    if not library:
        print("Your library is empty. Use the search option to add papers.")
        return
    for idx, paper in enumerate(library):
        status_symbol = "✓" if paper.read else "x"
        print(f"[{idx}] {status_symbol} {paper.title} ({paper.year if paper.year else 'n/a'})")
    detail_choice = input("Enter the index of a paper to see details, or press Enter to return: ").strip()
    if detail_choice:
        try:
            idx = int(detail_choice)
            if 0 <= idx < len(library):
                display_paper(library[idx])
            else:
                print("Invalid index.")
        except ValueError:
            print("Please enter a valid number.")


def mark_read_status(library: List[Paper]) -> None:
    """Toggle the read/unread status of selected papers."""
    if not library:
        print("Your library is empty.")
        return
    for idx, paper in enumerate(library):
        status_symbol = "✓" if paper.read else "x"
        print(f"[{idx}] {status_symbol} {paper.title}")
    indices = choose_indices("Enter the indices of papers to toggle read/unread status: ", len(library))
    if not indices:
        print("No changes made.")
        return
    for i in indices:
        library[i].read = not library[i].read
        status = "Read" if library[i].read else "Unread"
        print(f"Updated '{library[i].title}' to {status}.")
    save_library(library)


def main_menu() -> None:
    """Main interactive loop for the research assistant."""
    library = load_library()
    while True:
        print("\nResearch Assistant Menu")
        print("1. Search for papers")
        print("2. List your library")
        print("3. Mark papers as read/unread")
        print("4. Exit")
        choice = input("Choose an option (1-4): ").strip()
        if choice == "1":
            search_and_save(library)
        elif choice == "2":
            list_library(library)
        elif choice == "3":
            mark_read_status(library)
        elif choice == "4":
            print("Goodbye!")
            break
        else:
            print("Invalid choice. Please select 1, 2, 3, or 4.")


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\nExiting. Goodbye!")