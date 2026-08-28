"""Comprehensive tests for duck_diff — schema diffing, row diffing, licensing, CLI.

Covers the full feature matrix including the new security/license modules,
CLI subcommands, and export formats.  Every test is self-contained with
datasets generated into ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Sequence, Tuple

import duckdb
import pytest

import duck_diff
from duck_diff import SourceError, diff as one_shot_diff
from duck_diff.cli import EXIT_DRIFT, EXIT_ERROR, EXIT_OK, main
from duck_diff.engine import DuckDiffer
from duck_diff.io import detect_kind, load_source, quote_identifier
from duck_diff.license import (
    LicenseInfo,
    LicenseTier,
    activate_license,
    deactivate_license,
    generate_license_key,
    get_license_info,
    is_license_valid,
)
from duck_diff.reporter import to_html, to_json, to_markdown, render, render_ascii
from duck_diff.security import (
    Ed25519PublicKey,
    fingerprint_hwid,
    generate_keypair,
    sign,
    verify,
)
from duck_diff.schema_diff import SchemaDiffResult, diff_schemas
from duck_diff.webstudio import pricing_page, studio_page


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(tmp: Path, name: str, text: str) -> str:
    p = tmp / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def _write_parquet(tmp: Path, name: str, sql: str) -> str:
    p = tmp / name
    con = duckdb.connect(":memory:")
    try:
        con.execute(f"COPY ({sql}) TO {quote_identifier(str(p))} (FORMAT PARQUET)")
    finally:
        con.close()
    return str(p)


# ===================================================================
# 1. Schema diffing
# ===================================================================


class TestSchemaDriftExtra:
    def test_identical_schemas_no_drift(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,name\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,name\n2,y\n")
        result = one_shot_diff(a, b, keys=["id"])
        assert result.schema_diff.has_schema_drift is False
        assert result.schema_diff.only_in_a == []
        assert result.schema_diff.only_in_b == []
        assert result.schema_diff.type_mismatches == []

    def test_extra_columns_only_in_source(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,name,extra\n1,x,99\n")
        b = _write_csv(tmp_path, "b.csv", "id,name\n1,x\n")
        result = one_shot_diff(a, b, keys=["id"])
        assert result.schema_diff.has_schema_drift is True
        lower_a = [c.lower() for c in result.schema_diff.only_in_a]
        assert "extra" in lower_a
        assert result.schema_diff.only_in_b == []

    def test_extra_columns_only_in_target(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,name\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,name,newcol\n1,x,hello\n")
        result = one_shot_diff(a, b, keys=["id"])
        assert result.schema_diff.has_schema_drift is True
        assert result.schema_diff.only_in_a == []
        lower_b = [c.lower() for c in result.schema_diff.only_in_b]
        assert "newcol" in lower_b

    def test_type_mismatch_detected(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,val\n1,42\n")
        b = _write_csv(tmp_path, "b.csv", "id,val\n1,true\n")
        result = one_shot_diff(a, b, keys=["id"])
        assert result.schema_drift is True
        assert len(result.schema_diff.type_mismatches) > 0
        assert any("val" in m for m in result.schema_diff.type_mismatches)

    def test_schema_diff_dict_roundtrip(self) -> None:
        sd = SchemaDiffResult(
            shared_columns=[("id", "BIGINT", "BIGINT")],
            only_in_a=["old_col"],
            only_in_b=["new_col"],
            type_mismatches=["flag: BOOLEAN != INTEGER"],
        )
        d = sd.to_dict()
        assert d["has_schema_drift"] is True
        assert d["only_in_a"] == ["old_col"]
        assert d["only_in_b"] == ["new_col"]
        assert len(d["type_mismatches"]) == 1
        sd2 = SchemaDiffResult(**{k: d[k] for k in d if k != "has_schema_drift"})
        assert sd2.has_schema_drift is True


# ===================================================================
# 2. Row / data diffing via DuckDB engine
# ===================================================================


class TestRowDiffing:
    def test_identical_datasets_zero_drift(self, tmp_path: Path) -> None:
        csv = "id,v\n1,a\n2,b\n3,c\n"
        a = _write_csv(tmp_path, "a.csv", csv)
        b = _write_csv(tmp_path, "b.csv", csv)
        r = one_shot_diff(a, b, keys=["id"])
        assert r.drift_detected is False
        assert r.summary.identical_rows_count == 3
        assert r.summary.modified_rows_count == 0

    def test_modified_row_detected(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n2,y\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,x\n2,z\n")
        r = one_shot_diff(a, b, keys=["id"])
        assert r.summary.modified_rows_count == 1
        assert r.summary.column_drift_stats["v"]["mismatches"] == 1

    def test_added_and_deleted_rows(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n2,y\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,x\n3,z\n")
        r = one_shot_diff(a, b, keys=["id"])
        assert r.summary.added_rows_count == 1
        assert r.summary.deleted_rows_count == 1
        assert r.summary.modified_rows_count == 0

    def test_epsilon_tolerance_within_bounds(self, tmp_path: Path) -> None:
        pa = _write_parquet(tmp_path, "a.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, 100.0::DOUBLE), (2::BIGINT, 200.0::DOUBLE)) t(id,v)")
        pb = _write_parquet(tmp_path, "b.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, 100.005::DOUBLE), (2::BIGINT, 200.0::DOUBLE)) t(id,v)")
        r = one_shot_diff(pa, pb, keys=["id"], epsilon=0.01)
        assert r.summary.identical_rows_count == 2
        assert r.summary.modified_rows_count == 0

    def test_epsilon_exceeded_flags_modification(self, tmp_path: Path) -> None:
        pa = _write_parquet(tmp_path, "a.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, 100.0::DOUBLE)) t(id,v)")
        pb = _write_parquet(tmp_path, "b.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, 105.0::DOUBLE)) t(id,v)")
        r = one_shot_diff(pa, pb, keys=["id"], epsilon=1.0)
        assert r.summary.modified_rows_count == 1

    def test_null_equality(self, tmp_path: Path) -> None:
        pa = _write_parquet(tmp_path, "a.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, NULL::VARCHAR)) t(id,v)")
        pb = _write_parquet(tmp_path, "b.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, NULL::VARCHAR)) t(id,v)")
        r = one_shot_diff(pa, pb, keys=["id"])
        assert r.summary.identical_rows_count == 1
        assert r.drift_detected is False

    def test_ignore_case(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,Hello\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,hello\n")
        strict = one_shot_diff(a, b, keys=["id"])
        assert strict.summary.modified_rows_count == 1
        relaxed = one_shot_diff(a, b, keys=["id"], ignore_case=True)
        assert relaxed.summary.identical_rows_count == 1

    def test_keyless_mode(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "v\nx\ny\n")
        b = _write_csv(tmp_path, "b.csv", "v\nx\nz\n")
        r = one_shot_diff(a, b)
        assert r.mode == "keyless"
        assert r.summary.added_rows_count == 1
        assert r.summary.deleted_rows_count == 1

    def test_sample_limit_respected(self, tmp_path: Path) -> None:
        rows_a = "".join(f"{i},a{i}\n" for i in range(10))
        rows_b = "".join(f"{i},b{i}\n" for i in range(10))
        a = _write_csv(tmp_path, "a.csv", "id,v\n" + rows_a)
        b = _write_csv(tmp_path, "b.csv", "id,v\n" + rows_b)
        r = one_shot_diff(a, b, keys=["id"], sample_limit=3)
        assert len(r.summary.sample_mismatches) == 3

    def test_diff_result_to_dict(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,y\n")
        r = one_shot_diff(a, b, keys=["id"])
        d = r.to_dict()
        assert d["tool"] == "duck-diff"
        assert d["mode"] == "keyed"
        assert d["summary"]["modified_rows_count"] == 1
        assert isinstance(d["schema"], dict)
        assert isinstance(d["duration_seconds"], float)

    def test_invalid_epsilon_rejected(self) -> None:
        with pytest.raises(ValueError):
            DuckDiffer(epsilon=-1.0)
        with pytest.raises(ValueError):
            DuckDiffer(epsilon=float("nan"))

    def test_missing_key_raises(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,y\n")
        with pytest.raises(SourceError):
            one_shot_diff(a, b, keys=["nonexistent"])

    def test_context_manager(self, tmp_path: Path) -> None:
        with DuckDiffer() as d:
            a = _write_csv(tmp_path, "a.csv", "id\n1\n")
            b = _write_csv(tmp_path, "b.csv", "id\n2\n")
            r = d.diff(a, b, keys=["id"])
            assert r.summary.total_rows_a == 1


# ===================================================================
# 3. Ed25519 licensing and HWID
# ===================================================================


class TestEd25519:
    def test_generate_keypair_produces_valid_pair(self) -> None:
        seed, pubkey = generate_keypair()
        assert len(seed) == 32
        assert isinstance(pubkey, Ed25519PublicKey)
        assert len(pubkey.to_bytes()) == 32

    def test_sign_and_verify_roundtrip(self) -> None:
        seed, pubkey = generate_keypair()
        msg = b"hello duck-diff"
        sig = sign(seed, msg)
        assert len(sig) == 64
        assert verify(pubkey, msg, sig) is True

    def test_verify_wrong_message_fails(self) -> None:
        seed, pubkey = generate_keypair()
        sig = sign(seed, b"correct message")
        assert verify(pubkey, b"wrong message", sig) is False

    def test_verify_wrong_key_fails(self) -> None:
        seed1, _ = generate_keypair()
        _, pubkey2 = generate_keypair()
        sig = sign(seed1, b"test")
        assert verify(pubkey2, b"test", sig) is False

    def test_verify_tampered_signature_fails(self) -> None:
        seed, pubkey = generate_keypair()
        sig = sign(seed, b"test")
        tampered = bytearray(sig)
        tampered[0] ^= 0xFF
        assert verify(pubkey, b"test", bytes(tampered)) is False

    def test_verify_empty_signature_fails(self) -> None:
        _, pubkey = generate_keypair()
        assert verify(pubkey, b"test", b"") is False

    def test_verify_short_signature_fails(self) -> None:
        _, pubkey = generate_keypair()
        assert verify(pubkey, b"test", b"\x00" * 32) is False

    def test_pubkey_from_bytes_roundtrip(self) -> None:
        _, pubkey = generate_keypair()
        raw = pubkey.to_bytes()
        restored = Ed25519PublicKey.from_bytes(raw)
        assert pubkey == restored
        assert hash(pubkey) == hash(restored)

    def test_pubkey_inequality(self) -> None:
        _, pk1 = generate_keypair()
        _, pk2 = generate_keypair()
        assert pk1 != pk2
        assert pk1 != "not a key"

    def test_fingerprint_hwid_deterministic(self) -> None:
        h1 = fingerprint_hwid()
        h2 = fingerprint_hwid()
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_fingerprint_hwid_is_hex(self) -> None:
        hwid = fingerprint_hwid()
        int(hwid, 16)  # must not raise


class TestLicenseActivation:
    def test_community_tier_no_key_needed(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "duck_diff.license._license_path",
            lambda: tmp_path / "license.json",
        )
        info = activate_license("", tier=LicenseTier.COMMUNITY)
        assert info.is_valid is True
        assert info.tier == LicenseTier.COMMUNITY
        assert info.expires_at == 0.0

    def test_community_license_persisted(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "duck_diff.license._license_path",
            lambda: tmp_path / "license.json",
        )
        activate_license("", tier=LicenseTier.COMMUNITY)
        loaded = get_license_info()
        assert loaded.tier == LicenseTier.COMMUNITY
        assert loaded.is_valid is True

    def test_deactivate_reverts_to_community(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "duck_diff.license._license_path",
            lambda: tmp_path / "license.json",
        )
        activate_license("", tier=LicenseTier.COMMUNITY)
        deactivate_license()
        info = get_license_info()
        assert info.tier == LicenseTier.COMMUNITY

    def test_pro_license_activation(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "duck_diff.license._license_path",
            lambda: tmp_path / "license.json",
        )
        hwid = fingerprint_hwid()
        key = generate_license_key(LicenseTier.PRO_ANNUAL, hwid, duration_seconds=86400)
        info = activate_license(key, tier=LicenseTier.PRO_ANNUAL)
        assert info.is_valid is True
        assert info.tier == LicenseTier.PRO_ANNUAL

    def test_founder_license_activation(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "duck_diff.license._license_path",
            lambda: tmp_path / "license.json",
        )
        hwid = fingerprint_hwid()
        key = generate_license_key(LicenseTier.FOUNDER_LIFETIME, hwid, duration_seconds=0)
        info = activate_license(key, tier=LicenseTier.FOUNDER_LIFETIME)
        assert info.is_valid is True
        assert info.tier == LicenseTier.FOUNDER_LIFETIME
        assert info.expires_at == 0.0  # lifetime

    def test_invalid_key_fails(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(
            "duck_diff.license._license_path",
            lambda: tmp_path / "license.json",
        )
        info = activate_license("not-a-valid-key", tier=LicenseTier.PRO_ANNUAL)
        assert info.is_valid is False

    def test_license_info_to_dict(self) -> None:
        info = LicenseInfo(
            tier=LicenseTier.PRO_ANNUAL, hwid="abc123",
            activated_at=1000.0, expires_at=2000.0,
            public_key_hex="aa", signature_hex="bb", is_valid=True,
        )
        d = info.to_dict()
        assert d["tier"] == LicenseTier.PRO_ANNUAL
        assert d["is_valid"] is True
        restored = LicenseInfo.from_dict(d)
        assert restored.tier == LicenseTier.PRO_ANNUAL

    def test_license_tier_constants(self) -> None:
        assert LicenseTier.COMMUNITY == "community"
        assert LicenseTier.PRO_ANNUAL == "pro_annual"
        assert LicenseTier.FOUNDER_LIFETIME == "founder_lifetime"
        assert len(LicenseTier.ALL) == 3

    def test_is_license_valid_default(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "duck_diff.license._license_path",
            lambda: Path("/nonexistent/license.json"),
        )
        assert is_license_valid() is True  # community tier is always valid

    def test_license_days_remaining(self) -> None:
        info = LicenseInfo(expires_at=time.time() + 86400 * 7 + 120)
        assert info.days_remaining == 7
        info_unlimited = LicenseInfo(expires_at=0)
        assert info_unlimited.days_remaining is None

    def test_license_is_expired(self) -> None:
        info = LicenseInfo(expires_at=time.time() - 100)
        assert info.is_expired is True
        info_valid = LicenseInfo(expires_at=time.time() + 100)
        assert info_valid.is_expired is False
        info_unlimited = LicenseInfo(expires_at=0)
        assert info_unlimited.is_expired is False


# ===================================================================
# 4. CLI subcommands
# ===================================================================


class TestCliSubcommands:
    def test_diff_subcommand(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,y\n")
        assert main(["diff", a, b, "--key", "id", "--format", "json"]) == EXIT_OK

    def test_schema_subcommand_json(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,age\n1,30\n")
        b = _write_csv(tmp_path, "b.csv", "id,height\n1,170\n")
        assert main(["schema", a, b, "--format", "json"]) == EXIT_OK

    def test_schema_subcommand_text(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id\n1\n")
        b = _write_csv(tmp_path, "b.csv", "id\n2\n")
        assert main(["schema", a, b, "--format", "text"]) == EXIT_OK

    def test_pricing_subcommand(self) -> None:
        assert main(["pricing"]) == EXIT_OK

    def test_status_subcommand(self) -> None:
        assert main(["status"]) == EXIT_OK

    def test_activate_subcommand_deactivate(self) -> None:
        assert main(["activate", "--deactivate"]) == EXIT_OK

    def test_activate_subcommand_invalid_key(self, tmp_path: Path) -> None:
        assert main(["activate", "bad-key"]) == EXIT_ERROR

    def test_version_flag(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0

    def test_fail_on_drift_exits_one(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,y\n")
        assert main(["diff", a, b, "--key", "id", "--fail-on-drift"]) == EXIT_DRIFT

    def test_output_file_writes_report(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,y\n")
        out = tmp_path / "report.json"
        rc = main(["diff", a, b, "--key", "id", "--format", "json", "-o", str(out)])
        assert rc == EXIT_OK
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["summary"]["modified_rows_count"] == 1

    def test_html_output_file(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,y\n")
        out = tmp_path / "report.html"
        rc = main(["diff", a, b, "--key", "id", "--format", "html", "-o", str(out)])
        assert rc == EXIT_OK
        assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")

    def test_bad_source_exits_two(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n")
        ghost = str(tmp_path / "missing.parquet")
        assert main(["diff", ghost, a]) == EXIT_ERROR

    def test_schema_output_file(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id\n1\n")
        b = _write_csv(tmp_path, "b.csv", "id\n2\n")
        out = tmp_path / "schema.json"
        rc = main(["schema", a, b, "-o", str(out)])
        assert rc == EXIT_OK
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert "has_schema_drift" in doc


# ===================================================================
# 5. Export formats
# ===================================================================


class TestExportFormats:
    def _result(self, tmp_path: Path):
        a = _write_csv(tmp_path, "a.csv", "id,v\n1,x\n2,y\n")
        b = _write_csv(tmp_path, "b.csv", "id,v\n1,x\n2,z\n")
        return one_shot_diff(a, b, keys=["id"])

    def test_json_export(self, tmp_path: Path) -> None:
        doc = json.loads(to_json(self._result(tmp_path)))
        assert doc["tool"] == "duck-diff"
        assert doc["drift_detected"] is True

    def test_markdown_export(self, tmp_path: Path) -> None:
        md = to_markdown(self._result(tmp_path))
        assert "shields.io" in md
        assert "| Metric |" in md

    def test_html_export_structure(self, tmp_path: Path) -> None:
        html = to_html(self._result(tmp_path))
        assert html.startswith("<!DOCTYPE html>")
        assert "const REPORT =" in html

    def test_ascii_export(self, tmp_path: Path) -> None:
        out = render_ascii(self._result(tmp_path))
        assert "modified" in out

    def test_render_dispatch(self, tmp_path: Path) -> None:
        r = self._result(tmp_path)
        assert render(r, "json").startswith("{")
        assert render(r, "markdown").startswith("##")
        assert render(r, "html").startswith("<!")
        with pytest.raises(ValueError):
            render(r, "xml")

    def test_studio_page_contains_version(self) -> None:
        html = studio_page()
        assert duck_diff.__version__ in html

    def test_pricing_page_contains_tiers(self) -> None:
        html = pricing_page()
        assert "Community" in html
        assert "Pro Annual" in html
        assert "Founder Lifetime" in html


# ===================================================================
# 6. Format detection and IO
# ===================================================================


class TestFormatDetection:
    def test_all_suffixes(self, tmp_path: Path) -> None:
        cases = {
            "data.parquet": "parquet",
            "data.csv": "csv",
            "data.tsv": "tsv",
            "data.json": "json",
            "data.jsonl": "json",
            "data.ndjson": "json",
        }
        for name, expected in cases.items():
            p = tmp_path / name
            p.write_text("x\n1\n", encoding="utf-8")
            assert detect_kind(str(p)) == expected

    def test_unknown_suffix_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "data.xlsx"
        p.write_text("nope", encoding="utf-8")
        with pytest.raises(SourceError):
            detect_kind(str(p))

    def test_sqlite_scheme_detected(self) -> None:
        assert detect_kind("sqlite://whatever.db#t") == "sqlite"


# ===================================================================
# 7. Engine context manager and lifecycle
# ===================================================================


class TestEngineLifecycle:
    def test_context_manager_closes(self) -> None:
        with DuckDiffer() as d:
            assert d.connection is not None
        # After exit the connection should be closed

    def test_explicit_close(self) -> None:
        d = DuckDiffer()
        d.close()
        d.close()  # double-close must not raise

    def test_connection_property(self) -> None:
        d = DuckDiffer()
        try:
            assert d.connection is not None
        finally:
            d.close()


# ===================================================================
# 8. Column statistics
# ===================================================================


class TestColumnStatistics:
    def test_numeric_stats_present(self, tmp_path: Path) -> None:
        pa = _write_parquet(tmp_path, "a.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, 10.0::DOUBLE), (2::BIGINT, 20.0::DOUBLE)) t(id,v)")
        pb = _write_parquet(tmp_path, "b.parquet",
            "SELECT * FROM (VALUES (1::BIGINT, 10.0::DOUBLE), (2::BIGINT, 30.0::DOUBLE)) t(id,v)")
        r = one_shot_diff(pa, pb, keys=["id"])
        stats = r.summary.column_stats
        assert "v" in stats
        v = stats["v"]
        assert v["kind"] == "numeric"
        assert v["mean_a"] == pytest.approx(15.0)
        assert v["mean_b"] == pytest.approx(20.0)

    def test_categorical_stats(self, tmp_path: Path) -> None:
        a = _write_csv(tmp_path, "a.csv", "id,color\n1,red\n2,blue\n")
        b = _write_csv(tmp_path, "b.csv", "id,color\n1,red\n2,green\n3,yellow\n")
        r = one_shot_diff(a, b, keys=["id"])
        stats = r.summary.column_stats
        assert "color" in stats
        assert stats["color"]["kind"] == "categorical"
