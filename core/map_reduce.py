"""Map-Reduce framework for large-data processing tasks.

Three-stage pipeline:
  Map    — split items into batches, process each via agent, write to /tmp.
  Reduce — read all batch results, call agent to produce final report.
  Deliver — send the final file to the user via Feishu.

Usage::

    task = MapReduceTask(
        items=my_data_list,
        batch_size=20,
        map_fn="请分析以下数据，提取风险点：\n{batch}",
        reduce_fn="请汇总以下批次结果，生成最终报告：\n{results}",
        output_filename="risk_report.md",
    )
    await run_map_reduce(task, agent_runner=my_runner, send_file_fn=my_send_fn)
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

AgentRunner = Callable[[str], Awaitable[str]]
SendFileFn = Callable[[str, str], Awaitable[bool]]
ProgressCallback = Callable[[int, int, str], None]


@dataclass
class MapReduceTask:
    items: list
    batch_size: int
    map_fn: str          # must contain {batch}
    reduce_fn: str       # must contain {results}
    output_filename: str = "report.md"
    output_dir: str = "/tmp"
    item_serializer: Callable | None = None
    separator: str = "\n---\n"
    on_progress: ProgressCallback | None = None


async def run_map_reduce(
    task: MapReduceTask,
    agent_runner: AgentRunner,
    send_file_fn: SendFileFn | None = None,
) -> str:
    """Execute a MapReduceTask and optionally deliver the result.

    Returns the absolute path of the final report file.
    """
    run_id = int(time.time())
    output_dir = task.output_dir or "/tmp"
    serialize = task.item_serializer or str

    items = task.items
    batch_size = max(1, task.batch_size)
    batches = [items[i: i + batch_size] for i in range(0, len(items), batch_size)]
    num_batches = len(batches)

    logger.info(
        "[MapReduce] Start: %d items, batch_size=%d → %d batches",
        len(items), batch_size, num_batches,
    )

    # ── Map phase ──
    batch_result_paths: list[str] = []
    for idx, batch in enumerate(batches):
        batch_id = idx + 1
        label = f"Map {batch_id}/{num_batches}"
        batch_text = task.separator.join(serialize(item) for item in batch)
        prompt = task.map_fn.replace("{batch}", batch_text)

        try:
            result = await agent_runner(prompt)
        except Exception:
            logger.exception("[MapReduce] %s — agent call failed", label)
            result = f"[批次 {batch_id} 处理失败]"

        path = os.path.join(output_dir, f"mr_{run_id}_batch_{batch_id:04d}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(result)
            batch_result_paths.append(path)
            logger.info("[MapReduce] %s — wrote %d chars to %s", label, len(result), path)
        except OSError:
            logger.exception("[MapReduce] %s — failed to write batch result", label)

        if task.on_progress:
            try:
                task.on_progress(batch_id, num_batches, label)
            except Exception:
                logger.debug("[MapReduce] on_progress error", exc_info=True)

    # ── Reduce phase ──
    parts: list[str] = []
    for path in batch_result_paths:
        try:
            with open(path, encoding="utf-8") as f:
                parts.append(f.read())
        except OSError:
            logger.warning("[MapReduce] Could not read batch file: %s", path)

    reduce_prompt = task.reduce_fn.replace("{results}", "\n\n".join(parts))
    logger.info("[MapReduce] Reduce — calling agent with %d chars", len(reduce_prompt))

    try:
        final_result = await agent_runner(reduce_prompt)
    except Exception:
        logger.exception("[MapReduce] Reduce — agent call failed")
        final_result = "（汇总阶段出错，以下为各批次原始结果）\n\n" + "\n\n".join(parts)

    final_path = os.path.join(output_dir, task.output_filename)
    try:
        with open(final_path, "w", encoding="utf-8") as f:
            f.write(final_result)
        logger.info("[MapReduce] Reduce — wrote final report (%d chars) to %s", len(final_result), final_path)
    except OSError:
        logger.exception("[MapReduce] Failed to write final report to %s", final_path)

    if task.on_progress:
        try:
            task.on_progress(num_batches, num_batches, "Reduce")
        except Exception:
            logger.debug("[MapReduce] on_progress reduce error", exc_info=True)

    # ── Deliver phase ──
    if send_file_fn is not None:
        logger.info("[MapReduce] Deliver — sending %s", final_path)
        try:
            ok = await send_file_fn(final_path, task.output_filename)
            if ok:
                logger.info("[MapReduce] Deliver — success")
            else:
                logger.warning("[MapReduce] Deliver — send_file_fn returned False")
        except Exception:
            logger.exception("[MapReduce] Deliver — send_file_fn raised")
    else:
        logger.info("[MapReduce] Deliver — skipped (no send_file_fn)")

    return final_path
