"""Provision the fixed test encoding explicitly; service token counting stays offline."""

import hashlib
import os
from urllib.request import urlopen

from eventmem.core.retrieval import CL100K_CACHE_NAME, CL100K_SHA256, encoding, encoding_cache


def main():
    # Require an explicit destination so CI never depends on a runner's incidental cache.
    if not os.environ.get("TIKTOKEN_CACHE_DIR"):
        raise SystemExit("Set TIKTOKEN_CACHE_DIR to the test cache directory")
    directory = encoding_cache()
    target = directory / CL100K_CACHE_NAME
    data = target.read_bytes() if target.is_file() else b""
    if hashlib.sha256(data).hexdigest() != CL100K_SHA256:
        with urlopen("https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken", timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != CL100K_SHA256:
            raise SystemExit("cl100k_base checksum mismatch")
        directory.mkdir(parents=True, exist_ok=True)
        partial = directory / f".{CL100K_CACHE_NAME}.{os.getpid()}"
        partial.write_bytes(data)
        partial.replace(target)
    if encoding() is None:
        raise SystemExit("cl100k_base did not initialize")
    print("Verified cl100k_base test encoding")


if __name__ == "__main__":
    main()
