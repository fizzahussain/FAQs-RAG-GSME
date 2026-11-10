"""Derived vector index for approved evidence"""

from qdrant_client import QdrantClient, models
import math


class QdrantEvidence:
    def __init__(self, url: str, collection: str, dimension: int, hybrid: bool = False):
        self.client = QdrantClient(url=url, timeout=30)
        self.collection = collection
        self.dimension = dimension
        self.hybrid = hybrid
        self._ready = False

    def ensure_collection(self) -> None:
        if self._ready:
            return
        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(
                    size=self.dimension, distance=models.Distance.COSINE
                ),
            )
        info = self.client.get_collection(self.collection)
        configuration = info.config.params.vectors
        if (isinstance(configuration, dict) or configuration is None
                or configuration.size != self.dimension
                or configuration.distance != models.Distance.COSINE):
            raise ValueError(
                "Qdrant collection dimension or distance mismatch; use a new collection for this model"
            )
        for field in ("tenant_id", "version_id", "model_version", "text_sha256"):
            if field not in info.payload_schema:
                self.client.create_payload_index(
                    self.collection, field, models.PayloadSchemaType.KEYWORD, wait=True
                )
        if self.hybrid and "text" not in info.payload_schema:
            # The optional lexical path reads the same approved text payload
            self.client.create_payload_index(
                self.collection,
                "text",
                models.TextIndexParams(
                    type=models.TextIndexType.TEXT,
                    tokenizer=models.TokenizerType.WORD,
                    lowercase=True,
                    scoring=models.TextScoringParams(type=models.TextScoringType.BM25),
                ),
                wait=True,
            )
        self._ready = True

    def upsert(self, items: list[tuple[str, list[float], dict]]) -> None:
        if any(len(vector) != self.dimension or not all(math.isfinite(value) for value in vector)
               or not any(vector) for _, vector, _ in items):
            raise ValueError("Vector dimension, finite values, and nonzero magnitude are required")
        if items:
            self.client.upsert(
                self.collection,
                [
                    models.PointStruct(id=point_id, vector=vector, payload=payload)
                    for point_id, vector, payload in items
                ],
                wait=True,
            )

    def delete(self, point_ids: list[str]) -> None:
        if point_ids and self.client.collection_exists(self.collection):
            self.client.delete(self.collection, models.PointIdsList(points=point_ids), wait=True)

    def existing(self, point_ids: list[str]) -> dict[str, dict]:
        if not point_ids or not self.client.collection_exists(self.collection):
            return {}
        points = self.client.retrieve(
            self.collection, ids=point_ids, with_payload=True, with_vectors=False
        )
        return {str(point.id): point.payload or {} for point in points}

    def reusable_vector(self, tenant_id: str, model_version: str, text_sha256: str):
        """Reuse only identical document text in this tenant/model/collection.

        This is a derived-value cache, not evidence approval. Callers must first
        validate the new source's independent acceptance and token budget.
        Old points may have been reconciled away; a cache miss simply embeds.
        """
        from hashlib import sha256
        points, _ = self.client.scroll(self.collection,
            scroll_filter=models.Filter(must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))
                for key, value in (("tenant_id", tenant_id), ("model_version", model_version), ("text_sha256", text_sha256))]),
            limit=1, with_vectors=True, with_payload=True)
        if not points:
            return None
        point = points[0]
        payload, vector = point.payload or {}, point.vector
        if (payload.get("tenant_id") != tenant_id or payload.get("model_version") != model_version
                or payload.get("text_sha256") != text_sha256
                or sha256(payload.get("text", "").encode()).hexdigest() != text_sha256
                or not isinstance(vector, list) or len(vector) != self.dimension
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in vector)
                or not any(vector)):
            return None
        return vector

    def query(self, tenant_id: str, vector: list[float], limit: int, *, text: str | None = None,
              model_version: str | None = None, point_ids: list[str] | None = None):
        if (len(vector) != self.dimension or not all(math.isfinite(value) for value in vector)
                or not any(vector)):
            raise ValueError("Query vector is invalid")
        if not self.client.collection_exists(self.collection):
            return []
        if point_ids is not None and not point_ids:
            return []
        filter_ = models.Filter(
            must=[models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id))]
        )
        if model_version:
            filter_.must.append(models.FieldCondition(key="model_version", match=models.MatchValue(value=model_version)))
        if point_ids is not None:
            filter_.must.append(models.HasIdCondition(has_id=point_ids))
        if text is not None:
            if not self.hybrid:
                raise ValueError("Hybrid search was not enabled")
            self.ensure_collection()
            # RRF combines vector and keyword rankings from the same collection
            result = self.client.query_points(
                self.collection,
                prefetch=[
                    models.Prefetch(query=vector, limit=limit, filter=filter_),
                    models.Prefetch(
                        query=models.TextQuery(text=text), using="text", limit=limit, filter=filter_
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
                with_payload=False,
            )
        else:
            result = self.client.query_points(
                self.collection, query=vector, query_filter=filter_, limit=limit, with_payload=False
            )
        return result.points
