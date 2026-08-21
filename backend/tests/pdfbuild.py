"""Hand-built PDFs for tests that must not depend on a LaTeX engine.

Some behaviour is only reachable through a specific content stream — a
rule drawn mid-line, a text layer missing one character. Building the
page by hand pins that shape exactly and runs everywhere, including on a
machine with no engine installed.
"""


def one_page_pdf(content: str, width: int = 612, height: int = 792) -> bytes:
    """A minimal, valid one-page PDF wrapping a literal content stream."""
    body = content.encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (f"<</Type/Page/Parent 2 0 R/MediaBox[0 0 {width} {height}]"
         "/Resources<</Font<</F1 5 0 R>>>>/Contents 4 0 R>>").encode("latin-1"),
        b"<</Length " + str(len(body)).encode() + b">>stream\n" + body + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj".encode() + obj + b"endobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def text_pdf(text: str) -> bytes:
    """A one-page PDF whose text layer is exactly `text`."""
    literal = (text.replace("\\", "\\\\")
                   .replace("(", r"\(")
                   .replace(")", r"\)"))
    return one_page_pdf(f"BT\n/F1 12 Tf\n72 700 Td ({literal}) Tj\nET\n")
