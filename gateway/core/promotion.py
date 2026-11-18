"""Migration promotion (tier 38): the deliberate act that turns a go
verdict into serving traffic.

Tiers 13/35/36 collected the migration evidence, tier 37 turned it
into a go/no-go recommendation. What was still missing was the ACT: an
operator reading "go" had to edit config.yaml by hand — drop the
``shadow:`` lines, pick a weight, align the priority tier, reload, and
hope no typo took the gateway down mid-edit. This module is that edit,
made deliberate and safe:

* **config.yaml stays the single routing truth.** Promotion does not
  write a DB overlay or a side state: it renders the exact line-level
  edits (``build_plan`` — a pure function over the file text + the
  running config) and applies them to the file itself, then the
  EXISTING hot-reload path picks the new routing up. The file, the
  reload and the audit trail all tell the same story.
* **Plan first, apply second.** ``POST /admin/migration/plan`` renders
  the edits (a structured ``changes`` list + a unified diff) with NO
  side effects; ``POST /admin/migration/promote`` re-builds the plan
  from the file as it is at apply time (never a stale plan), gates on
  the evidence verdict, backs the file up, writes, reloads — and
  restores the backup if the reload refuses.
* **Canary semantics, nothing auto-removed.** A shadow candidate flips
  to serving at the requested ``weight`` (1-100) inside the incumbent's
  priority tier — the same canary ladder a manual edit would climb
  (promote again at a higher weight to shift more traffic). An
  already-serving candidate is a plain weight rebalance. A candidate
  living on ANOTHER public model is inserted (a copy) into the target
  model's deployments; the original is never touched. The incumbent is
  never demoted or removed — promotion only ever adds serving capacity.
* **Text surgery, honestly bounded.** The surgery is line-level over
  config.yaml's canonical BLOCK form only (dash line + indented
  ``key: value`` lines): comments between entries are preserved
  byte-for-byte, flow-style entries (``{...}``, inline multi-key
  lines) are refused with a clear 409 rather than guessed at, and any
  disagreement between the running config and the file (a shadow
  deployment whose entry has no ``shadow:`` line) is a refusal, never a
  best-effort edit.
"""

from __future__ import annotations

import difflib
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..config import DeploymentConfig, GatewayConfig, ModelConfig

# The three promotion modes, decided by where the candidate lives in
# the RUNNING config (the parsed truth) — never guessed from the text.
MODE_FLIP = "shadow_flip"          # shadow twin of the target model → serving
MODE_REBALANCE = "rebalance"       # already serving on the model → new weight
MODE_INSERT = "cross_model_insert"  # lives on another model → copy inserted

# Canonical block-form indents (config.yaml's own shape):
#   models:            0
#     - name:         2   (model dash)
#       deployments:  4
#         - name:     6   (deployment dash)
#           key:      8   (deployment keys)
_MODEL_DASH_PAD = "  "
_DEPLOYMENTS_PAD = "    "
_ENTRY_DASH_PAD = "      "
_ENTRY_KEY_PAD = "        "

# A dash line's scalar: '- name: <value>' with optional quotes and an
# optional trailing comment. Commas are excluded so an inline
# multi-key line ('- name: a, provider: b') never matches — it takes
# the flow-style refusal path instead of being mis-read.
_DASH_NAME_RE = re.compile(r"^name:[ \t]+(?P<raw>[^,{}]+?)[ \t]*(?:#[^\n]*)?$")
# A deployment key line: exactly the key indent, a bare key, ':' —
# the value (rest) is validated for flow markers separately.
_ENTRY_KEY_RE = re.compile(r"^(?P<pad> +)(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<rest>.*)$")
# Flow-style markers in a value: braces, or ', key:' — an inline map.
_FLOW_VALUE_RE = re.compile(r"[{}]|,\s*[A-Za-z_][\w-]*\s*:")


class PromotionError(Exception):
    """A refusal that maps to an HTTP error — the message is the whole
    story (the OpenAI-style error envelope carries no extra payload)."""

    def __init__(self, status_code: int, message: str, code: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# structural scan — canonical block form only
# ---------------------------------------------------------------------------


def _scalar(raw: str) -> str:
    """Normalize a YAML scalar the way the parser sees it: strip the
    trailing comment (already done by the dash regex) and one layer of
    matching quotes."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _dash_name(line: str, pad: str) -> str | None:
    """The entry name on a canonical dash line, or None when the line
    is not a dash entry at that indent. Raises on flow style / a
    non-name-first dash line — those files must be edited by hand."""
    if not line.startswith(pad + "- "):
        return None
    body = line[len(pad) + 2 :]
    if _FLOW_VALUE_RE.search(body):
        raise PromotionError(
            409,
            f"config.yaml entry '{line.strip()}' is flow-style — promotion "
            "only edits canonical block-form entries (dash line + indented "
            "'key: value' lines). Convert the entry to block form, or edit "
            "it by hand.",
            "yaml_flow_style",
        )
    m = _DASH_NAME_RE.match(body)
    if m is None:
        raise PromotionError(
            409,
            f"config.yaml entry '{line.strip()}' does not start with its "
            "'name:' key — promotion only edits canonical block-form "
            "entries. Edit the file by hand.",
            "yaml_not_canonical",
        )
    return _scalar(m.group("raw"))


def _models_section(lines: list[str]) -> tuple[int, int]:
    """Line range (exclusive end) of the top-level ``models:`` list."""
    for i, line in enumerate(lines):
        m = re.match(r"^models:(?P<rest>.*)$", line)
        if m is None:
            continue
        if _FLOW_VALUE_RE.search(m.group("rest")) or m.group("rest").strip():
            raise PromotionError(
                409,
                "config.yaml's 'models:' is not a block-form list — "
                "promotion only edits canonical block form. Edit the "
                "file by hand.",
                "yaml_flow_style",
            )
        end = len(lines)
        for j in range(i + 1, len(lines)):
            if re.match(r"^\S", lines[j]):  # next top-level key
                end = j
                break
        return i + 1, end
    raise PromotionError(
        409,
        "No 'models:' section found in config.yaml — promotion edits the "
        "models' deployment entries, so the file must carry them.",
        "yaml_not_found",
    )


def _model_block(lines: list[str], model_name: str) -> tuple[int, int]:
    """The one model's entry range inside the models: section."""
    start, end = _models_section(lines)
    for i in range(start, end):
        name = _dash_name(lines[i], _MODEL_DASH_PAD)
        if name is None or name != model_name:
            continue
        block_end = end
        for j in range(i + 1, end):
            if re.match(r"^(  - |\S)", lines[j]):
                block_end = j
                break
        return i, block_end
    raise PromotionError(
        409,
        f"Model '{model_name}' not found in config.yaml's 'models:' block "
        "(canonical block form, '  - name: …' entries). If the model is "
        "configured another way, promotion cannot edit it.",
        "yaml_not_found",
    )


def _deployments_key(lines: list[str], block: tuple[int, int]) -> int:
    """The model's ``deployments:`` key line."""
    for i in range(block[0], block[1]):
        m = re.match(r"^    deployments:(?P<rest>.*)$", lines[i])
        if m is None:
            continue
        if m.group("rest").strip():
            raise PromotionError(
                409,
                "config.yaml's 'deployments:' is flow-style (e.g. "
                "'deployments: []') — promotion only edits block-form "
                "lists. Edit the file by hand.",
                "yaml_flow_style",
            )
        return i
    raise PromotionError(
        409,
        "The model's 'deployments:' block was not found in config.yaml — "
        "promotion edits deployment entries, so the block must exist.",
        "yaml_not_found",
    )


def _entry_keys(
    lines: list[str], dash_idx: int, block_end: int
) -> tuple[int, dict[str, tuple[int, str]]]:
    """The entry's end line (exclusive) and its ``key → (line index,
    raw value)`` map. The key run is the dash line plus consecutive
    key lines — a blank line, a comment or the next entry ends it, and
    anything flow-style inside refuses."""
    keys: dict[str, tuple[int, str]] = {}
    end = dash_idx + 1
    for j in range(dash_idx + 1, block_end):
        line = lines[j]
        if not line.strip():
            break
        m = _ENTRY_KEY_RE.match(line)
        if m is None or len(m.group("pad")) != len(_ENTRY_KEY_PAD):
            break
        rest = m.group("rest")
        if _FLOW_VALUE_RE.search(rest):
            raise PromotionError(
                409,
                f"config.yaml line '{line.strip()}' is flow-style — "
                "promotion only edits canonical block-form entries. "
                "Edit the file by hand.",
                "yaml_flow_style",
            )
        keys[m.group("key")] = (j, _scalar(rest))
        end = j + 1
    return end, keys


def _find_entry(
    lines: list[str], deployments_idx: int, block_end: int, name: str
) -> tuple[int, int, dict[str, tuple[int, str]]]:
    """The candidate's entry (dash line + keys) inside the model's
    deployments list: ``(dash index, entry end exclusive, keys)``."""
    for i in range(deployments_idx + 1, block_end):
        if _dash_name(lines[i], _ENTRY_DASH_PAD) != name:
            continue
        entry_end, keys = _entry_keys(lines, i, block_end)
        return i, entry_end, keys
    raise PromotionError(
        409,
        f"Deployment '{name}' not found in config.yaml's deployments "
        "block in canonical block form ('      - name: …' + indented "
        "key lines). If the entry is commented out or written another "
        "way, promotion cannot edit it.",
        "yaml_not_found",
    )


def _last_entry_end(lines: list[str], deployments_idx: int, block_end: int) -> int:
    """The line index just past the LAST entry in the deployments list
    — where a cross-model insert lands. Comments and blank lines after
    the last entry stay after it."""
    end = deployments_idx + 1  # empty list: insert right after the key
    i = deployments_idx + 1
    while i < block_end:
        if lines[i].startswith(_ENTRY_DASH_PAD + "- "):
            _dash_name(lines[i], _ENTRY_DASH_PAD)  # flow-style check
            entry_end, _ = _entry_keys(lines, i, block_end)
            end = entry_end
            i = entry_end
        else:
            i += 1
    return end


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


def _serving_tier(model: ModelConfig) -> int | None:
    """The incumbent's priority tier: the best (lowest) priority among
    the model's serving deployments. None = the model has none — the
    candidate would become its only serving deployment."""
    return min(
        (d.priority for d in model.deployments if not d.shadow), default=None
    )


def _key_line(pad: str, key: str, value: object) -> str:
    return f"{pad}{key}: {value}"


def build_plan(
    text: str,
    config: GatewayConfig,
    model_name: str,
    candidate_name: str,
    weight: int,
) -> dict:
    """Render the exact config.yaml edits for promoting ``candidate_name``
    into ``model_name`` at ``weight`` — a pure function: no I/O, no
    side effects, the caller applies ``new_text``.

    The RUNNING config is the ground truth for WHAT the candidate is
    (shadow? which model owns it? current weight/priority); the file
    text is what gets edited. The two are cross-checked — any
    disagreement is a refusal, never a best-effort guess.

    Returns ``{model, deployment, mode, weight, priority, canary_share,
    changes, warnings, diff, new_text}``:
    * ``mode`` — shadow_flip / rebalance / cross_model_insert;
    * ``changes`` — structured edits (``{"action": "remove"|"set"|"add"
      |"insert_entry", "key", "from"?, "to"?}``) for the audit trail;
    * ``diff`` — a unified diff of the file (the human preview);
    * ``canary_share`` — the candidate's share of the serving tier's
      weight after the edit (the canary number an operator watches);
    * ``warnings`` — non-blocking notes (no incumbent to align to, a
      hold/insufficient verdict attached by the caller);
    * ``new_text`` — the edited file (byte-identical to ``text`` except
      for the rendered edits).
    """
    if not 1 <= weight <= 100:
        raise PromotionError(
            400,
            f"weight must be between 1 and 100 (got {weight}) — a canary "
            "share, not a percentage above it.",
            "invalid_weight",
        )

    model_cfg = next((m for m in config.models if m.name == model_name), None)
    if model_cfg is None:
        raise PromotionError(
            400,
            f"Unknown model '{model_name}' — promotion targets a "
            "configured public model.",
            "unknown_model",
        )
    candidate: DeploymentConfig | None = None
    candidate_owner: ModelConfig | None = None
    for m in config.models:
        for d in m.deployments:
            if d.name == candidate_name:
                candidate, candidate_owner = d, m
                break
        if candidate is not None:
            break
    if candidate is None or candidate_owner is None:
        raise PromotionError(
            400,
            f"Unknown deployment '{candidate_name}' — promotion promotes "
            "a configured deployment (the candidate the evidence is about).",
            "unknown_deployment",
        )

    if candidate_owner is model_cfg:
        mode = MODE_FLIP if candidate.shadow else MODE_REBALANCE
    else:
        mode = MODE_INSERT

    serving_tier = _serving_tier(model_cfg)
    warnings: list[str] = []
    # The tier the candidate will serve in: the incumbent's tier when
    # one exists, else the candidate's own priority (a model with no
    # serving deployment — the candidate becomes its only one).
    tier_priority = serving_tier if serving_tier is not None else candidate.priority
    if serving_tier is None:
        warnings.append(
            f"'{model_name}' has no serving (non-shadow) deployment — the "
            f"candidate becomes its only one (priority stays "
            f"{candidate.priority}; nothing to canary against)"
        )

    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    changes: list[dict] = []

    if mode == MODE_INSERT:
        block = _model_block(lines, model_name)
        deployments_idx = _deployments_key(lines, block)
        insert_at = _last_entry_end(lines, deployments_idx, block[1])
        entry = [
            f"{_ENTRY_DASH_PAD}- name: {candidate.name}",
            _key_line(_ENTRY_KEY_PAD, "provider", candidate.provider),
            _key_line(_ENTRY_KEY_PAD, "model", candidate.model),
        ]
        if candidate.residency is not None:
            entry.append(_key_line(_ENTRY_KEY_PAD, "residency", candidate.residency))
        if candidate.timeout_seconds is not None:
            entry.append(
                _key_line(_ENTRY_KEY_PAD, "timeout_seconds", candidate.timeout_seconds)
            )
        if candidate.max_retries is not None:
            entry.append(_key_line(_ENTRY_KEY_PAD, "max_retries", candidate.max_retries))
        entry.append(_key_line(_ENTRY_KEY_PAD, "priority", tier_priority))
        entry.append(_key_line(_ENTRY_KEY_PAD, "weight", weight))
        new_lines = lines[:insert_at] + entry + lines[insert_at:]
        changes.append(
            {
                "action": "insert_entry",
                "model": model_name,
                "deployment": candidate.name,
                "priority": tier_priority,
                "weight": weight,
            }
        )
    else:
        block = _model_block(lines, model_name)
        deployments_idx = _deployments_key(lines, block)
        dash_idx, entry_end, keys = _find_entry(
            lines, deployments_idx, block[1], candidate_name
        )
        removes: set[int] = set()
        sets: dict[int, str] = {}
        insert_block: list[str] = []

        if mode == MODE_FLIP:
            # The running config says shadow — the file must say so too,
            # or the two disagree and the edit would be a guess.
            if "shadow" not in keys:
                raise PromotionError(
                    409,
                    f"The running config has '{candidate_name}' as a shadow "
                    "deployment but its config.yaml entry carries no "
                    "'shadow:' line — the file and the running config "
                    "disagree. Edit the file by hand.",
                    "yaml_mismatch",
                )
            removes.add(keys["shadow"][0])
            changes.append(
                {"action": "remove", "key": "shadow", "value": keys["shadow"][1]}
            )
            if "shadow_fraction" in keys:
                removes.add(keys["shadow_fraction"][0])
                changes.append(
                    {
                        "action": "remove",
                        "key": "shadow_fraction",
                        "value": keys["shadow_fraction"][1],
                    }
                )

        # Align the priority tier (the incumbent's) — the canary ladder
        # lives INSIDE one tier; a flip that kept the candidate's own
        # priority would either shadow-serve or never serve.
        if serving_tier is not None and candidate.priority != serving_tier:
            line = _key_line(_ENTRY_KEY_PAD, "priority", serving_tier)
            if "priority" in keys:
                sets[keys["priority"][0]] = line
                changes.append(
                    {
                        "action": "set",
                        "key": "priority",
                        "from": candidate.priority,
                        "to": serving_tier,
                    }
                )
            else:
                insert_block.append(line)
                changes.append(
                    {"action": "add", "key": "priority", "to": serving_tier}
                )

        if "weight" in keys:
            if candidate.weight != weight:
                sets[keys["weight"][0]] = _key_line(_ENTRY_KEY_PAD, "weight", weight)
                changes.append(
                    {
                        "action": "set",
                        "key": "weight",
                        "from": candidate.weight,
                        "to": weight,
                    }
                )
        elif candidate.weight != weight:
            # No weight line in the file and the request differs from
            # the effective (default) weight — make the canary weight
            # explicit. Asking for what it already is adds noise: the
            # plan stays a genuine no-op.
            insert_block.append(_key_line(_ENTRY_KEY_PAD, "weight", weight))
            changes.append(
                {"action": "add", "key": "weight", "from": candidate.weight, "to": weight}
            )

        entry_lines = []
        for i in range(dash_idx, entry_end):
            if i in removes:
                continue
            entry_lines.append(sets[i] if i in sets else lines[i])
        entry_lines.extend(insert_block)
        new_lines = lines[:dash_idx] + entry_lines + lines[entry_end:]

    new_text = nl.join(new_lines)
    diff = "\n".join(
        difflib.unified_diff(
            lines,
            new_lines,
            fromfile="config.yaml (current)",
            tofile="config.yaml (after promotion)",
            lineterm="",
        )
    )

    # The canary share AFTER the edit: the candidate's new weight over
    # the tier's total (the incumbent's weights untouched + the
    # candidate's new weight). From the RUNNING config — the reload
    # makes the file agree with it.
    others = [
        d.weight
        for d in model_cfg.deployments
        if not d.shadow
        and d.priority == tier_priority
        and d.name != candidate_name
    ]
    tier_total = sum(others) + weight
    canary_share = round(weight / tier_total, 4) if tier_total else None

    return {
        "model": model_name,
        "deployment": candidate_name,
        "mode": mode,
        "weight": weight,
        "priority": tier_priority,
        "canary_share": canary_share,
        "changes": changes,
        "warnings": warnings,
        "diff": diff,
        "new_text": new_text,
    }


# ---------------------------------------------------------------------------
# apply: backup, write, restore
# ---------------------------------------------------------------------------


def read_config_text(path: Path) -> str:
    """Read the config file without newline translation, so CRLF files
    survive the surgery byte-for-byte."""
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def write_with_backup(path: Path, new_text: str) -> Path:
    """Back the file up (timestamped copy), then write the edited
    text. The backup is the safety net for the reload refusal path —
    and the operator's undo point."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    try:
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(new_text)
    except Exception:
        shutil.copy2(backup, path)  # best-effort: leave no half edit
        raise
    return backup


def restore_backup(backup: Path, path: Path) -> None:
    """Put the backup back — the reload-refusal path, so the file
    always matches the running config again."""
    shutil.copy2(backup, path)
