from __future__ import annotations

import json
import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..models import Failure, FailureCategory, Receipt
from ..storage import ProjectStore, digest, file_ref
from .datasheet import DatasheetArtifactAdapter
from .deep_reader import CodexDatasheetDeepReader, DatasheetDeepReader
from .registry import RegistryAdapter


def _normalized_text_with_offsets(text: str) -> tuple[str, list[int]]:
    """Normalize PDF extraction while retaining a map to the original span."""
    normalized: list[str] = []
    offsets: list[int] = []
    index = 0
    pending_space: int | None = None
    while index < len(text):
        char = text[index]
        # PDF extractors commonly split a word as ``imple-\nmentation``.
        if (
            char == "-"
            and index > 0
            and text[index - 1].isalnum()
            and index + 2 < len(text)
            and text[index + 1] in "\r\n"
        ):
            cursor = index + 1
            while cursor < len(text) and text[cursor] in "\r\n":
                cursor += 1
            if cursor < len(text) and text[cursor].isalnum():
                index = cursor
                continue
        expanded = unicodedata.normalize("NFKC", char)
        for value in expanded:
            if value.isspace():
                if normalized:
                    pending_space = index if pending_space is None else pending_space
                continue
            if pending_space is not None:
                normalized.append(" ")
                offsets.append(pending_space)
                pending_space = None
            normalized.append(value)
            offsets.append(index)
        index += 1
    return "".join(normalized), offsets


def normalized_quote_anchor(extracted: str, quote: str) -> dict[str, Any]:
    """Resolve a model quote to a hash-bound exact slice of receipt-owned text."""
    normalized_text, offsets = _normalized_text_with_offsets(extracted)
    normalized_quote, _ = _normalized_text_with_offsets(quote)
    if not normalized_quote:
        raise ValueError("deep-reader fact quote is empty after normalization")
    starts = [match.start() for match in re.finditer(re.escape(normalized_quote), normalized_text)]
    if not starts:
        raise ValueError("deep-reader fact quote is not present in normalized receipt extraction")
    normalized_start = starts[0]
    normalized_end = normalized_start + len(normalized_quote)
    original_start = offsets[normalized_start]
    original_end = offsets[normalized_end - 1] + 1
    exact = extracted[original_start:original_end]
    return {
        "start": original_start,
        "end": original_end,
        "exact_source_slice": exact,
        "source_slice_sha256": hashlib.sha256(exact.encode("utf-8")).hexdigest(),
        "normalized_quote_sha256": hashlib.sha256(normalized_quote.encode("utf-8")).hexdigest(),
        "occurrence_count": len(starts),
    }


class DesignGroundingAdapter:
    """Execute design provider calls and persist their unmodified outputs as Receipts."""

    def __init__(self, repo_root: Path, store: ProjectStore, run_id: str, registry: RegistryAdapter | None = None, datasheets: DatasheetArtifactAdapter | None = None, deep_reader: DatasheetDeepReader | None = None, receipt_category: str = "design"):
        self.repo_root, self.store, self.run_id = repo_root.resolve(), store, run_id
        self.registry = registry or RegistryAdapter()
        self.datasheets = datasheets or DatasheetArtifactAdapter(
            repo_root, project_id=store.project_dir.name
        )
        self.deep_reader = deep_reader or CodexDatasheetDeepReader(repo_root)
        self.receipt_category = receipt_category

    def _cached(self, operation: str, inputs: dict[str, Any]) -> Receipt | None:
        key = digest({"operation": operation, "inputs": inputs})
        # Receipts are immutable and category-specific.  A readiness reader
        # must materialize its own receipt even when Design already performed
        # an identical acquisition; later stage-receipt references are bound
        # to this adapter's category.
        for path in sorted(
            (self.store.receipts / self.receipt_category).glob("*.json"),
            reverse=True,
        ):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("success") is not True or value.get("operation") != operation:
                    continue
                if value.get("inputs", {}).get("cache_key") != key:
                    continue
                for artifact in value.get("artifacts", []):
                    source = self.store.project_dir / artifact["path"]
                    current = file_ref(source, self.store.project_dir, artifact.get("media_type", "application/octet-stream"))
                    if current.sha256 != artifact["sha256"]:
                        raise ValueError("cached provider artifact changed")
                return Receipt.model_validate(value)
            except (KeyError, TypeError, ValueError, FileNotFoundError, json.JSONDecodeError):
                continue
        return None

    def _receipt(self, operation: str, inputs: dict[str, Any], call, category: FailureCategory, cacheable: bool = True) -> Receipt:
        inputs = {**inputs, "provider_version": "grounding-v2"}
        cache_key = digest({"operation": operation, "inputs": inputs})
        if cacheable and (cached := self._cached(operation, inputs)):
            return cached
        inputs["cache_key"] = cache_key
        started = datetime.now(timezone.utc).isoformat(); receipt_id = self.store.new_id("design-provider")
        raw_path = self.store.logs / self.run_id / f"{receipt_id}.json"; raw_path.parent.mkdir(parents=True, exist_ok=True)
        failure = None
        try:
            outputs = call(); success = True
        except Exception as exc:
            outputs = {"error_type": type(exc).__name__, "summary": str(exc)}; success = False
            failure = Failure(category=category, summary=f"{operation} failed: {type(exc).__name__}: {exc}", retryable=category == FailureCategory.REGISTRY)
        raw_path.write_text(json.dumps(outputs, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        receipt = Receipt(receipt_id=receipt_id, run_id=self.run_id, operation=operation, started_at=started, finished_at=datetime.now(timezone.utc).isoformat(), success=success, command=["MCP" if category == FailureCategory.REGISTRY else "datasheet-artifact", operation], inputs=inputs, outputs=outputs, artifacts=[file_ref(raw_path, self.store.project_dir, "application/json")], failure=failure)
        self.store.write_receipt(receipt, self.receipt_category); return receipt

    def registry_search(self, subsystem_id: str, exact: str, capability: str, selected_component: str | None = None) -> Receipt:
        def call():
            result = self.registry.search_pair(exact, capability)
            if selected_component: result["selected_component_details"] = self.registry.candidate_details(selected_component)
            return result
        return self._receipt("registry_search_pair", {"subsystem_id": subsystem_id, "exact_query": exact, "capability_query": capability, "selected_component": selected_component}, call, FailureCategory.REGISTRY)

    def local_idf_selection(self, subsystem_id: str, exact: str, capability: str) -> Receipt:
        return self._receipt("local_idf_selection", {"subsystem_id": subsystem_id, "exact_query": exact, "capability_query": capability}, lambda: {"status": "local_idf", "exact_query": exact, "capability_query": capability}, FailureCategory.ENVIRONMENT)

    def datasheet_inspect(
        self,
        subsystem_id: str,
        record: dict[str, Any],
        requested_facts: list[dict[str, Any]] | None = None,
    ) -> Receipt:
        def acquire_call():
            destination = (
                self.repo_root / "hardware" / "datasheets" / "incoming"
                / self.store.project_dir.name / subsystem_id
            )
            try:
                resolved = self.datasheets.acquire_if_needed(record, destination)
            except Exception:
                # Model URLs/hashes are leads, not evidence. Fall back to the
                # repository acquisition cascade before recording failure.
                resolved = self.datasheets.acquire(record, destination)
            resolved = self.datasheets.canonicalize(resolved)
            return {"record": resolved}

        acquire = self._receipt(
            "datasheet_artifact_acquire",
            {
                "subsystem_id": subsystem_id,
                "part_number": record.get("part_number"),
                "source": record.get("source"),
                "declared_hash": record.get("content_hash"),
            },
            acquire_call,
            FailureCategory.DATASHEET,
            cacheable=True,
        )
        if not acquire.success:
            return self._receipt(
                "datasheet_artifact_inspect",
                {"subsystem_id": subsystem_id, "stage_receipt_id": acquire.receipt_id, "level": record.get("level")},
                lambda: (_ for _ in ()).throw(RuntimeError(acquire.failure.summary if acquire.failure else "datasheet acquisition failed")),
                FailureCategory.DATASHEET,
                cacheable=False,
            )
        resolved = dict(acquire.outputs["record"])
        record.clear()
        record.update(resolved)

        extract = self._receipt(
            "datasheet_artifact_extract",
            {
                "subsystem_id": subsystem_id,
                "source": resolved.get("source"),
                "content_hash": resolved.get("content_hash"),
                "extractor": "pdftotext-default-v1",
            },
            lambda: self.datasheets.inspect(resolved),
            FailureCategory.DATASHEET,
            cacheable=True,
        )
        if not extract.success:
            return self._receipt(
                "datasheet_artifact_inspect",
                {"subsystem_id": subsystem_id, "stage_receipt_id": extract.receipt_id, "level": record.get("level")},
                lambda: (_ for _ in ()).throw(RuntimeError(extract.failure.summary if extract.failure else "datasheet extraction failed")),
                FailureCategory.DATASHEET,
                cacheable=False,
            )

        inspected = dict(extract.outputs)
        implementation_facts: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        stage_receipts = [acquire, extract]
        if (
            requested_facts
            or str(record.get("level") or inspected.get("level") or "") in {"L2", "L3"}
        ):
            extracted = str(inspected.get("extracted_text") or "")
            text_hash = str(inspected.get("extracted_text_sha256") or "")

            def deep_read_call() -> dict[str, Any]:
                facts = (
                    self.deep_reader.read(
                        self.store.project_dir.name,
                        subsystem_id,
                        str(record.get("part_number") or inspected.get("part_number") or ""),
                        extracted,
                        text_hash,
                        requested_facts,
                    )
                    if requested_facts
                    else self.deep_reader.read(
                        self.store.project_dir.name,
                        subsystem_id,
                        str(record.get("part_number") or inspected.get("part_number") or ""),
                        extracted,
                        text_hash,
                    )
                )
                return {
                    "facts": facts,
                    "model_usage": getattr(
                        self.deep_reader, "last_usage",
                        {"source": "unavailable"},
                    ),
                    "model_context_digest": getattr(
                        self.deep_reader, "last_context_digest", None
                    ),
                    "model_context_bytes": getattr(
                        self.deep_reader, "last_context_bytes", None
                    ),
                }

            deep_read = self._receipt(
                "datasheet_deep_read",
                {
                    "subsystem_id": subsystem_id,
                    "part_number": record.get("part_number") or inspected.get("part_number"),
                    "extracted_text_sha256": text_hash,
                    "reader": "codex-deep-reader-v1",
                    "requested_facts": requested_facts or [],
                    # A graph repair generation deliberately re-runs only the
                    # probabilistic reader. Acquire/extract receipts remain
                    # reusable across generations.
                    "repair_generation": self.run_id,
                },
                deep_read_call,
                FailureCategory.DATASHEET,
                # A targeted readiness repair needs a fresh probabilistic
                # response. Reusing a same-run receipt that already omitted
                # the requested operations only repeats the identical gap.
                cacheable=not bool(requested_facts),
            )
            stage_receipts.append(deep_read)
            if not deep_read.success:
                diagnostics.append({
                    "code": "DATASHEET_DEEP_READ_FAILED",
                    "cause": FailureCategory.DATASHEET.value,
                    "disposition": "REPAIR_INTERNAL",
                    "severity": "BLOCKING",
                    "responsible_party": "design_grounding",
                    "affected_owner": subsystem_id,
                    "summary": deep_read.failure.summary if deep_read.failure else "datasheet deep read failed",
                    "retry_scope": f"{subsystem_id}:deep_read",
                })
            else:
                facts = list(deep_read.outputs.get("facts", []))

                def anchor_call():
                    accepted: list[dict[str, Any]] = []
                    rejected: list[dict[str, Any]] = []
                    for index, fact in enumerate(facts, start=1):
                        quote = str(fact.get("quote") or "")
                        try:
                            anchor = normalized_quote_anchor(extracted, quote)
                            tokens = [
                                str(token) for token in fact.get("source_tokens", [])
                                if str(token) and str(token) != "datasheet-extracted.txt"
                                and not str(token).lower().startswith("sha256:")
                            ]
                            if not tokens:
                                raise ValueError("deep-reader fact lacks source tokens")
                            accepted.append({
                                "id": f"IF_{subsystem_id.upper()}_{index:02d}",
                                "subsystem_id": subsystem_id,
                                "parameter": str(fact.get("parameter") or ""),
                                "value": fact.get("value"),
                                "unit": fact.get("unit"),
                                "source_kind": "datasheet",
                                "source_assertions": [{
                                    "path_glob": f"components/{subsystem_id}/**/*",
                                    "required_tokens": list(dict.fromkeys([
                                        str(record.get("part_number") or inspected.get("part_number") or ""),
                                        *tokens,
                                    ])),
                                }],
                                "evidence_locator": {
                                    "extracted_text_sha256": text_hash,
                                    # Keep the exact receipt-owned text for human
                                    # review while binding the machine check to a
                                    # normalized span and hashes.
                                    "quote": anchor["exact_source_slice"],
                                    **anchor,
                                },
                            })
                        except ValueError as exc:
                            rejected.append({
                                "fact_index": index,
                                "parameter": str(fact.get("parameter") or ""),
                                "summary": str(exc),
                            })
                    return {"accepted": accepted, "rejected": rejected}

                anchor = self._receipt(
                    "datasheet_fact_anchor",
                    {
                        "subsystem_id": subsystem_id,
                        "extracted_text_sha256": text_hash,
                        "facts_sha256": digest(facts),
                        "algorithm": "normalized-span-v1",
                    },
                    anchor_call,
                    FailureCategory.DATASHEET,
                    cacheable=True,
                )
                stage_receipts.append(anchor)
                if anchor.success:
                    implementation_facts = list(anchor.outputs.get("accepted", []))
                    for rejected in anchor.outputs.get("rejected", []):
                        diagnostics.append({
                            "code": "DATASHEET_FACT_ANCHOR_REJECTED",
                            "cause": FailureCategory.DATASHEET.value,
                            "disposition": "REPAIR_INTERNAL",
                            "severity": "REVIEW",
                            "responsible_party": "design_grounding",
                            "affected_owner": subsystem_id,
                            "summary": (
                                f"fact {rejected['fact_index']} "
                                f"({rejected['parameter'] or 'unnamed'}): "
                                f"{rejected['summary']}"
                            ),
                            "retry_scope": (
                                f"{subsystem_id}:fact:{rejected['fact_index']}"
                            ),
                        })
                    if not implementation_facts:
                        diagnostics.append({
                            "code": "DATASHEET_NO_ANCHORED_FACTS",
                            "cause": FailureCategory.DATASHEET.value,
                            "disposition": "REPAIR_INTERNAL",
                            "severity": "BLOCKING",
                            "responsible_party": "design_grounding",
                            "affected_owner": subsystem_id,
                            "summary": (
                                "deep reader produced no fact that could be "
                                "bound to the receipt-owned extraction"
                            ),
                            "retry_scope": f"{subsystem_id}:deep_read",
                        })
                else:
                    diagnostics.append({
                        "code": "DATASHEET_FACT_ANCHOR_FAILED",
                        "cause": FailureCategory.DATASHEET.value,
                        "disposition": "REPAIR_INTERNAL",
                        "severity": "BLOCKING",
                        "responsible_party": "design_grounding",
                        "affected_owner": subsystem_id,
                        "summary": anchor.failure.summary if anchor.failure else "datasheet fact anchor failed",
                        "retry_scope": f"{subsystem_id}:fact_anchor",
                    })

        inspected["implementation_facts"] = implementation_facts
        inspected["grounding_diagnostics"] = diagnostics
        inspected["stage_receipts"] = [
            {
                "receipt_id": item.receipt_id,
                "operation": item.operation,
                "success": item.success,
                "receipt_sha256": file_ref(
                    self.store.receipts / self.receipt_category / f"{item.receipt_id}.json",
                    self.store.project_dir,
                    "application/json",
                ).sha256,
            }
            for item in stage_receipts
        ]

        final_inputs = {
            "subsystem_id": subsystem_id,
            "source": record.get("source"),
            "declared_hash": record.get("content_hash"),
            "level": record.get("level"),
            "stage_receipt_ids": [item.receipt_id for item in stage_receipts],
        }
        return self._receipt(
            "datasheet_artifact_inspect",
            final_inputs,
            lambda: inspected,
            FailureCategory.DATASHEET,
            cacheable=not diagnostics,
        )
