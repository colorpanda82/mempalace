"""diary_ingest files under the canonical wing (fork, ws-migrate-wings follow-up).

A workspace flusher may still call ``ingest_diaries(wing="jarvis-ops")`` after
the palace was migrated to ``jarvis_ops``. Ingest must store the canonical
wing, keep skipping files it already ingested under the old spelling, and
replace (not duplicate) the drawers of a file that changed.
"""

import pytest

from mempalace import diary_ingest, wing_migrate
from mempalace.palace import get_collection

DAY = "2026-11-01"
ENTRY_1 = "## 09:00 backup\nThe backup job ran at three and the restore was verified on the mini."
ENTRY_2 = "## 14:00 drill\nThe restore drill ran against a scratch copy and passed without errors."
ENTRY_3 = "## 18:00 wrap\nNotes for tomorrow: rotate the offsite key and check the hive dashboard."


@pytest.fixture
def diary(tmp_path, monkeypatch):
    days = tmp_path / "daily"
    days.mkdir()
    path = days / f"{DAY}.md"
    path.write_text(f"# {DAY}\n\n{ENTRY_1}\n\n{ENTRY_2}\n", encoding="utf-8")
    palace = str(tmp_path / "palace")
    # 3.11 stored the wing as given: seed with the canonicalisation off.
    with monkeypatch.context() as m:
        m.setattr(diary_ingest, "mined_wing", lambda w: w, raising=False)
        diary_ingest.ingest_diaries(str(days), palace, wing="jarvis-ops")
    rows = _rows(palace, path)
    assert rows and {meta["wing"] for _i, _d, meta in rows} == {"jarvis-ops"}
    return days, path, palace


def _rows(palace, path):
    got = get_collection(palace).get(
        where={"source_file": str(path)}, include=["documents", "metadatas"]
    )
    return list(zip(got["ids"], got["documents"], got["metadatas"]))


@pytest.mark.parametrize("migrated", [True, False], ids=["migrated", "unmigrated"])
def test_ingest_of_a_changed_file_stores_the_canonical_wing(diary, migrated):
    days, path, palace = diary
    if migrated:
        wing_migrate.migrate_wings(palace, apply=True, explicit=True)
    path.write_text(f"# {DAY}\n\n{ENTRY_1}\n\n{ENTRY_2}\n\n{ENTRY_3}\n", encoding="utf-8")

    out = diary_ingest.ingest_diaries(str(days), palace, wing="jarvis-ops")

    assert out["days_updated"] == 1
    rows = _rows(palace, path)
    assert {meta["wing"] for _i, _d, meta in rows} == {"jarvis_ops"}
    # One drawer per entry: the old-spelling drawers were replaced.
    assert len(rows) == 3
    assert any("rotate the offsite key" in doc for _i, doc, _m in rows)


@pytest.mark.parametrize("migrated", [True, False], ids=["migrated", "unmigrated"])
def test_ingest_of_an_unchanged_file_is_still_skipped(diary, migrated):
    days, path, palace = diary
    if migrated:
        wing_migrate.migrate_wings(palace, apply=True, explicit=True)
    before = sorted(i for i, _d, _m in _rows(palace, path))

    out = diary_ingest.ingest_diaries(str(days), palace, wing="jarvis-ops")

    assert out["days_updated"] == 0
    assert sorted(i for i, _d, _m in _rows(palace, path)) == before
