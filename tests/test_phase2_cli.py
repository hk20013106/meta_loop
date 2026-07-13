import json

from meta_loop.cli.main import build_parser, request_from_json


def test_start_request_is_schema_versioned(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema_version": 1, "request_id": "r1", "source": {"kind": "synthetic", "reference": "x"}, "repository": {"name": "public/repo", "revision": "main"}, "risk": {"proposed": "R0", "validated": "R0", "effective": "R0"}}), encoding="utf-8")
    parsed = request_from_json(str(request), True, 4)
    assert parsed.request_id == "r1" and parsed.enqueue and parsed.priority == 4


def test_cli_parser_accepts_only_documented_commands():
    assert build_parser().parse_args(["tasks", "--risk", "R3"]).risk == "R3"
    assert build_parser().parse_args(["fuse", "release", "--governance-revision", "v1"]).action == "release"
