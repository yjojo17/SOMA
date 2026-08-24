"""
CLIP Post Classifier
--------------------
Zero-shot classification of Instagram posts against content buckets using
CLIP (ViT-B/32). Runs inline during collection — no ollama, no GPU, native ARM64.

Method (corrected):
  * Scores a post against the FULL category set and classifies by softmax/argmax,
    not by thresholding a raw cosine to a single prompt. Raw CLIP cosines cluster
    around ~0.2 regardless of content, so an absolute threshold cannot separate
    classes; the calibrated probability (logit_scale + softmax over candidates)
    can. `aligned` therefore means "the target bucket is the post's top category".
  * Fuses the caption into the query. "News", "Business" etc. are topical/source
    categories the image barely carries; the caption holds the signal. The query
    is a weighted blend of the caption-text and image embeddings, compared to the
    bucket-description embeddings in CLIP's shared space.

NOTE ON LANGUAGE: openai/clip-vit-base-patch32 is English-trained.

Requirements:
    pip install transformers torch pillow requests
"""

import io
import time
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from typing import Optional

from PIL import Image
import torch
from transformers import CLIPProcessor, CLIPModel

MODEL_NAME     = "openai/clip-vit-base-patch32"
CAPTION_WEIGHT = 0.4   # 0.0 = image only, 1.0 = caption only. Raise toward caption
                       # for topical/source buckets (News, Business); the image
                       # already carries the visual ones (Food, Pets, Fashion, Art).
TIMEOUT_IMG    = 15

log = logging.getLogger('clip')


# ── result type ───────────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    scores:     dict[str, float]   # softmax probability per bucket (sums to 1)
    top_bucket: str                # argmax category
    post_url:   str
    caption:    str

    def fits(self, bucket_name: str) -> bool:
        """True if this bucket is the post's top (argmax) category."""
        return bucket_name == self.top_bucket

    def in_top_k(self, bucket_name: str, k: int = 3) -> bool:
        """True if this bucket is among the k highest-probability categories."""
        ranked = sorted(self.scores, key=self.scores.get, reverse=True)
        return bucket_name in ranked[:k]

    def prob(self, bucket_name: str) -> float:
        """Calibrated probability for a bucket (use as the stored clip_score)."""
        return self.scores.get(bucket_name, 0.0)

    def best_target_score(self, target_names) -> float:
        return max((self.scores.get(n, 0.0) for n in target_names), default=0.0)

    def prob_for_target(self, target_names) -> float:
        """Summed probability across the account's target categories."""
        return sum(self.scores.get(n, 0.0) for n in target_names)

    @property
    def top_score(self) -> float:
        return self.scores.get(self.top_bucket, 0.0)


# ── core classifier ───────────────────────────────────────────────────────────

class CLIPClassifier:
    """Initialise with the FULL category set, not a single target bucket —
    argmax needs alternatives to be relative to. The account's target is applied
    later at decision time via result.fits(target) / result.in_top_k(target)."""

    def __init__(self, buckets: list[dict], caption_weight: float = CAPTION_WEIGHT):
        self.buckets        = buckets
        self.caption_weight = caption_weight

        self._model     = CLIPModel.from_pretrained(MODEL_NAME, local_files_only=True)
        self._processor = CLIPProcessor.from_pretrained(MODEL_NAME, local_files_only=True)
        self._model.eval()
        # Learned temperature (~100) — the scale CLIP is meant to be used at.
        self._logit_scale = self._model.logit_scale.exp().item()

        self._names = [b['name'] for b in buckets]
        texts       = [b['description'].strip() for b in buckets]
        inputs = self._processor(
            text=texts, return_tensors="pt",
            padding=True, truncation=True, max_length=77,
        )
        with torch.no_grad():
            tf = self._model.get_text_features(**inputs)
        self._text_features = tf / tf.norm(dim=-1, keepdim=True)   # [C, D]

    def _embed_image(self, image: Image.Image) -> torch.Tensor:
        inputs = self._processor(images=image, return_tensors="pt")
        with torch.no_grad():
            f = self._model.get_image_features(**inputs)
        return f / f.norm(dim=-1, keepdim=True)                    # [1, D]

    def _embed_text(self, text: str) -> torch.Tensor:
        inputs = self._processor(
            text=[text], return_tensors="pt",
            padding=True, truncation=True, max_length=77,
        )
        with torch.no_grad():
            f = self._model.get_text_features(**inputs)
        return f / f.norm(dim=-1, keepdim=True)                    # [1, D]

    def classify(self, image_url: str, caption: str = "", post_url: str = "",
                 session=None) -> ClassificationResult:
        t0 = time.time()
        resp = (session or requests).get(image_url, timeout=TIMEOUT_IMG)
        resp.raise_for_status()
        image = Image.open(io.BytesIO(resp.content)).convert("RGB")
        log.info(f"Download: {time.time()-t0:.1f}s — {post_url.split('/')[-2] if '/' in post_url else post_url}")

        t1 = time.time()
        img_feat = self._embed_image(image)                       # [1, D]

        caption = (caption or "").strip()
        if caption and self.caption_weight > 0:
            txt_feat = self._embed_text(caption)
            w = self.caption_weight
            query = w * txt_feat + (1 - w) * img_feat
            query = query / query.norm(dim=-1, keepdim=True)
        else:
            # Empty/emoji-only caption: nothing to embed, fall to image alone.
            query = img_feat

        logits = self._logit_scale * (query @ self._text_features.T)   # [1, C]
        probs  = logits.softmax(dim=-1).squeeze(0)                     # [C]

        scores = {name: round(probs[i].item(), 4) for i, name in enumerate(self._names)}
        top    = max(scores, key=scores.get)

        log.info(f"Inference: {time.time()-t1:.1f}s — top: {top} ({scores[top]:.3f})")
        return ClassificationResult(
            scores=scores, top_bucket=top, post_url=post_url, caption=caption
        )


# ── async service (production path) ──────────────────────────────────────────

class CLIPClassificationService:

    def __init__(self, buckets: list[dict], caption_weight: float = CAPTION_WEIGHT):
        self._classifier = CLIPClassifier(buckets, caption_weight)
        self._session    = requests.Session()
        self._executor   = ThreadPoolExecutor(max_workers=1)
        self._pending:   dict[str, Future] = {}
        self._results:   dict[str, ClassificationResult] = {}   # sync + completed async, reused everywhere
        # Serialises the shared requests.Session AND CLIP model across threads.
        self._lock = threading.Lock()

    def _classify(self, image_url: str, caption: str, post_url: str) -> ClassificationResult:
        """All classify calls funnel through here under one lock."""
        with self._lock:
            return self._classifier.classify(image_url, caption, post_url, self._session)

    def set_cookies(self, selenium_cookies: list) -> None:
        for c in selenium_cookies:
            self._session.cookies.set(c['name'], c['value'])
        log.info(f"Session cookies set: {len(selenium_cookies)} cookies")

    def submit(self, post_data: dict) -> None:
        post_link = post_data.get('post_link', '')
        image_url = post_data.get('image_url', '')
        if (not post_link or not image_url
                or post_link in self._pending or post_link in self._results):
            return
        log.debug(f"Submitted: {post_link.split('/')[-2]}")
        self._pending[post_link] = self._executor.submit(
            self._classify,
            image_url,
            post_data.get('caption', '') or post_data.get('description', ''),
            post_link,
        )

    def classify_sync(self, post_data: dict) -> Optional[ClassificationResult]:
        """Classify on the CALLING thread now and cache it, so the live interaction
        decision never races the async queue. result()/enrichment reuse the cache.
        Best-effort cancels a still-queued async future for the same post."""
        post_link = post_data.get('post_link', '')
        image_url = post_data.get('image_url', '')
        if not post_link or not image_url:
            return None
        if post_link in self._results:
            return self._results[post_link]
        fut = self._pending.pop(post_link, None)
        if fut is not None:
            fut.cancel()                       # skip the redundant async pass if not started
        try:
            r = self._classify(
                image_url,
                post_data.get('caption', '') or post_data.get('description', ''),
                post_link,
            )
            self._results[post_link] = r
            return r
        except Exception as e:
            log.warning(f"CLIP sync classify failed: {post_link} — {e}")
            return None

    def result(self, post_link: str, timeout: float = 8.0) -> Optional[ClassificationResult]:
        if post_link in self._results:
            return self._results[post_link]
        future = self._pending.get(post_link)
        if not future:
            return None
        try:
            r = future.result(timeout=timeout)
            self._results[post_link] = r
            return r
        except TimeoutError:
            log.warning(f"CLIP timeout: {post_link.split('/')[-2]}")
            return None
        except Exception as e:
            log.warning(f"CLIP error: {post_link.split('/')[-2]} — {e}")
            return None

    def activation(self, image_url: str, target_names, caption: str = "") -> float:
        """Synchronous single-image activation for extra carousel slides."""
        if not image_url:
            return 0.0
        try:
            r = self._classify(image_url, caption, image_url)
            return r.best_target_score(target_names)
        except Exception as e:
            log.warning(f"CLIP slide activation failed: {e}")
            return 0.0

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)