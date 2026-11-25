# 4.2E: exact-file model resolution

Base `702354136c07f21468bbe2c817b76765ac113c2e`; resolver `fb3bdd4`, local revision binding `2368cca`. Final tested implementation `d6f50248ed1fcae06a5d751f5cbf3b6b85150a0d`. See [discovery and sources](phase4_2C_runpod_discovery.md) (checked 2026-11-25) and [runtime report](phase4_2D_inference_runtime_report.md).

**No production manifest is supplied.** The expected model is Qwen3-4B-Instruct-2507 Q8_0. The reference notebook suggests a filename but does not establish a trusted HF repository, immutable revision, file digest or reproducible Kaggle binary. A reviewer must independently bind these values to the intended model before deployment. An operator-provided SHA is an integrity check, not an independent attestation of model authorship or quantization.

| Operator setting | Required value |
|---|---|
| `MODEL_SOURCE=cache` | Fixed mount `/runpod-volume/huggingface-cache/hub`; `MODEL_DIR` does not override it. |
| `MODEL_SOURCE=local` | `MODEL_DIR` is an absolute, operator-controlled read-only export directory for the approved repo. Stage exactly `MODEL_DIR/<revision>/<filename>`. No provider needed. |
| `MODEL_REPO`, `MODEL_REVISION` | Exact `org/repo`, full lowercase 40-hex commit, not a branch or tag. |
| `MODEL_FILENAME`, `MODEL_SHA256` | Exact basename ending in `Q8_0.gguf` (case-insensitive suffix, case-sensitive open); 64 lowercase hex characters. No paths or patterns. |

[Resolver and verification](../../inference_runtime/resolver.go) require a readable regular file, GGUF v3 magic/version and SHA256 over **all** bytes. The cached path is `models--ORG--REPO/snapshots/REVISION/FILENAME`. Open directory components relative to pinned descriptors without following symlinks. Accept only a regular snapshot file or the HF relative link `../../blobs/<expected SHA256>`; open that blob separately with no-follow. No glob, mutable ref, HF SDK, download, writes, alternate quant, or fallback. Pass the same verified descriptor to the child rather than reopening an arbitrary pathname. The mount root and ancestors must be trusted, and no other user/process may modify the mounted file contents in place; a pinned descriptor does not make mutable storage immutable. These are Linux/procfs semantics.

Hashing consumes one sequential file read on every process start. Its measured duration and the approved file identity are available in opt-in `model_verified` observations. The same 180 s startup budget includes verification, full-layer offload evidence and actual readiness. No checksum cache or unverified fast path. The resolver is not a full GGUF tensor parser: exact digest fixes the reviewed bytes, and llama.cpp must successfully load them.

**Local evidence:** [14 synthetic resolver cases](../../inference_runtime/resolver_test.go) cover both strategies, revision mismatch including local, exact selection in a multi-quant directory, safe HF link, unsafe link, linked snapshot, malformed header, missing/incompatible layout, wrong digest, traversal, wrong quant name and cancellation. Synthetic fixtures are not weights and are never proof of live cache delivery.

**Open gate:** the provider's Model field does not document guaranteed revision/file selection. If the approved snapshot is absent, record a failed startup and delete the test endpoint; do not broaden discovery or use another snapshot. A bounded alternative is an independently verified export mounted read-only with the local strategy. A private deployment can supply that mount; any paid RunPod storage, staging of weights or new network volume requires separate authorization, license review, size/time limit and cleanup plan.
