from __future__ import annotations

import csv
import io
import mimetypes
import shutil
import subprocess
import tempfile
from pathlib import Path

from .db import digest
from .envelopes import current_message
from .models import RecordInput, Scope
from .providers import NotConfigured


def ffmpeg():
    binary = shutil.which("ffmpeg")
    if binary:
        return binary
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as e:
        raise NotConfigured("Install eventmem[media] or FFmpeg") from e


def run_ffmpeg(args):
    result = subprocess.run(
        [ffmpeg(), "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        timeout=600,
    )
    if result.returncode:
        raise ValueError("FFmpeg could not decode the attachment")


def parse(engine, sid):
    source = engine.source(sid)
    path = engine.source(sid, content=True)
    mime = source["media_type"]
    pieces = []
    if mime.startswith(("audio/", "video/")):
        with tempfile.TemporaryDirectory(prefix="memorypalace-media-") as temp:
            if mime.startswith("audio/"):
                run_ffmpeg(
                    [
                        "-i",
                        str(path),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "segment",
                        "-segment_time",
                        "300",
                        str(Path(temp) / "audio-%05d.wav"),
                    ]
                )
            else:
                # A silent video has no audio stream. Optional mapping lets its
                # independently decodable keyframes proceed without an ASR call.
                result = subprocess.run(
                    [
                        ffmpeg(),
                        "-nostdin",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-i",
                        str(path),
                        "-map",
                        "0:a?",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        "-f",
                        "segment",
                        "-segment_time",
                        "300",
                        str(Path(temp) / "audio-%05d.wav"),
                    ],
                    capture_output=True,
                    timeout=600,
                )
                if (
                    result.returncode
                    and b"does not contain any stream" not in result.stderr
                ):
                    raise ValueError("FFmpeg could not decode the audio stream")
            for i, audio in enumerate(sorted(Path(temp).glob("audio-*.wav"))):
                pieces.append(
                    (
                        "Audio segment awaiting transcription",
                        {
                            "start_seconds": i * 300,
                            "end_seconds": (i + 1) * 300,
                            "type": "audio_segment",
                            "blob": engine.db.blob(audio.read_bytes()),
                            "analysis_role": "asr",
                        },
                    )
                )
            if mime.startswith("video/"):
                run_ffmpeg(
                    [
                        "-i",
                        str(path),
                        "-vf",
                        "fps=1/30,scale=768:-2",
                        str(Path(temp) / "frame-%05d.png"),
                    ]
                )
                # Always retain the first frame, including videos shorter than 30 s.
                if not list(Path(temp).glob("frame-*.png")):
                    run_ffmpeg(
                        [
                            "-i",
                            str(path),
                            "-frames:v",
                            "1",
                            str(Path(temp) / "frame-00001.png"),
                        ]
                    )
                for i, frame in enumerate(sorted(Path(temp).glob("frame-*.png"))):
                    pieces.append(
                        (
                            "Video keyframe awaiting analysis",
                            {
                                "start_seconds": i * 30,
                                "type": "keyframe",
                                "blob": engine.db.blob(frame.read_bytes()),
                                "analysis_role": "vision",
                                "mime": "image/png",
                            },
                        )
                    )
    elif mime.startswith("image/"):
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
        pieces.append(
            (
                "Image awaiting analysis",
                {
                    "type": "image",
                    "source_id": sid,
                    "width": width,
                    "height": height,
                    "analysis_role": "vision",
                    "mime": mime,
                },
            )
        )
    elif mime in ("text/plain", "text/markdown", "text/csv", "application/json"):
        text = path.read_text(encoding="utf-8-sig")
        if mime == "text/csv":
            for i, row in enumerate(csv.reader(io.StringIO(text))):
                pieces.append((" | ".join(row), {"row": i + 1, "type": "table"}))
        else:
            # Preserve paragraph boundaries; split exceptionally long paragraphs
            # with explicit offsets rather than manufacturing page numbers.
            offset = 0
            if (
                source["namespace"].startswith("host:")
                and source.get("metadata", {}).get("host_event") == "message"
                and source.get("metadata", {}).get("role") == "user"
            ):
                body = current_message(text)
                offset = len(text) - len(body)
                text = body
            for paragraph in text.split("\n\n"):
                for start in range(0, len(paragraph), 6000):
                    pieces.append(
                        (
                            paragraph[start : start + 6000],
                            {
                                "char_start": offset + start,
                                "char_end": offset + min(len(paragraph), start + 6000),
                                "type": "paragraph",
                            },
                        )
                    )
                offset += len(paragraph) + 2
    elif (
        mime == "application/pdf"
        and engine.settings("parsers").get("pdf", "native") == "native"
    ):
        # Docling's native PDF backend provides text cells and page coordinates
        # without downloading an offline layout-model stack. Scanned pages use
        # the configured vision endpoint. Full local layout parsing is opt-in.
        try:
            from docling.backend.docling_parse_backend import (
                DoclingParseDocumentBackend,
            )
            from docling.datamodel.document import InputDocument
            from docling.datamodel.base_models import InputFormat
        except ImportError as e:
            raise NotConfigured("Install eventmem[media] for PDF parsing") from e
        document = InputDocument(
            path_or_stream=path,
            format=InputFormat.PDF,
            backend=DoclingParseDocumentBackend,
        )
        backend = document._backend
        try:
            for page_number in range(backend.page_count()):
                page = backend.load_page(page_number)
                cells = list(page.get_text_cells())
                for i, cell in enumerate(cells):
                    value = cell.model_dump(mode="json")
                    pieces.append(
                        (
                            cell.text,
                            {
                                "page": page_number + 1,
                                "cell": i,
                                "type": "pdf_text_cell",
                                "coordinates": value.get("rect", value.get("bbox")),
                                "parser": "docling-native",
                            },
                        )
                    )
                if not cells:
                    image = page.get_page_image(scale=1.5)
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    pieces.append(
                        (
                            "Scanned page awaiting analysis",
                            {
                                "page": page_number + 1,
                                "type": "scanned_page",
                                "blob": engine.db.blob(buffer.getvalue()),
                                "analysis_role": "vision",
                                "mime": "image/png",
                            },
                        )
                    )
                page.unload()
        finally:
            backend.unload()
    else:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError as e:
            raise NotConfigured("Install eventmem[media] for document parsing") from e
        suffix = (
            Path(source["title"]).suffix or mimetypes.guess_extension(mime) or ".pdf"
        )
        with tempfile.TemporaryDirectory(prefix="memorypalace-document-") as temp:
            input_path = Path(temp) / ("source" + suffix)
            shutil.copyfile(path, input_path)
            doc = DocumentConverter().convert(input_path).document
            for item, level in doc.iterate_items():
                text = (
                    item.export_to_markdown(doc=doc)
                    if hasattr(item, "export_to_markdown")
                    else getattr(item, "text", "")
                )
                if not text:
                    continue
                locator = {
                    "type": str(item.label),
                    "ref": item.self_ref,
                    "level": level,
                    "provenance": [
                        p.model_dump(mode="json") for p in getattr(item, "prov", [])
                    ],
                }
                if getattr(item, "prov", []):
                    locator["page"] = item.prov[0].page_no
                pieces.append((text, locator))
    root_id = "mem_" + digest([sid, "document"])[:32]
    root = RecordInput(
        id=root_id,
        kind="knowledge",
        title=source["title"],
        content=source["title"] or "Attachment",
        scope=Scope(**source["scope"]),
        source_ids=[sid],
        valid_from=source["occurred_at"],
        confirmation="documented",
        locator={"source_id": sid, "type": "document"},
        attributes={"document_version": source["version"]},
    )
    records = [root]
    for i, (text, locator) in enumerate(pieces):
        if not text.strip():
            continue
        records.append(
            RecordInput(
                id="mem_" + digest([sid, i])[:32],
                kind="knowledge",
                title=f"{source['title']} · {i + 1}",
                content=text,
                scope=root.scope,
                source_ids=[sid],
                parent_id=root_id,
                valid_from=source["occurred_at"],
                confirmation="inferred"
                if locator.get("type")
                in {"image", "keyframe", "transcript", "scanned_page"}
                else "documented",
                generated=False,
                locator=locator | {"source_id": sid},
                attributes={
                    "document_version": source["version"],
                    "analysis_pending": bool(locator.get("analysis_role")),
                },
            )
        )
    return records


def clip(engine, sid, start, end):
    if start < 0 or end <= start or end - start > 120:
        raise ValueError("Clip duration must be between 0 and 120 seconds")
    source = engine.source(sid)
    if not source["media_type"].startswith(("audio/", "video/")):
        raise ValueError("Source is not audio or video")
    suffix = ".mp4" if source["media_type"].startswith("video/") else ".wav"
    with tempfile.TemporaryDirectory(prefix="memorypalace-clip-") as temp:
        output = Path(temp) / ("clip" + suffix)
        run_ffmpeg(
            [
                "-ss",
                str(start),
                "-i",
                str(engine.source(sid, content=True)),
                "-t",
                str(end - start),
                str(output),
            ]
        )
        return output.read_bytes(), "video/mp4" if suffix == ".mp4" else "audio/wav"
