import asyncio
import io
import os
import random
import time
import aiohttp
from yarl import URL

# some important tricks:
# 1. os.sched_getaffinity to work right in docker
# 2. memoryview for less copies
# 3. keep redirects from the first head

class Downloader:
    def __init__(self, concurrency: int | None = None) -> None:
        if not concurrency:
            concurrency = len(os.sched_getaffinity(0))
        self.concurrency = concurrency
        self.retries = 0
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = asyncio.new_event_loop()

    _session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if not self._session:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit_per_host=self.concurrency),
                loop=self.loop,
            )
        return self._session

    async def get_remote_file_size(self, url: str | URL) -> "tuple[URL, int]":
        for i in range(3):
            start = time.time()
            headers = {"Retry-Count": str(i)} if i else {}
            try:
                response = await self.session.head(
                    url, allow_redirects=True, headers=headers
                )
                if response.status >= 400:
                    print("HEAD failed:", response, response.headers.items())
                # https://docs.aiohttp.org/en/stable/client_reference.html#aiohttp.ClientResponse.url
                # .url is the url of the final request, as opposed to .real_url
                return response.url, int(response.headers["Content-Length"])
            except KeyError as e:
                print("HEAD failed", repr(e))
                print(response.headers, response)
            except asyncio.TimeoutError:
                print(f"HEAD {url} timed out after {time.time() - start:.4f}")
            except aiohttp.ClientError as e:
                print(f"HEAD {url} {repr(e)}")
            await asyncio.sleep(random.random() / 10)
        raise ValueError(f"Failed to HEAD {url} after multiple retries")

    async def download_chunk(
        self, url: str | URL, start: int, end: int, buffer_view: memoryview
    ) -> None:
        for i in range(5):
            headers = {"Retry-Count": str(i)} if i else {}
            try:
                headers |= {"Range": f"bytes={start}-{end}"}
                async with self.session.get(url, headers=headers) as response:
                    buffer_view[start : end + 1] = await response.read()
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"Error: {e}")
                await asyncio.sleep(random.random() / 10)  # sleep 0-100ms
        raise ValueError(f"Failed to download {url} after multiple retries")

    async def download_file(self, url: str | URL) -> io.BytesIO:
        self.retries = 0
        start_time = time.time()
        url, file_size = await self.get_remote_file_size(url)
        chunk_size = file_size // self.concurrency
        # if it's less than 1kB, download only as a single chunk
        if chunk_size < 1 << 10:
            concurrency = 1
            chunk_size = file_size
        else:
            concurrency = self.concurrency
        buf = io.BytesIO()
        buf.write(b"\0" * file_size)
        buf.seek(0)
        buffer_view = memoryview(buf.getbuffer())
        tasks = [
            self.download_chunk(
                url,
                (start := i * chunk_size),
                start + chunk_size - 1 if i != concurrency - 1 else file_size - 1,
                buffer_view,
            )
            for i in range(concurrency)
        ]
        await asyncio.gather(*tasks)
        buf.seek(0)
        print(
            f"Downloaded {os.path.basename(str(url))} as {concurrency} {chunk_size // 1024}"
            f" kB chunks in {time.time() - start_time:.4f} with {self.retries} retries"
        )
        self.retries = 0
        return buf

    def sync_download_file(self, url: str) -> io.BytesIO:
        try:
            return self.loop.run_until_complete(self.download_file(url))
        except RuntimeError as e:
            if e.args[0] == "Event loop is closed":
                print("has to start a new event loop")
                self.loop = asyncio.new_event_loop()
                self._session = None
                return self.loop.run_until_complete(self.download_file(url))
            raise e
