"""Bulk validation, CSV handling and the output formats."""

import csv
import io
import json

import httpx
import pytest

from gst_validator import GSTClient, TTLCache, enrich_many, validate_many
from gst_validator.cli import main

from .support import GOODS_PAYLOAD, PUBLIC_GSTIN, VALID_GSTIN


class TestValidateMany:
    def test_yields_one_result_per_input_and_never_raises(self) -> None:
        rows = list(validate_many([VALID_GSTIN, "nope", "  2317uno00001und  ", ""]))
        assert [row.is_valid for row in rows] == [True, False, True, False]
        assert rows[1].error == "does not match the GSTIN format"
        assert rows[2].gstin is not None
        assert rows[2].gstin.value == "2317UNO00001UND"  # normalised

    def test_keeps_the_input_verbatim(self) -> None:
        row = next(iter(validate_many(["  27aaacr5055k1z7 "])))
        assert row.value == "  27aaacr5055k1z7 "
        assert row.gstin is not None
        assert row.gstin.value == PUBLIC_GSTIN

    def test_extras_are_carried_through(self) -> None:
        rows = list(validate_many([VALID_GSTIN], extras=[{"name": "A Ltd"}]))
        assert rows[0].as_dict()["name"] == "A Ltd"

    def test_as_dict_keys_are_stable_between_valid_and_invalid(self) -> None:
        good, bad = validate_many([VALID_GSTIN, "nope"])
        assert set(good.as_dict()) == set(bad.as_dict())


class TestEnrichMany:
    @staticmethod
    def _transport(fail: bool = False) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/services/searchtp":
                return httpx.Response(200, text="<html></html>")
            if fail:
                return httpx.Response(503)
            if request.url.path.endswith("goodservice"):
                return httpx.Response(200, json=GOODS_PAYLOAD)
            return httpx.Response(200, json={"status": 1, "data": []})

        return httpx.MockTransport(handler)

    def test_adds_the_captcha_free_data(self) -> None:
        with GSTClient(transport=self._transport(), cache=TTLCache()) as client:
            rows = list(enrich_many(validate_many([VALID_GSTIN]), client=client))
        assert rows[0].goods_and_services[0].code == "998314"

    def test_one_session_serves_the_whole_batch(self) -> None:
        sessions = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal sessions
            if request.url.path == "/services/searchtp":
                sessions += 1
                return httpx.Response(200, text="<html></html>")
            return httpx.Response(200, json={"status": 1, "data": []})

        with GSTClient(transport=httpx.MockTransport(handler), cache=TTLCache()) as client:
            list(enrich_many(validate_many([VALID_GSTIN, PUBLIC_GSTIN]), client=client))
        assert sessions == 1

    def test_a_failure_is_recorded_and_the_run_continues(self) -> None:
        with GSTClient(transport=self._transport(fail=True), cache=TTLCache()) as client:
            rows = list(enrich_many(validate_many([VALID_GSTIN, PUBLIC_GSTIN]), client=client))
        assert len(rows) == 2
        assert all(row.is_valid for row in rows)
        assert all(row.enrichment_error for row in rows)

    def test_invalid_rows_pass_through_untouched(self) -> None:
        with GSTClient(transport=self._transport(), cache=TTLCache()) as client:
            rows = list(enrich_many(validate_many(["nope"]), client=client))
        assert rows[0].goods_and_services == ()
        assert rows[0].error is not None


class TestCsvInput:
    SAMPLE = "name,gstin,city\nReliance,27AAACR5055K1Z7,Mumbai\nBad,NOPE,Delhi\n"

    def test_reads_a_named_column_and_carries_the_rest(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(self.SAMPLE))
        assert main(["-", "--offline", "--column", "gstin", "--format", "csv"]) == 2
        rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
        assert [row["name"] for row in rows] == ["Reliance", "Bad"]
        assert rows[0]["city"] == "Mumbai"
        assert rows[0]["valid"] == "true"
        assert rows[1]["valid"] == "false"
        assert rows[1]["error"] == "does not match the GSTIN format"

    def test_a_missing_column_is_reported_not_guessed(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(self.SAMPLE))
        assert main(["-", "--offline", "--column", "gst_number"]) == 2
        assert "no column 'gst_number'" in capsys.readouterr().err

    def test_blank_cells_are_skipped(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(f"gstin\n{VALID_GSTIN}\n\n   \n"))
        assert main(["-", "--offline", "--column", "gstin", "--format", "jsonl"]) == 0
        assert len(capsys.readouterr().out.strip().splitlines()) == 1


class TestOutputFormats:
    def test_csv_has_one_header_and_a_row_per_input(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--offline", "--format", "csv"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("input,valid,error,gstin,")

    def test_csv_is_plain_text_with_no_styling(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, "--offline", "--format", "csv"]) == 0
        assert "\x1b[" not in capsys.readouterr().out

    def test_json_is_an_object_for_one_and_an_array_for_many(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--offline", "--format", "json"]) == 0
        assert isinstance(json.loads(capsys.readouterr().out), dict)
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--offline", "--format", "json"]) == 0
        assert isinstance(json.loads(capsys.readouterr().out), list)

    def test_format_beats_the_shorthands(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, "--offline", "--json", "--format", "csv"]) == 0
        assert capsys.readouterr().out.startswith("input,valid")


class TestOutputFile:
    def test_writes_the_result_and_leaves_stdout_empty(
        self, tmp_path: "object", capsys: pytest.CaptureFixture[str]
    ) -> None:
        from pathlib import Path

        target = Path(str(tmp_path)) / "out.json"
        assert main([VALID_GSTIN, "--offline", "--format", "json", "-o", str(target)]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "written to" in captured.err
        assert json.loads(target.read_text())["gstin"] == VALID_GSTIN

    def test_table_output_also_lands_in_the_file(self, tmp_path: "object") -> None:
        from pathlib import Path

        target = Path(str(tmp_path)) / "out.txt"
        assert main([VALID_GSTIN, "--offline", "-o", str(target)]) == 0
        assert "Maharashtra" in target.read_text()


class TestCsvRealWorldQuirks:
    """Two things a spreadsheet actually does that a naive reader gets wrong."""

    def test_a_byte_order_mark_does_not_hide_the_column(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Excel writes a BOM, which would make the first column "﻿gstin"."""
        monkeypatch.setattr("sys.stdin", io.StringIO(f"﻿gstin,name\n{VALID_GSTIN},A Ltd\n"))
        assert main(["-", "--offline", "--column", "gstin", "--format", "jsonl"]) == 0
        row = json.loads(capsys.readouterr().out)
        assert row["gstin"] == VALID_GSTIN
        assert row["name"] == "A Ltd"

    def test_a_clashing_column_is_kept_not_overwritten(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Their "valid" column must survive beside the one we compute."""
        monkeypatch.setattr(
            "sys.stdin",
            io.StringIO(f"gstin,valid,state_name\n{VALID_GSTIN},maybe,Narnia\n"),
        )
        assert main(["-", "--offline", "--column", "gstin", "--format", "jsonl"]) == 0
        row = json.loads(capsys.readouterr().out)
        assert row["valid"] is True  # ours, computed
        assert row["source_valid"] == "maybe"  # theirs, preserved
        assert row["state_name"] == "Maharashtra"
        assert row["source_state_name"] == "Narnia"

    def test_quoted_commas_and_newlines_survive_the_round_trip(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(f'gstin,note\n{VALID_GSTIN},"a, b\nsecond line"\n')
        )
        assert main(["-", "--offline", "--column", "gstin", "--format", "csv"]) == 0
        rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
        assert rows[0]["note"] == "a, b\nsecond line"
