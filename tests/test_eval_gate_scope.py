"""The eval gate's trigger must cover everything that changes what the
agent does (D29b — agent SLOs and the CI eval gate).

This exists because the filter shipped without `skills/**` while the
skill body was being loaded straight into the system prompt: editing
the agent's investigative discipline changed its behavior and the gate
never ran. A control that silently stops covering an input is worse
than no control — the green check still appears.

So the inputs are discovered rather than listed. Any new file the
prompt builder reads, and any new module whose content is embedded in
the prompt, has to be inside the gate's paths or this fails.
"""

from fnmatch import fnmatch
from pathlib import Path

import pytest
import yaml

from resgraph.analyst import models, prompts
from resgraph.tools import registry

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/eval-gate.yml"

# Modules whose content reaches the model: the prompt text itself, the
# report schema serialized into the output contract, and the tool
# descriptions the registry supplies as tool blocks.
EMBEDDED_IN_THE_PROMPT = (prompts, models, registry)


def gate_globs() -> list[str]:
    doc = yaml.safe_load(WORKFLOW.read_text())
    # PyYAML reads the `on:` key as the boolean True
    trigger = doc.get("on", doc.get(True))
    return list(trigger["pull_request"]["paths"])


def covered(path: str, globs: list[str]) -> bool:
    for glob in globs:
        # a diff carries file paths only, so `x/**` means "under x/"
        if glob.endswith("/**"):
            if path.startswith(glob[:-2]):
                return True
        elif fnmatch(path, glob):
            return True
    return False


def prompt_input_files() -> set[Path]:
    """Every file that feeds the prompt, read off the modules so a new
    one cannot be added without this test noticing."""
    files: set[Path] = {Path(m.__file__) for m in EMBEDDED_IN_THE_PROMPT if m.__file__}
    for module in EMBEDDED_IN_THE_PROMPT:
        files |= {v for v in vars(module).values() if isinstance(v, Path)}
    return files


def test_the_gate_watches_every_input_to_the_prompt():
    globs = gate_globs()
    uncovered = []
    for path in sorted(prompt_input_files()):
        assert path.exists(), f"{path} is read by the prompt builder but does not exist"
        relative = path.resolve().relative_to(ROOT).as_posix()
        if not covered(relative, globs):
            uncovered.append(relative)
    assert not uncovered, (
        f"these change the agent's behavior but do not trigger the eval gate: {uncovered}. "
        f"Add a matching path to {WORKFLOW.name} — a change here ships unmeasured."
    )


def test_the_skill_body_is_one_of_those_inputs():
    """The specific case that was missed, pinned by name so a refactor
    moving the skill out of the prompt has to face this test."""
    relative = prompts.SKILL_PATH.resolve().relative_to(ROOT).as_posix()
    assert relative.startswith("skills/")
    assert covered(relative, gate_globs())


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("skills/change-forensics/SKILL.md", True),
        ("skills", False),
        ("skills-other/x.md", False),
        ("src/resgraph/analyst/prompts.py", True),
        ("src/resgraph/api/app.py", False),
    ],
)
def test_glob_matching(path, expected):
    """The matcher is the test's own load-bearing part: too permissive
    and it passes on a filter that would not fire."""
    assert covered(path, gate_globs()) is expected
