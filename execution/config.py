from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionConfig:
    min_latency_ms: int = 50
    max_latency_ms: int = 250
    participation_rate: float = 0.5
