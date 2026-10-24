import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "text-plagiarism" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*a, **k):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


# ligatures are not in WinAnsi: map them to codes 1-5 through a /Differences encoding
_LIGATURES = {"ﬀ": 1, "ﬁ": 2, "ﬂ": 3, "ﬃ": 4, "ﬄ": 5}


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """No keys or identities from the developer's environment; caches and run dirs under tmp_path."""
    for name in ("OPENALEX_API_KEY", "CORE_API_KEY", "BRAVE_API_KEY", "TEXT_PLAGIARISM_MAILTO"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TEXT_PLAGIARISM_CACHE", str(tmp_path / "_cache"))
    import net

    net.reset_rate_limits()


def _esc(s: str) -> bytes:
    out = bytearray()
    for ch in s:
        if ch in _LIGATURES:
            out.append(_LIGATURES[ch])
        else:
            out += ch.encode("cp1252", errors="replace")
    raw = bytes(out)
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def make_pdf(lines: list[str], pages: int = 1) -> bytes:
    """Hand-written minimal PDF: Helvetica, WinAnsiEncoding, one Tj per line, lines split over pages."""
    per = max(1, -(-len(lines) // pages)) if lines else 1
    chunks = [lines[i * per:(i + 1) * per] for i in range(pages)]
    n_pages = len(chunks)
    # object ids: 1 catalog, 2 pages, 3 font, then (page, content) pairs
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n_pages))
    objs: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding << /Type /Encoding "
        b"/BaseEncoding /WinAnsiEncoding /Differences [1 /ff /fi /fl /ffi /ffl] >> >>",
    ]
    for i, chunk in enumerate(chunks):
        ops = [b"BT", b"/F1 11 Tf", b"14 TL", b"50 750 Td"]
        for ln in chunk:
            ops.append(b"(" + _esc(ln) + b") Tj T*")
        ops.append(b"ET")
        stream = b"\n".join(ops)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {5 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def blank_pdf() -> bytes:
    """A PDF page with no text (what a scanned document looks like to a text extractor)."""
    return make_pdf([])
