"""The deployed Hermes files: thin prompts, two wrappers, one contract.

The prompts must stay dumb. Anything that looks like biology, state or a
decision rule inside them is a regression: the script owns all of that now
(``mushroom_alerts/report.py``).
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
HERMES = ROOT / "hermes"

PROMPTS = ("daily_prompt.txt", "friday_prompt.txt")

#: Vocabulary that only belongs in the script.
FORBIDDEN = (
    "notepad",
    "{{JOB_ID}}",
    "{{HERMES_CLI}}",
    "mushroom_state",
    "API30",
    "D+7",
    "эпизод",
    "порог",
    "пересчита",
    "предыдущего запуска",
)


def _prompt(name: str) -> str:
    return (HERMES / name).read_text(encoding="utf-8")


def test_prompts_are_short_and_only_format():
    for name in PROMPTS:
        text = _prompt(name)
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) <= 12, (name, len(lines))
        assert "ОТПРАВЛЯТЬ: нет" in text
        assert "`[SILENT]`" in text
        assert "дословно" in text


def test_prompts_carry_no_biology_state_or_decision_rules():
    for name in PROMPTS:
        text = _prompt(name)
        for word in FORBIDDEN:
            assert word not in text, (name, word)


def test_each_prompt_names_its_own_mode():
    assert "--mode daily" in _prompt("daily_prompt.txt")
    assert "--mode weekend" in _prompt("friday_prompt.txt")


def test_the_notepad_renderer_is_gone():
    assert not (HERMES / "render_prompts.py").exists()
    assert not (HERMES / "daily_prompt.txt.in").exists()
    assert not (HERMES / "friday_prompt.txt.in").exists()


def test_two_wrappers_run_the_two_modes():
    daily = (HERMES / "mushroom_brief.sh").read_text(encoding="utf-8")
    weekend = (HERMES / "mushroom_weekend.sh").read_text(encoding="utf-8")
    assert "brief --mode daily" in daily
    assert "brief --mode weekend" in weekend
    for text in (daily, weekend):
        assert 'export MUSHROOM_DB="$REPO/state.sqlite"' in text


def test_both_wrappers_ask_for_the_map_and_make_its_directory():
    for name in ("mushroom_brief.sh", "mushroom_weekend.sh"):
        text = (HERMES / name).read_text(encoding="utf-8")
        assert "REPO=/home/ihor.travkin/mushroom-alerts" in text
        assert 'MAPS="$REPO/maps"' in text
        assert 'mkdir -p "$MAPS"' in text
        assert '--map-dir "$MAPS"' in text
        # a missing maps directory must never stop the block from printing
        assert 'mkdir -p "$MAPS" || exit' not in text


def test_both_prompts_carry_the_media_line_through_untouched():
    for name in PROMPTS:
        text = _prompt(name)
        assert "MEDIA:" in text
        assert "дословно и без изменений" in text
        assert "отдельной строкой" in text
        # and it stays a formatting instruction: no path, no photo API
        assert "/home/" not in text and "sendPhoto" not in text


def test_the_contract_documents_the_map_as_an_attachment():
    docs = (HERMES / "PROMPT.md").read_text(encoding="utf-8")
    assert "--map-dir" in docs
    assert "MEDIA:" in docs
    assert "latest.png" in docs  # named as the thing we deliberately do not do
    assert "31" in docs
    assert "© OpenStreetMap contributors" in docs


def test_hermes_contract_documents_the_script_side_decision():
    docs = (HERMES / "PROMPT.md").read_text(encoding="utf-8")
    assert "delivery_outcome" in docs
    assert "Exactly-once не гарантируется" in docs
    assert "не синхронизирует его обратно в SQLite" in docs
    assert "`reports`" in docs
    assert "Continuity is no longer the agent's business" in docs
