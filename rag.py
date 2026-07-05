"""
RAG engine — 100% free stack:
  - PDF text extraction: pypdf (local)
  - Embeddings: sentence-transformers all-MiniLM-L6-v2 (runs locally, data never leaves your machine)
  - Vector store: numpy in-memory + saved to disk (no database needed)
  - LLM: Groq free tier (llama-3.3-70b-versatile) — Groq does not train on your data.
         Optionally Gemini (NOT recommended: free tier data may be used for training).
"""

import os
import json
import pickle
import numpy as np
import aiohttp
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
INDEX_PATH = os.path.join(DATA_DIR, "index.pkl")

# ---------------------------------------------------------------------------
# Embedding model (lazy-loaded, local, free)
# ---------------------------------------------------------------------------
_model = None

def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")  # ~90MB, downloads once
    return _model


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def extract_pdf_text(pdf_path: str) -> list[str]:
    """Extract text per page from a PDF."""
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    return [(page.extract_text() or "") for page in reader.pages]


def chunk_text(pages: list[str], chunk_size: int = 900, overlap: int = 150) -> list[dict]:
    """Split pages into overlapping chunks, remembering page numbers."""
    chunks = []
    for page_no, text in enumerate(pages, start=1):
        text = " ".join(text.split())  # normalize whitespace
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            # try to break on a sentence/space boundary
            if end < len(text):
                space = text.rfind(". ", start, end)
                if space > start + chunk_size // 2:
                    end = space + 1
            chunk = text[start:end].strip()
            if len(chunk) > 50:
                chunks.append({"text": chunk, "page": page_no})
            start = max(end - overlap, start + 1)
    return chunks


def build_index(pdf_path: str):
    """Build and save the vector index from a PDF. Run once (or when the PDF changes)."""
    print(f"Extracting text from {pdf_path} ...")
    pages = extract_pdf_text(pdf_path)
    chunks = chunk_text(pages)
    print(f"Created {len(chunks)} chunks. Embedding locally (nothing is uploaded) ...")
    model = get_model()
    embeddings = model.encode(
        [c["text"] for c in chunks],
        normalize_embeddings=True,
        show_progress_bar=True,
    )
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(INDEX_PATH, "wb") as f:
        pickle.dump({"chunks": chunks, "embeddings": np.asarray(embeddings)}, f)
    print(f"Index saved to {INDEX_PATH}")


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
_index = None

def load_index():
    global _index
    if _index is None:
        if not os.path.exists(INDEX_PATH):
            raise FileNotFoundError(
                "No index found. Run:  python ingest.py path/to/your.pdf"
            )
        with open(INDEX_PATH, "rb") as f:
            _index = pickle.load(f)
    return _index


def retrieve(query: str, top_k: int = 4) -> list[dict]:
    index = load_index()
    q = get_model().encode([query], normalize_embeddings=True)[0]
    scores = index["embeddings"] @ q  # cosine similarity (normalized vectors)
    top = np.argsort(scores)[::-1][:top_k]
    return [
        {**index["chunks"][i], "score": float(scores[i])}
        for i in top
        if scores[i] > 0.15  # drop irrelevant matches
    ]


# ---------------------------------------------------------------------------
# LLM call (Groq free tier by default)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a helpful assistant that answers questions using ONLY the provided document excerpts.
Rules:
- Answer clearly and concisely based on the excerpts.
- If the answer is not in the excerpts, say you couldn't find it in the document and suggest contacting HR.
- Mention the page number(s) you used, like (p. 7).
- Never invent policies, numbers, or dates."""


async def generate_answer(question: str, history: list[dict] | None = None) -> str:
    provider = os.environ.get("LLM_PROVIDER", "groq").lower()
    contexts = retrieve(question)

    if not contexts:
        return ("I couldn't find anything relevant in the document for that. "
                "Try rephrasing, or contact HR directly.")

    context_block = "\n\n---\n\n".join(
        f"[Page {c['page']}]\n{c['text']}" for c in contexts
    )
    user_prompt = f"Document excerpts:\n\n{context_block}\n\nQuestion: {question}"

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history[-6:])  # keep last 3 exchanges for follow-up questions
    messages.append({"role": "user", "content": user_prompt})

    if provider == "gemini":
        return await _call_gemini(messages)
    return await _call_groq(messages)


async def _call_groq(messages: list[dict]) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "GROQ_API_KEY is not set. Get a free key at https://console.groq.com"
    payload = {
        "model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 700,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            data = await resp.json()
            if resp.status != 200:
                return f"LLM error ({resp.status}): {json.dumps(data)[:300]}"
            return data["choices"][0]["message"]["content"]


async def _call_gemini(messages: list[dict]) -> str:
    """Optional. WARNING: Gemini FREE tier may use your data to improve Google's
    models. Only use this if that's acceptable, or if you have a PAID key."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "GEMINI_API_KEY is not set."
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    system = next((m["content"] for m in messages if m["role"] == "system"), "")
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user",
         "parts": [{"text": m["content"]}]}
        for m in messages if m["role"] != "system"
    ]
    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            data = await resp.json()
            if resp.status != 200:
                return f"LLM error ({resp.status}): {json.dumps(data)[:300]}"
            return data["candidates"][0]["content"]["parts"][0]["text"]
