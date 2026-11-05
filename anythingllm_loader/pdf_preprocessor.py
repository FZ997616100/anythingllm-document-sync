"""Optional, offline PDF extraction with page-level source markers.

``auto`` keeps a page's text layer when it has at least ``min_text_chars``
non-whitespace characters; otherwise it renders the *entire* page for OCR.
A page with sufficient text plus scanned text in images requires ``force``:
``auto`` cannot detect missing image text. ``force`` OCRs every complete page.
OCR is a text approximation, not a table/layout/circuit interpretation engine.
The original PDF is opened read-only. Nothing is downloaded or sent to a cloud.
An unreadable page, missing language, or OCR failure aborts the document;
callers must retain the previous index on failure. A page with no text layer
and an entirely white full-page rendering is explicitly marked blank. Any
other page returning empty OCR requires review, never a guess that it is blank.
PDFium rendering must not run concurrently in multiple threads.
"""

from pathlib import Path
import subprocess
import tempfile


class PdfExtractionError(RuntimeError):
    """Extraction could not produce a complete document for safe indexing."""


def extract_pdf_text(
    path,
    *,
    mode="auto",
    min_text_chars=30,
    dpi=300,
    tesseract_cmd="tesseract",
    languages="chi_sim+eng",
    tessdata_dir=None,
    timeout=120,
):
    """Return text with filename, 1-based page number, and extraction method.

    Install requirements-pdf.txt separately. OCR additionally needs a locally
    installed Tesseract CLI and every requested language's .traineddata file.
    ``tesseract_cmd`` is one executable path, not a shell command; optional
    ``tessdata_dir`` points directly at the directory holding .traineddata.
    ``timeout`` is the maximum seconds for each Tesseract invocation.
    """
    if mode not in {"auto", "force"}:
        raise ValueError("PDF mode must be 'auto' or 'force'")
    if not isinstance(min_text_chars, int) or min_text_chars < 1:
        raise ValueError("min_text_chars must be a positive integer")
    if not isinstance(dpi, int) or dpi < 1:
        raise ValueError("dpi must be a positive integer")
    if not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a positive finite number")
    if not isinstance(languages, str) or not all(languages.split("+")):
        raise ValueError("languages must contain language codes joined by '+'")

    source = Path(path)
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise PdfExtractionError("Install optional requirements-pdf.txt for PDF preprocessing") from exc

    pages = []
    renderer = None
    ocr_ready = False
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
                    if not ocr_ready:
                        _check_languages(tesseract_cmd, languages, tessdata_dir, timeout)
                        ocr_ready = True
                    command = [str(tesseract_cmd), str(image_path), "stdout", "-l", languages,
                               "--dpi", str(dpi), "--psm", "3"]
                    if tessdata_dir is not None:
                        command.extend(["--tessdata-dir", str(tessdata_dir)])
                    text = _run_tesseract(command, timeout).strip()
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


def _run_tesseract(command, timeout):
    try:
        result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=True)
    except FileNotFoundError as exc:
        raise PdfExtractionError("Tesseract executable not found; configure a local tesseract_cmd") from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfExtractionError("Tesseract timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise PdfExtractionError(f"Tesseract failed with exit code {exc.returncode}: {exc.stderr or ''}") from exc
    return result.stdout


def _check_languages(tesseract_cmd, languages, tessdata_dir, timeout):
    command = [str(tesseract_cmd), "--list-langs"]
    if tessdata_dir is not None:
        command.extend(["--tessdata-dir", str(tessdata_dir)])
    available = set(_run_tesseract(command, timeout).splitlines())
    missing = set(languages.split("+")) - available
    if missing:
        raise PdfExtractionError("Missing local Tesseract language data: " + ", ".join(sorted(missing)))
