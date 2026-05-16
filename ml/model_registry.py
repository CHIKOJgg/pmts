"""ml/model_registry.py — Model versioning and deployment management."""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ModelVersion:
    version_id: str
    created_at: int
    metrics: Dict[str, float]
    config: Dict[str, Any]
    feature_names: List[str]
    status: str = "staging"  # staging, production, archived
    description: str = ""


class ModelRegistry:
    """Tracks model versions, metrics, and deployment status.

    Supports promoting models from staging to production,
    and archiving old versions.
    """

    def __init__(self, registry_dir: str = "ml_registry") -> None:
        self._registry_dir = registry_dir
        os.makedirs(registry_dir, exist_ok=True)
        self._models: Dict[str, ModelVersion] = {}
        self._active_version: Optional[str] = None
        self._load_registry()

    def register(
        self,
        version_id: str,
        metrics: Dict[str, float],
        config: Dict[str, Any],
        feature_names: List[str],
        description: str = "",
    ) -> ModelVersion:
        model = ModelVersion(
            version_id=version_id,
            created_at=int(time.time() * 1000),
            metrics=metrics,
            config=config,
            feature_names=feature_names,
            status="staging",
            description=description,
        )
        self._models[version_id] = model
        self._save_registry()
        logger.info("Registered model version: %s", version_id)
        return model

    def promote(self, version_id: str) -> None:
        if version_id not in self._models:
            raise ValueError(f"Unknown version: {version_id}")

        for v in self._models.values():
            if v.status == "production":
                v.status = "archived"

        self._models[version_id].status = "production"
        self._active_version = version_id
        self._save_registry()
        logger.info("Promoted model version to production: %s", version_id)

    def archive(self, version_id: str) -> None:
        if version_id not in self._models:
            raise ValueError(f"Unknown version: {version_id}")
        self._models[version_id].status = "archived"
        if self._active_version == version_id:
            self._active_version = None
        self._save_registry()
        logger.info("Archived model version: %s", version_id)

    def get_active(self) -> Optional[ModelVersion]:
        if self._active_version and self._active_version in self._models:
            return self._models[self._active_version]
        return None

    def get_version(self, version_id: str) -> Optional[ModelVersion]:
        return self._models.get(version_id)

    def list_versions(self, status: Optional[str] = None) -> List[ModelVersion]:
        versions = list(self._models.values())
        if status:
            versions = [v for v in versions if v.status == status]
        versions.sort(key=lambda v: v.created_at, reverse=True)
        return versions

    def get_best_version(self, metric: str = "f1_score") -> Optional[ModelVersion]:
        versions = [v for v in self._models.values() if v.status != "archived"]
        if not versions:
            return None
        return max(versions, key=lambda v: v.metrics.get(metric, 0.0))

    def _save_registry(self) -> None:
        filepath = os.path.join(self._registry_dir, "registry.json")
        data = {
            "active_version": self._active_version,
            "models": {vid: asdict(v) for vid, v in self._models.items()},
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

    def _load_registry(self) -> None:
        filepath = os.path.join(self._registry_dir, "registry.json")
        if not os.path.exists(filepath):
            return
        try:
            with open(filepath, "r") as f:
                data = json.load(f)
            self._active_version = data.get("active_version")
            for vid, vdata in data.get("models", {}).items():
                self._models[vid] = ModelVersion(**vdata)
        except Exception as e:
            logger.error("Failed to load model registry: %s", e)
