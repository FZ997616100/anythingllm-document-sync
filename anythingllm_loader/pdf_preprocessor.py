"""Optional, offline PDF extraction with page-level source markers.

``auto`` keeps a page's text layer when it has at least ``min_text_chars``
non-whitespace characters; otherwise it renders the *entire* page for OCR.
A page with sufficient text plus scanned text in images requires ``force``:
``auto`` cannot detect missing image text. ``force`` OCRs every complete page.
OCR is a text approximation, not a table/layout/circuit interpretation engine.
The original PDF is opened read-only. Nothing is downloaded or sent to a cloud.
An unreadable page, missing local model, or OCR failure aborts the document;
callers must retain the previous index on failure. A page with no text layer
and an entirely white full-page rendering is explicitly marked blank. Any
other page returning empty OCR requires review, never a guess that it is blank.
PDFium rendering must not run concurrently in multiple threads.
"""

from pathlib import Path
import tempfile


class PdfExtractionError(RuntimeError):
    """Extraction could not produce a complete document for safe indexing."""


def extract_pdf_text(
    path,
    *,
    mode="auto",
    min_text_chars=30,
    dpi=300,
    model_dir=None,
):
    """Return text with filename, 1-based page number, and extraction method.

    Install requirements-pdf.txt separately. RapidOCR's pinned wheel includes
    the CPU Chinese/English models. ``model_dir`` optionally selects a local
    directory containing those same three model filenames. Missing files or
    an absent embedded character dictionary fail instead of downloading.
    """
    if mode not in {"auto", "force"}:
        raise ValueError("PDF mode must be 'auto' or 'force'")
    if not isinstance(min_text_chars, int) or min_text_chars < 1:
        raise ValueError("min_text_chars must be a positive integer")
    if not isinstance(dpi, int) or dpi < 1:
        raise ValueError("dpi must be a positive integer")

    source = Path(path)
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfExtractionError("Install optional requirements-pdf.txt for PDF preprocessing") from exc

    pages = []
    renderer = None
    engine = None
    current_page = 0
    try:
        with source.open("rb") as stream, tempfile.TemporaryDirectory(prefix="anythingllm-pdf-") as temporary:
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise PdfExtractionError("Encrypted PDF is unsupported; provide an authorized decrypted copy")
            if not reader.pages:
                raise PdfExtractionError("PDF has no pages")
            for current_page, page in enumerate(reader.pages, 1):
                text_layer = (page.extract_text() or "").strip()
                text = text_layer if mode == "auto" else ""
                method = "text"
                if mode == "force" or len("".join(text.split())) < min_text_chars:
                    if renderer is None:
                        try:
                            import pypdfium2 as pdfium
                        except ImportError as exc:
                            raise PdfExtractionError("Install optional requirements-pdf.txt to render PDF pages") from exc
                        renderer = pdfium.PdfDocument(str(source))
                    image_path = Path(temporary) / "page.png"
                    rendered_page = renderer[current_page - 1]
                    try:
                        bitmap = rendered_page.render(scale=dpi / 72)
                        try:
                            image = bitmap.to_pil()
                            try:
                                with image.convert("RGB") as rgb:
                                    blank = not text_layer and all(
                                        bounds == (255, 255) for bounds in rgb.getextrema()
                                    )
                                if not blank:
                                    image.save(image_path)
                            finally:
                                image.close()
                        finally:
                            bitmap.close()
                    finally:
                        rendered_page.close()
                    if blank:
                        pages.append(f"Source: {source.name} | Page: {current_page} | Method: blank\n"
                                     "[Blank page: no text layer and entirely white rendering]")
                        continue
                    if engine is None:
                        engine = _create_ocr_engine(model_dir)
                    result = engine(str(image_path))
                    text = "\n".join(result.txts or []).strip()
                    method = "OCR"
                    if not text:
                        raise PdfExtractionError("OCR returned no text; review this page before indexing")
                pages.append(f"Source: {source.name} | Page: {current_page} | Method: {method}\n{text}")
    except PdfExtractionError as exc:
        raise PdfExtractionError(f"{source.name}, page {current_page or '?'}: {exc}") from exc
    except Exception as exc:
        raise PdfExtractionError(f"{source.name}, page {current_page or '?'}: PDF extraction failed ({exc})") from exc
    finally:
        if renderer is not None:
            renderer.close()
    return "\n\n".join(pages)


def _create_ocr_engine(model_dir):
    try:
        import onnxruntime
        import rapidocr
    except ImportError as exc:
        raise PdfExtractionError("Install optional requirements-pdf.txt for local RapidOCR") from exc

    directory = Path(model_dir) if model_dir is not None else Path(rapidocr.__file__).parent / "models"
    names = {"Det": "PP-OCRv6_det_small.onnx",
             "Cls": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
             "Rec": "PP-OCRv6_rec_small.onnx"}
    paths = {kind: directory / name for kind, name in names.items()}
    for path in paths.values():
        if not path.is_file():
            raise PdfExtractionError(f"Missing local OCR model: {path}; automatic downloads are disabled")

    onnxruntime.disable_telemetry_events()
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    # RapidOCR otherwise downloads a character dictionary for a custom ONNX
    # model lacking metadata. Reject that case before its initializer runs.
    session = onnxruntime.InferenceSession(str(paths["Rec"]), sess_options=options,
                                           providers=["CPUExecutionProvider"])
    if not session.get_modelmeta().custom_metadata_map.get("character", "").strip():
        raise PdfExtractionError("Local OCR recognition model has no embedded character dictionary; automatic downloads are disabled")
    del session

    params = {f"{kind}.model_path": str(path) for kind, path in paths.items()}
    params.update({"EngineConfig.onnxruntime.use_cuda": False,
                   "EngineConfig.onnxruntime.use_dml": False,
                   "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                   "EngineConfig.onnxruntime.inter_op_num_threads": 1})
    return rapidocr.RapidOCR(params=params)
