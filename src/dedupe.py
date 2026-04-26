"""Semantic duplicate detection for news summaries.

Different outlets often cover the same story (e.g. three articles all about
the same White House announcement). RSS-level dedupe only catches identical
URLs/titles, so we ask Claude once to cluster a candidate pool by story.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from anthropic import Anthropic, APIError

from .summarize import Summary

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """以下のニュース要約リストから、内容が「同じ出来事」を扱っている重複グループを特定してください。

【重複と判定する基準】
- 同じ出来事・事件・発表を別の角度や別ソースから書いた記事
- 主役となる人物・組織・場所・日時がほぼ一致し、伝えている事実の核が重なっている

【重複でない例】
- 異なる出来事 (別の事件、別の発表)
- 同じテーマ (経済、政治) でも対象が別の事象

【出力】
- 各記事には [N] のインデックスが振られています (1始まり)
- 重複しているインデックスのリストをグループとして返してください
- 重複が無い記事はグループに含めない (単独記事はグループ化しない)
- どのグループにも 2 件以上のインデックスを含めること

【例】
入力:
[1] 米トランプ大統領、新たな関税政策を発表 (NRC)
[2] ホワイトハウス、対中関税引き上げを表明 (Volkskrant)
[3] 米中関税合戦、再燃の様相 (NU.nl)
[4] アムステルダムの住宅着工が10%減 (Het Parool)
[5] EUの新たな移民政策案 (Trouw)

出力:
groups: [[1, 2, 3]]

→ 記事 [1][2][3] は同じ米国関税のニュースで重複。記事 [4][5] は別件なのでグループ化しない。

必ず submit_dedupe ツールで返してください。重複が一切なければ groups: [] を返してください。"""

TOOL = {
    "name": "submit_dedupe",
    "description": "重複している記事のインデックスグループを返す",
    "input_schema": {
        "type": "object",
        "properties": {
            "groups": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 2,
                },
                "description": "重複している記事の 1-based インデックスグループ",
            },
        },
        "required": ["groups"],
    },
}


@dataclass
class DedupeResult:
    groups: list[list[int]]  # 1-based indices
    kept: list[Summary]
    dropped: list[Summary]


def find_duplicate_groups(
    summaries: list[Summary],
    client: Anthropic,
    model: str,
) -> list[list[int]]:
    """Ask Claude to cluster duplicates. Returns 1-based index groups."""
    if len(summaries) < 2:
        return []

    user_content = "\n".join(
        f"[{i + 1}] {s.title_ja} ({s.source}) — {s.summary_ja}"
        for i, s in enumerate(summaries)
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[TOOL],
            tool_choice={"type": "tool", "name": "submit_dedupe"},
            messages=[{"role": "user", "content": user_content}],
        )
    except APIError as e:
        log.warning("Dedupe API call failed: %s — falling back to no dedupe", e)
        return []
    except Exception as e:
        log.warning("Dedupe failed unexpectedly: %s — falling back to no dedupe", e)
        return []

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_dedupe":
            raw = block.input.get("groups", []) or []
            cleaned: list[list[int]] = []
            for grp in raw:
                idxs = [int(i) for i in grp if isinstance(i, int) and 1 <= i <= len(summaries)]
                idxs = sorted(set(idxs))
                if len(idxs) >= 2:
                    cleaned.append(idxs)
            return cleaned

    log.warning("Dedupe response had no tool_use block — falling back to no dedupe")
    return []


def apply_dedupe(
    summaries: list[Summary],
    groups: list[list[int]],
) -> DedupeResult:
    """Drop duplicates, keeping the highest-importance entry from each group.

    Tie-breaker: earliest position in the input list (preserves stable ordering).
    """
    drop_indices: set[int] = set()
    for group in groups:
        # 1-based -> 0-based
        idxs = [i - 1 for i in group if 1 <= i <= len(summaries)]
        if len(idxs) < 2:
            continue
        # Keep the one with highest importance; earlier wins on tie
        best = idxs[0]
        for i in idxs[1:]:
            if summaries[i].importance > summaries[best].importance:
                best = i
        for i in idxs:
            if i != best:
                drop_indices.add(i)

    kept = [s for i, s in enumerate(summaries) if i not in drop_indices]
    dropped = [s for i, s in enumerate(summaries) if i in drop_indices]
    return DedupeResult(groups=groups, kept=kept, dropped=dropped)
