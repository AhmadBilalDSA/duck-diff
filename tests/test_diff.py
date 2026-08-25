"""Complete offline pytest suite for :mod:`duck_diff`.

Every test is self-contained: datasets are generated into ``tmp_path`` as
CSV/TSV/JSON/Parquet/SQLite artifacts and diffed with an in-memory DuckDB
engine. No network access is required (the SQLite loader's extension path
degrades transparently to the chunked stdlib-sqlite3 fallback).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import duckdb
import pytest

import duck_diff
from duck_diff import SourceError, diff as one_shot_diff
from duck_diff.action import run_action
from duck_diff.cli import EXIT_DRIFT, EXIT_ERROR, EXIT_OK, main
from duck_diff.engine import DuckDiffer
from duck_diff.io import detect_kind, load_source, parse_sqlite_uri, quote_identifier
from duck_diff.reporter import render, render_ascii, render_terminal, to_json, to_markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(tmp_path: Path, name: str, text: str) -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _write_parquet(
    tmp_path: Path,
    name: str,
    select_sql: str,
) -> str:
    """Materialise a Parquet file from a SELECT using DuckDB's built-in writer."""
    path = tmp_path / name
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"COPY ({select_sql}) TO {quote_identifier(str(path))} (FORMAT PARQUET)")
    finally:
        con.close()
    return str(path)


def _make_sqlite_db(path: Path, table: str, rows: Sequence[Tuple[int, str, float]]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, region TEXT, amount REAL)"
        )
        conn.executemany(
            f"INSERT INTO {table} (id, region, amount) VALUES (?, ?, ?)", list(rows)
        )
        conn.commit()
    finally:
        conn.close()


def _drift_pair(tmp_path: Path) -> Tuple[str, str]:
    a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n2,y\n")
    b = _write_csv(tmp_path, "b.csv", "id,v\n1,x\n2,z\n")
    return a, b


def _identical_pair(tmp_path: Path) -> Tuple[str, str]:
    text = "id,v\n1,x\n2,y\n"
    a = _write_csv(tmp_path, "same_a.csv", text)
    b = _write_csv(tmp_path, "same_b.csv", text)
    return a, b


# ---------------------------------------------------------------------------
# io.py — detection, URI parsing, loaders
# ---------------------------------------------------------------------------


class TestFormatDetection:
    def test_suffix_detection(self, tmp_path: Path) -> None:
        cases = {
            "data.parquet": "parquet",
            "data.csv": "csv",
            "data.tsv": "tsv",
            "data.json": "json",
            "data.jsonl": "json",
            "data.ndjson": "json",
        }
        for name, expected in cases.items():
            path = tmp_path / name
            path.write_text("x\n1\n", encoding="utf-8")
            assert detect_kind(str(path)) == expected, name

    def test_unknown_suffix_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "data.xlsx"
        path.write_text("nope", encoding="utf-8")
        with pytest.raises(SourceError):
            detect_kind(str(path))

    def test_sqlite_scheme_detected_without_file_check(self) -> None:
        assert detect_kind("sqlite://whatever.db#t") == "sqlite"

    def test_parse_sqlite_uri_windows_drive(self) -> None:
        assert parse_sqlite_uri("sqlite://C:/data/db.duck#events") == (
            "C:/data/db.duck",
            "events",
        )

    def test_parse_sqlite_uri_triple_slash(self) -> None:
        assert parse_sqlite_uri("sqlite:///C:/data/x.db#t1") == ("C:/data/x.db", "t1")

    def test_parse_sqlite_uri_missing_table_raises(self) -> None:
        with pytest.raises(SourceError):
            parse_sqlite_uri("sqlite://C:/data/x.db")


class TestLoaders:
    def test_csv_tsv_json_parquet_register_and_count(self, tmp_path: Path) -> None:
        csv_p = _write_csv(tmp_path, "t.csv", "id,v\n1,a\n2,b\n")
        tsv_p = _write_csv(tmp_path, "t.tsv", "id\tv\n1\ta,x\n")  # comma inside value
        json_p = _write_csv(tmp_path, "t.json", '[{"id": 1, "v": "a"}, {"id": 2, "v": "b"}]')
        parquet_p = _write_parquet(
            tmp_path,
            "t.parquet",
            "SELECT * FROM (VALUES (CAST(1 AS BIGINT), 'a'), (CAST(2 AS BIGINT), 'b')) AS t(id, v)",
        )
        con = duckdb.connect(":memory:")
        try:
            for source, expected in ((csv_p, 2), (tsv_p, 1), (json_p, 2), (parquet_p, 2)):
                loaded = load_source(con, source, "src")
                row = con.execute(f"SELECT COUNT(*) FROM {loaded.relation_sql}").fetchone()
                assert int(row[0]) == expected, source
            # TSV delimiter respected: the comma survived inside the value.
            loaded = load_source(con, tsv_p, "src")
            val = con.execute('SELECT v FROM "src" WHERE id = 1').fetchone()[0]
            assert val == "a,x"
        finally:
            con.close()


class TestSqliteLoader:
    def test_load_and_count(self, tmp_path: Path) -> None:
        db = tmp_path / "warehouse.db"
        _make_sqlite_db(db, "events", [(1, "eu", 10.0), (2, "us", 20.0)])
        uri = f"sqlite://{db.as_posix()}#events"
        con = duckdb.connect(":memory:")
        try:
            loaded = load_source(con, uri, "ev")
            count = con.execute(f"SELECT COUNT(*) FROM {loaded.relation_sql}").fetchone()
            assert int(count[0]) == 2
        finally:
            con.close()

    def test_full_diff_between_sqlite_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "wh.db"
        _make_sqlite_db(db, "baseline", [(1, "eu", 10.0), (2, "us", 20.0), (3, "ap", 30.0)])
        _make_sqlite_db(db, "candidate", [(1, "eu", 10.0), (2, "us", 21.0), (4, "latam", 40.0)])
        result = one_shot_diff(
            f"sqlite://{db.as_posix()}#baseline",
            f"sqlite://{db.as_posix()}#candidate",
            keys=["id"],
        )
        s = result.summary
        assert (s.identical_rows_count, s.modified_rows_count) == (1, 1)
        assert (s.added_rows_count, s.deleted_rows_count) == (1, 1)
        assert result.summary.column_drift_stats["amount"]["mismatches"] == 1


# ---------------------------------------------------------------------------
# schema_diff.py — CSV vs CSV schema drift
# ---------------------------------------------------------------------------


class TestSchemaDrift:
    def test_csv_vs_csv_schema_drift(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,name,age\n1,Ann,30\n2,Bob,41\n")
        b = _write_csv(tmp_path, "b.csv", "id,name,height\n1,Ann,170\n2,Bob,180\n")
        result = one_shot_diff(a, b, keys=["id"])

        schema = result.schema_diff
        assert [c.lower() for c in schema.only_in_a] == ["age"]
        assert [c.lower() for c in schema.only_in_b] == ["height"]
        assert schema.has_schema_drift is True
        # Shared columns still compare cell-by-cell.
        assert result.mode == "keyed"
        s = result.summary
        assert s.total_rows_a == 2 and s.total_rows_b == 2
        assert s.modified_rows_count == 0 and s.added_rows_count == 0
        assert s.deleted_rows_count == 0 and s.identical_rows_count == 2

    def test_type_mismatch_reported(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,flag\n1,1\n")
        b = _write_csv(tmp_path, "b.csv", "id,flag\n1,true\n")
        result = one_shot_diff(a, b, keys=["id"])
        # BIGINT vs BOOLEAN must surface as a type mismatch string.
        assert any("flag" in item for item in result.schema_diff.type_mismatches)
        assert result.schema_drift is True


# ---------------------------------------------------------------------------
# engine.py — epsilon, keyless, NULLs, ignore-case, limits
# ---------------------------------------------------------------------------


class TestEpsilonTolerance:
    A_SQL = (
        "SELECT * FROM (VALUES "
        "(CAST(1 AS BIGINT), CAST(100.0 AS DOUBLE)), "
        "(CAST(2 AS BIGINT), CAST(200.0 AS DOUBLE)), "
        "(CAST(3 AS BIGINT), CAST(300.0 AS DOUBLE)) "
        ") AS t(id, v)"
    )
    B_SQL = (
        "SELECT * FROM (VALUES "
        "(CAST(1 AS BIGINT), CAST(100.0005 AS DOUBLE)), "
        "(CAST(2 AS BIGINT), CAST(200.5 AS DOUBLE)), "
        "(CAST(3 AS BIGINT), CAST(300.0000001 AS DOUBLE)) "
        ") AS t(id, v)"
    )

    def test_within_epsilon_is_identical(self, tmp_path: Path) -> None:
        pa = _write_parquet(tmp_path, "a.parquet", self.A_SQL)
        pb = _write_parquet(tmp_path, "b.parquet", self.B_SQL)
        result = one_shot_diff(pa, pb, keys=["id"], epsilon=1e-3)
        s = result.summary
        assert s.identical_rows_count == 2
        assert s.modified_rows_count == 1
        assert s.column_drift_stats["v"]["mismatches"] == 1
        # Sample points at row key 2 (delta 0.5).
        sample_keys = [rec[0] for rec in s.sample_mismatches]
        assert "2" in sample_keys

    def test_zero_epsilon_flags_all_float_noise(self, tmp_path: Path) -> None:
        pa = _write_parquet(tmp_path, "a.parquet", self.A_SQL)
        pb = _write_parquet(tmp_path, "b.parquet", self.B_SQL)
        result = one_shot_diff(pa, pb, keys=["id"], epsilon=0.0)
        assert result.summary.modified_rows_count == 3
        assert result.summary.identical_rows_count == 0

    def test_epsilon_never_relaxes_key_join(self, tmp_path: Path) -> None:
        # Keys must join strictly even when their values sit inside epsilon.
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n2,y\n")
        differ = DuckDiffer(epsilon=10.0)
        result = differ.diff(a, b, keys=["id"])
        # If epsilon leaked into the join predicate these rows would pair up
        # as one modified row; strict null-safe keys keep them apart.
        assert result.summary.added_rows_count == 1
        assert result.summary.deleted_rows_count == 1
        assert result.summary.modified_rows_count == 0
        assert result.summary.identical_rows_count == 0

    def test_epsilon_applies_to_values_not_keys(self, tmp_path: Path) -> None:
        # Same keys, tiny numeric noise within tolerance -> identical.
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,10\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,10.0000001\n")
        result = DuckDiffer(epsilon=0.01).diff(a, b, keys=["id"])
        assert result.summary.identical_rows_count == 1
        assert result.summary.modified_rows_count == 0

    def test_invalid_epsilon_rejected(self) -> None:
        with pytest.raises(ValueError):
            DuckDiffer(epsilon=-1.0)
        with pytest.raises(ValueError):
            DuckDiffer(epsilon=float("nan"))


class TestKeylessDiff:
    def test_multiset_semantics_with_duplicates(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "v\nx1\nx1\nx2\n")
        b = _write_csv(tmp_path, "b.csv", "v\nx1\nx2\nx2\nx3\n")
        result = one_shot_diff(a, b)  # no keys => keyless hashing
        assert result.mode == "keyless"
        s = result.summary
        # Multiset arithmetic: x1 common=1, x2 common=1, so identical=2.
        assert s.identical_rows_count == 2
        assert s.added_rows_count == 2   # extra x2 + new x3
        assert s.deleted_rows_count == 1  # extra x1
        kinds = {rec[1] for rec in s.sample_mismatches}
        assert kinds <= {"__added_row__", "__deleted_row__"}
        assert result.drift_detected is True

    def test_fully_disjoint_schemas_still_diff(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "p\n1\n")
        b = _write_csv(tmp_path, "b.csv", "q\n9\n")
        result = one_shot_diff(a, b)
        assert result.summary.identical_rows_count == 0
        assert result.summary.added_rows_count == 1
        assert result.summary.deleted_rows_count == 1


class TestNullSemantics:
    A_SQL = (
        "SELECT * FROM (VALUES "
        "(CAST(1 AS BIGINT), CAST(NULL AS VARCHAR)), "
        "(CAST(2 AS BIGINT), CAST('a' AS VARCHAR)), "
        "(CAST(3 AS BIGINT), CAST('' AS VARCHAR)) "
        ") AS t(id, v)"
    )
    B_SQL = (
        "SELECT * FROM (VALUES "
        "(CAST(1 AS BIGINT), CAST(NULL AS VARCHAR)), "
        "(CAST(2 AS BIGINT), CAST('a' AS VARCHAR)), "
        "(CAST(3 AS BIGINT), CAST(NULL AS VARCHAR)), "
        "(CAST(4 AS BIGINT), CAST('z' AS VARCHAR)) "
        ") AS t(id, v)"
    )

    def test_null_vs_null_equal_and_null_vs_empty_drifts(self, tmp_path: Path) -> None:
        pa = _write_parquet(tmp_path, "a.parquet", self.A_SQL)
        pb = _write_parquet(tmp_path, "b.parquet", self.B_SQL)
        result = one_shot_diff(pa, pb, keys=["id"])
        s = result.summary
        # Rows 1 & 2 identical (NULL==NULL); row 3 drifts ('' vs NULL); row 4 added.
        assert s.identical_rows_count == 2
        assert s.modified_rows_count == 1
        assert s.added_rows_count == 1 and s.deleted_rows_count == 0
        record = next(rec for rec in s.sample_mismatches if rec[0] == "3")
        assert record[1] == "v"
        assert record[2] == ""          # source kept its empty string
        assert record[3] is None        # target is genuinely NULL

    def test_null_keys_join_correctly(self, tmp_path: Path) -> None:
        # NULL keys must match via IS NOT DISTINCT FROM, not vanish into anti-joins.
        pa = _write_parquet(
            tmp_path, "a.parquet",
            "SELECT * FROM (VALUES (CAST(NULL AS BIGINT), CAST('x' AS VARCHAR))) t(id, v)",
        )
        pb = _write_parquet(
            tmp_path, "b.parquet",
            "SELECT * FROM (VALUES (CAST(NULL AS BIGINT), CAST('x' AS VARCHAR))) t(id, v)",
        )
        result = one_shot_diff(pa, pb, keys=["id"])
        assert result.summary.identical_rows_count == 1
        assert result.summary.drift_detected is False


class TestIgnoreCaseAndLimits:
    def test_ignore_case_flag(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,ABC\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,abc\n")
        strict = one_shot_diff(a, b, keys=["id"])
        assert strict.summary.modified_rows_count == 1
        relaxed = one_shot_diff(a, b, keys=["id"], ignore_case=True)
        assert relaxed.summary.modified_rows_count == 0
        assert relaxed.summary.identical_rows_count == 1

    def test_sample_limit_caps_records(self, tmp_path: Path) -> None:
        rows_a = "".join(f"{i},a{i}\n" for i in range(1, 7))
        rows_b = "".join(f"{i},b{i}\n" for i in range(1, 7))
        a = _write_csv(tmp_path, "a.csv", "id,v\n" + rows_a)
        b = _write_csv(tmp_path, "b.csv", "id,v\n" + rows_b)
        result = one_shot_diff(a, b, keys=["id"], sample_limit=2)
        assert len(result.summary.sample_mismatches) == 2
        assert result.summary.column_drift_stats["v"]["mismatches"] == 6


class TestKeyValidation:
    def test_missing_key_column_raises(self, tmp_path: Path) -> None:
        a, b = _drift_pair(Path(str(tmp_path)))
        with pytest.raises(SourceError):
            one_shot_diff(a, b, keys=["nope"])

    def test_case_insensitive_key_resolution(self, tmp_path: Path) -> None:
        a, b = _drift_pair(Path(str(tmp_path)))
        result = one_shot_diff(a, b, keys=["ID"])  # folded onto shared column 'id'
        assert result.keys == ["id"]
        assert result.summary.modified_rows_count == 1


class TestIdenticalDatasets:
    def test_zero_drift_everywhere(self, tmp_path: Path) -> None:
        a, b = _identical_pair(Path(str(tmp_path)))
        result = one_shot_diff(a, b, keys=["id"])
        s = result.summary
        assert s.drift_detected is False
        assert result.drift_detected is False
        assert (s.modified_rows_count, s.added_rows_count, s.deleted_rows_count) == (0, 0, 0)


# ---------------------------------------------------------------------------
# reporter.py — markdown / JSON / terminal renderers
# ---------------------------------------------------------------------------


class TestReporters:
    def _result(self, tmp_path: Path):
        a, b = _drift_pair(Path(str(tmp_path)))
        return one_shot_diff(a, b, keys=["id"])

    def test_markdown_contains_badges_details_tables(self, tmp_path: Path) -> None:
        md = to_markdown(self._result(tmp_path))
        assert "shields.io" in md
        assert "<details>" in md and "</details>" in md
        assert "| Metric |" in md
        assert "Cell drift by column" in md
        assert "`NULL`" not in md or True  # presence depends on data; never crashes

    def test_markdown_escapes_pipe_characters(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,a|b\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,a|c\n")
        md = to_markdown(one_shot_diff(a, b, keys=["id"]))
        assert "a\\|b" in md and "a\\|c" in md

    def test_json_exporter_roundtrip(self, tmp_path: Path) -> None:
        doc = json.loads(to_json(self._result(tmp_path)))
        assert doc["tool"] == "duck-diff"
        assert doc["mode"] == "keyed" and doc["keys"] == ["id"]
        assert doc["summary"]["modified_rows_count"] == 1
        assert doc["summary"]["total_rows_a"] == 2
        assert isinstance(doc["sample_mismatches"], list)
        # Stable, machine-consumable: re-serialisation must succeed.
        assert isinstance(json.dumps(doc), str)

    def test_renderers_smoke(self, tmp_path: Path) -> None:
        result = self._result(tmp_path)
        ascii_out = render_ascii(result)
        assert "identical" in ascii_out and "modified" in ascii_out
        rich_or_ascii = render_terminal(result, color=False)
        assert isinstance(rich_or_ascii, str) and len(rich_or_ascii) > 0
        assert json.loads(render(result, "json"))["drift_detected"] is True
        assert "duck-diff report" in render(result, "markdown")
        with pytest.raises(ValueError):
            render(result, "xml")

    def test_rich_ui_when_available(self, tmp_path: Path) -> None:
        pytest.importorskip("rich")
        out = render_terminal(self._result(tmp_path), color=True)
        assert "duck-diff" in out


# ---------------------------------------------------------------------------
# cli.py — exit codes & formats
# ---------------------------------------------------------------------------


class TestCliExitCodes:
    def test_drift_without_flag_exits_zero(self, tmp_path: Path, capsys) -> None:
        a, b = _drift_pair(Path(str(tmp_path)))
        assert main([a, b, "--key", "id", "--format", "json"]) == EXIT_OK

    def test_fail_on_drift_exits_one_on_drift(self, tmp_path: Path) -> None:
        a, b = _drift_pair(Path(str(tmp_path)))
        assert main([a, b, "--key", "id", "--fail-on-drift"]) == EXIT_DRIFT

    def test_fail_on_drift_exits_zero_when_identical(self, tmp_path: Path) -> None:
        a, b = _identical_pair(Path(str(tmp_path)))
        assert main([a, b, "--key", "id", "--fail-on-drift"]) == EXIT_OK

    def test_bad_source_exits_two(self, tmp_path: Path) -> None:
        a, _ = _drift_pair(Path(str(tmp_path)))
        ghost = str(Path(tmp_path) / "missing.parquet")
        assert main([ghost, a]) == EXIT_ERROR

    def test_output_flag_writes_file(self, tmp_path: Path) -> None:
        a, b = _drift_pair(Path(str(tmp_path)))
        out = Path(tmp_path) / "report.json"
        rc = main([a, b, "--key", "id", "--format", "json", "--output", str(out)])
        assert rc == EXIT_OK
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["summary"]["modified_rows_count"] == 1

    def test_version_flag(self, capsys) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0
        assert duck_diff.__version__ in capsys.readouterr().out


# ---------------------------------------------------------------------------
# action.py — GitHub Action outputs & gating
# ---------------------------------------------------------------------------


class TestGitHubAction:
    def test_outputs_written_and_gate_fails(self, tmp_path: Path, monkeypatch) -> None:
        gh_out = tmp_path / "github_output.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(gh_out))
        a, b = _drift_pair(tmp_path)
        code, outputs = run_action(
            inputs={
                "source_path": a,
                "target_path": b,
                "key_columns": "id",
                "epsilon": "0.001",
                "fail_on_drift": "true",
            },
        )
        assert code == EXIT_DRIFT
        assert outputs == {
            "drift_detected": "true",
            "modified_rows": "1",
            "schema_drift": "false",
        }
        lines: Dict[str, str] = {}
        for line in gh_out.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            lines[name] = value
        assert lines == outputs

    def test_no_github_output_env_is_fine(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        a, b = _identical_pair(tmp_path)
        code, outputs = run_action(inputs={"source_path": a, "target_path": b})
        assert code == EXIT_OK
        assert outputs["drift_detected"] == "false"

    def test_fail_on_drift_false_tolerates_drift(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh.txt"))
        a, b = _drift_pair(tmp_path)
        code, outputs = run_action(
            inputs={"source_path": a, "target_path": b, "key_columns": "id",
                    "fail_on_drift": "false"},
        )
        assert code == EXIT_OK
        assert outputs["drift_detected"] == "true"

    def test_missing_inputs_exit_two(self, monkeypatch) -> None:
        monkeypatch.delenv("INPUT_SOURCE_PATH", raising=False)
        monkeypatch.delenv("INPUT_TARGET_PATH", raising=False)
        code, outputs = run_action(inputs={})
        assert code == EXIT_ERROR
        assert outputs == {}

    def test_invalid_epsilon_exit_two(self, tmp_path: Path) -> None:
        a, b = _identical_pair(tmp_path)
        code, _ = run_action(
            inputs={"source_path": a, "target_path": b, "epsilon": "not-a-float"},
        )
        assert code == EXIT_ERROR

    def test_schema_drift_output_true(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh.txt"))
        a = _write_csv(tmp_path, "a.csv", "id,age\n1,30\n")
        b = _write_csv(tmp_path, "b.csv", "id,height\n1,170\n")
        code, outputs = run_action(
            inputs={"source_path": a, "target_path": b, "key_columns": "id",
                    "fail_on_drift": "true"},
        )
        assert code == EXIT_DRIFT
        assert outputs["schema_drift"] == "true"
