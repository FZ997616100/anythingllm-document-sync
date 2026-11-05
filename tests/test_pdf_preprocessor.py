"""Real synthetic PDF parsing/rendering; OCR subprocess is deliberately a stub.

These tests do not claim to verify recognition accuracy of a local OCR engine.
"""

import hashlib
from pathlib import Path
import subprocess

import pytest

pytest.importorskip("pypdf")
pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                           NameObject, NumberObject)

from anythingllm_loader.pdf_preprocessor import PdfExtractionError, extract_pdf_text


def make_pdf(path, pages):
    """Create actual text/image PDF objects, avoiding extra fixture libraries."""
    writer = PdfWriter()
    for text, images in pages:
        page = writer.add_blank_page(width=200, height=300)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        resources = DictionaryObject({NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font)})})
        commands = [f"BT /F1 10 Tf 10 270 Td ({text}) Tj ET"] if text else []
        xobjects = DictionaryObject()
        for index, (x, y, rgb) in enumerate(images):
            image = DecodedStreamObject()
            image.set_data(bytes(rgb) * 100)
            image.update({NameObject("/Type"): NameObject("/XObject"),
                          NameObject("/Subtype"): NameObject("/Image"),
                          NameObject("/Width"): NumberObject(10),
                          NameObject("/Height"): NumberObject(10),
                          NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                          NameObject("/BitsPerComponent"): NumberObject(8)})
            name = f"/Im{index}"
            xobjects[NameObject(name)] = writer._add_object(image)
            commands.append(f"q 50 0 0 50 {x} {y} cm {name} Do Q")
        resources[NameObject("/XObject")] = xobjects
        page[NameObject("/Resources")] = resources
        contents = DecodedStreamObject()
        contents.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(contents)
    with path.open("wb") as stream:
        writer.write(stream)


@pytest.fixture
def ocr_stub(monkeypatch):
    calls = []

    def run(command, **kwargs):
        assert kwargs["check"] is True
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["timeout"] == 120
        if "--list-langs" in command:
            return subprocess.CompletedProcess(command, 0, "List of available languages (2):\nchi_sim\neng\n", "")
        with Image.open(command[1]) as image:
            calls.append((command, image.copy()))
        return subprocess.CompletedProcess(command, 0, "扫描文字 scanned text\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    yield calls
    for _, image in calls:
        image.close()


def test_text_page_keeps_text_without_ocr_or_engine(tmp_path, monkeypatch):
    source = tmp_path / "文字.pdf"
    text = "This text page contains more than thirty nonspace characters."
    make_pdf(source, [(text, [])])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Text page must not invoke OCR"))
    result = extract_pdf_text(source)
    assert f"Source: {source.name} | Page: 1 | Method: text" in result
    assert text in result
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_mixed_pdf_ocr_only_scanned_page_and_render_all_images(tmp_path, ocr_stub):
    source = tmp_path / "mixed.pdf"
    make_pdf(source, [("Sufficient digital text for the first non-scanned page.", []),
                      ("", [(10, 20, (255, 0, 0)), (120, 190, (0, 0, 255))])])
    result = extract_pdf_text(source, dpi=72, tesseract_cmd="C:/Local OCR/tesseract.exe",
                              tessdata_dir="C:/Local OCR/tessdata")
    assert "Page: 1 | Method: text" in result
    assert "Page: 2 | Method: OCR\n扫描文字 scanned text" in result
    assert len(ocr_stub) == 1
    command, image = ocr_stub[0]
    assert image.size == (200, 300)  # Full page, not the first embedded image.
    assert image.convert("RGB").getpixel((30, 250)) == (255, 0, 0)
    assert image.convert("RGB").getpixel((140, 80)) == (0, 0, 255)
    assert command[0] == "C:/Local OCR/tesseract.exe"
    assert command[command.index("-l") + 1] == "chi_sim+eng"
    assert command[command.index("--tessdata-dir") + 1] == "C:/Local OCR/tessdata"
    assert not Path(command[1]).exists()  # Temporary page images are removed.


def test_force_ocr_covers_text_and_image_on_same_page(tmp_path, ocr_stub):
    source = tmp_path / "hybrid.pdf"
    make_pdf(source, [("This digital heading has enough text to bypass automatic OCR.",
                       [(10, 20, (255, 0, 0))])])
    auto = extract_pdf_text(source, dpi=72)
    assert "Method: text" in auto
    assert not ocr_stub  # Auto intentionally does not detect image text.
    forced = extract_pdf_text(source, mode="force", dpi=72)
    assert "Method: OCR" in forced
    assert "扫描文字" in forced
    assert len(ocr_stub) == 1


def test_short_text_page_is_ocr_candidate(tmp_path, ocr_stub):
    source = tmp_path / "short.pdf"
    make_pdf(source, [("1", [])])
    assert "Method: OCR" in extract_pdf_text(source, dpi=72)
    assert len(ocr_stub) == 1


@pytest.mark.parametrize("failure, message", [
    (FileNotFoundError(), "executable not found"),
    (subprocess.TimeoutExpired("tesseract", 120), "timed out"),
    (subprocess.CalledProcessError(1, "tesseract", stderr="bad language"), "exit code 1"),
])
def test_engine_errors_abort_document(tmp_path, monkeypatch, failure, message):
    source = tmp_path / "scanned.pdf"
    make_pdf(source, [("", [(10, 20, (255, 0, 0))])])

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(PdfExtractionError, match=message):
        extract_pdf_text(source)


def test_missing_chinese_language_is_not_silent_fallback(tmp_path, monkeypatch):
    source = tmp_path / "scanned.pdf"
    make_pdf(source, [("", [(10, 20, (0, 0, 0))])])
    monkeypatch.setattr(subprocess, "run", lambda command, **kw:
                        subprocess.CompletedProcess(command, 0, "List:\neng\n", ""))
    with pytest.raises(PdfExtractionError, match="Missing local Tesseract language data: chi_sim"):
        extract_pdf_text(source)


def test_empty_ocr_aborts_instead_of_returning_partial_document(tmp_path, monkeypatch):
    source = tmp_path / "partial.pdf"
    make_pdf(source, [("A good first page must not hide a failed second-page OCR.", []),
                      ("", [(10, 20, (0, 0, 0))])])

    def run(command, **kwargs):
        output = "chi_sim\neng" if "--list-langs" in command else " \n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PdfExtractionError, match="page 2: OCR returned no text"):
        extract_pdf_text(source, dpi=72)


@pytest.mark.parametrize("mode", ["auto", "force"])
def test_verified_white_page_is_marked_blank_without_ocr(tmp_path, monkeypatch, mode):
    source = tmp_path / "blank.pdf"
    make_pdf(source, [("", []), ("", [(10, 20, (255, 255, 255))])])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("Verified blank page needs no OCR"))
    result = extract_pdf_text(source, mode=mode, dpi=72)
    assert "Page: 1 | Method: blank" in result
    assert "Page: 2 | Method: blank" in result


def test_nearly_white_page_is_not_guessed_blank(tmp_path, monkeypatch):
    source = tmp_path / "faint.pdf"
    make_pdf(source, [("", [(10, 20, (254, 254, 254))])])

    def run(command, **kwargs):
        output = "chi_sim\neng" if "--list-langs" in command else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(PdfExtractionError, match="OCR returned no text"):
        extract_pdf_text(source, dpi=72)


def test_invalid_pdf_reports_failure(tmp_path):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"broken")
    with pytest.raises(PdfExtractionError, match="PDF extraction failed"):
        extract_pdf_text(source)


def test_encrypted_pdf_reports_failure(tmp_path):
    source = tmp_path / "encrypted.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    writer.encrypt("secret")
    with source.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(PdfExtractionError, match="Encrypted PDF"):
        extract_pdf_text(source)


@pytest.mark.parametrize("options", [{"mode": "off"}, {"min_text_chars": 0}, {"dpi": 0},
                                     {"timeout": 0}, {"languages": "chi_sim+"}])
def test_invalid_options_are_explicit(options):
    with pytest.raises(ValueError):
        extract_pdf_text("unused.pdf", **options)
