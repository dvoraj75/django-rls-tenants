"""Tests for the supported-versions matrix: every file that states it must agree (#114).

The constants below are the single place to edit when the matrix changes (see the
supported-versions policy in ``docs/getting-started/installation.md``). Every other file
that states the matrix is parsed and compared against them, and each failure names the
file that disagrees.

This module reads ``.github/``, ``docs/`` and ``AGENTS.md``, none of which ship in the
sdist, so ``pyproject.toml`` excludes this file from the sdist.
"""

from __future__ import annotations

import itertools
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

PYTHON = ("3.12", "3.13", "3.14")
DJANGO = ("5.2", "6.0", "6.1")
POSTGRES = ("15", "16", "17", "18")
DEFAULT_POSTGRES = "16"
COVERAGE_LEG = ("3.12", "6.1", "psycopg2")
DRIVERS = ("psycopg2", "psycopg3")

ROOT = Path(__file__).resolve().parents[1]

# The prose uses typographic characters; spelled as escapes to keep ruff's RUF001 quiet.
_EN_DASH = "\u2013"
_TIMES = "\u00d7"

_TABLE_ROW = re.compile(r"^\| (Python|Django|PostgreSQL)\s*\| ([^|]+?)\s*\|", re.MULTILINE)


def _key(version: str) -> tuple[int, ...]:
    """Sort key for ``"3.12"``, ``"18"`` or a year-based ``"2028"``."""
    return tuple(map(int, version.split(".")))


def _min(versions: tuple[str, ...]) -> str:
    return min(versions, key=_key)


def _max(versions: tuple[str, ...]) -> str:
    return max(versions, key=_key)


def _read(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        pytest.fail(f"{rel} is missing; the support-matrix drift test expects it at {path}")
    return path.read_text(encoding="utf-8")


def _workflow(rel: str) -> dict[Any, Any]:
    data = yaml.safe_load(_read(rel))
    assert isinstance(data, dict), f"{rel}: not a YAML mapping"
    return data


def _table(rel: str) -> dict[str, set[str]]:
    """Parse the ``| Python | 3.12, 3.13 |`` rows of a requirements table."""
    rows = {
        name: {v.strip() for v in cell.split(",")} for name, cell in _TABLE_ROW.findall(_read(rel))
    }
    assert set(rows) == {"Python", "Django", "PostgreSQL"}, (
        f"{rel}: expected a requirements table with Python, Django and PostgreSQL rows, "
        f"found {sorted(rows)}"
    )
    return rows


def _assert_table(rel: str) -> None:
    rows = _table(rel)
    expected = {"Python": set(PYTHON), "Django": set(DJANGO), "PostgreSQL": set(POSTGRES)}
    for name, versions in expected.items():
        assert rows[name] == versions, (
            f"{rel}: the {name} row lists {sorted(rows[name])}, "
            f"expected {sorted(versions, key=_key)}"
        )


def _assert_django_badge(rel: str) -> None:
    match = re.search(r"badge/django-(.+?)-blue", _read(rel))
    assert match, f"{rel}: Django versions badge not found"
    found = match.group(1).split("%20%7C%20")
    assert found == list(DJANGO), f"{rel}: Django badge lists {found}, expected {list(DJANGO)}"


def _gate_value(condition: str, axis: str) -> str | None:
    match = re.search(rf"matrix\.{axis} == '([^']+)'", condition)
    return match.group(1) if match else None


def _expand(rel: str, job: str, matrix: dict[str, Any]) -> list[dict[str, str]]:
    """Expand a job's matrix into its legs, with GitHub's ``include`` rules.

    An ``include`` row extends every original combination whose axis values it does not
    overwrite (values added by an earlier row may be overwritten); a row that extends none
    becomes a combination of its own, which later rows do not extend.
    """
    assert "exclude" not in matrix, f"{rel}: {job} has exclude rows, which _expand ignores"
    axes = {key: values for key, values in matrix.items() if key != "include"}
    # product() of no axes is one empty combination, which every row would extend;
    # GitHub runs each row of an include-only matrix as a leg of its own.
    assert axes, f"{rel}: {job} has no matrix axes, which _expand does not handle"
    originals = [
        dict(zip(axes, combo, strict=True)) for combo in itertools.product(*axes.values())
    ]
    legs = [dict(combo) for combo in originals]
    added = []
    for row in matrix.get("include", []):
        targets = [
            leg
            for original, leg in zip(originals, legs, strict=True)
            if all(original[key] == value for key, value in row.items() if key in original)
        ]
        for leg in targets:
            leg.update(row)
        if not targets:
            added.append(dict(row))
    return legs + added


def _substitute(template: str, leg: dict[str, str]) -> str:
    """Replace each ``${{ matrix.<axis> }}`` in ``template`` with the leg's value.

    A key the leg lacks is left as the expression, so the leg fails the supported-versions
    check with a message instead of a ``KeyError``.
    """
    return re.sub(
        r"\$\{\{ matrix\.([\w-]+) \}\}", lambda m: leg.get(m.group(1), m.group(0)), template
    )


def _test_legs(rel: str) -> list[dict[str, str]]:
    """Every leg of the library test jobs, with the PostgreSQL major each one runs."""
    jobs = _workflow(rel)["jobs"]
    legs = []
    for job in ("test", "test-postgres"):
        image = jobs[job]["services"]["postgres"]["image"]
        for leg in _expand(rel, job, jobs[job]["strategy"]["matrix"]):
            postgres = _substitute(image, leg).removeprefix("postgres:")
            legs.append({**leg, "job": job, "postgres": postgres})
    return legs


# --- pyproject.toml ---------------------------------------------------------------------


def test_pyproject_toml():
    rel = "pyproject.toml"
    data = tomllib.loads(_read(rel))
    project = data["project"]
    classifiers = project["classifiers"]
    python = {
        c.rsplit(" :: ", 1)[1]
        for c in classifiers
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    }
    django = {
        c.rsplit(" :: ", 1)[1] for c in classifiers if c.startswith("Framework :: Django :: ")
    }

    assert python == set(PYTHON), f"{rel}: Python classifiers {sorted(python)}, expected {PYTHON}"
    assert django == set(DJANGO), f"{rel}: Django classifiers {sorted(django)}, expected {DJANGO}"
    assert project["requires-python"] == f">={_min(PYTHON)}", (
        f"{rel}: requires-python is {project['requires-python']!r}, expected '>={_min(PYTHON)}'"
    )
    assert f"django>={_min(DJANGO)}" in project["dependencies"], (
        f"{rel}: dependencies {project['dependencies']} lack 'django>={_min(DJANGO)}'"
    )
    target = "py" + _min(PYTHON).replace(".", "")
    assert data["tool"]["ruff"]["target-version"] == target, (
        f"{rel}: ruff target-version != {target!r}"
    )
    assert data["tool"]["mypy"]["python_version"] == _min(PYTHON), (
        f"{rel}: mypy python_version != {_min(PYTHON)!r}"
    )
    test_group = data["dependency-groups"]["test"]
    assert any(req.startswith("pyyaml") for req in test_group), (
        f"{rel}: the test group must declare pyyaml (this module imports it)"
    )
    sdist_exclude = data["tool"]["hatch"]["build"]["targets"]["sdist"].get("exclude", [])
    assert f"tests/{Path(__file__).name}" in sdist_exclude, (
        f"{rel}: the sdist must exclude tests/{Path(__file__).name} (it reads files the sdist "
        "does not ship)"
    )


# --- .github/workflows/ci.yml -----------------------------------------------------------


def test_ci_test_matrix():
    rel = ".github/workflows/ci.yml"
    jobs = _workflow(rel)["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]

    for axis, expected in (("python-version", PYTHON), ("django-version", DJANGO)):
        assert matrix[axis] == list(expected), (
            f"{rel}: test {axis} axis is {matrix[axis]}, expected {list(expected)}"
        )
    assert "exclude" not in matrix, (
        f"{rel}: every Python x Django pair is supported upstream, so the test matrix needs "
        "no exclude rows"
    )
    assert matrix["driver"] == ["psycopg2"], f"{rel}: test driver axis must be ['psycopg2']"
    psycopg3 = {
        (inc["python-version"], inc["django-version"])
        for inc in matrix.get("include", [])
        if inc.get("driver") == "psycopg3"
    }
    expected = {(_min(PYTHON), _min(DJANGO)), (_min(PYTHON), _max(DJANGO))}
    assert psycopg3 == expected, (
        f"{rel}: test psycopg3 legs are {sorted(psycopg3)}, expected the oldest Python on the "
        f"oldest and newest Django {sorted(expected)}"
    )


def test_ci_postgres_edges():
    rel = ".github/workflows/ci.yml"
    jobs = _workflow(rel)["jobs"]
    matrix = jobs["test-postgres"]["strategy"]["matrix"]

    edges = [_min(POSTGRES), _max(POSTGRES)]
    assert matrix["postgres"] == edges, (
        f"{rel}: test-postgres runs {matrix['postgres']}, expected {edges}"
    )
    django_edges = [_min(DJANGO), _max(DJANGO)]
    assert matrix["django-version"] == django_edges, (
        f"{rel}: test-postgres Django {matrix['django-version']}, expected {django_edges}"
    )
    assert matrix.get("driver") == ["psycopg2"], (
        f"{rel}: test-postgres driver axis must be ['psycopg2']"
    )
    for job in ("test", "example-smoke", "test-plain-role"):
        image = jobs[job]["services"]["postgres"]["image"]
        assert image == f"postgres:{DEFAULT_POSTGRES}", (
            f"{rel}: {job} runs {image}, expected postgres:{DEFAULT_POSTGRES}"
        )


def test_expand_follows_githubs_include_rules():
    """The worked example of "Expanding or adding matrix configurations" in GitHub's docs."""
    matrix = {
        "fruit": ["apple", "pear"],
        "animal": ["cat", "dog"],
        "include": [
            {"color": "green"},
            {"color": "pink", "animal": "cat"},
            {"fruit": "apple", "shape": "circle"},
            {"fruit": "banana"},
            {"fruit": "banana", "animal": "cat"},
        ],
    }
    assert _expand("example", "job", matrix) == [
        {"fruit": "apple", "animal": "cat", "color": "pink", "shape": "circle"},
        {"fruit": "apple", "animal": "dog", "color": "green", "shape": "circle"},
        {"fruit": "pear", "animal": "cat", "color": "pink"},
        {"fruit": "pear", "animal": "dog", "color": "green"},
        {"fruit": "banana"},
        {"fruit": "banana", "animal": "cat"},
    ]


def test_ci_legs_name_supported_versions():
    """Every leg of the library test jobs runs versions the matrix supports."""
    for leg in _test_legs(".github/workflows/ci.yml"):
        for key, supported in (
            ("python-version", PYTHON),
            ("django-version", DJANGO),
            ("postgres", POSTGRES),
            ("driver", DRIVERS),
        ):
            assert leg.get(key) in supported, (
                f".github/workflows/ci.yml: {leg['job']} leg {leg} has {key} "
                f"{leg.get(key)!r}, not one of {supported}"
            )


def test_ci_every_postgres_major_has_a_leg_on_the_oldest_django():
    """No supported major is covered by inference (#151)."""
    rel = ".github/workflows/ci.yml"
    covered = {
        leg["postgres"] for leg in _test_legs(rel) if leg.get("django-version") == _min(DJANGO)
    }
    missing = sorted(set(POSTGRES) - covered, key=_key)
    assert not missing, f"{rel}: PostgreSQL {missing} has no leg on Django {_min(DJANGO)}"


def test_ci_psycopg3_runs_the_oldest_django_and_a_non_default_postgres():
    """The connection-pool tests run only on psycopg 3, so its legs must reach the edges (#151)."""
    rel = ".github/workflows/ci.yml"
    legs = [leg for leg in _test_legs(rel) if leg.get("driver") == "psycopg3"]
    assert any(leg.get("django-version") == _min(DJANGO) for leg in legs), (
        f"{rel}: no psycopg3 leg runs the oldest supported Django {_min(DJANGO)}"
    )
    assert any(leg["postgres"] != DEFAULT_POSTGRES for leg in legs), (
        f"{rel}: every psycopg3 leg runs PostgreSQL {DEFAULT_POSTGRES}; one must run another "
        "supported major"
    )


@pytest.mark.parametrize("job", ["test", "test-postgres"])
def test_ci_driver_legs_install_and_verify_the_driver(job):
    """A leg's ``driver`` must be what it runs: psycopg 3 installed, the driver asserted."""
    rel = ".github/workflows/ci.yml"
    spec = _workflow(rel)["jobs"][job]
    assert spec["env"]["DRIVER"] == "${{ matrix.driver }}", (
        f"{rel}: {job} must pass matrix.driver to the driver check as DRIVER"
    )
    runs = "\n".join(step.get("run", "") for step in spec["steps"])
    guard = 'if [ "${{ matrix.driver }}" = "psycopg3" ]; then'
    install = '"psycopg[binary,pool]"'
    assert guard in runs, f"{rel}: {job} must install psycopg 3 only on its psycopg3 legs"
    assert install in runs, f"{rel}: {job} must install psycopg[binary,pool] on its psycopg3 legs"
    assert runs.index(guard) < runs.index(install), (
        f"{rel}: {job} must install psycopg[binary,pool] inside the driver guard, or every "
        "psycopg2 leg flips to psycopg 3"
    )
    # Line-anchored: a commented-out assert must not pass.
    assert re.search(r'^\s*assert driver == os\.environ\["DRIVER"\]', runs, re.MULTILINE), (
        f"{rel}: {job} must assert that the active driver is the leg's"
    )


def test_ci_plain_role_leg():
    """The non-superuser leg (#131) runs a supported Python and deselects superuser tests."""
    rel = ".github/workflows/ci.yml"
    job = _workflow(rel)["jobs"]["test-plain-role"]
    assert job["env"]["UV_PYTHON"] in PYTHON, (
        f"{rel}: test-plain-role runs Python {job['env']['UV_PYTHON']}, not one of {PYTHON}"
    )
    assert job["env"]["POSTGRES_USER"] != "postgres", (
        f"{rel}: test-plain-role must connect as a plain role, not the postgres superuser"
    )
    runs = " ".join(step.get("run", "") for step in job["steps"])
    assert '-m "not superuser_only"' in runs, (
        f"{rel}: test-plain-role must deselect the superuser_only tests"
    )


def test_ci_coverage_gate_names_a_real_leg():
    """The coverage comment and upload must be pinned to a leg that exists.

    Dropping a Django series without moving this gate would silently disable coverage reporting.
    """
    rel = ".github/workflows/ci.yml"
    steps = _workflow(rel)["jobs"]["test"]["steps"]
    gated = [s for s in steps if "matrix.python-version ==" in str(s.get("if", ""))]
    assert gated, f"{rel}: no coverage steps gated on a matrix leg found"
    for step in gated:
        found = tuple(
            _gate_value(step["if"], axis)
            for axis in ("python-version", "django-version", "driver")
        )
        assert found == COVERAGE_LEG, (
            f"{rel}: step {step['name']!r} is gated on {found}, expected {COVERAGE_LEG}"
        )


def test_canary_runs_beyond_the_matrix():
    """The canary must stay one step ahead, so a matrix bump reminds you to move it."""
    rel = ".github/workflows/canary.yml"
    workflow = _workflow(rel)
    jobs = workflow["jobs"]

    assert workflow["env"]["DJANGO_SERIES"] == _max(DJANGO), (
        f"{rel}: DJANGO_SERIES is {workflow['env']['DJANGO_SERIES']!r}, "
        f"expected the newest supported series {_max(DJANGO)!r}"
    )
    (python_next,) = jobs["python-next"]["strategy"]["matrix"]["python-version"]
    assert _key(python_next) > _key(_max(PYTHON)), (
        f"{rel}: python-next runs {python_next}, which is not newer than {_max(PYTHON)}"
    )
    (postgres_next,) = jobs["postgres-next"]["strategy"]["matrix"]["postgres"]
    major = re.match(r"\d+", postgres_next)
    assert major, f"{rel}: postgres-next tag {postgres_next!r} does not start with a major"
    assert int(major.group()) > int(_max(POSTGRES)), (
        f"{rel}: postgres-next runs {postgres_next}, which is not newer than {_max(POSTGRES)}"
    )
    for job in ("django-main", "postgres-next"):
        python = jobs[job]["env"]["UV_PYTHON"]
        assert python == _max(PYTHON), (
            f"{rel}: {job} runs Python {python}, expected the newest supported {_max(PYTHON)}"
        )


# --- README and docs --------------------------------------------------------------------


@pytest.mark.parametrize("rel", ["README.md", "docs/index.md"])
def test_readme_and_docs_index(rel):
    _assert_django_badge(rel)
    _assert_table(rel)


def test_installation_guide():
    rel = "docs/getting-started/installation.md"
    _assert_table(rel)
    assert re.search(r"^## Supported versions$", _read(rel), re.MULTILINE), (
        f"{rel}: the '## Supported versions' policy section is missing"
    )


def test_comparison_page():
    rel = "docs/comparison.md"
    match = re.search(r"\| Django version support \| (\S+) — (\S+) \|", _read(rel))
    assert match, f"{rel}: 'Django version support' row not found"
    assert match.groups() == (_min(DJANGO), _max(DJANGO)), (
        f"{rel}: Django range {match.groups()}, expected {(_min(DJANGO), _max(DJANGO))}"
    )


# --- AGENTS.md and the contributing guides ----------------------------------------------


def test_agents_md():
    rel = "AGENTS.md"
    text = _read(rel)
    overview = (
        f"Python {_min(PYTHON)}+, Django {_min(DJANGO)}+, "
        f"PostgreSQL {_min(POSTGRES)}{_EN_DASH}{_max(POSTGRES)}"
    )
    assert overview in text, f"{rel}: project overview must read {overview!r}"
    match = re.search(rf"Python \{{([^}}]+)\}} {_TIMES} Django \{{([^}}]+)\}}", text)
    assert match, f"{rel}: CI section 'Python {{...}} {_TIMES} Django {{...}}' not found"
    python, django = ({v.strip() for v in group.split(",")} for group in match.groups())
    assert python == set(PYTHON), f"{rel}: CI Python set {sorted(python)}, expected {PYTHON}"
    assert django == set(DJANGO), f"{rel}: CI Django set {sorted(django)}, expected {DJANGO}"


@pytest.mark.parametrize("rel", ["docs/development/contributing.md", ".github/CONTRIBUTING.md"])
def test_contributing_guides(rel):
    text = _read(rel)
    match = re.search(r"Python (\d+\.\d+)-(\d+\.\d+) x Django (\S+)-(\S+)", text)
    assert match, f"{rel}: CI line 'Python A-B x Django C-D' not found"
    expected = (_min(PYTHON), _max(PYTHON), _min(DJANGO), _max(DJANGO))
    assert match.groups() == expected, f"{rel}: CI line says {match.groups()}, expected {expected}"
    legs = f"PostgreSQL {_min(POSTGRES)} and {_max(POSTGRES)} legs"
    assert legs in text, f"{rel}: {legs!r} not found"
    prerequisites = (
        f"Python {_min(PYTHON)} or newer",
        f"PostgreSQL {_min(POSTGRES)}{_EN_DASH}{_max(POSTGRES)}",
    )
    for line in prerequisites:
        assert f"- {line}" in text, f"{rel}: prerequisite '- {line}' not found"


# --- example app ------------------------------------------------------------------------


def test_example_dockerfile():
    rel = "example/Dockerfile"
    match = re.search(r"django>=(\d+\.\d+),<", _read(rel))
    assert match, f"{rel}: no 'django>=X.Y,<' pin found"
    assert match.group(1) in DJANGO, f"{rel}: pins Django {match.group(1)}, not one of {DJANGO}"
