import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_daily_prompt_updates_its_notepad_even_when_silent():
    prompt = (ROOT / "hermes" / "daily_prompt.txt.in").read_text(encoding="utf-8")
    assert "cron notepad {{JOB_ID}} set mushroom_state" in prompt
    assert "cron notepad mushroom-daily" not in prompt
    assert "даже если ответ будет `[SILENT]`" in prompt
    assert '"error_class"' in prompt and '"rules_version"' in prompt
    assert '"rules_version":"3"' in prompt
    assert "копируй из строк «вердикт сегодня»" in prompt
    assert "mushroom-weekend set" not in prompt


def test_weekend_prompt_uses_an_independent_notepad():
    prompt = (ROOT / "hermes" / "friday_prompt.txt.in").read_text(encoding="utf-8")
    assert "cron notepad {{JOB_ID}} set mushroom_state" in prompt
    assert "cron notepad mushroom-weekend" not in prompt
    assert "Не читай и не изменяй notepad ежедневного задания" in prompt
    assert '"rules_version":"3"' in prompt
    assert "Вердикты бери только из этих полей" in prompt
    assert "mushroom-daily set" not in prompt


def test_prompt_renderer_binds_job_ids_and_absolute_cli(tmp_path):
    cli = "/opt/hermes/bin/hermes"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "hermes" / "render_prompts.py"),
            "--daily-job-id",
            "daily123",
            "--weekend-job-id",
            "weekend456",
            "--hermes-cli",
            cli,
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
    )
    daily = (tmp_path / "daily_prompt.txt").read_text(encoding="utf-8")
    weekend = (tmp_path / "friday_prompt.txt").read_text(encoding="utf-8")
    assert f"{cli} --profile family cron notepad daily123" in daily
    assert f"{cli} --profile family cron notepad weekend456" in weekend
    assert "{{" not in daily and "{{" not in weekend


def test_prompt_renderer_rejects_shared_notepad_identity(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "hermes" / "render_prompts.py"),
            "--daily-job-id",
            "same",
            "--weekend-job-id",
            "same",
            "--hermes-cli",
            "/opt/hermes/bin/hermes",
            "--output-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not list(tmp_path.iterdir())


def test_hermes_contract_does_not_claim_exactly_once_delivery():
    docs = (ROOT / "hermes" / "PROMPT.md").read_text(encoding="utf-8")
    assert "delivery_outcome" in docs
    assert "Exactly-once не гарантируется" in docs
    assert "не синхронизирует его обратно в SQLite" in docs
