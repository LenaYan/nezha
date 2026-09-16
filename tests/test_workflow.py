import pytest

from nezha.workflow import Workflow, WorkflowError, render_template, expand_env

MINIMAL = """---
tracker:
  kind: file
  provider:
    path: ./board.json
  active_states: [Todo]
  terminal_states: [Done]
workspace:
  root: /tmp/ws
---
Do {{ issue.identifier }} now.
"""


def test_parse_minimal_applies_defaults():
    wf = Workflow.parse(MINIMAL)
    assert wf.tracker["kind"] == "file"
    assert wf.agent["max_concurrent_agents"] == 2
    assert wf.sandbox["backend"] == "sandbox-exec"
    assert wf.poll_interval_sec == 15.0
    assert "Do {{ issue.identifier }} now." in wf.prompt_template


def test_missing_front_matter_rejected():
    with pytest.raises(WorkflowError, match="front matter"):
        Workflow.parse("no front matter here")


def test_empty_prompt_rejected():
    with pytest.raises(WorkflowError, match="prompt body is empty"):
        Workflow.parse("---\ntracker:\n  active_states: [A]\n  terminal_states: [B]\n---\n   ")


def test_overlapping_states_rejected():
    text = MINIMAL.replace("terminal_states: [Done]", "terminal_states: [Todo, Done]")
    with pytest.raises(WorkflowError, match="both active and terminal"):
        Workflow.parse(text)


def test_bad_sandbox_backend_rejected():
    text = MINIMAL.replace("---\nDo", "sandbox:\n  backend: chroot\n---\nDo")
    with pytest.raises(WorkflowError, match="sandbox.backend"):
        Workflow.parse(text)


def test_section_must_be_mapping():
    with pytest.raises(WorkflowError, match="must be a mapping"):
        Workflow.parse("---\ntracker: [1,2]\n---\nbody")


def test_env_indirection_and_default():
    assert expand_env("${FOO}", {"FOO": "bar"}) == "bar"
    assert expand_env("${MISSING:-fallback}", {}) == "fallback"
    assert expand_env({"a": ["${X}"]}, {"X": "1"}) == {"a": ["1"]}


def test_env_indirection_missing_without_default_fails():
    with pytest.raises(WorkflowError, match="NOPE"):
        expand_env("${NOPE}", {})


def test_invalid_yaml_rejected():
    with pytest.raises(WorkflowError, match="invalid YAML"):
        Workflow.parse("---\na: [1,\n---\nbody")


# -- template engine ------------------------------------------------------

def test_render_variable_and_dotted_path():
    out = render_template("x={{ a.b.c }} y={{ missing }}", {"a": {"b": {"c": 7}}})
    assert out == "x=7 y="


def test_render_if_else():
    tpl = "{% if flag %}YES{% else %}NO{% endif %}"
    assert render_template(tpl, {"flag": True}) == "YES"
    assert render_template(tpl, {"flag": None}) == "NO"
    assert render_template(tpl, {"flag": []}) == "NO"


def test_render_nested_if():
    tpl = "{% if a %}A{% if b %}B{% endif %}{% endif %}"
    assert render_template(tpl, {"a": 1, "b": 1}) == "AB"
    assert render_template(tpl, {"a": 1, "b": 0}) == "A"
    assert render_template(tpl, {"a": 0, "b": 1}) == ""


def test_unclosed_tag_rejected():
    with pytest.raises(WorkflowError, match="unclosed"):
        render_template("{% if a %}x", {})


def test_unknown_tag_rejected():
    with pytest.raises(WorkflowError, match="unsupported template tag"):
        render_template("{% for x in y %}{% endfor %}", {})


@pytest.mark.parametrize("tag", ["{% %}", "{%  %}", "{%\t%}"])
def test_empty_tag_raises_workflow_error_not_indexerror(tag):
    """Regression: a whitespace-only tag used to escape as a bare IndexError,
    bypassing the CLI's WorkflowError handler and hiding the real cause."""
    with pytest.raises(WorkflowError, match="empty template tag"):
        render_template(tag, {})


def test_if_without_expression_rejected():
    with pytest.raises(WorkflowError, match="requires an expression"):
        render_template("{% if %}x{% endif %}", {})


def test_shipped_workflow_is_valid(repo_root):
    wf = Workflow.load(str(repo_root / "WORKFLOW.md"))
    rendered = wf.render_prompt({
        "issue": {"identifier": "ABC-1", "title": "T", "state": "Todo",
                  "url": "u", "description": "d"},
        "attempt": 2,
        "workspace": {"path": "/tmp/x", "branch": "nezha/abc-1"},
        "base_ref": "HEAD",
    })
    assert "ABC-1" in rendered
    assert "attempt #2" in rendered
    assert "{{" not in rendered and "{%" not in rendered
