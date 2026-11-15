"""Provider-independent labeling boundary (no SDK imports, no network).

Only the deterministic ``FakeProvider`` is implemented. It proves orchestration,
chunking, validation and accounting; it says nothing about live model accuracy.

Environment configuration (read by ``provider_from_env``)
  FOIA_BIAS_LLM_PROVIDER        "disabled" (default) | "fake" | "openai" | "anthropic"
  FOIA_BIAS_LLM_FAKE_ALLOWED    must be "1" to build the fake (tests/fixtures only)
  FOIA_BIAS_LLM_MODEL           model identifier for a live provider
  OPENAI_API_KEY / ANTHROPIC_API_KEY   provider credential (never committed)
  FOIA_BIAS_LLM_MAX_CALLS_PER_RUN      required positive call ceiling for live runs
Live adapters are intentionally not implemented in this build: selecting one
raises ``ProviderNotImplemented`` that names the missing pieces, so no paid
call can happen by accident.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


class ProviderError(RuntimeError):
    pass


class ProviderNotConfigured(ProviderError):
    pass


class ProviderNotImplemented(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    input_tokens: int
    output_tokens: int
    refusal: bool = False


@dataclass
class LabelProvider:
    """Interface: ``complete(system, user)`` returns a ProviderResponse or raises."""
    name: str = "abstract"
    model: str = "none"
    json_output: bool = False
    max_input_tokens: int = 0
    live: bool = False

    def complete(self, system: str, user: str) -> ProviderResponse:  # pragma: no cover - interface
        raise NotImplementedError


SIGNAL = re.compile(r"\[\[signal party=(D|R) wrongdoing=([0-9.]+) favorability=(-?[0-9.]+)\]\]")
FAKE_MODE = re.compile(r"\[\[fake (malformed|refusal|out_of_range|conflict|error)\]\]")


@dataclass
class FakeProvider(LabelProvider):
    """Deterministic labeler driven by synthetic ``[[signal ...]]`` markers.

    ``bias`` lets QA tests build a deliberately asymmetric labeler.
    """
    name: str = "fake"
    model: str = "fake-deterministic-v1"
    json_output: bool = True
    max_input_tokens: int = 4000
    live: bool = False
    bias: dict = field(default_factory=dict)
    calls: int = 0

    def complete(self, system, user):
        self.calls += 1
        document = user.split("<<<DOCUMENT", 1)[-1]
        tokens_in = (len(system) + len(user)) // 4 + 1
        mode = FAKE_MODE.search(document)
        if mode and mode.group(1) == "error":
            raise ProviderError("synthetic transient provider error")
        if mode and mode.group(1) == "refusal":
            return ProviderResponse("", tokens_in, 0, refusal=True)
        if mode and mode.group(1) == "malformed":
            return ProviderResponse("{not json", tokens_in, 3)
        wrong = {"D": 0.0, "R": 0.0}
        fav = {"D": 0.0, "R": 0.0}
        targets = []
        for party, w, f in SIGNAL.findall(document):
            wrong[party] = max(wrong[party], float(w))
            fav[party] = float(f)
            targets.append({"name": f"synthetic {party} actor", "party": party, "role": "synthetic"})
        for party, shift in self.bias.items():
            wrong[party] = min(1.0, wrong[party] + shift)
        if mode and mode.group(1) == "out_of_range":
            wrong["D"] = 1.7
        if mode and mode.group(1) == "conflict":
            targets.append({"name": "synthetic D actor", "party": "R", "role": "synthetic"})
        relevance = "high" if targets else "none"
        payload = {"political_relevance": relevance, "main_partisan_targets": targets,
                   "wrongdoing_by_party": wrong, "favorability_by_party": fav,
                   "evidence": [m.group(0) for m in SIGNAL.finditer(document)][:5],
                   "notes": "deterministic fake provider"}
        text = json.dumps(payload, sort_keys=True)
        return ProviderResponse(text, tokens_in, len(text) // 4 + 1)


def provider_from_env(env=None) -> LabelProvider | None:
    env = os.environ if env is None else env
    name = env.get("FOIA_BIAS_LLM_PROVIDER", "disabled")
    if name == "disabled":
        return None
    if name == "fake":
        if env.get("FOIA_BIAS_LLM_FAKE_ALLOWED") != "1":
            raise ProviderNotConfigured("fake provider requires FOIA_BIAS_LLM_FAKE_ALLOWED=1")
        return FakeProvider()
    if name in ("openai", "anthropic"):
        key = "OPENAI_API_KEY" if name == "openai" else "ANTHROPIC_API_KEY"
        missing = [v for v in (key, "FOIA_BIAS_LLM_MODEL", "FOIA_BIAS_LLM_MAX_CALLS_PER_RUN") if not env.get(v)]
        raise ProviderNotImplemented(
            f"live provider {name!r} adapter is not implemented in this build"
            + (f"; also missing {', '.join(missing)}" if missing else "")
            + ". Live labeling requires an authorized budget and a reviewed adapter.")
    raise ProviderNotConfigured(f"unknown provider {name!r}")
