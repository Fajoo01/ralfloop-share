from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .registry import ModelToolSpec, validate_schema_value
from .remote_code import run_sandboxed_remote_code
from .web_research import run_deep_web_research


def _device(spec: ModelToolSpec) -> str:
    return "cuda" if spec.device == "cuda" else "cpu"


def _mean_pool(hidden, mask):
    import torch

    expanded = mask.unsqueeze(-1).expand(hidden.size()).float()
    return torch.sum(hidden * expanded, dim=1) / torch.clamp(expanded.sum(dim=1), min=1e-9)


def _embedding_rank(spec: ModelToolSpec, snapshot: Path, payload: dict[str, Any]) -> dict[str, Any]:
    import torch
    import torch.nn.functional as functional
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
    )
    model = AutoModel.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=None,
    ).to(_device(spec))
    model.eval()
    documents = list(payload["documents"])
    texts = [str(payload["query"])] + [str(item["text"]) for item in documents]
    encoded = tokenizer(texts, padding=True, truncation=True, return_tensors="pt")
    encoded = {key: value.to(_device(spec)) for key, value in encoded.items()}
    with torch.inference_mode():
        hidden = model(**encoded).last_hidden_state
        vectors = functional.normalize(_mean_pool(hidden, encoded["attention_mask"]), p=2, dim=1)
    scores = torch.matmul(vectors[1:], vectors[0]).detach().cpu().tolist()
    ranked = sorted(
        ({"document_id": str(item["document_id"]), "score": float(score)} for item, score in zip(documents, scores)),
        key=lambda row: row["score"],
        reverse=True,
    )
    return {"ranked": ranked}


def _bge_m3_rank(spec: ModelToolSpec, snapshot: Path, payload: dict[str, Any]) -> dict[str, Any]:
    import torch
    import torch.nn.functional as functional
    from transformers import AutoModel, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=None,
    ).to(_device(spec))
    model.eval()
    documents = list(payload["documents"])
    texts = [str(payload["query"])] + [str(item["text"]) for item in documents]
    encoded = tokenizer(texts, padding=True, truncation=True, max_length=8192, return_tensors="pt")
    encoded = {key: value.to(_device(spec)) for key, value in encoded.items()}
    with torch.inference_mode():
        hidden = model(**encoded).last_hidden_state
        vectors = functional.normalize(hidden[:, 0], p=2, dim=1)
    scores = torch.matmul(vectors[1:], vectors[0]).detach().cpu().tolist()
    ranked = sorted(
        ({"document_id": str(item["document_id"]), "score": float(score)} for item, score in zip(documents, scores)),
        key=lambda row: row["score"],
        reverse=True,
    )
    top_k = int(payload.get("top_k") or len(ranked))
    return {"ranked": ranked[: max(0, top_k)]}


def _extract_structured_data(
    spec: ModelToolSpec,
    snapshot: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
    model = AutoModelForCausalLM.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        torch_dtype=torch.float32 if _device(spec) == "cpu" else torch.float16,
    ).to(_device(spec))
    model.eval()
    requested_schema = payload["json_schema"]
    schema = json.dumps(
        _extraction_template(requested_schema), ensure_ascii=False, indent=2, sort_keys=True
    )
    prompt = (
        "<|input|>\n### Template:\n"
        + schema
        + "\n### Text:\n"
        + str(payload["text"])
        + "\n\n<|output|>"
    )
    encoded = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=20_000)
    encoded = {key: value.to(_device(spec)) for key, value in encoded.items()}
    with torch.inference_mode():
        generated = model.generate(
            **encoded,
            do_sample=False,
            temperature=None,
            top_p=None,
            max_new_tokens=2048,
            pad_token_id=tokenizer.eos_token_id,
        )
    decoded = tokenizer.decode(generated[0], skip_special_tokens=True)
    raw = decoded.split("<|output|>", 1)[-1].strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError("structured_extraction_non_object")
    if isinstance(requested_schema, dict) and requested_schema.get("type"):
        valid, error = validate_schema_value(data, requested_schema)
        if not valid:
            raise RuntimeError(f"structured_extraction_schema_invalid:{error}")
    return {"data": data}


def _extraction_template(schema: Any) -> Any:
    if not isinstance(schema, dict) or "type" not in schema:
        return schema
    kind = schema.get("type")
    if kind == "object":
        return {
            str(key): _extraction_template(value)
            for key, value in dict(schema.get("properties") or {}).items()
        }
    if kind == "array":
        item = schema.get("items")
        return [_extraction_template(item)] if isinstance(item, dict) else []
    if kind == "boolean":
        return False
    if kind in {"integer", "number"}:
        return None
    return ""


def _document_images(path: Path, max_pages: int) -> list[Any]:
    from PIL import Image

    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            return [pdf[index].render(scale=1.5).to_pil().convert("RGB") for index in range(min(len(pdf), max_pages))]
        finally:
            pdf.close()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
        with Image.open(path) as image:
            return [image.convert("RGB").copy()]
    raise RuntimeError("unsupported_document_type")


def _document_to_markdown(
    spec: ModelToolSpec,
    snapshot: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if spec.backend == "smoldocling_onnx_uint8":
        return _document_to_markdown_onnx(spec, snapshot, payload)

    import torch
    from docling_core.types.doc import DoclingDocument
    from docling_core.types.doc.document import DocTagsDocument
    from transformers import AutoModelForVision2Seq, AutoProcessor

    path = Path(str(payload["file_path"])).expanduser().resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("document_path_invalid")
    max_pages = max(1, min(int(payload.get("max_pages") or 20), 20))
    images = _document_images(path, max_pages)
    processor = AutoProcessor.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
    dtype = torch.float32 if _device(spec) == "cpu" else torch.float16
    model = AutoModelForVision2Seq.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=True,
        torch_dtype=dtype,
        _attn_implementation="eager",
    ).to(_device(spec))
    model.eval()
    messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": "Convert this page to docling."}]}]
    prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
    markdown_pages: list[str] = []
    layout: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        inputs = processor(text=prompt, images=[image], return_tensors="pt")
        inputs = {key: value.to(_device(spec)) for key, value in inputs.items()}
        with torch.inference_mode():
            generated = model.generate(**inputs, do_sample=False, max_new_tokens=2048)
        trimmed = generated[:, inputs["input_ids"].shape[1] :]
        doctags = processor.batch_decode(trimmed, skip_special_tokens=False)[0].lstrip()
        doctags_doc = DocTagsDocument.from_doctags_and_image_pairs([doctags], [image])
        document = DoclingDocument.load_from_doctags(doctags_doc, document_name=f"{path.stem}-{index}")
        markdown_pages.append(document.export_to_markdown())
        layout.append({"page": index, "doctags": doctags})
    return {
        "markdown": "\n\n".join(markdown_pages),
        "layout": layout,
        "page_count": len(images),
        "source_path": str(path),
    }


def _document_to_markdown_onnx(
    spec: ModelToolSpec,
    snapshot: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    import numpy as np
    import onnxruntime
    from docling_core.types.doc import DoclingDocument
    from docling_core.types.doc.document import DocTagsDocument
    from transformers import AutoConfig, AutoProcessor

    path = Path(str(payload["file_path"])).expanduser().resolve(strict=True)
    max_pages = max(1, min(int(payload.get("max_pages") or 20), 20))
    images = _document_images(path, max_pages)
    config = AutoConfig.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
    processor = AutoProcessor.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
    providers = ["CPUExecutionProvider"]
    model_dir = snapshot / "onnx"
    vision_session = onnxruntime.InferenceSession(
        str(model_dir / "vision_encoder_uint8.onnx"), providers=providers
    )
    embed_session = onnxruntime.InferenceSession(
        str(model_dir / "embed_tokens_uint8.onnx"), providers=providers
    )
    decoder_session = onnxruntime.InferenceSession(
        str(model_dir / "decoder_model_merged_uint8.onnx"), providers=providers
    )
    text_config = config.text_config
    num_key_value_heads = int(text_config.num_key_value_heads)
    head_dim = int(text_config.head_dim)
    num_hidden_layers = int(text_config.num_hidden_layers)
    eos_ids = text_config.eos_token_id
    stop_ids = {int(value) for value in (eos_ids if isinstance(eos_ids, list) else [eos_ids])}
    stop_ids.add(int(processor.tokenizer.convert_tokens_to_ids("<end_of_utterance>")))
    image_token_id = int(config.image_token_id)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": "Convert this page to docling."},
            ],
        }
    ]
    prompt = processor.apply_chat_template(messages, add_generation_prompt=True)
    markdown_pages: list[str] = []
    layout: list[dict[str, Any]] = []
    for index, image in enumerate(images, start=1):
        inputs = processor(text=prompt, images=[image], return_tensors="np")
        batch_size = inputs["input_ids"].shape[0]
        past_key_values = {
            f"past_key_values.{layer}.{kind}": np.zeros(
                [batch_size, num_key_value_heads, 0, head_dim], dtype=np.float32
            )
            for layer in range(num_hidden_layers)
            for kind in ("key", "value")
        }
        input_ids = inputs["input_ids"]
        attention_mask = inputs["attention_mask"]
        position_ids = np.cumsum(attention_mask, axis=-1)
        image_features = None
        generated_tokens: list[int] = []
        for _ in range(2048):
            inputs_embeds = embed_session.run(None, {"input_ids": input_ids})[0]
            if image_features is None:
                image_features = vision_session.run(
                    ["image_features"],
                    {
                        "pixel_values": inputs["pixel_values"],
                        "pixel_attention_mask": inputs["pixel_attention_mask"].astype(np.bool_),
                    },
                )[0]
                inputs_embeds[inputs["input_ids"] == image_token_id] = image_features.reshape(
                    -1, image_features.shape[-1]
                )
            logits, *present_key_values = decoder_session.run(
                None,
                {
                    "inputs_embeds": inputs_embeds,
                    "attention_mask": attention_mask,
                    "position_ids": position_ids,
                    **past_key_values,
                },
            )
            input_ids = logits[:, -1].argmax(-1, keepdims=True).astype(np.int64)
            token_id = int(input_ids[0, 0])
            generated_tokens.append(token_id)
            attention_mask = np.ones_like(input_ids)
            position_ids = position_ids[:, -1:] + 1
            for offset, key in enumerate(past_key_values):
                past_key_values[key] = present_key_values[offset]
            if token_id in stop_ids:
                break
        doctags = processor.batch_decode(
            np.asarray([generated_tokens], dtype=np.int64), skip_special_tokens=False
        )[0].lstrip()
        doctags_doc = DocTagsDocument.from_doctags_and_image_pairs([doctags], [image])
        document = DoclingDocument.load_from_doctags(
            doctags_doc, document_name=f"{path.stem}-{index}"
        )
        markdown_pages.append(document.export_to_markdown())
        layout.append({"page": index, "doctags": doctags})
    return {
        "markdown": "\n\n".join(markdown_pages),
        "layout": layout,
        "page_count": len(images),
        "source_path": str(path),
    }


def _rerank(spec: ModelToolSpec, snapshot: Path, payload: dict[str, Any]) -> dict[str, Any]:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
    model = AutoModelForSequenceClassification.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=None,
    ).to(_device(spec))
    model.eval()
    documents = list(payload["documents"])
    pairs = [[str(payload["query"]), str(item["text"])] for item in documents]
    encoded = tokenizer(pairs, padding=True, truncation=True, return_tensors="pt")
    encoded = {key: value.to(_device(spec)) for key, value in encoded.items()}
    with torch.inference_mode():
        logits = model(**encoded).logits
    values = logits.reshape(-1).detach().cpu().tolist()
    ranked = sorted(
        ({"document_id": str(item["document_id"]), "score": float(score)} for item, score in zip(documents, values)),
        key=lambda row: row["score"],
        reverse=True,
    )
    return {"ranked": ranked}


def _nli(spec: ModelToolSpec, snapshot: Path, payload: dict[str, Any]) -> dict[str, Any]:
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True, trust_remote_code=False)
    model = AutoModelForSequenceClassification.from_pretrained(
        snapshot,
        local_files_only=True,
        trust_remote_code=False,
        use_safetensors=None,
    ).to(_device(spec))
    model.eval()
    encoded = tokenizer(
        str(payload["premise"]),
        str(payload["hypothesis"]),
        truncation=True,
        return_tensors="pt",
    )
    encoded = {key: value.to(_device(spec)) for key, value in encoded.items()}
    with torch.inference_mode():
        probabilities = torch.softmax(model(**encoded).logits[0], dim=-1).detach().cpu().tolist()
    raw_labels = {int(index): str(label).casefold() for index, label in model.config.id2label.items()}
    scores = {raw_labels.get(index, f"label_{index}"): float(score) for index, score in enumerate(probabilities)}
    label = max(scores, key=scores.get)
    return {"label": label, "scores": scores, "consultative": True}


def _entities(spec: ModelToolSpec, snapshot: Path, payload: dict[str, Any]) -> dict[str, Any]:
    from gliner import GLiNER

    model = GLiNER.from_pretrained(str(snapshot), local_files_only=True)
    entities = model.predict_entities(str(payload["text"]), list(payload["labels"]))
    return {
        "entities": [
            {
                "text": str(item.get("text") or ""),
                "label": str(item.get("label") or ""),
                "start": int(item.get("start", 0)),
                "end": int(item.get("end", 0)),
                "score": float(item.get("score", 0.0)),
            }
            for item in entities
        ]
    }


def execute(spec: ModelToolSpec, snapshot: Path, payload: dict[str, Any]) -> dict[str, Any]:
    if spec.trust_remote_code:
        raise RuntimeError("sandboxed_remote_code_required")
    if not spec.local_files_only:
        raise RuntimeError("network_model_loading_forbidden")
    if spec.capability == "document_to_markdown":
        return _document_to_markdown(spec, snapshot, payload)
    if spec.capability == "extract_structured_data":
        return _extract_structured_data(spec, snapshot, payload)
    if spec.backend == "bge_m3_embedding":
        return _bge_m3_rank(spec, snapshot, payload)
    if spec.capability in {"semantic_search", "retrieve_code_context"}:
        return _embedding_rank(spec, snapshot, payload)
    if spec.capability == "rerank_documents":
        return _rerank(spec, snapshot, payload)
    if spec.capability == "verify_claim_support":
        return _nli(spec, snapshot, payload)
    if spec.capability == "extract_entities":
        return _entities(spec, snapshot, payload)
    if spec.capability == "sandboxed_remote_code" and spec.backend == "bubblewrap_systemd":
        return run_sandboxed_remote_code(payload)
    if spec.capability == "deep_web_research" and spec.backend == "agentcpm_llama_cpp":
        return run_deep_web_research(snapshot, payload)
    raise RuntimeError("unsupported_model_tool_capability")


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        spec = ModelToolSpec.model_validate(request["spec"])
        snapshot = Path(request["snapshot"]).resolve()
        payload = request["input"]
        if not snapshot.is_dir():
            raise RuntimeError("snapshot_missing")
        valid, error = validate_schema_value(payload, spec.input_schema)
        if not valid:
            raise RuntimeError(f"invalid_input_schema:{error}")
        output = execute(spec, snapshot, payload)
        valid, error = validate_schema_value(output, spec.output_schema)
        if not valid:
            raise RuntimeError(f"invalid_output_schema:{error}")
        sys.stdout.write(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}:{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
