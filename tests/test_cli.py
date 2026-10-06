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
            "gstin": VALID_GSTIN,
            "valid": True,
            "state_code": "27",
            "state_name": "Maharashtra",
            "identifier": "ABCFE1234F",
            "identifier_type": "PAN",
            "pan": "ABCFE1234F",
            "tan": None,
            "entity_type": "Firm / LLP",
            "registration_sequence": "1",
            "registration_type": "Regular",
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
    def test_several_gstins_emit_json_lines(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main([VALID_GSTIN, PUBLIC_GSTIN, "--offline", "--json"]) == 0
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
        assert main(["-", "--offline", "--json"]) == 0
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
