"""
Build the local vector index from your PDF. Run once, and again whenever the PDF changes.

Usage:
    python ingest.py document/employee_handbook.pdf
"""
import sys
from rag import build_index

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python ingest.py <path-to-pdf>")
        sys.exit(1)
    build_index(sys.argv[1])
