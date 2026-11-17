"""Bounded SSE delivery for evidence-checked answer claims."""

import asyncio
import json
from queue import Queue, Empty, Full
from threading import Event
import time

from fastapi.responses import StreamingResponse

from gsme_rag.generation.answers import AnswerCancelled
from gsme_rag.observability import REQUEST_ID, emit


def answer_stream(run, request, public_error, release_slot, validate_event=None):
    """Run blocking providers off the event loop; cancel at safe I/O boundaries.

    A disconnected client cannot kill an in-flight SDK read instantly. Its
    existing provider timeout still bounds that read, and the next boundary
    checks cancellation before sending/saving anything else. Slots stay held
    until the real worker finishes, so disconnects cannot spawn unlimited work.
    """
    events, cancelled = Queue(maxsize=24), Event()
    request_id, started = REQUEST_ID.get(), time.perf_counter()

    def publish(value):
        while not cancelled.is_set():
            try:
                events.put(value, timeout=.25)
                return
            except Full:
                continue
        raise AnswerCancelled()

    def worker():
        token = REQUEST_ID.set(request_id)
        try:
            result = run(publish, cancelled.is_set)
            publish({"type":"done", "result":result})
        except AnswerCancelled:
            emit("hosted.stream.cancelled")
        except Exception as error:
            if not cancelled.is_set():
                try:
                    publish({"type":"error", **public_error(error), "request_id":request_id})
                except AnswerCancelled:
                    pass
        finally:
            emit("hosted.stream.finished", elapsed_ms=round((time.perf_counter()-started)*1000, 2))
            release_slot()
            REQUEST_ID.reset(token)

    async def body():
        task = asyncio.create_task(asyncio.to_thread(worker))
        heartbeat = time.monotonic()
        try:
            yield "event: status\ndata: " + json.dumps({"stage":"searching", "request_id":request_id}) + "\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.to_thread(events.get, True, 1)
                except Empty:
                    if task.done():
                        # A worker cannot disappear silently and leave a draft
                        # displayed as though it were a finished answer.
                        yield 'event: error\ndata: {"detail":"The answer stream ended before completion. Please try again."}\n\n'
                        return
                    if time.monotonic() - heartbeat >= 10:
                        yield ": keep-alive\n\n"
                        heartbeat = time.monotonic()
                    continue
                kind = event["type"]
                if validate_event and kind in {"claim", "done"} and not await asyncio.to_thread(validate_event, event):
                    cancelled.set()
                    emit("hosted.stream.source_changed", request_id=request_id)
                    changed={"route":"documents", "status":"evidence_changed", "request_id":request_id,
                             "results":[], "answer_sources":[], "answer":{"status":"evidence_changed",
                             "text":"Source access changed. Please search again.", "claims":[], "citations":[]}}
                    yield "event: done\ndata: " + json.dumps({"result":changed}) + "\n\n"
                    return
                event.pop("type")
                yield f"event: {kind}\ndata: " + json.dumps(event, ensure_ascii=True) + "\n\n"
                if kind in {"done", "error"}:
                    return
        finally:
            cancelled.set()
            # Do not cancel/restart the real worker after an observation ends.
            # It retains its concurrency slot until its bounded provider I/O ends.
            task.add_done_callback(lambda completed: None if completed.cancelled() else completed.exception())

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering":"no", "Cache-Control":"no-store"})
