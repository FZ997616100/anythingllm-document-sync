"""Optional real offline OCR checks using synthetic content only.

Run with requirements-pdf.txt and a local CJK font. RapidOCR's wheel includes
the OCR models. No dependencies or models are downloaded by these tests.
Optional environment variables:
PDF_OCR_TEST_FONT (a Chinese-capable .ttf/.ttc/.otf file), and
PDF_OCR_TEST_FONT_INDEX (the face index for a font collection).
Missing Python dependencies or fonts skip these tests; missing/broken models
and recognition failures do not skip. Ubuntu CI can use fonts-noto-cjk.
This verifies clean scan recognition, not real-document accuracy.
"""

import hashlib
import os
from pathlib import Path
import socket

import pytest

pytest.importorskip("pypdf")
pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")
pytest.importorskip("rapidocr")
pytest.importorskip("onnxruntime")

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from pypdf.generic import (DecodedStreamObject, DictionaryObject,
                           NameObject, NumberObject)

from anythingllm_loader.pdf_preprocessor import extract_pdf_text
from rapidocr.utils.download_file import DownloadFile


@pytest.fixture
def live_ocr(monkeypatch):
    attempts = []

    def reject_network(*args, **kwargs):
        attempts.append(args)
        pytest.fail("Local OCR attempted a network connection or model download")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr("requests.sessions.Session.request", reject_network)
    monkeypatch.setattr(DownloadFile, "run", reject_network)
    candidates = [os.environ.get("PDF_OCR_TEST_FONT"),
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                  "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
                  "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                  "C:/Windows/Fonts/simsun.ttc"]
    font_path = next((path for path in candidates if path and Path(path).is_file()), None)
    if not font_path:
        pytest.skip("A local Chinese-capable font is required for synthetic scan fixtures")
    noto_collection = Path(font_path).name in {"NotoSansCJK-Regular.ttc", "NotoSerifCJK-Regular.ttc"}
    # Noto's TTC face 0 is Japanese; face 2 supplies Simplified Chinese forms.
    font_index = int(os.environ.get("PDF_OCR_TEST_FONT_INDEX", "2" if noto_collection else "0"))
    if noto_collection:
        assert ImageFont.truetype(font_path, 104, index=font_index).getname()[0].endswith("CJK SC"), (
            "Synthetic Chinese scans require the Simplified Chinese Noto face"
        )
    yield {}, font_path, font_index
    assert not attempts


def add_page(writer, images=(), text=""):
    """Embed separate image XObjects so OCR must render the whole PDF page."""
    page = writer.add_blank_page(width=600, height=800)
    resources = DictionaryObject()
    commands = []
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                 NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        resources[NameObject("/Font")] = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
        commands.append(f"BT /F1 18 Tf 50 700 Td ({text}) Tj ET")
    xobjects = DictionaryObject()
    for index, (image, x, y, width, height) in enumerate(images):
        stream = DecodedStreamObject()
        stream.set_data(image.tobytes())
        stream.update({NameObject("/Type"): NameObject("/XObject"),
                       NameObject("/Subtype"): NameObject("/Image"),
                       NameObject("/Width"): NumberObject(image.width),
                       NameObject("/Height"): NumberObject(image.height),
                       NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                       NameObject("/BitsPerComponent"): NumberObject(8)})
        name = f"/Scan{index}"
        xobjects[NameObject(name)] = writer._add_object(stream)
        commands.append(f"q {width} 0 0 {height} {x} {y} cm {name} Do Q")
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources
    contents = DecodedStreamObject()
    contents.set_data("\n".join(commands).encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(contents)


@pytest.mark.parametrize("mode", ["auto", "force"])
def test_live_chinese_english_and_multiple_images(tmp_path, live_ocr, mode):
    options, font_path, font_index = live_ocr
    source = tmp_path / "synthetic_local_ocr.pdf"
    writer = PdfWriter()
    add_page(writer, text="LOCAL DIGITAL DOCUMENT TEXT FOR THE FIRST PAGE")

    chinese = Image.new("RGB", (2000, 2600), "white")
    draw = ImageDraw.Draw(chinese)
    chinese_font = ImageFont.truetype(font_path, 104, index=font_index)
    draw.text((150, 300), "安全学习", font=chinese_font, fill="black")
    draw.text((150, 550), "列车运行", font=chinese_font, fill="black")
    add_page(writer, [(chinese, 60, 80, 480, 624)])
    chinese.close()

    upper = Image.new("RGB", (1900, 360), "white")
    lower = Image.new("RGB", (1900, 360), "white")
    english_font = ImageFont.truetype(font_path, 86, index=font_index)
    ImageDraw.Draw(upper).text((80, 100), "UPPER PANEL ALPHA", font=english_font, fill="black")
    ImageDraw.Draw(lower).text((80, 100), "LOWER PANEL BRAVO", font=english_font, fill="black")
    add_page(writer, [(upper, 72, 570, 456, 86.4), (lower, 72, 180, 456, 86.4)])
    upper.close()
    lower.close()
    add_page(writer)  # A genuinely white page is explicit, not an OCR failure.
    with source.open("wb") as stream:
        writer.write(stream)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    result = extract_pdf_text(source, mode=mode, dpi=300, **options)
    normalized = "".join(result.split())
    assert "安全学习" in normalized
    assert "列车运行" in normalized
    assert "UPPERPANELALPHA" in normalized.upper()
    assert "LOWERPANELBRAVO" in normalized.upper()
    assert "LOCALDIGITALDOCUMENT" in normalized.upper()
    first_method = "text" if mode == "auto" else "OCR"
    assert f"Source: {source.name} | Page: 1 | Method: {first_method}" in result
    assert f"Source: {source.name} | Page: 2 | Method: OCR" in result
    assert f"Source: {source.name} | Page: 3 | Method: OCR" in result
    assert f"Source: {source.name} | Page: 4 | Method: blank" in result
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
