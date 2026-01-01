# minimal_paced_embeddings.py
import time
from typing import List
from math import ceil
from langchain_core.embeddings import Embeddings


class PacedEmbeddings(Embeddings):
    def __init__(self, base: Embeddings, tpm_limit: int = 150_000, batch_size: int = 32, cushion_sec: float = 0.25):
        self.base = base
        self.tpm_limit = tpm_limit
        self.batch_size = batch_size
        self.cushion_sec = cushion_sec  # tiny buffer so we don't hit the wall exactly
        self._window_start = time.time()
        self._tokens_used = 0

    @staticmethod
    def _est_tokens(texts: List[str]) -> int:
        # rough but good enough: 4 chars ≈ 1 token
        return sum(max(1, len(t) // 4) for t in texts)

    def _maybe_sleep(self, tokens_needed: int):
        now = time.time()
        elapsed = now - self._window_start

        # new 60s window?
        if elapsed >= 60:
            self._window_start = now
            self._tokens_used = 0
            elapsed = 0

        if self._tokens_used + tokens_needed > self.tpm_limit:
            # wait until the minute rolls over, plus a tiny cushion
            sleep_s = max(0.0, 60 - elapsed) + self.cushion_sec
            time.sleep(sleep_s)
            self._window_start = time.time()
            self._tokens_used = 0

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i:i + self.batch_size]
            tokens = self._est_tokens(batch)
            self._maybe_sleep(tokens)
            out.extend(self.base.embed_documents(batch))
            self._tokens_used += tokens
            time.sleep(0.05)  # tiny spread so requests don't bunch up in the same second
        return out

    def embed_query(self, text: str) -> List[float]:
        tokens = self._est_tokens([text])
        self._maybe_sleep(tokens)
        vec = self.base.embed_query(text)
        self._tokens_used += tokens
        return vec
