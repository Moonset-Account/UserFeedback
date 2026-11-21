"""Local checkpoint shard recovery infrastructure.

Public surface:

* :class:`~shard_recovery.geometry.Rect`, :func:`~shard_recovery.geometry.cover_rect`
* :class:`~shard_recovery.shard.ShardFile`, :func:`~shard_recovery.shard.write_shard`,
  :class:`~shard_recovery.shard.ByteRange`
* :func:`~shard_recovery.checkpoint.build_checkpoint`,
  :func:`~shard_recovery.checkpoint.load_manifest`
* :func:`~shard_recovery.recovery.recover_rank`,
  :func:`~shard_recovery.recovery.publish_recovery`,
  :class:`~shard_recovery.recovery.RecoveryResult`,
  :class:`~shard_recovery.recovery.RecoveryError`
"""

from .checkpoint import (
    MANIFEST_FILENAME,
    ShardPlan,
    TensorSource,
    build_checkpoint,
    load_manifest,
)
from .errors import (
    ManifestError,
    RecoveryPublishError,
    ShardFormatError,
    ShardRecoveryError,
)
from .geometry import CoverReport, Rect, cover_rect
from .recovery import (
    RecoveryError,
    RecoveryResult,
    ShardAccess,
    TensorRecovery,
    publish_recovery,
    recover_rank,
)
from .shard import ByteRange, ShardFile, ShardHeader, write_shard

__version__ = "0.1.0"

__all__ = [
    "ByteRange",
    "CoverReport",
    "MANIFEST_FILENAME",
    "ManifestError",
    "Rect",
    "RecoveryError",
    "RecoveryPublishError",
    "RecoveryResult",
    "ShardAccess",
    "ShardFile",
    "ShardFormatError",
    "ShardHeader",
    "ShardPlan",
    "ShardRecoveryError",
    "TensorRecovery",
    "TensorSource",
    "build_checkpoint",
    "cover_rect",
    "load_manifest",
    "publish_recovery",
    "recover_rank",
    "write_shard",
    "__version__",
]
