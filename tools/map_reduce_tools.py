"""Map-Reduce file I/O tools, callable by agents during map-reduce tasks.

Tools:
1. write_batch_result  — write one batch output to /tmp.
2. read_all_batch_results — read all batch outputs for the reduce step.
3. write_final_report  — write the final consolidated report to /tmp.
"""

import logging
import os
import glob as _glob

logger = logging.getLogger(__name__)

_DEFAULT_DIR = "/tmp"


def write_batch_result(batch_id: int, content: str, output_dir: str = _DEFAULT_DIR) -> dict:
    """Write a single batch processing result to a file.

    Args:
        batch_id: Unique integer identifier for this batch (used in filename).
        content: The text content produced by processing this batch.
        output_dir: Directory to store the file. Defaults to /tmp.

    Returns:
        dict with keys ``ok`` (bool), ``path`` (str), ``error`` (str).
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"batch_{batch_id:04d}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("write_batch_result: batch_id=%d path=%s (%d chars)", batch_id, path, len(content))
        return {"ok": True, "path": path, "error": ""}
    except OSError as exc:
        logger.warning("write_batch_result failed: %s", exc)
        return {"ok": False, "path": "", "error": str(exc)}


def read_all_batch_results(output_dir: str = _DEFAULT_DIR) -> dict:
    """Read all batch result files from output_dir, sorted by batch_id.

    Args:
        output_dir: Directory previously passed to write_batch_result.

    Returns:
        dict with keys ``ok`` (bool), ``results`` (list[dict]), ``combined`` (str),
        ``count`` (int), ``error`` (str).
    """
    try:
        file_paths = sorted(_glob.glob(os.path.join(output_dir, "batch_*.txt")))
        if not file_paths:
            return {"ok": False, "results": [], "combined": "", "count": 0,
                    "error": f"no batch files found in '{output_dir}'"}

        results = []
        for fp in file_paths:
            basename = os.path.basename(fp)
            try:
                batch_id = int(basename.replace("batch_", "").replace(".txt", ""))
            except ValueError:
                batch_id = -1
            try:
                with open(fp, encoding="utf-8") as f:
                    content = f.read()
            except OSError as exc:
                logger.warning("read_all_batch_results: cannot read %s: %s", fp, exc)
                content = f"[读取失败: {exc}]"
            results.append({"batch_id": batch_id, "content": content})

        combined = "\n\n".join(r["content"] for r in results)
        logger.info("read_all_batch_results: %d files, %d chars in '%s'", len(results), len(combined), output_dir)
        return {"ok": True, "results": results, "combined": combined, "count": len(results), "error": ""}

    except OSError as exc:
        logger.warning("read_all_batch_results failed: %s", exc)
        return {"ok": False, "results": [], "combined": "", "count": 0, "error": str(exc)}


def write_final_report(content: str, filename: str, output_dir: str = _DEFAULT_DIR) -> dict:
    """Write the final consolidated report to a file.

    Args:
        content: Full text of the report.
        filename: Target filename (e.g. "risk_report.md"). No path separators allowed.
        output_dir: Directory to store the report. Defaults to /tmp.

    Returns:
        dict with keys ``ok`` (bool), ``path`` (str), ``error`` (str).
    """
    try:
        if not filename or os.sep in filename or "/" in filename:
            raise ValueError(f"Invalid filename '{filename}': must be a plain name without path separators")
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info("write_final_report: path=%s (%d chars)", path, len(content))
        return {"ok": True, "path": path, "error": ""}
    except (ValueError, OSError) as exc:
        logger.warning("write_final_report failed: %s", exc)
        return {"ok": False, "path": "", "error": str(exc)}
