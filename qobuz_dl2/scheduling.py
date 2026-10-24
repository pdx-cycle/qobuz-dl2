"""Bounded async work with source-ordered outcomes and settled cancellation."""

import asyncio


async def bounded_map(function, items, limit):
    limit = max(1, int(limit))
    queue = asyncio.Queue(maxsize=2 * limit)
    sentinel = object()
    results = []

    async def produce():
        async def enqueue(item):
            index = len(results)
            results.append(None)
            await queue.put((index, item))

        try:
            if hasattr(items, "__aiter__"):
                async for item in items:
                    await enqueue(item)
            else:
                for item in items:
                    await enqueue(item)
        except Exception as error:
            results.append(error)
        for _ in range(limit):
            await queue.put(sentinel)

    async def consume():
        while True:
            job = await queue.get()
            try:
                if job is sentinel:
                    return
                index, item = job
                try:
                    results[index] = await function(item)
                except Exception as error:
                    results[index] = error
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(produce()), *[asyncio.create_task(consume()) for _ in range(limit)]]
    try:
        await asyncio.gather(*tasks)
        return results
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_owned_thread(function, *args):
    """A cancelled await must settle its file/database writer before returning."""
    future = asyncio.get_running_loop().run_in_executor(None, function, *args)
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not future.cancelled():
            future.exception()
        raise
