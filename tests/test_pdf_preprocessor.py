"""Real synthetic PDF parsing/rendering; OCR inference is deliberately a stub.

These tests do not claim to verify recognition accuracy of a local OCR engine.
"""

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("pypdf")
pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                           NameObject, NumberObject)

from anythingllm_loader.pdf_preprocessor import PdfExtractionError, extract_pdf_text
from anythingllm_loader import pdf_preprocessor


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

    def infer(image_path):
        with Image.open(image_path) as image:
            calls.append((image_path, image.copy()))
        return SimpleNamespace(txts=["扫描文字 scanned text"])

    monkeypatch.setattr(pdf_preprocessor, "_create_ocr_engine", lambda model_dir: infer)
    yield calls
    for _, image in calls:
        image.close()


def test_text_page_keeps_text_without_ocr_or_engine(tmp_path, monkeypatch):
    source = tmp_path / "文字.pdf"
    text = "This text page contains more than thirty nonspace characters."
    make_pdf(source, [(text, [])])
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr(pdf_preprocessor, "_create_ocr_engine", lambda *a: pytest.fail("Text page must not invoke OCR"))
    result = extract_pdf_text(source)
    assert f"Source: {source.name} | Page: 1 | Method: text" in result
    assert text in result
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_mixed_pdf_ocr_only_scanned_page_and_render_all_images(tmp_path, ocr_stub):
    source = tmp_path / "mixed.pdf"
    make_pdf(source, [("Sufficient digital text for the first non-scanned page.", []),
                      ("", [(10, 20, (255, 0, 0)), (120, 190, (0, 0, 255))])])
    result = extract_pdf_text(source, dpi=72)
    assert "Page: 1 | Method: text" in result
    assert "Page: 2 | Method: OCR\n扫描文字 scanned text" in result
    assert len(ocr_stub) == 1
    image_path, image = ocr_stub[0]
    assert image.size == (200, 300)  # Full page, not the first embedded image.
    assert image.convert("RGB").getpixel((30, 250)) == (255, 0, 0)
    assert image.convert("RGB").getpixel((140, 80)) == (0, 0, 255)
    assert not Path(image_path).exists()  # Temporary page images are removed.


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


def test_engine_errors_abort_document(tmp_path, monkeypatch):
    source = tmp_path / "scanned.pdf"
    make_pdf(source, [("", [(10, 20, (255, 0, 0))])])

    def fail(*args, **kwargs):
        raise RuntimeError("inference failed")

    monkeypatch.setattr(pdf_preprocessor, "_create_ocr_engine", lambda model_dir: fail)
    with pytest.raises(PdfExtractionError, match="inference failed"):
        extract_pdf_text(source)


def test_missing_model_is_not_silent_download_fallback(tmp_path, monkeypatch):
    pytest.importorskip("rapidocr")
    from rapidocr.utils.download_file import DownloadFile
    source = tmp_path / "scanned.pdf"
    make_pdf(source, [("", [(10, 20, (0, 0, 0))])])
    monkeypatch.setattr(DownloadFile, "run", lambda *a: pytest.fail("No download fallback"))
    with pytest.raises(PdfExtractionError, match="Missing local OCR model"):
        extract_pdf_text(source, model_dir=tmp_path, dpi=72)


def test_missing_embedded_dictionary_is_not_downloaded(tmp_path, monkeypatch):
    pytest.importorskip("rapidocr")
    import onnxruntime
    import rapidocr
    for name in ["PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx",
                 "ch_ppocr_mobile_v2.0_cls_mobile.onnx"]:
        (tmp_path / name).touch()
    session = SimpleNamespace(get_modelmeta=lambda: SimpleNamespace(custom_metadata_map={}))
    monkeypatch.setattr(onnxruntime, "InferenceSession", lambda *a, **kw: session)
    monkeypatch.setattr(rapidocr, "RapidOCR", lambda *a, **kw: pytest.fail("Dictionary must be checked before initialization"))
    with pytest.raises(PdfExtractionError, match="no embedded character dictionary"):
        pdf_preprocessor._create_ocr_engine(tmp_path)


def test_empty_ocr_aborts_instead_of_returning_partial_document(tmp_path, monkeypatch):
    source = tmp_path / "partial.pdf"
    make_pdf(source, [("A good first page must not hide a failed second-page OCR.", []),
                      ("", [(10, 20, (0, 0, 0))])])

    monkeypatch.setattr(pdf_preprocessor, "_create_ocr_engine", lambda model_dir:
                        lambda image_path: SimpleNamespace(txts=[]))
    with pytest.raises(PdfExtractionError, match="page 2: OCR returned no text"):
        extract_pdf_text(source, dpi=72)


@pytest.mark.parametrize("mode", ["auto", "force"])
def test_verified_white_page_is_marked_blank_without_ocr(tmp_path, monkeypatch, mode):
    source = tmp_path / "blank.pdf"
    make_pdf(source, [("", []), ("", [(10, 20, (255, 255, 255))])])
    monkeypatch.setattr(pdf_preprocessor, "_create_ocr_engine", lambda *a: pytest.fail("Verified blank page needs no OCR"))
    result = extract_pdf_text(source, mode=mode, dpi=72)
    assert "Page: 1 | Method: blank" in result
    assert "Page: 2 | Method: blank" in result


def test_nearly_white_page_is_not_guessed_blank(tmp_path, monkeypatch):
    source = tmp_path / "faint.pdf"
    make_pdf(source, [("", [(10, 20, (254, 254, 254))])])

    monkeypatch.setattr(pdf_preprocessor, "_create_ocr_engine", lambda model_dir:
                        lambda image_path: SimpleNamespace(txts=[]))
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


@pytest.mark.parametrize("options", [{"mode": "off"}, {"min_text_chars": 0}, {"dpi": 0}])
def test_invalid_options_are_explicit(options):
    with pytest.raises(ValueError):
        extract_pdf_text("unused.pdf", **options)
