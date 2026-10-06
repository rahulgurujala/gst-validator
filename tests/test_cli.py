"""The command line: output formats, batching and safety."""

import io
import json
import sys
from importlib import metadata
from pathlib import Path

import httpx
import pytest

from gst_validator import (
    Captcha,
)
from gst_validator.cli import main

from .support import (
    PAYLOAD,
    PUBLIC_GSTIN,
    VALID_GSTIN,
    answer,
    client_factory,
    transport,
)


class TestCLI:
    def test_offline_validation(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, "--offline", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["pan"] == "ABCFE1234F"

    def test_invalid_gstin_exits_two(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["NOPE", "--offline"]) == 2
        assert "invalid GSTIN" in capsys.readouterr().err


class TestCaptchaCleanup:
    def test_image_is_deleted_after_solving(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "captcha.png"
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--captcha-path", str(target)]) == 0
        assert not target.exists()
        assert "ACME TRADERS" in capsys.readouterr().out

    def test_keep_captcha_retains_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "captcha.png"
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--captcha-path", str(target), "--keep-captcha"]) == 0
        assert target.read_bytes() == b"\x89PNG-bytes"


class TestOfflineOutput:
    def test_json_carries_everything_the_number_encodes(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--offline", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "input": VALID_GSTIN,
            "valid": True,
            "error": None,
            "gstin": VALID_GSTIN,
            "state_code": "27",
            "state_name": "Maharashtra",
            "is_union_territory": False,
            "identifier": "ABCFE1234F",
            "identifier_type": "PAN",
            "pan": "ABCFE1234F",
            "tan": None,
            "entity_type": "Firm / LLP",
            "registration_sequence": "1",
            "registration_type": "Regular",
            "layout": "pan",
        }


class TestCaptchaSave:
    def test_accepts_both_str_and_path(self, tmp_path: Path) -> None:
        captcha = Captcha(b"\x89PNG-bytes")
        as_path = tmp_path / "from-path.png"
        as_str = tmp_path / "from-str.png"
        captcha.save(as_path)
        captcha.save(str(as_str))
        assert as_path.read_bytes() == as_str.read_bytes() == b"\x89PNG-bytes"


class TestRichOutput:
    """The human output is styled; the machine output must stay byte-exact."""

    def test_json_output_has_no_styling_or_wrapping(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--json"]) == 0
        stdout = capsys.readouterr().out
        assert "\x1b[" not in stdout  # no ANSI escapes
        assert json.loads(stdout)["legal_name"] == "ACME TRADERS"

    def test_raw_output_round_trips(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--raw"]) == 0
        assert json.loads(capsys.readouterr().out) == PAYLOAD

    def test_table_renders_objects_not_dicts(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "998314 - Information technology design services" in stdout
        assert "Q1: quarterly" in stdout
        assert "{'code'" not in stdout  # never the repr of a dict
        assert "is active" not in stdout  # redundant with `status`

    def test_offline_table_lists_the_decoded_parts(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--offline", "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "valid" in stdout
        assert "Maharashtra" in stdout
        assert "Firm / LLP" in stdout

    def test_prompt_never_lands_on_stdout(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real `input()` writes its prompt to stdout, which would corrupt --json.

        The stub here mimics that, so the test fails if the prompt is ever
        passed to `input()` again instead of being printed to stderr.
        """

        def prompting_input(prompt: str = "") -> str:
            sys.stdout.write(prompt)
            return "1a2b3"

        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", prompting_input)
        assert main([VALID_GSTIN, "--json"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["gstin"] == VALID_GSTIN
        assert "captcha text" in captured.err

    def test_captcha_data_uri_goes_to_stderr(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--captcha-base64 with --json must leave stdout as pure JSON."""
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--json", "--captcha-base64"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["gstin"] == VALID_GSTIN
        assert "data:image/png;base64," in captured.err

    def test_errors_go_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["NOPE", "--offline"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "invalid GSTIN" in captured.err


class TestUnmappedRendering:
    def test_extra_fields_render_as_pairs(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A portal change surfaces in `extra`; it should read, not be a repr."""
        grown = dict(PAYLOAD) | {"newField": "surprise"}

        def handler(request: httpx.Request) -> httpx.Response:
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case "/services/api/search/taxpayerDetails":
                    return httpx.Response(200, json=grown)
                case _:
                    return httpx.Response(200, json={"status": 1, "data": []})

        monkeypatch.setattr(
            "gst_validator.cli.GSTClient",
            client_factory(httpx.MockTransport(handler)),
        )
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "newField: surprise" in stdout
        assert "{'newField'" not in stdout


class TestMarkupSafety:
    """Portal responses and argv are data, never rich markup."""

    def test_square_brackets_in_an_argument_do_not_crash(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`[/]` is a closing tag to rich: interpolating it raises MarkupError."""
        assert main(["[/]", "--offline"]) == 2
        assert "invalid GSTIN" in capsys.readouterr().err

    def test_markup_in_portal_data_is_shown_literally(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hostile = dict(PAYLOAD)
        hostile["lgnm"] = "ACME [/] [bold red]INJECTED[/] TRADERS"

        def handler(request: httpx.Request) -> httpx.Response:
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case "/services/api/search/taxpayerDetails":
                    return httpx.Response(200, json=hostile)
                case _:
                    return httpx.Response(200, json={"status": 1, "data": []})

        monkeypatch.setattr(
            "gst_validator.cli.GSTClient",
            client_factory(httpx.MockTransport(handler)),
        )
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "[bold red]INJECTED[/]" in stdout  # printed, not interpreted


class TestBatchInput:
    def test_several_gstins_emit_a_json_array(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--format json stays valid JSON overall, so jq can read the lot."""
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--offline", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert [row["gstin"] for row in rows] == [VALID_GSTIN, PUBLIC_GSTIN]

    def test_jsonl_streams_one_object_per_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--offline", "--format", "jsonl"]) == 0
        lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert [row["gstin"] for row in lines] == [VALID_GSTIN, PUBLIC_GSTIN]

    def test_a_single_gstin_still_prints_one_indented_object(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--offline", "--json"]) == 0
        stdout = capsys.readouterr().out
        assert stdout.startswith("{\n")  # unchanged from before batching
        assert json.loads(stdout)["gstin"] == VALID_GSTIN

    def test_stdin_is_read_for_a_dash(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{VALID_GSTIN}\n\n{PUBLIC_GSTIN}\n"))
        assert main(["-", "--offline", "--format", "jsonl"]) == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 2  # the blank line is skipped

    def test_worst_exit_code_wins(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, "NOPE", "--offline"]) == 2
        captured = capsys.readouterr()
        assert "invalid GSTIN" in captured.err
        assert VALID_GSTIN in captured.out  # the valid one still reported

    def test_no_gstin_at_all_is_an_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--offline"]) == 2
        assert "no GSTIN given" in capsys.readouterr().err


class TestVersionFlag:
    def test_version_matches_the_distribution(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            main(["--version"])
        assert exit_info.value.code == 0
        assert metadata.version("gst-validator") in capsys.readouterr().out


class TestLookupOutputFormats:
    """The online path's formats, which only the captcha route reaches."""

    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))

    def test_jsonl_for_several_lookups(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--format", "jsonl"]) == 0
        rows = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(rows) == 2
        assert rows[0]["legal_name"] == "ACME TRADERS"

    def test_csv_for_a_lookup(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, "--format", "csv"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("gstin,legal_name,")
        assert "ACME TRADERS" in lines[1]

    def test_json_for_one_is_an_object_and_for_several_a_list(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "--format", "json"]) == 0
        assert isinstance(json.loads(capsys.readouterr().out), dict)
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--format", "json"]) == 0
        assert isinstance(json.loads(capsys.readouterr().out), list)

    def test_an_invalid_input_among_valid_ones_still_looks_the_rest_up(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([VALID_GSTIN, "NOPE", "--format", "jsonl"]) == 2
        captured = capsys.readouterr()
        assert "invalid GSTIN" in captured.err
        assert json.loads(captured.out)["gstin"] == VALID_GSTIN

    def test_every_input_invalid_reports_without_touching_the_portal(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["NOPE", "ALSO-BAD"]) == 2
        assert capsys.readouterr().out == ""


class TestCacheAndAbort:
    def test_clear_cache_reports_and_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--clear-cache"]) == 0
        assert "cleared" in capsys.readouterr().err

    def test_ctrl_c_at_the_prompt_is_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def interrupt(*_args: object, **_kwargs: object) -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", interrupt)
        assert main([VALID_GSTIN]) == 130
        assert "aborted" in capsys.readouterr().err


class TestPanFlag:
    """`--pan` is its own mode: one captcha, a list of registrations out."""

    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))

    def test_json_lists_every_registration(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--pan", "AAACR5055K", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == 7
        assert rows[0]["gstin"] == "24AAACR5055K2ZC"
        assert rows[0]["is_active"] is False
        assert rows[0]["state_name"] == "Gujarat"

    def test_json_is_a_list_even_for_one_row(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A PAN result is always a collection, unlike a single GSTIN lookup."""
        assert main(["--pan", "AAACR5055K", "--json"]) == 0
        assert isinstance(json.loads(capsys.readouterr().out), list)

    def test_csv_carries_a_header_and_a_row_each(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--pan", "AAACR5055K", "--format", "csv"]) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        assert lines[0].startswith("gstin,status,is_active,")
        assert len(lines) == 8

    def test_jsonl_streams_one_per_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--pan", "AAACR5055K", "--format", "jsonl"]) == 0
        lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
        assert len(lines) == 7
        assert json.loads(lines[0])["gstin"] == "24AAACR5055K2ZC"

    def test_the_table_names_the_states(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--pan", "AAACR5055K", "--no-color"]) == 0
        stdout = capsys.readouterr().out
        assert "Gujarat" in stdout
        assert "Telangana" in stdout
        assert "Inactive" in stdout

    def test_a_lower_case_pan_is_accepted(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--pan", "  aaacr5055k ", "--json"]) == 0
        assert len(json.loads(capsys.readouterr().out)) == 7

    def test_a_bad_pan_exits_two_without_a_captcha(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(*_args: object, **_kwargs: object) -> str:  # pragma: no cover
            raise AssertionError("no captcha should be requested")

        monkeypatch.setattr("builtins.input", refuse)
        assert main(["--pan", "NOPE"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "invalid PAN" in captured.err

    def test_the_captcha_image_is_cleaned_up(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "pan-captcha.png"
        assert main(["--pan", "AAACR5055K", "--captcha-path", str(target), "--json"]) == 0
        assert not target.exists()
        assert "captcha image written to" in capsys.readouterr().err

    def test_output_goes_to_a_file_when_asked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        target = tmp_path / "pan.json"
        assert main(["--pan", "AAACR5055K", "--json", "-o", str(target)]) == 0
        assert capsys.readouterr().out == ""
        assert len(json.loads(target.read_text())) == 7

    def test_a_portal_rejection_exits_one(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case _:
                    return httpx.Response(200, json={"errorCode": "SWEB_9000"})

        monkeypatch.setattr(
            "gst_validator.cli.GSTClient", client_factory(httpx.MockTransport(handler))
        )
        assert main(["--pan", "AAACR5055K", "--json"]) == 1
        assert "PAN lookup failed" in capsys.readouterr().err

    def test_an_empty_result_says_so_on_stderr(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case _:
                    return httpx.Response(200, json={"gstinResList": []})

        monkeypatch.setattr(
            "gst_validator.cli.GSTClient", client_factory(httpx.MockTransport(handler))
        )
        assert main(["--pan", "AAACR5055K", "--json"]) == 0
        captured = capsys.readouterr()
        assert "no registrations found" in captured.err
        assert json.loads(captured.out) == []

    def test_the_captcha_data_uri_stays_off_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["--pan", "AAACR5055K", "--json", "--captcha-base64"]) == 0
        captured = capsys.readouterr()
        assert "data:image/png;base64," in captured.err
        assert isinstance(json.loads(captured.out), list)


class TestErrorMessagesAreNotDoubled:
    """Each failure is announced once, not with the prefix repeated.

    The exceptions already name themselves - "invalid GSTIN 'X': reason" - so
    a renderer that prepends "invalid GSTIN " too produced "invalid GSTIN
    invalid GSTIN 'X': ...". Asserting the substring is present does not catch
    that, which is why it survived; these count instead.
    """

    @staticmethod
    def _dead_transport() -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            match request.url.path:
                case "/services/searchtp":
                    return httpx.Response(200, text="<html></html>")
                case "/services/captcha":
                    return httpx.Response(
                        200, content=b"\x89PNG", headers={"content-type": "image/png"}
                    )
                case _:
                    return httpx.Response(503)

        return httpx.MockTransport(handler)

    def test_invalid_gstin_is_announced_once_offline(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["NOPE", "--offline"]) == 2
        assert capsys.readouterr().err.count("invalid GSTIN") == 1

    def test_invalid_gstin_is_announced_once_online(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        assert main(["NOPE"]) == 2
        assert capsys.readouterr().err.count("invalid GSTIN") == 1

    def test_invalid_pan_is_announced_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["--pan", "NOPE"]) == 2
        assert capsys.readouterr().err.count("invalid PAN") == 1

    def test_a_transport_failure_is_not_prefixed_twice(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(self._dead_transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main(["--pan", "AAACR5055K", "--json"]) == 1
        stderr = capsys.readouterr().err
        assert stderr.count("PAN lookup failed") == 1
        assert "failed: request to" in stderr  # the client names the endpoint, once

    def test_every_invalid_row_is_named_once_in_a_batch(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["NOPE", "ALSOBAD", "--offline"]) == 2
        assert capsys.readouterr().err.count("invalid GSTIN") == 2


class TestRawFormatHonesty:
    """`raw` means the portal's own body, and a PAN row does not keep one."""

    def test_pan_raw_says_it_is_printing_json_instead(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main(["--pan", "AAACR5055K", "--raw"]) == 0
        captured = capsys.readouterr()
        assert "keeps no raw body" in captured.err
        assert len(json.loads(captured.out)) == 7

    def test_a_gstin_lookup_raw_really_is_the_portal_body(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gst_validator.cli.GSTClient", client_factory(transport()))
        monkeypatch.setattr("builtins.input", answer("1a2b3"))
        assert main([VALID_GSTIN, "--raw"]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out) == PAYLOAD
        assert "keeps no raw body" not in captured.err
