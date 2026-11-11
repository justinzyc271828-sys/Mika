"""OpenAI access with structured output, image input and honest cost accounting.

No model price is assumed.  A call is marked ``cost_known=False`` unless the
owner configured both input and output prices for that exact model.  This
prevents a plausible-looking but invented cost total.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from models.schemas import ApiUsage

from .config import SettingsManager
from .db import Database
from .logging_setup import get_logger
from .secrets import SecretStore

logger = get_logger("ai")

REDACTED = "***"


class AiError(RuntimeError):
    pass


class AiNotConfiguredError(AiError):
    pass


class AiAuthError(AiError):
    pass


class AiPermissionError(AiError):
    pass


class AiQuotaError(AiError):
    pass


class AiRateLimitError(AiError):
    pass


class AiResponseFormatError(AiError):
    pass


@dataclass
class AiResult:
    text: str
    data: dict[str, Any] | None
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = False
    latency_ms: int = 0
    request_id: str = ""
    raw_meta: dict[str, Any] = field(default_factory=dict)


def _image_data_url(path: Path, max_side: int = 1400) -> str:
    with Image.open(path) as image:
        image = image.convert("RGB")
        if max(image.size) > max_side:
            ratio = max_side / max(image.size)
            image = image.resize(
                (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        import io

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=82, optimize=True)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{payload}"


def extract_json(text: str) -> dict[str, Any]:
    """Parse model output that may be fenced or wrapped in prose."""

    if not text or not text.strip():
        raise AiResponseFormatError("模型返回为空")
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = [line for line in candidate.splitlines() if not line.strip().startswith("```")]
        candidate = "\n".join(lines).strip()
    try:
        payload = json.loads(candidate)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        snippet = candidate[start : end + 1]
        try:
            payload = json.loads(snippet)
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            pass
    raise AiResponseFormatError(f"无法从模型输出中解析 JSON：{candidate[:300]}")


class AiClient:
    def __init__(
        self,
        settings: SettingsManager,
        secrets: SecretStore,
        db: Database | None = None,
        *,
        client: Any | None = None,
    ):
        self.settings = settings
        self.secrets = secrets
        self.db = db
        self._client = client
        self._injected = client is not None

    # ---------------------------------------------------------------- connection
    def api_key(self) -> str:
        return (self.secrets.get("openai_api_key") or "").strip()

    def configured(self) -> tuple[bool, str]:
        if not self.api_key():
            return False, "未配置 OpenAI API Key"
        if not str(self.settings.get("openai.model", "")).strip():
            return False, "未配置主模型 ID"
        return True, ""

    def _openai(self) -> Any:
        if self._client is not None:
            return self._client
        if self._injected:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency error
            raise AiNotConfiguredError(f"未安装 openai 包：{exc}") from exc
        key = self.api_key()
        if not key:
            raise AiNotConfiguredError("未配置 OpenAI API Key")
        base_url = str(self.settings.get("openai.base_url", "")).strip() or None
        timeout = float(self.settings.get("openai.timeout_seconds", 240))
        self._client = OpenAI(
            api_key=key,
            base_url=base_url,
            timeout=timeout,
            max_retries=0,
        )
        return self._client

    def check_connection(self, model: str | None = None) -> dict[str, Any]:
        """Verify credentials and model visibility without generating tokens."""

        model = (model or str(self.settings.get("openai.model", ""))).strip()
        ok, reason = self.configured()
        if not ok:
            return {"ok": False, "reason": reason, "checked_at": time.time()}
        try:
            client = self._openai()
            models = client.models.list()
            ids = {item.id for item in getattr(models, "data", []) or []}
            if model and ids and model not in ids:
                return {
                    "ok": False,
                    "reason": f"当前 Key 未列出模型 {model}",
                    "models": sorted(ids)[:50],
                }
            return {"ok": True, "reason": "", "model": model, "models_count": len(ids)}
        except Exception as exc:
            return {"ok": False, "reason": self._classify(exc, raise_it=False), "checked_at": time.time()}

    # -------------------------------------------------------------------- calls
    def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        images: list[str] | None = None,
        purpose: str = "solve",
        product_id: str = "",
        exercise_id: str = "",
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        api_mode: str | None = None,
    ) -> AiResult:
        if not self.api_key() and not self._injected:
            raise AiNotConfiguredError("未配置 OpenAI API Key")
        if not model.strip():
            raise AiNotConfiguredError("未配置模型 ID")
        mode = (api_mode or str(self.settings.get("openai.api_mode", "responses"))).strip().lower()
        images = [path for path in (images or []) if path and Path(path).exists()]
        attempts = max(1, int(self.settings.get("openai.max_retries", 3)))
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                if mode == "responses":
                    return self._responses_call(
                        model=model,
                        system=system,
                        user=user,
                        schema_name=schema_name,
                        schema=schema,
                        images=images,
                        purpose=purpose,
                        product_id=product_id,
                        exercise_id=exercise_id,
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                    )
                return self._chat_call(
                    model=model,
                    system=system,
                    user=user,
                    schema_name=schema_name,
                    schema=schema,
                    images=images,
                    purpose=purpose,
                    product_id=product_id,
                    exercise_id=exercise_id,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                )
            except (AiAuthError, AiPermissionError, AiQuotaError, AiNotConfiguredError):
                raise
            except AiResponseFormatError:
                raise
            except Exception as exc:
                last_error = exc
                message = str(exc).lower()
                # A gateway that does not implement the Responses JSON-schema
                # field can still answer through Chat Completions.
                if mode == "responses" and any(
                    token in message
                    for token in ("json_schema", "text.format", "response_format", "unknown parameter", "unsupported")
                ):
                    mode = "chat"
                    continue
                classification = self._classify(exc, raise_it=False)
                if isinstance(classification, AiRateLimitError) and attempt + 1 < attempts:
                    time.sleep(min(30.0, 2.0 ** attempt))
                    continue
                if attempt + 1 < attempts and not isinstance(classification, AiError):
                    time.sleep(min(15.0, 1.5 ** attempt))
                    continue
                raise classification if isinstance(classification, AiError) else AiError(str(exc)) from exc
        raise AiError(str(last_error) if last_error else "AI 调用失败")

    def _responses_call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        images: list[str],
        purpose: str,
        product_id: str,
        exercise_id: str,
        max_output_tokens: int | None,
        temperature: float | None,
    ) -> AiResult:
        content: list[dict[str, Any]] = [{"type": "input_text", "text": user}]
        for path in images:
            content.append({"type": "input_image", "image_url": _image_data_url(Path(path))})
        started = time.perf_counter()
        response = self._openai().responses.create(
            model=model,
            input=[
                {"role": "system", "content": [{"type": "input_text", "text": system}]},
                {"role": "user", "content": content},
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "schema": schema,
                    "strict": True,
                }
            },
            max_output_tokens=int(
                max_output_tokens or self.settings.get("openai.max_output_tokens", 4500)
            ),
            temperature=(
                float(temperature)
                if temperature is not None
                else float(self.settings.get("openai.temperature", 0.2))
            ),
        )
        latency = int((time.perf_counter() - started) * 1000)
        text = getattr(response, "output_text", "") or ""
        data = extract_json(text)
        usage = getattr(response, "usage", None)
        tokens_in, tokens_out, cached = self._usage_fields(usage)
        result = self._finalise(
            text=text,
            data=data,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached=cached,
            latency=latency,
            request_id=str(getattr(response, "id", "") or ""),
            purpose=purpose,
            product_id=product_id,
            exercise_id=exercise_id,
        )
        return result

    def _chat_call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        images: list[str],
        purpose: str,
        product_id: str,
        exercise_id: str,
        max_output_tokens: int | None,
        temperature: float | None,
    ) -> AiResult:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for path in images:
            user_content.append(
                {"type": "image_url", "image_url": {"url": _image_data_url(Path(path))}}
            )
        started = time.perf_counter()
        try:
            response = self._openai().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content if images else user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
                max_tokens=int(
                    max_output_tokens or self.settings.get("openai.max_output_tokens", 4500)
                ),
                temperature=(
                    float(temperature)
                    if temperature is not None
                    else float(self.settings.get("openai.temperature", 0.2))
                ),
            )
        except Exception as exc:
            message = str(exc).lower()
            if "json_schema" not in message and "response_format" not in message:
                raise
            response = self._openai().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content if images else user},
                ],
                response_format={"type": "json_object"},
                max_tokens=int(
                    max_output_tokens or self.settings.get("openai.max_output_tokens", 4500)
                ),
                temperature=(
                    float(temperature)
                    if temperature is not None
                    else float(self.settings.get("openai.temperature", 0.2))
                ),
            )
        latency = int((time.perf_counter() - started) * 1000)
        choice = (getattr(response, "choices", None) or [None])[0]
        message = getattr(choice, "message", None)
        text = getattr(message, "content", "") or ""
        data = extract_json(text)
        tokens_in, tokens_out, cached = self._usage_fields(getattr(response, "usage", None))
        return self._finalise(
            text=text,
            data=data,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached=cached,
            latency=latency,
            request_id=str(getattr(response, "id", "") or ""),
            purpose=purpose,
            product_id=product_id,
            exercise_id=exercise_id,
        )

    def _finalise(
        self,
        *,
        text: str,
        data: dict[str, Any],
        model: str,
        tokens_in: int,
        tokens_out: int,
        cached: int,
        latency: int,
        request_id: str,
        purpose: str,
        product_id: str,
        exercise_id: str,
    ) -> AiResult:
        cost, known = self.estimate_cost(model, tokens_in, tokens_out)
        result = AiResult(
            text=text,
            data=data,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cached_tokens=cached,
            cost=cost,
            cost_known=known,
            latency_ms=latency,
            request_id=request_id,
        )
        if self.db is not None:
            self.db.add_usage(
                ApiUsage(
                    product_id=product_id,
                    exercise_id=exercise_id,
                    kind=purpose,
                    model=model,
                    input_tokens=tokens_in,
                    output_tokens=tokens_out,
                    cached_tokens=cached,
                    estimated_cost=cost,
                    cost_known=known,
                )
            )
        logger.info(
            "AI %s model=%s tokens=%s/%s cost=%s known=%s latency=%sms",
            purpose,
            model,
            tokens_in,
            tokens_out,
            cost,
            known,
            latency,
        )
        return result

    def estimate_cost(self, model: str, tokens_in: int, tokens_out: int) -> tuple[float, bool]:
        prices = self.settings.get("budget.prices", {}) or {}
        entry = prices.get(model) if isinstance(prices, dict) else None
        if not isinstance(entry, dict):
            return 0.0, False
        input_price = entry.get("input_per_million")
        output_price = entry.get("output_per_million")
        if input_price is None or output_price is None:
            return 0.0, False
        try:
            cost = (tokens_in / 1_000_000) * float(input_price) + (
                tokens_out / 1_000_000
            ) * float(output_price)
        except (TypeError, ValueError):
            return 0.0, False
        return round(cost, 6), True

    @staticmethod
    def _usage_fields(usage: Any) -> tuple[int, int, int]:
        if usage is None:
            return 0, 0, 0
        tokens_in = int(getattr(usage, "input_tokens", 0) or getattr(usage, "prompt_tokens", 0) or 0)
        tokens_out = int(
            getattr(usage, "output_tokens", 0) or getattr(usage, "completion_tokens", 0) or 0
        )
        details = getattr(usage, "input_tokens_details", None) or getattr(
            usage, "prompt_tokens_details", None
        )
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
        return tokens_in, tokens_out, cached

    @staticmethod
    def _classify(exc: Exception, *, raise_it: bool = True) -> Any:
        name = type(exc).__name__
        message = str(exc)
        lower = message.lower()
        if "insufficient_quota" in lower or "quota" in lower or "billing" in lower:
            mapped: Exception = AiQuotaError("API 余额不足或配额已用尽")
        elif name in {"AuthenticationError"} or "invalid api key" in lower or "401" in message:
            mapped = AiAuthError("API Key 无效或已过期")
        elif name in {"PermissionDeniedError"} or "permission" in lower or "403" in message:
            mapped = AiPermissionError("当前 API Key 无该模型访问权限")
        elif name in {"RateLimitError"} or "rate limit" in lower or "429" in message:
            mapped = AiRateLimitError("请求频率受限")
        elif name in {"APITimeoutError", "TimeoutError"} or "timeout" in lower:
            mapped = AiError("API 请求超时")
        elif name in {"APIConnectionError", "ConnectError"}:
            mapped = AiError("无法连接 API 服务")
        else:
            mapped = AiError(f"{name}: {message[:500]}")
        if raise_it:
            raise mapped from exc
        return mapped


class BudgetGuard:
    """Stop new calls when a configured budget would be exceeded.

    Unknown-cost calls never silently count as zero: when enforcement is on and
    prices are missing, the guard refuses to continue instead of pretending the
    budget is safe.
    """

    def __init__(self, settings: SettingsManager, db: Database):
        self.settings = settings
        self.db = db

    def check(self, product_id: str) -> tuple[bool, str, dict[str, Any]]:
        enforce = bool(self.settings.get("budget.enforce", False))
        product = self.db.usage_for_product(product_id)
        today = self.db.usage_today()
        if not enforce:
            return True, "", {"product": product, "today": today, "enforced": False}
        per_book = float(self.settings.get("budget.per_book", 0.0) or 0.0)
        daily = float(self.settings.get("budget.daily", 0.0) or 0.0)
        if per_book > 0:
            if int(product.get("unknown_cost") or 0) > 0:
                return False, "存在未配置单价的模型调用，预算无法可靠计算，已暂停。", {
                    "product": product,
                    "today": today,
                    "enforced": True,
                }
            if float(product.get("cost") or 0.0) >= per_book:
                return False, f"单本教材预算 {per_book:.2f} 已用完，已暂停。", {
                    "product": product,
                    "today": today,
                    "enforced": True,
                }
        if daily > 0:
            if int(today.get("unknown_cost") or 0) > 0:
                return False, "今日存在未配置单价的模型调用，预算无法可靠计算，已暂停。", {
                    "product": product,
                    "today": today,
                    "enforced": True,
                }
            if float(today.get("cost") or 0.0) >= daily:
                return False, f"今日预算 {daily:.2f} 已用完，已暂停。", {
                    "product": product,
                    "today": today,
                    "enforced": True,
                }
        return True, "", {"product": product, "today": today, "enforced": True}
